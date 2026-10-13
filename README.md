# MP3 Ad Voiceover Automation

This is a simple voiceover automation for you and your editors.

You can use it in two ways:

1. **Website UI:** your editors open a web page, upload a video, and download MP3s.
2. **GitHub Actions:** your editors upload a video to GitHub and run the workflow.

This does **not** use GitHub Pages as a backend. GitHub Pages is only for static websites. The automation runs inside **GitHub Actions**, where Python and FFmpeg can process the video.

For the website UI, you need a real backend host such as Render, Railway, Fly.io, a VPS, or your own computer running the app. GitHub Pages cannot run this website because the app needs to process video and call OpenAI/ElevenLabs securely.

## What Your Team Gets

By default, the workflow gives you:

```text
de_german_voiceover.mp3
fr_french_voiceover.mp3
```

These are the files your editors can place into Premiere Pro, CapCut, DaVinci Resolve, Final Cut, or any other video editor.

The automation does not replace your full video editing workflow. You and your editors can still manually remove overlays, replace competitor product shots, and insert the client's product clips.

## Website Workflow

This is the easiest workflow for your team.

1. You open the website.
2. Upload the English ad video.
3. Enter language codes such as `de`, `fr`, or `es`.
4. Paste the approved client script if you have one.
5. Add competitor names or product names to avoid.
6. Click `Generate voiceovers`.
7. Wait for processing.
8. Download the ZIP file with MP3 voiceovers.

## GitHub Actions Workflow

Use GitHub as the shared automation dashboard if you do not want to host a web backend yet.

1. You or an editor uploads the English ad video to the `inputs` folder.
2. The file should usually be named:

```text
ad.mp4
```

3. Someone runs the GitHub Action.
4. GitHub processes the video.
5. Everyone downloads the finished MP3 voiceovers from the workflow result.

## What The Automation Does

1. Takes the English ad video.
2. Extracts the English voice audio.
3. Transcribes the English narration.
4. Uses narration timing by default, with optional B-roll frame analysis if you enable it.
5. Translates the narration into German and/or French.
6. Generates voiceover using ElevenLabs.
7. Checks if each voiceover line fits the original timing.
8. Shortens and regenerates lines that are too long, instead of making the voice sound rushed.
9. Exports final synchronized MP3 voiceovers.

The translation prompt is tuned for native-speaker German/French ad voiceover, not direct word-for-word translation.

By default, the website uses quality mode:

- analyzes B-roll frames
- adapts the approved script or transcript to match the visuals
- regenerates lines that are too long when needed
- prioritizes natural native-speaker delivery

This is better when you want the voiceover to closely match what is happening in the B-roll. It costs more and takes longer than fast mode.

The approved script box is recommended when localizing competitor ads. It prevents the automation from repeating the competitor product name from the original video transcript.

Supported default language codes:

```text
de = German
fr = French
es = Spanish
it = Italian
pt = Portuguese
nl = Dutch
```

## Files In This Repository

```text
.github/workflows/generate-voiceovers.yml
app/main.py
app/static/
scripts/ad_voiceover_pipeline.py
config/languages.json
inputs/
outputs/
work/
requirements.txt
```

If you use the website UI, your editors only touch the website.

If you use GitHub Actions, your editors normally only touch:

```text
inputs/
```

Your editors do not need to edit the Python script.

## Website Setup

Use this when you want the browser upload page.

### What You Need

- A place to host the backend app, such as Render, Railway, Fly.io, a VPS, or a computer that stays on.
- Your OpenAI API key.
- Your ElevenLabs API key.
- Optional: fixed ElevenLabs German and French voice IDs.

### Run The Website On Your Computer

Install Python and FFmpeg first.

Then run:

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open:

```text
http://localhost:8000
```

Before using it, the computer or host must have these environment variables:

```text
OPENAI_API_KEY=your_openai_key
ELEVENLABS_API_KEY=your_elevenlabs_key
```

The app can automatically choose voices from your ElevenLabs account by matching the original ad's voice style. It matches the style and casting direction; it does not clone or impersonate a real person.

