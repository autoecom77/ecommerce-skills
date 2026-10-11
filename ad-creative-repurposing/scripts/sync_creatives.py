#!/usr/bin/env python3
"""
Sync Creatives Helper.
Triggers one-way synchronization of /home/ubuntu/AdCreatives to gdrive:AdCreatives.
Supports fast targeted synchronization of individual product folders (< 1s)
as well as full directory synchronization via systemd user service / sync script.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

BASE_ADCREATIVES_DIR = Path("/home/ubuntu/AdCreatives")
REMOTE_TARGET = "gdrive:AdCreatives"
SYNC_SCRIPT = Path(os.path.expanduser("~/scripts/sync-ad-creatives.sh"))
RCLONE_BIN = "/usr/bin/rclone"


def sync_product_folder(
    product_slug: str,
    base_dir: Path = BASE_ADCREATIVES_DIR,
    quiet: bool = False,
) -> bool:
    """
    Fast, targeted ONE-WAY sync of an individual product folder to Google Drive (< 1s).
    Mirroring: local source -> gdrive destination.
    """
    src_dir = base_dir / product_slug
    if not src_dir.is_dir():
        if not quiet:
            print(f"[Sync Warning] Local directory does not exist: {src_dir}", file=sys.stderr)
        return False

    remote_dest = f"{REMOTE_TARGET}/{product_slug}"
    if not quiet:
        print(f"[Sync] Fast one-way sync: {src_dir} -> {remote_dest}...")

    cmd = [
        RCLONE_BIN,
        "sync",
        str(src_dir),
        remote_dest,
        "--fast-list",
        "--exclude", ".DS_Store",
        "--exclude", "*.tmp",
        "--exclude", "*.lock",
    ]

    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if res.returncode == 0:
        if not quiet:
            print(f"[Sync] ✅ Successfully persisted {product_slug} to Google Drive.")
        return True
    else:
        err = res.stderr.strip() or res.stdout.strip()
        print(f"[Sync Error] Failed to sync {product_slug}: {err}", file=sys.stderr)
        return False


def trigger_sync(product_slug: Optional[str] = None) -> bool:
    """
    Trigger one-way synchronization to Google Drive.
    If product_slug is provided, performs fast targeted sync (< 1s).
    Otherwise, runs full sync via systemd user service or fallback script.
    """
    if product_slug:
        return sync_product_folder(product_slug)

    print("[Sync] Triggering full one-way rclone sync to Google Drive (gdrive:AdCreatives)...")

    # Try triggering via user systemd service first
    res = subprocess.run(
        ["systemctl", "--user", "start", "rclone-adcreatives-sync.service"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if res.returncode == 0:
        print("[Sync] Successfully triggered systemd user service: rclone-adcreatives-sync.service")
        return True

    # Fallback to direct script execution
    if SYNC_SCRIPT.exists():
        print(f"[Sync] Running script directly: {SYNC_SCRIPT}")
        res2 = subprocess.run(
            ["/usr/bin/env", "bash", str(SYNC_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if res2.returncode == 0:
            print("[Sync] Direct sync script completed successfully.")
            return True
        else:
            print(f"[Sync Error] {res2.stderr or res2.stdout}", file=sys.stderr)
            return False

    print("[Sync Warning] Neither systemd service nor sync script could be run.", file=sys.stderr)
    return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="One-Way Synchronization of AdCreatives to Google Drive"
    )
    parser.add_argument(
        "--product", "-p", default=None,
        help="Optional product slug for fast targeted synchronization (< 1s)"
    )
    args = parser.parse_args()

    ok = trigger_sync(product_slug=args.product)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
