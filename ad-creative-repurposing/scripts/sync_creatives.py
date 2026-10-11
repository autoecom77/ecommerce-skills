#!/usr/bin/env python3
"""
Sync Creatives Helper.
Triggers one-way synchronization of /home/ubuntu/AdCreatives to gdrive:AdCreatives.
Uses systemd user service (rclone-adcreatives-sync.service) or direct script execution.
"""

import os
import subprocess
import sys
from pathlib import Path

SYNC_SCRIPT = Path(os.path.expanduser("~/scripts/sync-ad-creatives.sh"))


def trigger_sync() -> bool:
    print("[Sync] Triggering one-way rclone sync to Google Drive (gdrive:AdCreatives)...")

    # Try triggering via user systemd service first
    res = subprocess.run(["systemctl", "--user", "start", "rclone-adcreatives-sync.service"],
                         capture_output=True, text=True)
    if res.returncode == 0:
        print("[Sync] Successfully triggered systemd user service: rclone-adcreatives-sync.service")
        return True

    # Fallback to direct script execution
    if SYNC_SCRIPT.exists():
        print(f"[Sync] Running script directly: {SYNC_SCRIPT}")
        res2 = subprocess.run(["/usr/bin/env", "bash", str(SYNC_SCRIPT)], capture_output=True, text=True)
        if res2.returncode == 0:
            print("[Sync] Direct sync script completed successfully.")
            return True
        else:
            print(f"[Sync Error] {res2.stderr or res2.stdout}", file=sys.stderr)
            return False

    print("[Sync Warning] Neither systemd service nor sync script could be run.", file=sys.stderr)
    return False


if __name__ == "__main__":
    ok = trigger_sync()
    sys.exit(0 if ok else 1)
