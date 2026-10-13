from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles


ROOT = Path(__file__).resolve().parents[1]
JOBS_DIR = ROOT / "app" / "jobs"
STATIC_DIR = ROOT / "app" / "static"
PIPELINE_SCRIPT = ROOT / "scripts" / "ad_voiceover_pipeline.py"

app = FastAPI(title="Ad Voiceover Automation")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


def status_path(job_id: str) -> Path:
    return job_dir(job_id) / "status.json"


def write_status(job_id: str, data: dict) -> None:
    path = status_path(job_id)
    existing = {}
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
    existing.update(data)
    existing["updated_at"] = now_iso()
    path.write_text(json.dumps(existing, indent=2), encoding="utf-8")


def read_status(job_id: str) -> dict:
    path = status_path(job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Job not found")
    return json.loads(path.read_text(encoding="utf-8"))


def zip_outputs(job_id: str) -> Path:
    outputs_dir = job_dir(job_id) / "outputs"
    zip_path = job_dir(job_id) / "voiceovers.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in outputs_dir.glob("*.mp3"):
            archive.write(path, path.name)
    return zip_path


def run_voiceover_job(
    job_id: str,
    video_path: Path,
    languages: list[str],
    script_path: Path | None,
    banned_terms: str,
    translation_mode: str,
) -> None:
    work_dir = job_dir(job_id) / "work"
    outputs_dir = job_dir(job_id) / "outputs"
    work_dir.mkdir(parents=True, exist_ok=True)
    outputs_dir.mkdir(parents=True, exist_ok=True)

    write_status(job_id, {"state": "running", "message": "Generating translated MP3 voiceovers..."})
    command = [
        sys.executable,
        str(PIPELINE_SCRIPT),
        "--video",
        str(video_path),
        "--languages",
        *languages,
        "--output-dir",
        str(outputs_dir),
        "--work-dir",
        str(work_dir),
        "--output-format",
        "mp3",
    ]
    if translation_mode == "fast":
        command.extend(["--broll-frames", "0", "--max-attempts", "1", "--max-pause-ms", "700", "--skip-regeneration"])
    elif translation_mode == "tight":
        command.extend(["--broll-frames", "18", "--max-attempts", "3", "--max-pause-ms", "350"])
    else:
        command.extend(["--broll-frames", "18", "--max-attempts", "3", "--max-pause-ms", "1200"])

    if script_path:
        command.extend(["--script-file", str(script_path)])
    if banned_terms.strip():
        command.extend(["--banned-terms", banned_terms.strip()])

    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    log_path = job_dir(job_id) / "job.log"
    job_log = (result.stdout or "") + "\n" + (result.stderr or "")
    log_path.write_text(job_log, encoding="utf-8")

    if result.returncode != 0:
        error_tail = "\n".join(job_log.strip().splitlines()[-12:])
        print(f"Voiceover job {job_id} failed:\n{error_tail}", flush=True)
        write_status(
            job_id,
            {
                "state": "failed",
                "message": error_tail or "The voiceover job failed. Check that the API keys and video file are valid.",
            },
        )
        return

    mp3_files = sorted(path.name for path in outputs_dir.glob("*.mp3"))
    zip_path = zip_outputs(job_id)
    write_status(
        job_id,
        {
            "state": "complete",
            "message": "MP3 voiceovers are ready.",
            "files": mp3_files,
            "download_url": f"/api/jobs/{job_id}/download",
            "zip_size_bytes": zip_path.stat().st_size,
        },
    )


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.post("/api/jobs")
async def create_job(
    background_tasks: BackgroundTasks,
    video: UploadFile = File(...),
    languages: str = Form("de"),
    translation_mode: str = Form("broll"),
    approved_script: str = Form(""),
    banned_terms: str = Form(""),
) -> JSONResponse:
    selected_languages = [item.strip().lower() for item in languages.replace(",", " ").split() if item.strip()]
    if not selected_languages:
        raise HTTPException(status_code=400, detail="Choose at least one language.")
    if translation_mode not in {"fast", "broll", "tight"}:
        raise HTTPException(status_code=400, detail="Choose a valid translation mode.")

    job_id = uuid.uuid4().hex
    directory = job_dir(job_id)
    uploads_dir = directory / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    suffix = Path(video.filename or "ad.mp4").suffix or ".mp4"
    video_path = uploads_dir / f"source{suffix}"
    with video_path.open("wb") as target:
        shutil.copyfileobj(video.file, target)

    script_path = None
    if approved_script.strip():
        script_path = uploads_dir / "approved_script.txt"
        script_path.write_text(approved_script.strip(), encoding="utf-8")

    write_status(
        job_id,
        {
            "job_id": job_id,
            "state": "queued",
            "message": "Upload received. Waiting to start...",
            "created_at": now_iso(),
            "languages": selected_languages,
            "translation_mode": translation_mode,
            "source_filename": video.filename,
            "has_approved_script": bool(script_path),
            "banned_terms": banned_terms,
        },
    )
    background_tasks.add_task(run_voiceover_job, job_id, video_path, selected_languages, script_path, banned_terms, translation_mode)
    return JSONResponse({"job_id": job_id, "status_url": f"/api/jobs/{job_id}"})


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> JSONResponse:
    return JSONResponse(read_status(job_id))


@app.get("/api/jobs/{job_id}/download")
def download_job(job_id: str) -> FileResponse:
    status = read_status(job_id)
    if status.get("state") != "complete":
        raise HTTPException(status_code=409, detail="Job is not complete yet.")
    zip_path = job_dir(job_id) / "voiceovers.zip"
    if not zip_path.exists():
        raise HTTPException(status_code=404, detail="Download not found")
    return FileResponse(zip_path, media_type="application/zip", filename=f"{job_id}-voiceovers.zip")
