#!/usr/bin/env python3
"""
Vozo Translate & Dub Helper for Hermes Ecommerce Skills.

Automates video localization, dubbing, subtitle replacement, and audio-video
alignment using the official Vozo REST API (POST/GET /v1/media/translate)
with fallback to vozo-cli for local media files.

Default configuration:
- Source language: "auto" (automatic speech recognition)
- Target language: "fr" (French)
- Voice model: "auto" (automatic voice cloning)
- Speaker count: "auto" (automatic speaker diarization)
- Subtitle mode: "replace" (erases original burnt-in subtitles & renders new translated subtitles)
- Auto align video: True (adjusts video speed segment-by-segment to match dubbing timing)
- Project mode: "editable" (accessible in Vozo web console)
- Export type: "video" (renders localized .mp4)
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Optional

VOZO_API_BASE = "https://api.vozo.ai/v1"


def get_vozo_api_key() -> str:
    """
    Retrieve VOZO_API_KEY from environment, ~/.hermes/.env, or current directory .env.
    """
    # 1. Direct environment variable
    key = os.environ.get("VOZO_API_KEY", "").strip()
    if key:
        return key

    # 2. Check ~/.hermes/.env
    hermes_env = Path.home() / ".hermes" / ".env"
    if hermes_env.is_file():
        try:
            with open(hermes_env, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("VOZO_API_KEY="):
                        val = line.split("=", 1)[1].strip(" \"'")
                        if val:
                            return val
        except Exception:
            pass

    # 3. Check local .env
    local_env = Path(".env")
    if local_env.is_file():
        try:
            with open(local_env, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("VOZO_API_KEY="):
                        val = line.split("=", 1)[1].strip(" \"'")
                        if val:
                            return val
        except Exception:
            pass

    raise ValueError(
        "VOZO_API_KEY not found. Please set VOZO_API_KEY in your environment or in ~/.hermes/.env"
    )


def _api_request(
    endpoint: str,
    method: str = "GET",
    payload: Optional[Dict[str, Any]] = None,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Perform an authenticated HTTP request against the Vozo API.
    """
    token = api_key or get_vozo_api_key()
    url = f"{VOZO_API_BASE.rstrip('/')}/{endpoint.lstrip('/')}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Hermes-VozoHelper/1.0",
    }

    data_bytes = None
    if payload is not None:
        data_bytes = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            content = resp.read().decode("utf-8")
            if not content.strip():
                return {}
            return json.loads(content)
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(error_body)
            msg = parsed.get("message") or parsed.get("err_message") or error_body
        except Exception:
            msg = error_body
        raise RuntimeError(f"Vozo API error (HTTP {e.code}): {msg}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error connecting to Vozo API: {e.reason}") from e


def create_translate_dub_job(
    media_url: str,
    source_language: str = "auto",
    target_language: str = "fr",
    voice_model: str = "auto",
    subtitle_mode: str = "replace",
    align_video: bool = True,
    project_mode: str = "editable",
    export_type: str = "video",
    speaker_min: Optional[int] = None,
    speaker_max: Optional[int] = None,
    subtitle_url: Optional[str] = None,
    subtitle_usage_type: Optional[str] = None,
    user_prompt: Optional[str] = None,
    callback_url: Optional[str] = None,
    glossary_ids: Optional[list] = None,
    api_key: Optional[str] = None,
) -> str:
    """
    Submit a Translate & Dub job to Vozo API.

    Returns:
        task_id (str)
    """
    payload: Dict[str, Any] = {
        "media_type": "video",
        "media_url": media_url,
        "source_language": source_language,
        "target_language": target_language,
        "voice_model": voice_model,
        "subtitle_mode": subtitle_mode,
        "align_video": bool(align_video),
        "project_mode": project_mode,
        "export_type": export_type,
    }

    # Speakers: auto if omitted
    if speaker_min is not None or speaker_max is not None:
        spk: Dict[str, int] = {}
        if speaker_min is not None:
            spk["min"] = speaker_min
        if speaker_max is not None:
            spk["max"] = speaker_max
        payload["speaker_number"] = spk

    if subtitle_url:
        payload["subtitle_url"] = subtitle_url
        if subtitle_usage_type:
            payload["subtitle_usage_type"] = subtitle_usage_type

    if user_prompt:
        payload["user_prompt"] = user_prompt

    if callback_url:
        payload["callback_url"] = callback_url

    if glossary_ids:
        payload["glossary_ids"] = glossary_ids[:3]

    res = _api_request("media/translate", method="POST", payload=payload, api_key=api_key)
    task_id = res.get("task_id")
    if not task_id:
        raise RuntimeError(f"Vozo API did not return task_id: {res}")
    return task_id


def get_job_status(task_id: str, api_key: Optional[str] = None) -> Dict[str, Any]:
    """
    Query the status of a Translate & Dub job.

    Returns dict with keys:
        status: "queuing" | "preprocessing" | "transcribing" | "translating" | "dubbing" | "rendering" | "done" | "failed"
        result: dict with video_url, audio_url, subtitle_url, project_url (if done)
        err_code: string (if failed)
        err_message: string (if failed)
    """
    return _api_request(f"media/translate/{task_id}", method="GET", api_key=api_key)


