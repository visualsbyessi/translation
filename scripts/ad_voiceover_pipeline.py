#!/usr/bin/env python3
"""Create timing-aware German/French voiceovers for English ad videos.

The pipeline:
1. Extracts the English audio from a source ad video.
2. Transcribes narration into timed segments.
3. Optionally samples B-roll frames and detects visual scene boundaries.
4. Uses OpenAI to translate/adapt each segment to fit the original timing.
5. Generates voiceover segments with ElevenLabs.
6. Regenerates shortened versions when translated speech is too long.
7. Exports synchronized MP3 voiceover files.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import subprocess
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import cv2
import requests
from dotenv import load_dotenv
from openai import OpenAI
from pydub import AudioSegment


DEFAULT_LANGUAGES = ["de", "fr"]
DEFAULT_CONFIG = Path("config/languages.json")


@dataclass
class Segment:
    index: int
    start: float
    end: float
    text: str
    visual_context: str = ""

    @property
    def duration_ms(self) -> int:
        return max(250, int((self.end - self.start) * 1000))


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def ffprobe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


def extract_audio(video_path: Path, audio_path: Path) -> None:
    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            str(audio_path),
        ]
    )


def transcribe_audio(client: OpenAI, audio_path: Path, model: str) -> list[Segment]:
    with audio_path.open("rb") as audio_file:
        transcript = client.audio.transcriptions.create(
            model=model,
            file=audio_file,
            language="en",
            response_format="verbose_json",
        )

    raw_segments = getattr(transcript, "segments", None) or transcript.model_dump().get("segments")
    if not raw_segments:
        text = getattr(transcript, "text", "") or transcript.model_dump().get("text", "")
        duration = ffprobe_duration(audio_path)
        raw_segments = [{"start": 0, "end": duration, "text": text}]

    segments: list[Segment] = []
    for idx, item in enumerate(raw_segments, start=1):
        start = float(item["start"] if isinstance(item, dict) else item.start)
        end = float(item["end"] if isinstance(item, dict) else item.end)
        text = str(item["text"] if isinstance(item, dict) else item.text).strip()
        if text:
            segments.append(Segment(index=idx, start=start, end=end, text=text))
    return segments


def merge_segments(segments: list[Segment], max_duration_sec: float, max_chars: int, max_gap_sec: float) -> list[Segment]:
    if not segments:
        return []

    merged: list[Segment] = []
    current = Segment(index=1, start=segments[0].start, end=segments[0].end, text=segments[0].text)

    for segment in segments[1:]:
        combined_text = f"{current.text} {segment.text}".strip()
        combined_duration = segment.end - current.start
        gap = segment.start - current.end
        if combined_duration <= max_duration_sec and len(combined_text) <= max_chars and gap <= max_gap_sec:
            current.end = segment.end
            current.text = combined_text
        else:
            merged.append(current)
            current = Segment(index=len(merged) + 1, start=segment.start, end=segment.end, text=segment.text)

    merged.append(current)
    for idx, segment in enumerate(merged, start=1):
        segment.index = idx
    return merged


def apply_approved_script_to_timing(segments: list[Segment], approved_script: str) -> list[Segment]:
    clean_script = " ".join(approved_script.split())
    if not clean_script or not segments:
        return segments

    total_chars = sum(max(1, len(segment.text)) for segment in segments)
    cursor = 0
    scripted_segments: list[Segment] = []

    for idx, segment in enumerate(segments, start=1):
        if idx == len(segments):
            chunk = clean_script[cursor:].strip()
        else:
            share = max(1, round(len(segment.text) / total_chars * len(clean_script)))
            split_at = min(len(clean_script), cursor + share)
            next_space = clean_script.find(" ", split_at)
            if next_space != -1 and next_space - split_at < 40:
                split_at = next_space
            chunk = clean_script[cursor:split_at].strip()
            cursor = split_at
        if chunk:
            scripted_segments.append(Segment(index=idx, start=segment.start, end=segment.end, text=chunk))

    for idx, segment in enumerate(scripted_segments, start=1):
        segment.index = idx
    return scripted_segments or segments


def elevenlabs_voices(api_key: str) -> list[dict[str, Any]]:
    response = requests.get(
        "https://api.elevenlabs.io/v1/voices",
        headers={"xi-api-key": api_key},
        timeout=60,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"ElevenLabs voices error {response.status_code}: {response.text}")
    return response.json().get("voices", [])


def analyze_voice_style(client: OpenAI, segments: list[Segment], broll: dict[str, Any], model: str) -> dict[str, Any]:
    sample_text = " ".join(segment.text for segment in segments[:8])
    prompt = {
        "task": "Infer the original English ad voiceover style from narration and B-roll context. Do not identify or clone a real person. Return JSON only.",
        "narration_sample": sample_text,
        "broll_context": broll.get("overall_visual_story", ""),
        "return_schema": {
            "gender_presentation": "male|female|neutral|unknown",
            "age_impression": "young_adult|adult|mature|unknown",
            "energy": "calm|warm|confident|energetic|urgent|luxury|friendly",
            "pace": "slow|medium|fast",
            "tone": "short description",
            "voice_direction": "short casting direction for a native speaker voice actor",
        },
    }
    response = client.responses.create(
        model=model,
        input=[{"role": "user", "content": [{"type": "input_text", "text": json.dumps(prompt, ensure_ascii=False)}]}],
        text={"format": {"type": "json_object"}},
    )
    return json.loads(response.output_text)


def voice_matches_language(voice: dict[str, Any], language_code: str, language_name: str) -> bool:
    labels = voice.get("labels") or {}
    searchable = " ".join(
        str(value).lower()
        for value in [
            voice.get("name", ""),
            voice.get("description", ""),
            labels.get("language", ""),
            labels.get("accent", ""),
            labels.get("use_case", ""),
        ]
    )
    language_terms = {
        "de": ["german", "deutsch"],
        "fr": ["french", "francais", "français"],
    }.get(language_code, [language_code.lower(), language_name.lower()])
    return any(term in searchable for term in language_terms)


def choose_elevenlabs_voice(
    client: OpenAI,
    voices: list[dict[str, Any]],
    language_code: str,
    language_name: str,
    voice_style: dict[str, Any],
    model: str,
) -> str:
    candidates = [voice for voice in voices if voice_matches_language(voice, language_code, language_name)]
    if not candidates:
        candidates = voices
    if not candidates:
        raise RuntimeError("No ElevenLabs voices found in this account.")

    compact_candidates = []
    for voice in candidates[:40]:
        compact_candidates.append(
            {
                "voice_id": voice.get("voice_id"),
                "name": voice.get("name"),
                "description": voice.get("description"),
                "labels": voice.get("labels", {}),
                "preview_url": voice.get("preview_url"),
            }
        )

    prompt = {
        "task": "Choose the best ElevenLabs voice for a translated ad voiceover. Match the original style, but do not clone or impersonate a real person.",
        "target_language": language_name,
        "original_voice_style": voice_style,
        "candidates": compact_candidates,
        "selection_rules": [
            "Prefer voices that are native or clearly appropriate for the target language.",
            "Match gender presentation, energy, pace, and commercial tone when possible.",
            "Prefer a natural ad voice over an extreme character voice.",
            "Return one voice_id from the candidates.",
            "Return JSON only.",
        ],
        "return_schema": {"voice_id": "string", "reason": "short explanation"},
    }
    response = client.responses.create(
        model=model,
        input=[{"role": "user", "content": [{"type": "input_text", "text": json.dumps(prompt, ensure_ascii=False)}]}],
        text={"format": {"type": "json_object"}},
    )
    selected = json.loads(response.output_text)
    selected_id = selected["voice_id"]
    valid_ids = {voice.get("voice_id") for voice in candidates}
    if selected_id not in valid_ids:
        return candidates[0]["voice_id"]
    return selected_id


def encode_frame(frame: Any, max_width: int = 640) -> str:
    height, width = frame.shape[:2]
    if width > max_width:
        scale = max_width / width
        frame = cv2.resize(frame, (max_width, int(height * scale)))
    ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
    if not ok:
        raise RuntimeError("Unable to encode sampled frame")
    return base64.b64encode(buffer).decode("utf-8")


def sample_frames(video_path: Path, max_frames: int = 18) -> list[dict[str, Any]]:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Unable to open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = frame_count / fps if frame_count else ffprobe_duration(video_path)
    samples: list[dict[str, Any]] = []

    for i in range(max_frames):
        timestamp = (duration * (i + 0.5)) / max_frames
        cap.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000)
        ok, frame = cap.read()
        if ok:
            samples.append({"timestamp": round(timestamp, 2), "image_b64": encode_frame(frame)})
    cap.release()
    return samples


def detect_scene_changes(video_path: Path, threshold: float = 32.0, min_gap_sec: float = 0.7) -> list[float]:
    cap = cv2.VideoCapture(str(video_path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    prev_hist = None
    changes: list[float] = [0.0]
    frame_idx = 0

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if frame_idx % max(1, int(fps / 2)) == 0:
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            hist = cv2.calcHist([hsv], [0, 1], None, [32, 32], [0, 180, 0, 256])
            cv2.normalize(hist, hist)
            if prev_hist is not None:
                diff = cv2.compareHist(prev_hist, hist, cv2.HISTCMP_BHATTACHARYYA) * 100
                timestamp = frame_idx / fps
                if diff > threshold and timestamp - changes[-1] >= min_gap_sec:
                    changes.append(round(timestamp, 2))
            prev_hist = hist
        frame_idx += 1

    cap.release()
    return changes


def analyze_broll(client: OpenAI, video_path: Path, model: str, max_frames: int) -> dict[str, Any]:
    if max_frames <= 0:
        return {
            "overall_visual_story": "B-roll analysis skipped to reduce cost and runtime.",
            "shots": [],
            "translation_notes": "Use narration timing only.",
            "detected_scene_changes": detect_scene_changes(video_path),
        }

    frames = sample_frames(video_path, max_frames=max_frames)
    scene_changes = detect_scene_changes(video_path)
    content: list[dict[str, Any]] = [
        {
            "type": "input_text",
            "text": (
                "Describe the B-roll in this ad video. Return compact JSON with: "
                "overall_visual_story, shots [{timestamp as seconds number, description, product_or_action, mood}], "
                "and translation_notes for syncing narration to visuals."
            ),
        }
    ]
    for frame in frames:
        content.append({"type": "input_text", "text": f"Frame around {frame['timestamp']}s"})
        content.append(
            {
                "type": "input_image",
                "image_url": f"data:image/jpeg;base64,{frame['image_b64']}",
            }
        )

    response = client.responses.create(
        model=model,
        input=[{"role": "user", "content": content}],
        text={"format": {"type": "json_object"}},
    )
    analysis = json.loads(response.output_text)
    analysis["detected_scene_changes"] = scene_changes
    return analysis


def visual_context_for_segment(segment: Segment, broll: dict[str, Any]) -> str:
    shots = broll.get("shots", [])
    matching = []
    for shot in shots:
        timestamp = parse_timestamp_value(shot.get("timestamp", -1))
        if segment.start - 1 <= timestamp <= segment.end + 1:
            matching.append(shot.get("description", ""))
    if matching:
        return " | ".join(x for x in matching if x)
    return broll.get("overall_visual_story", "")


def parse_timestamp_value(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        value = value.strip()
        if ":" in value:
            parts = [float(part) for part in value.split(":")]
            total = 0.0
            for part in parts:
                total = total * 60 + part
            return total
        return float(value)
    return -1.0


def translate_segments(
    client: OpenAI,
    segments: list[Segment],
    broll: dict[str, Any],
    language_name: str,
    model: str,
    max_chars_per_second: float,
    banned_terms: list[str],
    using_approved_script: bool,
) -> list[dict[str, Any]]:
    payload = []
    for segment in segments:
        segment.visual_context = visual_context_for_segment(segment, broll)
        payload.append(asdict(segment) | {"max_chars": int((segment.end - segment.start) * max_chars_per_second)})

    prompt = {
        "target_language": language_name,
        "task": "Translate English ad narration into natural target-language voiceover while fitting each original segment duration.",
        "rules": [
            "Keep meaning and sales intent.",
            "The provided source text is the approved script. Use it as the source of truth.",
            "Do not restore competitor product names from the original ad transcript.",
            "Use visual_context to decide how each line should be phrased near the matching B-roll.",
            "If the approved script and B-roll timing conflict, keep the approved message but make the phrasing fit the visual moment.",
            "Adapt phrasing to sound native, not literal.",
            f"Write like a native {language_name} speaker recording a paid social ad.",
            "Prefer short natural phrases over word-for-word translation.",
            "Avoid stiff, formal, or machine-translated wording.",
            "Do not mention overlays or competitor products unless in the narration.",
            "Use each segment start/end exactly as provided.",
            "Keep each translated_text concise enough for voiceover timing.",
            "Do not cram too many words into short visual moments.",
            "Return valid JSON only.",
        ],
        "approved_script_mode": using_approved_script,
        "banned_terms": banned_terms,
        "broll_analysis": broll,
        "segments": payload,
        "return_schema": {
            "segments": [
                {
                    "index": "number",
                    "start": "number",
                    "end": "number",
                    "source_text": "string",
                    "translated_text": "string",
                    "visual_context": "string",
                }
            ]
        },
    }
    response = client.responses.create(
        model=model,
        input=[
            {
                "role": "user",
                "content": [{"type": "input_text", "text": json.dumps(prompt, ensure_ascii=False)}],
            }
        ],
        text={"format": {"type": "json_object"}},
    )
    data = json.loads(response.output_text)
    return data["segments"]


def shorten_translation(
    client: OpenAI,
    translated_text: str,
    language_name: str,
    target_ms: int,
    model: str,
) -> str:
    prompt = (
        f"Shorten this {language_name} ad voiceover line so spoken audio can fit within "
        f"{target_ms / 1000:.2f} seconds without sounding rushed. Keep the core sales message, "
        "use natural native-speaker phrasing, and remove less important words. Return JSON as "
        '{"text":"..."} only.\n\n'
        f"Line: {translated_text}"
    )
    response = client.responses.create(
        model=model,
        input=[{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
        text={"format": {"type": "json_object"}},
    )
    return json.loads(response.output_text)["text"].strip()


def elevenlabs_tts(text: str, voice_id: str, api_key: str, out_path: Path) -> None:
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    response = requests.post(
        url,
        headers={
            "xi-api-key": api_key,
            "Accept": "audio/mpeg",
            "Content-Type": "application/json",
        },
        json={
            "text": text,
            "model_id": os.getenv("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2"),
            "voice_settings": {
                "stability": float(os.getenv("ELEVENLABS_STABILITY", "0.62")),
                "similarity_boost": float(os.getenv("ELEVENLABS_SIMILARITY_BOOST", "0.82")),
                "style": float(os.getenv("ELEVENLABS_STYLE", "0.28")),
                "use_speaker_boost": True,
            },
        },
        timeout=120,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"ElevenLabs error {response.status_code}: {response.text}")
    out_path.write_bytes(response.content)


def audio_duration_ms(path: Path) -> int:
    return len(AudioSegment.from_file(path))


def generate_timed_voice_segments(
    client: OpenAI,
    translated_segments: list[dict[str, Any]],
    language_name: str,
    voice_id: str,
    elevenlabs_key: str,
    text_model: str,
    work_dir: Path,
    max_attempts: int,
    timing_pad_ms: int,
    regenerate_long_segments: bool,
) -> list[dict[str, Any]]:
    generated: list[dict[str, Any]] = []
    for segment in translated_segments:
        text = segment["translated_text"].strip()
        target_ms = max(300, int((float(segment["end"]) - float(segment["start"])) * 1000) - timing_pad_ms)
        final_path = work_dir / f"{language_name.lower()}_{int(segment['index']):03d}.mp3"

        for attempt in range(1, max_attempts + 1):
            elevenlabs_tts(text, voice_id, elevenlabs_key, final_path)
            actual_ms = audio_duration_ms(final_path)
            if actual_ms <= target_ms or attempt == max_attempts or not regenerate_long_segments:
                break
            text = shorten_translation(client, text, language_name, target_ms, text_model)
            time.sleep(0.4)

        generated.append(segment | {"final_text": text, "audio_path": str(final_path), "audio_duration_ms": actual_ms})
    return generated


def assemble_synced_audio(generated: list[dict[str, Any]], video_duration_ms: int) -> AudioSegment:
    bed = AudioSegment.silent(duration=video_duration_ms)
    ordered = sorted(generated, key=lambda item: float(item["start"]))
    for index, segment in enumerate(ordered):
        audio = AudioSegment.from_file(segment["audio_path"])
        start_ms = int(float(segment["start"]) * 1000)
        segment_end_ms = int(float(segment["end"]) * 1000)
        next_start_ms = int(float(ordered[index + 1]["start"]) * 1000) if index + 1 < len(ordered) else video_duration_ms
        hard_end_ms = min(segment_end_ms, max(start_ms + 250, next_start_ms - 80))
        available_ms = max(250, hard_end_ms - start_ms)
        if len(audio) > available_ms:
            speed = min(1.06, len(audio) / available_ms)
            audio = audio._spawn(audio.raw_data, overrides={"frame_rate": int(audio.frame_rate * speed)}).set_frame_rate(audio.frame_rate)
            audio = audio[:available_ms]
        bed = bed.overlay(audio, position=start_ms)
    return bed


def export_synced_voiceover(generated: list[dict[str, Any]], video_duration_ms: int, output_path: Path, output_format: str) -> None:
    audio = assemble_synced_audio(generated, video_duration_ms)
    export_kwargs: dict[str, Any] = {"format": output_format}
    if output_format == "mp3":
        export_kwargs["bitrate"] = "192k"
    audio.export(output_path, **export_kwargs)


def srt_timestamp(seconds: float) -> str:
    millis = int(round(seconds * 1000))
    hours = millis // 3_600_000
    millis %= 3_600_000
    minutes = millis // 60_000
    millis %= 60_000
    secs = millis // 1000
    millis %= 1000
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def write_srt(generated: list[dict[str, Any]], output_path: Path) -> None:
    lines = []
    for i, segment in enumerate(generated, start=1):
        lines.extend(
            [
                str(i),
                f"{srt_timestamp(float(segment['start']))} --> {srt_timestamp(float(segment['end']))}",
                segment["final_text"],
                "",
            ]
        )
    output_path.write_text("\n".join(lines), encoding="utf-8")


def load_languages(config_path: Path) -> dict[str, Any]:
    return json.loads(config_path.read_text(encoding="utf-8"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate synced translated ad voiceovers.")
    parser.add_argument("--video", required=True, type=Path, help="Source English ad video.")
    parser.add_argument("--languages", nargs="+", default=DEFAULT_LANGUAGES, help="Language codes from config/languages.json.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=Path("outputs"))
    parser.add_argument("--work-dir", type=Path, default=Path("work"))
    parser.add_argument("--transcription-model", default=os.getenv("OPENAI_TRANSCRIPTION_MODEL", "whisper-1"))
    parser.add_argument("--text-model", default=os.getenv("OPENAI_TEXT_MODEL", "gpt-4o-mini"))
    parser.add_argument("--vision-model", default=os.getenv("OPENAI_VISION_MODEL", "gpt-4o-mini"))
    parser.add_argument("--max-attempts", type=int, default=int(os.getenv("MAX_TTS_ATTEMPTS", "3")))
    parser.add_argument("--timing-pad-ms", type=int, default=150)
    parser.add_argument("--max-chars-per-second", type=float, default=13.0)
    parser.add_argument("--output-format", choices=["mp3", "wav"], default="mp3")
    parser.add_argument("--broll-frames", type=int, default=int(os.getenv("BROLL_FRAMES", "18")), help="0 skips vision analysis for lower cost.")
    parser.add_argument("--merge-segment-seconds", type=float, default=float(os.getenv("MERGE_SEGMENT_SECONDS", "14")))
    parser.add_argument("--merge-segment-chars", type=int, default=int(os.getenv("MERGE_SEGMENT_CHARS", "280")))
    parser.add_argument("--script-file", type=Path)
    parser.add_argument("--banned-terms", default="")
    parser.add_argument("--skip-regeneration", action="store_true", help="Skip regenerating long TTS segments.")
    parser.add_argument("--write-debug-files", action="store_true", help="Also export JSON/SRT files for troubleshooting.")
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.work_dir.mkdir(parents=True, exist_ok=True)

    openai_key = os.getenv("OPENAI_API_KEY")
    elevenlabs_key = os.getenv("ELEVENLABS_API_KEY")
    if not openai_key:
        raise RuntimeError("Missing OPENAI_API_KEY")
    if not elevenlabs_key:
        raise RuntimeError("Missing ELEVENLABS_API_KEY")

    client = OpenAI(api_key=openai_key)
    languages = load_languages(args.config)
    video_duration_ms = math.ceil(ffprobe_duration(args.video) * 1000)

    source_audio = args.work_dir / "source_english.wav"
    extract_audio(args.video, source_audio)
    segments = transcribe_audio(client, source_audio, args.transcription_model)
    segments = merge_segments(segments, args.merge_segment_seconds, args.merge_segment_chars, max_gap_sec=1.2)
    approved_script = args.script_file.read_text(encoding="utf-8").strip() if args.script_file and args.script_file.exists() else ""
    using_approved_script = bool(approved_script)
    if using_approved_script:
        segments = apply_approved_script_to_timing(segments, approved_script)
    broll = analyze_broll(client, args.video, args.vision_model, args.broll_frames)
    voice_style = analyze_voice_style(client, segments, broll, args.text_model)
    available_voices = elevenlabs_voices(elevenlabs_key)

    if args.write_debug_files:
        (args.output_dir / "broll_analysis.json").write_text(json.dumps(broll, indent=2, ensure_ascii=False), encoding="utf-8")
        (args.output_dir / "english_segments.json").write_text(
            json.dumps([asdict(segment) for segment in segments], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        (args.output_dir / "original_voice_style.json").write_text(json.dumps(voice_style, indent=2, ensure_ascii=False), encoding="utf-8")

    for language_code in args.languages:
        if language_code not in languages:
            raise RuntimeError(f"Unknown language '{language_code}'. Add it to {args.config}.")
        language = languages[language_code]
        voice_id = os.getenv(language["elevenlabs_voice_id_env"])
        if not voice_id:
            voice_id = choose_elevenlabs_voice(
                client,
                available_voices,
                language_code,
                language["name"],
                voice_style,
                args.text_model,
            )

        translated = translate_segments(
            client,
            segments,
            broll,
            language["name"],
            args.text_model,
            args.max_chars_per_second,
            [term.strip() for term in args.banned_terms.split(",") if term.strip()],
            using_approved_script,
        )
        generated = generate_timed_voice_segments(
            client,
            translated,
            language["name"],
            voice_id,
            elevenlabs_key,
            args.text_model,
            args.work_dir,
            args.max_attempts,
            args.timing_pad_ms,
            not args.skip_regeneration,
        )

        stem = f"{language_code}_{language['name'].lower()}"
        export_synced_voiceover(generated, video_duration_ms, args.output_dir / f"{stem}_voiceover.{args.output_format}", args.output_format)
        if args.write_debug_files:
            write_srt(generated, args.output_dir / f"{stem}_subtitles.srt")
            (args.output_dir / f"{stem}_script.json").write_text(
                json.dumps({"selected_voice_id": voice_id, "segments": generated}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )


if __name__ == "__main__":
    main()
