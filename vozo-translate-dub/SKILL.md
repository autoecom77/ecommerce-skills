---
name: vozo-translate-dub
description: "Localize ecommerce videos and competitor ads using Vozo Translate & Dub API (default: auto source to French, auto voice clone, auto speakers, replace subtitles, auto align video & audio)."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [ecommerce, video, translation, dubbing, vozo, ads, creative, localization]
    related_skills: [ad-creative-repurposing, recherche-produit-trendtrack]
---

# Vozo Video Translate & Dub

Automates video localization, multilingual voice dubbing, subtitle replacement, and audio-video timing synchronization using the **Vozo Translate & Dub API** (`POST /v1/media/translate` and `GET /v1/media/translate/{task_id}`).

Particularly designed for ecommerce dropshipping pipelines to adapt high-converting winning competitor ads (from Facebook Ad Library, TikTok, or TrendTrack) into the French target market.

---

## Default Configuration

All default parameters match the European dropshipping workflow requirements:

| Parameter | Default Value | Description |
| :--- | :--- | :--- |
| **`source_language`** | `"auto"` | Automatically detects speech language in the source video. |
| **`target_language`** | `"fr"` | Translates audio and subtitles to French. |
| **`voice_model`** | `"auto"` | Automatic voice cloning mode (`auto` / `real` / `native`). |
| **`speaker_number`** | `auto` | Auto-detects speaker diarization without requiring manual speaker count. |
| **`subtitle_mode`** | `"replace"` | **Erases original burnt-in subtitles** from the video frames and burns new translated French subtitles. |
| **`align_video`** | `true` | **Automatically adjusts video speed segment-by-segment** to synchronize video pacing with translated speech. |
| **`project_mode`** | `"editable"` | Creates an editable project in the Vozo web dashboard (`app.vozo.ai`) for manual review if needed. |
| **`export_type`** | `"video"` | Renders and exports the final localized MP4 video. |

---

## Authentication

The helper loads `VOZO_API_KEY` in order of priority:
1. `VOZO_API_KEY` environment variable.
2. `~/.hermes/.env` (standard Hermes agent secrets file).
3. Local `.env` file.

To verify key availability on `julien-hermes`:
```bash
python3 vozo_translate.py status --task-id test_id
```

---

## Usage

### 1. High-Level Python API

Inside Hermes or Python scripts:

```python
from vozo_translate import translate_video

# Run end-to-end (submit, wait for processing, download localized MP4 + SRT)
result = translate_video(
    source="https://video.xx.fbcdn.net/v/example_ad.mp4",
    output_dir="./localized_ads",
    target_language="fr",
)

print("Downloaded translated video:", result["local_files"]["video"])
print("Downloaded French subtitles:", result["local_files"]["subtitles"])
print("Vozo project URL:", result["result_urls"]["project_url"])
```

### 2. Command Line Interface (CLI)

#### A. Full Run (Submit, Wait, Download)
```bash
python3 vozo_translate.py run \
  --source "https://example.com/competitor_ad.mp4" \
  --output-dir ./out
```

#### B. Asynchronous Job Submission
```bash
# Submit and get task_id
python3 vozo_translate.py submit --url "https://example.com/ad.mp4"
# Output: {"task_id": "c1a2...3b4", "status": "submitted"}
```

#### C. Check Job Status
```bash
python3 vozo_translate.py status --task-id "c1a2...3b4"
```

#### D. Wait and Download Existing Task
```bash
python3 vozo_translate.py wait --task-id "c1a2...3b4" --output-dir ./out
```

---

## Integration with `ad-creative-repurposing`

In dropshipping creative workflows:
1. **Locate Creative:** Extract the direct CDN MP4 URL from Facebook Ad Library using `ad-creative-repurposing`.
2. **Translate & Dub:** Pass that CDN URL directly to `vozo_translate.py` to produce a French-dubbed version with replaced subtitles and synchronized video timing.
3. **Post-Process:** Use FFmpeg for brand intro/outro splicing, color grading, or resizing if needed.