def wait_for_job(
    task_id: str,
    poll_interval: int = 10,
    timeout: int = 1800,
    on_progress: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Poll Vozo API until the job reaches 'done' or 'failed'.
    """
    start_time = time.time()
    last_status = None

    while True:
        elapsed = time.time() - start_time
        if elapsed > timeout:
            raise TimeoutError(f"Job {task_id} timed out after {timeout} seconds.")

        data = get_job_status(task_id, api_key=api_key)
        status = data.get("status", "unknown")

        if status != last_status:
            last_status = status
            if on_progress:
                on_progress(status, data)
            else:
                print(f"[Vozo] Task {task_id} status: {status} (elapsed: {int(elapsed)}s)")

        if status == "done":
            return data
        elif status == "failed":
            code = data.get("err_code", "UNKNOWN")
            msg = data.get("err_message", "Unknown error")
            raise RuntimeError(f"Job {task_id} failed [{code}]: {msg}")

        time.sleep(poll_interval)


def download_file(url: str, dest_path: Path) -> Path:
    """
    Download a remote asset to local disk.
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "Hermes-VozoHelper/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest_path, "wb") as f:
        shutil.copyfileobj(resp, f)
    return dest_path


def download_results(
    result_data: Dict[str, Any],
    output_dir: str = "./vozo_output",
    base_name: Optional[str] = None,
) -> Dict[str, str]:
    """
    Download all artifacts (video, audio, srt) from a completed Vozo task result.

    Returns:
        Dict of artifact_name -> local_file_path
    """
    res = result_data.get("result") or result_data
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    prefix = base_name or f"vozo_{int(time.time())}"
    downloaded: Dict[str, str] = {}

    if res.get("video_url"):
        v_path = out_path / f"{prefix}_translated.mp4"
        print(f"[Vozo] Downloading video to {v_path}...")
        download_file(res["video_url"], v_path)
        downloaded["video"] = str(v_path.resolve())

    if res.get("subtitle_url"):
        s_path = out_path / f"{prefix}_subtitles.srt"
        print(f"[Vozo] Downloading subtitles to {s_path}...")
        download_file(res["subtitle_url"], s_path)
        downloaded["subtitles"] = str(s_path.resolve())

    if res.get("audio_url"):
        a_path = out_path / f"{prefix}_dubbed_audio.mp3"
        print(f"[Vozo] Downloading audio to {a_path}...")
        download_file(res["audio_url"], a_path)
        downloaded["audio"] = str(a_path.resolve())

    if res.get("project_url"):
        downloaded["project_url"] = res["project_url"]

    return downloaded


def run_vozo_cli_fallback(
    file_path: str,
    target_language: str = "fr",
    output_dir: str = "./vozo_output",
) -> Dict[str, Any]:
    """
    Fallback execution using vozo-cli for local media files.
    """
    p = Path(file_path).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"Local file not found: {file_path}")

    # Verify if vozo-cli is present
    cli_bin = shutil.which("vozo-cli")
    cmd_prefix = ["vozo-cli"] if cli_bin else ["npx", "-y", "@vozoai/cli@latest"]

    print(f"[Vozo CLI] Creating translate_dub project from local file: {p}")
    create_cmd = cmd_prefix + [
        "project",
        "translate_dub",
        "create",
        "--from-file",
        str(p),
        "--target-language",
        target_language,
        "--original-language",
        "auto",
        "--dub-preference",
        "auto",
        "--speaker-number",
        "Auto",
        "--enable-subtitles",
        "--remove-original-subtitle",
        "--auto-align-video",
    ]

    res = subprocess.run(create_cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"vozo-cli create failed:\n{res.stderr or res.stdout}")

    stdout = res.stdout.strip()
    print(stdout)
    return {"status": "submitted_via_cli", "raw_output": stdout}


