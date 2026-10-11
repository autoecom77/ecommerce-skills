#!/usr/bin/env python3
"""
Vozo Video Translator.
Interfaces with vozo_translate.py to perform automated dubbing,
subtitle recreation, and video-audio alignment into French.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

VOZO_HELPER_PATH = os.path.expanduser("~/.hermes/skills/ecommerce/vozo_translate.py")
LOCAL_VOZO_HELPER = Path(__file__).resolve().parent.parent.parent / "vozo_translate.py"


def get_vozo_script() -> str:
    if os.path.exists(VOZO_HELPER_PATH):
        return VOZO_HELPER_PATH
    if LOCAL_VOZO_HELPER.exists():
        return str(LOCAL_VOZO_HELPER)
    raise FileNotFoundError("vozo_translate.py not found in skills/ecommerce/")


def translate_video_vozo(
    video_source: str,
    output_dir: Path,
    base_name: str = "ad_dubbed_fr",
    poll_interval: int = 10,
    timeout: int = 1800,
) -> Dict[str, Any]:
    """
    Execute end-to-end Vozo translation pipeline.
    Produces dubbed video + SRT subtitles.
    """
    vozo_script = get_vozo_script()
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        vozo_script,
        "run",
        "--source", str(video_source),
        "--target-language", "fr",
        "--source-language", "auto",
        "--voice-model", "auto",
        "--subtitle-mode", "replace",
        "--output-dir", str(output_dir),
        "--poll-interval", str(poll_interval),
    ]

    print(f"[Vozo] Executing video localization for {video_source}...")
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 60)

    if res.returncode != 0:
        raise RuntimeError(f"Vozo translation failed:\n{res.stderr or res.stdout}")

    stdout = res.stdout.strip()
    try:
        # Find JSON payload in stdout
        idx = stdout.find("{")
        if idx != -1:
            parsed = json.loads(stdout[idx:])
            return parsed
    except Exception:
        pass

    return {
        "status": "completed",
        "raw_output": stdout,
        "output_dir": str(output_dir),
    }


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 vozo_video_translator.py <video_source> <output_dir>", file=sys.stderr)
        sys.exit(1)

    src = sys.argv[1]
    out_d = Path(sys.argv[2])
    res = translate_video_vozo(src, out_d)
    print(json.dumps(res, indent=2))