If you want to force specific voices instead of auto-selecting, also set:

```text
ELEVENLABS_VOICE_ID_DE=your_german_voice_id
ELEVENLABS_VOICE_ID_FR=your_french_voice_id
```

Your editors should not see these keys. They stay on the server.

### Host The Website

Use a backend host, not GitHub Pages.

Good beginner-friendly choices:

- Render
- Railway
- Fly.io

This folder includes:

```text
Dockerfile
render.yaml
```

That means a host like Render can run the app with FFmpeg included.

When deploying, add these environment variables in the host dashboard:

```text
OPENAI_API_KEY
ELEVENLABS_API_KEY
```

After deployment, your editors use the website URL instead of touching GitHub Actions.

## GitHub Actions Setup

### 1. Create A GitHub Repository

Create a new GitHub repository for this automation. A private repo is recommended.

Then upload all the files from this folder into that repository.

### 2. Add Your Team

In GitHub:

```text
Repository -> Settings -> Collaborators
```

Invite your two editors.

Give them access so they can upload videos and run the workflow.

### 3. Add Your Secret Keys

In GitHub:

```text
Repository -> Settings -> Secrets and variables -> Actions -> New repository secret
```

Add these secrets:

| Secret | What It Is |
|---|---|
| `OPENAI_API_KEY` | Your OpenAI API key |
| `ELEVENLABS_API_KEY` | Your ElevenLabs API key |
| `ELEVENLABS_VOICE_ID_DE` | Optional fixed ElevenLabs voice ID for German |
| `ELEVENLABS_VOICE_ID_FR` | Optional fixed ElevenLabs voice ID for French |

Your editors do **not** need to see these keys. GitHub keeps them hidden.

## How To Use It Each Time

### Step 1: Upload The Video

Upload the English ad video into:

```text
inputs/ad.mp4
```

If you want to keep multiple ads, use names like:

```text
inputs/client1-ad1.mp4
inputs/client1-ad2.mp4
inputs/client2-ad1.mp4
```

### Step 2: Run The Automation

In GitHub:

```text
Actions -> Generate translated ad voiceovers -> Run workflow
```

Fill in:

| Field | Example |
|---|---|
| `video_path` | `inputs/ad.mp4` |
| `languages` | `de fr` |
| `write_debug_files` | `false` |

Use:

```text
de
```

for German only.

Use:

```text
fr
```

for French only.

Use:

```text
de fr
```

for both.

### Step 3: Download The MP3 Files

When the workflow finishes, open the finished run and download:

```text
translated-mp3-voiceovers
```

Inside, you will see:

```text
de_german_voiceover.mp3
fr_french_voiceover.mp3
```

Give those MP3 files to your editors.

## Optional Debug Files

Most of the time, leave this off.

If something sounds wrong and you want to inspect what happened, run the workflow with:

```text
write_debug_files = true
```

Then the download will also include script, timing, subtitle, and B-roll analysis files.

## Local Test Run

This part is optional. You can ignore it if you only want to use GitHub.

If you run it on your computer, create a `.env` file:

```text
OPENAI_API_KEY=your_openai_key
ELEVENLABS_API_KEY=your_elevenlabs_key
ELEVENLABS_VOICE_ID_DE=your_german_voice_id
ELEVENLABS_VOICE_ID_FR=your_french_voice_id
```

Then run:

```bash
pip install -r requirements.txt
python scripts/ad_voiceover_pipeline.py --video inputs/ad.mp4 --languages de fr
```

The MP3 files will appear in:

```text
outputs/
```

## Important Notes

- Use short ad videos first while testing.
- The final output is voiceover audio, not a fully edited video.
- Your editors still place the MP3 into the video manually.
- GitHub Actions is the automation runner. GitHub Pages is not used.
- If a translated line is too long, the automation tries to rewrite it naturally before making any speed adjustment.
- Speed adjustment is intentionally limited so the MP3 does not sound too fast, too slow, or awkward.