def translate_video(
    source: str,
    target_language: str = "fr",
    source_language: str = "auto",
    voice_model: str = "auto",
    subtitle_mode: str = "replace",
    align_video: bool = True,
    project_mode: str = "editable",
    output_dir: Optional[str] = None,
    poll_interval: int = 10,
    timeout: int = 1800,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """
    High-level end-to-end video translation & dubbing pipeline.
    Accepts a public URL or a local file path.
    """
    is_url = source.startswith("http://") or source.startswith("https://")

    if not is_url:
        # Local file path
        local_p = Path(source)
        if not local_p.is_file():
            raise FileNotFoundError(f"Source file not found: {source}")
        print(f"[Vozo] Local file provided ({source}). Delegating to vozo-cli...")
        return run_vozo_cli_fallback(
            file_path=str(local_p),
            target_language=target_language,
            output_dir=output_dir or "./vozo_output",
        )

    # Public URL path via official Vozo REST API
    print(f"[Vozo API] Submitting Translate & Dub job for URL: {source}")
    print(f"           Source Language: {source_language}")
    print(f"           Target Language: {target_language}")
    print(f"           Voice Cloning:   {voice_model}")
    print(f"           Speakers:        auto")
    print(f"           Subtitles:       {subtitle_mode} (remove original + render new)")
    print(f"           Video Alignment: {align_video} (auto-adjust video speed)")

    task_id = create_translate_dub_job(
        media_url=source,
        source_language=source_language,
        target_language=target_language,
        voice_model=voice_model,
        subtitle_mode=subtitle_mode,
        align_video=align_video,
        project_mode=project_mode,
        api_key=api_key,
    )
    print(f"[Vozo API] Job submitted successfully. Task ID: {task_id}")

    final_status = wait_for_job(
        task_id=task_id,
        poll_interval=poll_interval,
        timeout=timeout,
        api_key=api_key,
    )

    result_meta = final_status.get("result", {})
    output_info: Dict[str, Any] = {
        "task_id": task_id,
        "status": "done",
        "result_urls": result_meta,
    }

    if output_dir:
        base_name = Path(urllib.parse.urlparse(source).path).stem or f"task_{task_id}"
        downloaded = download_results(final_status, output_dir=output_dir, base_name=base_name)
        output_info["local_files"] = downloaded

    return output_info


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Vozo Translate & Dub Helper for Hermes Ecommerce Skills"
    )
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # 1. 'run' command (create + wait + download)
    run_parser = subparsers.add_parser("run", help="Translate, wait, and download completed video")
    run_parser.add_argument(
        "--source", "-s", required=True, help="Video URL (public) or local file path"
    )
    run_parser.add_argument(
        "--target-language", "-t", default="fr", help="Target language code (default: fr)"
    )
    run_parser.add_argument(
        "--source-language", default="auto", help="Source language code (default: auto)"
    )
    run_parser.add_argument(
        "--voice-model",
        default="auto",
        choices=["auto", "real", "native"],
        help="Voice model (default: auto)",
    )
    run_parser.add_argument(
        "--subtitle-mode",
        default="replace",
        choices=["replace", "add", "none"],
        help="Subtitle mode (default: replace)",
    )
    run_parser.add_argument(
        "--no-align", action="store_true", help="Disable auto video-speed alignment"
    )
    run_parser.add_argument(
        "--output-dir", "-o", default="./vozo_output", help="Output directory for downloads"
    )
    run_parser.add_argument(
        "--poll-interval", type=int, default=10, help="Polling interval in seconds (default: 10)"
    )

    # 2. 'submit' command (create only)
    sub_parser = subparsers.add_parser("submit", help="Submit a job and return task ID")
    sub_parser.add_argument("--url", "-u", required=True, help="Public video URL")
    sub_parser.add_argument(
        "--target-language", "-t", default="fr", help="Target language code (default: fr)"
    )
    sub_parser.add_argument(
        "--source-language", default="auto", help="Source language code (default: auto)"
    )
    sub_parser.add_argument(
        "--voice-model", default="auto", choices=["auto", "real", "native"]
    )
    sub_parser.add_argument(
        "--subtitle-mode", default="replace", choices=["replace", "add", "none"]
    )
    sub_parser.add_argument(
        "--no-align", action="store_true", help="Disable auto video-speed alignment"
    )

    # 3. 'status' command
    status_parser = subparsers.add_parser("status", help="Check status of an existing task")
    status_parser.add_argument("--task-id", "-i", required=True, help="Vozo task ID")

    # 4. 'wait' command
    wait_parser = subparsers.add_parser("wait", help="Wait for task to finish and optionally download")
    wait_parser.add_argument("--task-id", "-i", required=True, help="Vozo task ID")
    wait_parser.add_argument(
        "--output-dir", "-o", default="./vozo_output", help="Directory to save downloaded files"
    )
    wait_parser.add_argument(
        "--poll-interval", type=int, default=10, help="Polling interval in seconds"
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        if args.command == "run":
            res = translate_video(
                source=args.source,
                target_language=args.target_language,
                source_language=args.source_language,
                voice_model=args.voice_model,
                subtitle_mode=args.subtitle_mode,
                align_video=not args.no_align,
                output_dir=args.output_dir,
                poll_interval=args.poll_interval,
            )
            print("\n[Vozo] Execution completed successfully:")
            print(json.dumps(res, indent=2))

        elif args.command == "submit":
            task_id = create_translate_dub_job(
                media_url=args.url,
                source_language=args.source_language,
                target_language=args.target_language,
                voice_model=args.voice_model,
                subtitle_mode=args.subtitle_mode,
                align_video=not args.no_align,
            )
            print(json.dumps({"task_id": task_id, "status": "submitted"}, indent=2))

        elif args.command == "status":
            st = get_job_status(args.task_id)
            print(json.dumps(st, indent=2))

        elif args.command == "wait":
            final_st = wait_for_job(args.task_id, poll_interval=args.poll_interval)
            print(f"[Vozo] Job {args.task_id} finished:")
            out = {"task_id": args.task_id, "status": "done", "result": final_st.get("result", {})}
            if args.output_dir:
                files = download_results(final_st, output_dir=args.output_dir, base_name=args.task_id)
                out["local_files"] = files
            print(json.dumps(out, indent=2))

    except Exception as e:
        print(f"[Vozo Error] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
