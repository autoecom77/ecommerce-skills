---
name: vozo-translate-dub
description: "Autonomous video localization and dubbing for Hermes Agent. Use whenever a video, creative, or competitor ad needs to be translated into French with voice cloning, subtitle replacement, and audio-video timing synchronization."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [ecommerce, video, translation, dubbing, vozo, ads, creative, localization]
    related_skills: [ad-creative-repurposing, recherche-produit-trendtrack]
---

# Vozo Video Translate & Dub (Autonomous Agent Directive)

## Directive for Hermes Agent

When instructed to translate, localize, or dub a video (such as a competitor ad, UGC video, product demonstration, or creative asset) into French:

**Do NOT prompt the user to specify CLI arguments or configuration parameters.**  
Execute the standardized command immediately with the following enforced defaults:

1. **Source Language:** `auto` (auto speech transcription)
2. **Target Language:** `fr` (French market localization)
3. **Voice Cloning:** `auto` (automatic voice model selection)
4. **Speaker Count:** `Auto` (automatic speaker diarization)
5. **Subtitles:** Erase original burnt-in subtitles + burn new translated French subtitles (`--enable-subtitles --remove-original-subtitle`)
6. **Video/Audio Alignment:** Enabled (`--auto-align-video` segment-by-segment speed adjustment)

---

## Standard Execution Procedure

### Step 1: Submit the Translation Job

#### Option A: Using `vozo-cli` (Recommended for Local Files or URLs)
```bash
vozo-cli project translate_dub create \
  --from-file "/path/to/source.mp4" \
  --target-language fr \
  --original-language auto \
  --dub-preference auto \
  --speaker-number Auto \
  --enable-subtitles \
  --remove-original-subtitle \
  --auto-align-video
```
*(If working with a public web URL, substitute `--from-file` with `--from-url "<url>"`).*

The command outputs the project metadata and Project ID (e.g., `{"projectId": "...", "status": "processing"}`).

#### Option B: Using the Python Helper (`vozo_translate.py`)
```bash
python3 ~/.hermes/skills/ecommerce/vozo_translate.py run \
  --source "<url_or_filepath>" \
  --output-dir "./output"
```

---

### Step 2: Track Job Progress
Poll project status until processing completes:

```bash
vozo-cli project translate_dub get <project_id>
```

Status stages: `processing` / `translating` / `dubbing` / `rendering` -> `ready` / `completed`.

---

### Step 3: Download Localized Assets
Once completed, download the finalized French video and subtitle files:

```bash
# Download the final dubbed video
vozo-cli project translate_dub download <project_id> \
  --artifact dubbed_video \
  --output "./output/"

# Download the French subtitles (SRT)
vozo-cli project translate_dub download <project_id> \
  --artifact subtitles \
  --output "./output/"
```

---

## Environment & Authentication Details

- **CLI Binary:** `/usr/local/bin/vozo-cli` (installed globally on `julien-hermes`)
- **Active Session:** Authenticated via `~/.vozo/cli/session.json` (`autoecom77@gmail.com`)
- **REST API Key:** Exported in `~/.hermes/.env` as `VOZO_API_KEY`
- **Quota Verification:**
  ```bash
  vozo-cli auth status
  vozo-cli auth points
  ```
