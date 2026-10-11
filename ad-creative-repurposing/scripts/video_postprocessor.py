#!/usr/bin/env python3
"""
Video Postprocessor for Ad Creative Repurposing.
Applies a warm night filter (colortemperature anti-blue) using FFmpeg.
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

DEFAULT_COLOR_TEMP = 5500  # Warm night shift (neutral daylight is 6500K)


def check_ffmpeg() -> str:
    """Verify that ffmpeg binary is available in PATH."""
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise RuntimeError("ffmpeg binary not found in system PATH.")
    return ffmpeg_path


def apply_video_night_filter(
    input_video: Path,
    output_video: Path,
    color_temp: int = DEFAULT_COLOR_TEMP,
    crf: int = 20,
    preset: str = "fast",
) -> Path:
    """
    Apply a night color temperature filter to an ad creative video.

    Args:
        input_video: Source video file path.
        output_video: Target video file path.
        color_temp: Target Kelvin temperature (default 5500K, pl=1 to preserve lightness).
        crf: Constant Rate Factor for x264 quality (default 20).
        preset: x264 encoding preset (default fast).

    Returns:
        output_video Path
    """
    check_ffmpeg()
    input_p = Path(input_video).resolve()
    output_p = Path(output_video).resolve()

    if not input_p.is_file():
        raise FileNotFoundError(f"Input video not found: {input_p}")

    output_p.parent.mkdir(parents=True, exist_ok=True)

    # Build filtergraph
    if color_temp and color_temp != 6500:
        vf_arg = f"colortemperature=temperature={color_temp}:pl=1"
    else:
        vf_arg = "null"

    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(input_p),
        "-vf", vf_arg,
        "-c:v", "libx264",
        "-preset", preset,
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(output_p),
    ]

    print(f"[FFmpeg] Applying night filter ({color_temp}K): {input_p.name} -> {output_p.name}")
    res = subprocess.run(cmd, capture_output=True, text=True)

    if res.returncode != 0:
        # Fallback if audio copy fails (e.g. incompatible bitstream format)
        print("[FFmpeg] Audio stream copy failed, falling back to AAC re-encoding...")
        cmd_fallback = [
            "ffmpeg",
            "-y",
            "-i", str(input_p),
            "-vf", vf_arg,
            "-c:v", "libx264",
            "-preset", preset,
            "-crf", str(crf),
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            str(output_p),
        ]
        res2 = subprocess.run(cmd_fallback, capture_output=True, text=True)
        if res2.returncode != 0:
            raise RuntimeError(f"FFmpeg transcode failed:\n{res.stderr or res2.stderr}")

    return output_p


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply FFmpeg night filter (anti-blue) to an ad creative video"
    )
    parser.add_argument("--input", "-i", required=True, help="Path to input video (.mp4)")
    parser.add_argument("--output", "-o", required=True, help="Path to output video (.mp4)")
    parser.add_argument(
        "--temp", "-t", type=int, default=DEFAULT_COLOR_TEMP,
        help=f"Color temperature in Kelvin (default: {DEFAULT_COLOR_TEMP})"
    )
    parser.add_argument("--crf", type=int, default=20, help="x264 CRF quality (default: 20)")
    parser.add_argument("--preset", default="fast", help="x264 preset (default: fast)")

    args = parser.parse_args()

    out = apply_video_night_filter(
        input_video=Path(args.input),
        output_video=Path(args.output),
        color_temp=args.temp,
        crf=args.crf,
        preset=args.preset,
    )
    print(f"Successfully generated: {out}")


if __name__ == "__main__":
    main()
