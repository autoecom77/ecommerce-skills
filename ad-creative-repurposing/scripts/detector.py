#!/usr/bin/env python3
"""
Media Type & Language Detector.
- Detects whether file is a Video or Image via ffprobe / file headers.
- For Videos: extracts 15s audio and runs local faster-whisper (0 credits, CPU)
  to detect language and transcribe initial speech.
- For Images: runs vision analysis to check for visible text and detect language.
"""

import base64
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

HERMES_PYTHON = os.path.expanduser("~/.hermes/hermes-agent/venv/bin/python")
ECOMM_PYTHON = os.path.expanduser("~/.hermes/venvs/ecommerce/bin/python")
PYTHON_BIN = HERMES_PYTHON if os.path.exists(HERMES_PYTHON) else sys.executable


def detect_media_type(file_path: Path) -> str:
    """Detect whether file is a video or an image.

    Use `file --mime-type` magic FIRST: ffprobe reports still images (JPEG/PNG)
    as codec_type "video" (mjpeg/png codecs), which misroutes images into the
    audio/whisper branch. ffprobe remains the authority for real videos.
    """
    res = subprocess.run(["file", "--mime-type", "-b", str(file_path)], capture_output=True, text=True)
    mime = (res.stdout or "").strip().lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/") or mime == "application/octet-stream":
        # Confirm real motion video via ffprobe
        cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "stream=codec_type",
            "-of", "csv=p=0",
            str(file_path),
        ]
        out = subprocess.run(cmd, capture_output=True, text=True).stdout.strip().lower()
        if "video" in out or "audio" in out:
            return "video"

    # Fallback to suffix
    suf = file_path.suffix.lower()
    if suf in [".mp4", ".mov", ".webm", ".mkv"]:
        return "video"
    if suf in [".jpg", ".jpeg", ".png", ".webp", ".gif"]:
        return "image"
    return "unknown"


def detect_video_language(video_path: Path) -> Dict[str, Any]:
    """
    Extract first 15 seconds of audio from video and run faster-whisper.
    Returns language, probability, transcription, and is_french boolean.
    """
    temp_wav = video_path.parent / f"{video_path.stem}_temp_audio.wav"
    try:
        # Extract audio track (16kHz mono PCM)
        extract_cmd = [
            "ffmpeg",
            "-y",
            "-i", str(video_path),
            "-t", "15",
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", "16000",
            "-ac", "1",
            str(temp_wav),
        ]
        res = subprocess.run(extract_cmd, capture_output=True, text=True)
        if res.returncode != 0 or not temp_wav.exists() or temp_wav.stat().st_size < 1000:
            return {
                "has_speech": False,
                "language": "unknown",
                "probability": 0.0,
                "is_french": False,
                "text": "",
            }

        # Run faster-whisper inside Hermes venv
        py_code = f"""
import json
from faster_whisper import WhisperModel
model = WhisperModel("tiny", device="cpu", compute_type="int8")
segments, info = model.transcribe("{temp_wav}", beam_size=1)
text = " ".join([s.text.strip() for s in segments])
res = {{
    "language": info.language,
    "probability": float(info.language_probability),
    "has_speech": bool(text.strip()),
    "is_french": (info.language == "fr"),
    "text": text[:300]
}}
print(json.dumps(res))
"""
        whisper_cmd = [PYTHON_BIN, "-c", py_code]
        w_res = subprocess.run(whisper_cmd, capture_output=True, text=True, timeout=60)
        if w_res.returncode == 0:
            for line in w_res.stdout.splitlines():
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    return json.loads(line)
        return {
            "has_speech": False,
            "language": "unknown",
            "probability": 0.0,
            "is_french": False,
            "text": "",
            "error": w_res.stderr.strip(),
        }
    finally:
        if temp_wav.exists():
            temp_wav.unlink(missing_ok=True)


def get_openrouter_api_key() -> str:
    """Retrieve OPENROUTER_API_KEY from environment, ~/.hermes/.env, or local .env."""
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key

    paths = [
        Path.home() / ".hermes" / ".env",
        Path("/home/ubuntu/.hermes/.env"),
        Path(".env"),
    ]
    for p in paths:
        if p.is_file():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("OPENROUTER_API_KEY="):
                            val = line.split("=", 1)[1].strip(" \"'")
                            if val:
                                return val
            except Exception:
                pass

    raise RuntimeError(
        "OPENROUTER_API_KEY not found in environment, ~/.hermes/.env, or .env"
    )


def detect_image_language(
    image_path: Path,
    model: str = "google/gemini-3.8-flash",
) -> Dict[str, Any]:
    """
    Check if image contains text, detect its language, transcribe ad copy,
    and generate natural French translation using OpenRouter vision.
    """
    try:
        api_key = get_openrouter_api_key()

        import urllib.request
        with open(image_path, "rb") as f:
            b64_img = base64.b64encode(f.read()).decode("utf-8")

        mime = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
        prompt = (
            "Analyze this advertisement image carefully. Answer strictly in JSON (no markdown fences) "
            "with keys:\n"
            '{\n'
            '  "has_text": true/false,\n'
            '  "language": "fr"/"en"/"de"/...,\n'
            '  "is_french": true/false,\n'
            '  "transcribed_text": "...",\n'
            '  "french_translation": "...",\n'
            '  "has_logo": true/false,\n'
            '  "logo_description": "..." (brief description of any visible competitor logo, brand icon, watermark, or brand name emblem, or "" if none)\n'
            '}'
        )

        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64_img}"}},
                    ],
                }
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
        }

        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-Title": "Hermes Ad Creative Detector",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=40) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            # Tolerate markdown-fenced JSON answers
            content = content.strip()
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            parsed = json.loads(content.strip())
            return {
                "has_text": bool(parsed.get("has_text", True)),
                "language": parsed.get("language", "unknown"),
                "is_french": bool(parsed.get("is_french", False)),
                "detected_text": parsed.get("transcribed_text", ""),
                "french_translation": parsed.get("french_translation", ""),
                "has_logo": bool(parsed.get("has_logo", False)),
                "logo_description": str(parsed.get("logo_description", "") or "").strip(),
            }
    except Exception as e:
        return {
            "has_text": True,
            "language": "unknown",
            "is_french": False,
            "detected_text": "",
            "french_translation": "",
            "has_logo": False,
            "logo_description": "",
            "error": str(e),
        }


def analyze_asset(file_path: Path) -> Dict[str, Any]:
    """Full analysis entrypoint for any media file."""
    m_type = detect_media_type(file_path)
    result: Dict[str, Any] = {
        "file_path": str(file_path),
        "media_type": m_type,
    }

    if m_type == "video":
        vid_info = detect_video_language(file_path)
        result.update(vid_info)
    elif m_type == "image":
        img_info = detect_image_language(file_path)
        result.update(img_info)

    return result


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 detector.py <file_path>", file=sys.stderr)
        sys.exit(1)
    res = analyze_asset(Path(sys.argv[1]))
    print(json.dumps(res, indent=2, ensure_ascii=False))
