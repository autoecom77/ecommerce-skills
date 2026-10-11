#!/usr/bin/env python3
"""
OpenRouter Image Translator.
Translates visible text in competitor ad images into French using OpenRouter's
Unified Image API (POST https://openrouter.ai/api/v1/images) with image-to-image
reference inputs, preserving visual layout, typography style, and product visuals.
"""

import argparse
import base64
import json
import mimetypes
import os
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_IMAGE_MODEL = "bytedance-seed/seedream-5-0-flash"
OPENROUTER_IMAGES_ENDPOINT = "https://openrouter.ai/api/v1/images"

BASE_TRANSLATION_PROMPT = (
    "Translate ALL visible text in this advertisement image to French. "
    "Keep the layout, composition, colors, typography style, product packaging, lighting, "
    "and visual elements strictly identical to the original image. "
    "Accurately replace all foreign headlines, badges, labels, and bullet points with natural, "
    "high-converting French translations. Only the text changes."
)


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


def build_image_reference(image_source: str, media_url: Optional[str] = None) -> str:
    """
    Produce a valid image reference URL or base64 data URI for OpenRouter input_references.
    If media_url is a public HTTP/HTTPS URL, it can be used directly.
    Otherwise, reads local file and converts to data:{mime};base64,{data}.
    """
    if str(image_source).startswith(("http://", "https://")):
        return str(image_source)

    if media_url and str(media_url).startswith(("http://", "https://")):
        return str(media_url)

    local_path = Path(image_source)
    if not local_path.is_file():
        raise FileNotFoundError(f"Source image file not found: {image_source}")

    mime, _ = mimetypes.guess_type(str(local_path))
    if not mime or not mime.startswith("image/"):
        ext = local_path.suffix.lower()
        if ext in [".jpg", ".jpeg"]:
            mime = "image/jpeg"
        elif ext == ".png":
            mime = "image/png"
        elif ext == ".webp":
            mime = "image/webp"
        else:
            mime = "image/jpeg"

    with open(local_path, "rb") as f:
        b64_data = base64.b64encode(f.read()).decode("utf-8")

    return f"data:{mime};base64,{b64_data}"


def download_file(url: str, dest_path: Path) -> Path:
    """Download image from URL to local file."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest_path, "wb") as f:
        shutil.copyfileobj(resp, f)
    return dest_path


def translate_image_openrouter(
    image_source: str,
    output_path: Path,
    custom_prompt: Optional[str] = None,
    media_url: Optional[str] = None,
    model: str = DEFAULT_IMAGE_MODEL,
    french_text_guide: Optional[str] = None,
    aspect_ratio: str = "1:1",
    timeout: int = 120,
) -> Dict[str, Any]:
    """
    Translate image text using OpenRouter's Image API with image-to-image input references.
    """
    api_key = get_openrouter_api_key()
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Build prompt
    prompt = custom_prompt or BASE_TRANSLATION_PROMPT
    if french_text_guide and not custom_prompt:
        clean_guide = french_text_guide.strip()
        prompt = (
            f"{prompt}\n\n"
            f"Use the following high-converting French marketing translations for the ad copy:\n"
            f"{clean_guide}"
        )

    # Build image input reference
    image_ref = build_image_reference(image_source, media_url=media_url)

    payload = {
        "model": model,
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
        "input_references": [
            {
                "type": "image_url",
                "image_url": {"url": image_ref},
            }
        ],
    }

    req = urllib.request.Request(
        OPENROUTER_IMAGES_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Title": "Hermes Ad Creative Repurposing",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"OpenRouter image generation API failed ({e.code}): {err_body}"
        ) from e
    except Exception as e:
        raise RuntimeError(f"OpenRouter image generation request error: {e}") from e

    images = data.get("data", [])
    if not images:
        raise RuntimeError(f"OpenRouter returned no image data: {data}")

    first_image = images[0]
    out_b64 = first_image.get("b64_json")
    out_url = first_image.get("url")

    if out_b64:
        img_bytes = base64.b64decode(out_b64)
        with open(output_path, "wb") as f:
            f.write(img_bytes)
    elif out_url:
        download_file(out_url, output_path)
    else:
        raise RuntimeError(
            f"OpenRouter response contained neither b64_json nor url: {first_image}"
        )

    return {
        "status": "success",
        "provider": "openrouter",
        "model": model,
        "output_file": str(output_path.resolve()),
        "source_image": str(image_source),
        "usage": data.get("usage", {}),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Translate advertisement image text to French via OpenRouter Image API"
    )
    parser.add_argument("source", help="Path to local source image or public image URL")
    parser.add_argument("output", help="Destination path for translated image")
    parser.add_argument(
        "--model", default=DEFAULT_IMAGE_MODEL, help=f"Image model (default: {DEFAULT_IMAGE_MODEL})"
    )
    parser.add_argument(
        "--media-url", default=None, help="Optional public CDN URL of source image"
    )
    parser.add_argument(
        "--guide", default=None, help="Optional French text replacement guide"
    )
    parser.add_argument(
        "--aspect-ratio", default="1:1", help="Aspect ratio (default: 1:1)"
    )

    args = parser.parse_args()

    try:
        res = translate_image_openrouter(
            image_source=args.source,
            output_path=Path(args.output),
            media_url=args.media_url,
            model=args.model,
            french_text_guide=args.guide,
            aspect_ratio=args.aspect_ratio,
        )
        print(json.dumps(res, indent=2))
    except Exception as e:
        print(f"[Error] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
