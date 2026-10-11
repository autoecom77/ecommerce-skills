#!/usr/bin/env python3
"""
Ad Resolver — Extract media asset from Facebook Ad Library or TrendTrack.
Uses Chrome CDP (port 9222) to navigate without anti-bot blockage,
extracting direct CDN media URLs (.mp4 or high-res image) with 0 API credits consumed.
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

CDP_URL = "http://127.0.0.1:9222"
ECOMM_PYTHON = os.path.expanduser("~/.hermes/venvs/ecommerce/bin/python")


def extract_ad_id(url: str) -> Optional[str]:
    """Extract ad numeric ID from URL."""
    m = re.search(r"[?&]id=(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"[?&]ad=(facebook_)?(\d+)", url)
    if m:
        return m.group(2)
    m = re.search(r"/ads/(facebook_)?(\d+)", url)
    if m:
        return m.group(2)
    return None


def extract_full_ad_id(url: str) -> Optional[str]:
    """Extract the full canonical ad id (with platform prefix), e.g. 'facebook_123'."""
    m = re.search(r"[?&]ad=(facebook_\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"/ads/(facebook_\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"/ads/(\d+)", url)
    if m:
        return f"facebook_{m.group(1)}"
    m = re.search(r"[?&]ad=(\d+)", url)
    if m:
        return f"facebook_{m.group(1)}"
    m = re.search(r"[?&]id=(facebook_)?(\d+)", url)
    if m:
        return f"facebook_{m.group(2)}"
    return None


def resolve_media_via_rest(full_ad_id: str) -> Dict[str, Any]:
    """Resolve media via the TrendTrack public REST API by ad id (0 credits)."""
    import json as _json

    key_path = Path(os.path.expanduser("~/.hermes/mcp-tokens/trendtrack_rest_key.txt"))
    if not key_path.exists():
        raise RuntimeError("TrendTrack REST key not found at ~/.hermes/mcp-tokens/trendtrack_rest_key.txt")
    api_key = key_path.read_text().strip()

    req = urllib.request.Request(
        f"https://api.trendtrack.io/v1/ads/{full_ad_id}",
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=40) as resp:
        data = _json.loads(resp.read().decode())

    record = data.get("data", data)
    media = record.get("media") or {}
    media_url = media.get("mediaUrl") or media.get("thumbnailUrl") or ""
    m_type = "video" if media.get("type") == "video" else "image"
    if not media_url:
        raise RuntimeError(f"No mediaUrl in REST record for {full_ad_id}")
    return {
        "media_type": m_type,
        "media_url": media_url,
        "ad_id": full_ad_id,
        "source_url": f"https://app.trendtrack.io/en/ju-2/explorer?tab=ads&ad={full_ad_id}&adSource=meta",
        "resolver": "trendtrack_rest",
    }


def resolve_media_via_cdp(ad_url: str, timeout_seconds: int = 30) -> Dict[str, Any]:
    """
    Connect to existing Chrome via CDP (9222), navigate to the ad URL,
    wait for DOM render, and extract video or image CDN source.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        # Fallback to invoking via the ecommerce venv where playwright is installed
        if os.path.exists(ECOMM_PYTHON) and sys.executable != ECOMM_PYTHON:
            cmd = [
                ECOMM_PYTHON,
                __file__,
                ad_url,
                "--resolve-only",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds + 10)
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    line = line.strip()
                    if line.startswith("{") and line.endswith("}"):
                        return json.loads(line)
            raise RuntimeError(f"CDP resolution via ecommerce venv failed: {res.stderr or res.stdout}")
        raise RuntimeError("playwright is not available to connect to CDP.")

    ad_id = extract_ad_id(ad_url)
    target_url = f"https://www.facebook.com/ads/library/?id={ad_id}" if ad_id else ad_url

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        try:
            page.goto(target_url, timeout=timeout_seconds * 1000, wait_until="domcontentloaded")
            # Wait up to 10 seconds for video or creative image to load
            result = None
            for _ in range(10):
                time.sleep(1)
                res = page.evaluate("""() => {
                    // 1. Check for video elements
                    const videos = Array.from(document.querySelectorAll('video'));
                    for (const v of videos) {
                        const s = v.currentSrc || v.src;
                        if (s && (s.includes('fbcdn') || s.includes('.mp4'))) {
                            return { type: 'video', src: s };
                        }
                    }

                    // 2. Check for high-res creative image (filter out avatars and small badges)
                    const imgs = Array.from(document.querySelectorAll('img')).filter(i => {
                        const s = i.src || '';
                        if (!s.includes('fbcdn') && !s.includes('cdn')) return false;
                        if (s.includes('s148x148') || s.includes('p50x50') || s.includes('s50x50') || s.includes('emoji')) return false;
                        const w = i.naturalWidth || i.width || 0;
                        const h = i.naturalHeight || i.height || 0;
                        return (w >= 200 || h >= 200 || (!w && !h));
                    });

                    if (imgs.length > 0) {
                        imgs.sort((a, b) => ((b.naturalWidth * b.naturalHeight) || 0) - ((a.naturalWidth * a.naturalHeight) || 0));
                        return { type: 'image', src: imgs[0].src };
                    }
                    return null;
                }""")
                if res and res.get("src"):
                    result = res
                    break

            if result:
                return {
                    "media_type": result["type"],
                    "media_url": result["src"],
                    "ad_id": ad_id or "",
                    "source_url": ad_url,
                }
        finally:
            page.close()

    raise RuntimeError(f"Could not resolve media element from {ad_url} via CDP.")


def download_asset(media_url: str, dest_path: Path) -> Path:
    """Download media file to disk with proper user-agent headers."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(
        media_url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest_path, "wb") as f:
        while chunk := resp.read(64 * 1024):
            f.write(chunk)
    return dest_path


def resolve_and_download(
    ad_url: str,
    output_dir: Path,
    slot_index: int = 1,
) -> Dict[str, Any]:
    """
    High-level entrypoint: resolve media URL and download to local output_dir.
    TrendTrack ad URLs (…/explorer?tab=ads&ad=facebook_<id> or /ads/<id>) are resolved
    via the public REST API (0 credits); Facebook Ad Library URLs fall back to Chrome CDP.
    Returns metadata dict including local file path.
    """
    full_ad_id = extract_full_ad_id(ad_url) if "trendtrack.io" in ad_url else None
    if full_ad_id:
        media_info = resolve_media_via_rest(full_ad_id)
    else:
        media_info = resolve_media_via_cdp(ad_url)
    m_type = media_info["media_type"]
    ext = "mp4" if m_type == "video" else "jpg"
    dest_file = output_dir / f"ad_{slot_index}_source.{ext}"

    download_asset(media_info["media_url"], dest_file)
    media_info["local_source_file"] = str(dest_file.resolve())
    return media_info


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[2] == "--resolve-only":
        info = resolve_media_via_cdp(sys.argv[1])
        print(json.dumps(info))
        sys.exit(0)

    if len(sys.argv) < 2:
        print("Usage: python3 ad_resolver.py <ad_url> [output_dir]", file=sys.stderr)
        sys.exit(1)
    url = sys.argv[1]
    out_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("/tmp")
    info = resolve_and_download(url, out_dir)
    print(json.dumps(info, indent=2))
