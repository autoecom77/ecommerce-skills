#!/usr/bin/env python3
"""
Image Translator (OpenRouter integration, backward-compatible wrapper).
Historically routed through Nous Portal; now delegates directly to OpenRouter Image API.
"""

import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# Local import
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from openrouter_image_translator import (
    translate_image_openrouter,
    BASE_TRANSLATION_PROMPT,
)


def translate_image_nous(
    image_source: str,
    output_path: Path,
    custom_prompt: Optional[str] = None,
    media_url: Optional[str] = None,
    french_text_guide: Optional[str] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Backward-compatible entrypoint. Delegates to translate_image_openrouter().
    """
    kwargs = {}
    if model:
        kwargs["model"] = model
    return translate_image_openrouter(
        image_source=image_source,
        output_path=output_path,
        custom_prompt=custom_prompt,
        media_url=media_url,
        french_text_guide=french_text_guide,
        **kwargs,
    )


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(
            "Usage: python3 nous_image_translator.py <source_image> <output_file>",
            file=sys.stderr,
        )
        sys.exit(1)

    src = sys.argv[1]
    dest = Path(sys.argv[2])
    res = translate_image_nous(src, dest)
    print(json.dumps(res, indent=2))
