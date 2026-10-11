#!/usr/bin/env python3
"""
Repurpose Ad Creatives — Master Decision-Tree Pipeline Orchestrator.

Given a ticked row in the Google Sheet (Products tab), extracts up to X competitor ads
and routes them through the deterministic decision tree:
- Detect Media Type (Video vs Image)
- Video:
  - If French audio (faster-whisper) -> Keep raw .mp4 as-is (0 credits).
  - If Non-French audio -> Vozo Translate & Dub (voice clone, auto align, French subs).
- Image:
  - If French text or no text -> Keep raw image as-is.
  - If Non-French text -> Nous Portal in-place translation preserving visual layout.
- Saves assets to /home/ubuntu/AdCreatives/<product_slug>/
- Automatically triggers persistent one-way rclone sync to gdrive:AdCreatives.
"""

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List

# Local imports
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from sheet_rows import get_ticked_products
from ad_resolver import resolve_and_download
from detector import analyze_asset
from openrouter_image_translator import translate_image_openrouter, DEFAULT_IMAGE_MODEL
from vozo_video_translator import translate_video_vozo
from video_postprocessor import apply_video_night_filter, DEFAULT_COLOR_TEMP
from sync_creatives import trigger_sync


def slugify(text: str) -> str:
    """Convert product title to filesystem-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-") or "product"


def process_product_ads(
    row_number: int,
    max_ads: int = 3,
    base_output_dir: Path = Path("/home/ubuntu/AdCreatives"),
    dry_run: bool = False,
    skip_sync: bool = False,
    ad_slots: list = None,
    image_model: str = DEFAULT_IMAGE_MODEL,
    video_color_temp: int = DEFAULT_COLOR_TEMP,
) -> Dict[str, Any]:
    """ad_slots: optional explicit list of AD slot numbers (e.g. [3]) to process
    exclusively, instead of the first max_ads slots."""
    print(f"\n=======================================================")
    print(f"🎬 Starting Ad Creative Repurposing for Sheet Row {row_number}")
    print(f"   Max Ads: {max_ads} | Dry Run: {dry_run} | Image Model: {image_model}")
    print(f"=======================================================\n")

    # 1. Fetch ticked product row
    products = get_ticked_products(filter_row=row_number)
    if not products:
        raise ValueError(
            f"Row {row_number} is not ticked (Validation / Lancement != TRUE) or product name is empty."
        )

    product = products[0]
    p_name = product["name"]
    p_slug = slugify(p_name)
    all_ad_slots = product.get("ads", [])

    print(f"📦 Product: {p_name}")
    print(f"🔗 URL: {product.get('url')}")
    print(f"🎯 Total Ad slots available: {len(all_ad_slots)}")

    if not all_ad_slots:
        print("[Warning] No competitor ad links found in this row.")
        return {"status": "no_ads_found", "product": p_name, "row": row_number}

    # Restrict to max_ads, or to the explicitly requested AD slots
    if ad_slots:
        wanted = {int(s) for s in ad_slots}
        target_ads = [a for a in all_ad_slots if a["slot"] in wanted]
    else:
        target_ads = all_ad_slots[:max_ads]
    prod_dir = base_output_dir / p_slug
    prod_dir.mkdir(parents=True, exist_ok=True)

    manifest: Dict[str, Any] = {
        "product_name": p_name,
        "product_slug": p_slug,
        "sheet_row": row_number,
        "product_url": product.get("url"),
        "processed_ads": [],
        "dry_run": dry_run,
    }

    for idx, ad_info in enumerate(target_ads, start=1):
        slot = ad_info["slot"]
        ad_url = ad_info["url"]
        print(f"\n--- Processing Ad #{idx} (Slot AD{slot}) ---")
        print(f"Ad URL: {ad_url}")

        ad_record: Dict[str, Any] = {
            "index": idx,
            "slot": slot,
            "source_url": ad_url,
        }

        try:
            # 2. Resolve media asset via Chrome CDP / direct CDN
            print(f"[1/3] Resolving media asset...")
            media_info = resolve_and_download(ad_url, prod_dir, slot_index=slot)
            source_file = Path(media_info["local_source_file"])
            m_type = media_info["media_type"]
            media_url = media_info.get("media_url", "")
            ad_record["source_file"] = str(source_file)
            ad_record["media_type"] = m_type
            ad_record["media_url"] = media_url

            # 3. Detect media type and language
            print(f"[2/3] Analyzing media content & language...")
            analysis = analyze_asset(source_file)
            ad_record["analysis"] = analysis

            # 4. Decision Tree Branching
            print(f"[3/3] Executing Decision Tree for {m_type.upper()}...")

            if m_type == "video":
                is_fr = analysis.get("is_french", False)
                detected_lang = analysis.get("language", "unknown")
                prob = analysis.get("probability", 0.0)

                print(f"      Detected speech language: {detected_lang} (prob: {prob:.2f})")

                if is_fr:
                    # French Video: keep raw as-is! 0 Vozo credits.
                    raw_dest = prod_dir / f"ad_{slot}_raw.mp4"
                    shutil.copy2(source_file, raw_dest)
                    ad_record["action"] = "direct_download_raw"
                    ad_record["raw_video"] = str(raw_dest)
                    ad_record["reason"] = "Audio already in French (preserved raw, 0 credits)"
                    print(f"      ✅ Audio is in French! Preserved raw: {raw_dest.name}")

                    # Final FFmpeg Post-Processing: Night filter (anti-blue)
                    repurposed_dest = prod_dir / f"ad_{slot}_repurposed.mp4"
                    if dry_run:
                        ad_record["output_video"] = str(repurposed_dest)
                        ad_record["postprocess"] = {
                            "filter": "colortemperature",
                            "temperature": video_color_temp,
                            "status": "dry_run_skipped",
                        }
                        print(f"      ⚠️ Dry Run: Would apply FFmpeg night filter ({video_color_temp}K)")
                    else:
                        print(f"      🌙 Applying FFmpeg night filter ({video_color_temp}K)...")
                        apply_video_night_filter(
                            input_video=raw_dest,
                            output_video=repurposed_dest,
                            color_temp=video_color_temp,
                        )
                        ad_record["output_video"] = str(repurposed_dest)
                        ad_record["postprocess"] = {
                            "filter": "colortemperature",
                            "temperature": video_color_temp,
                            "status": "completed",
                        }
                        print(f"      ✅ Final repurposed creative ready: {repurposed_dest.name}")
                else:
                    # Non-French Video: Vozo pipeline
                    ad_record["action"] = "vozo_dub_and_subtitles"
                    ad_record["reason"] = f"Audio in {detected_lang}; routed to Vozo dubbing & subtitles"

                    if dry_run:
                        ad_record["status"] = "dry_run_skipped"
                        print(f"      ⚠️ Dry Run: Would submit to Vozo (Dub FR + Subtitles replace)")
                    else:
                        print(f"      🚀 Launching Vozo Translate & Dub pipeline...")
                        vozo_res = translate_video_vozo(
                            video_source=str(source_file),
                            output_dir=prod_dir,
                            base_name=f"ad_{slot}_dubbed_fr",
                        )
                        ad_record["vozo_result"] = vozo_res
                        print(f"      ✅ Vozo localization completed successfully!")

                        # Locate the dubbed video from vozo output
                        dubbed_video_path = None
                        if isinstance(vozo_res, dict):
                            local_files = vozo_res.get("local_files") or {}
                            if "video" in local_files:
                                candidate_p = Path(local_files["video"])
                                if candidate_p.is_file():
                                    dubbed_video_path = candidate_p
                        if not dubbed_video_path:
                            candidate1 = prod_dir / f"ad_{slot}_dubbed_fr_translated.mp4"
                            candidate2 = prod_dir / f"ad_{slot}_dubbed_fr.mp4"
                            if candidate1.is_file():
                                dubbed_video_path = candidate1
                            elif candidate2.is_file():
                                dubbed_video_path = candidate2

                        if dubbed_video_path and dubbed_video_path.is_file():
                            ad_record["dubbed_video"] = str(dubbed_video_path)
                            repurposed_dest = prod_dir / f"ad_{slot}_repurposed.mp4"
                            print(f"      🌙 Applying FFmpeg night filter ({video_color_temp}K)...")
                            apply_video_night_filter(
                                input_video=dubbed_video_path,
                                output_video=repurposed_dest,
                                color_temp=video_color_temp,
                            )
                            ad_record["output_video"] = str(repurposed_dest)
                            ad_record["postprocess"] = {
                                "filter": "colortemperature",
                                "temperature": video_color_temp,
                                "status": "completed",
                            }
                            print(f"      ✅ Final repurposed creative ready: {repurposed_dest.name}")
                        else:
                            print(f"      [Warning] Dubbed video file not found for post-processing.")

            elif m_type == "image":
                has_txt = analysis.get("has_text", False)
                is_fr = analysis.get("is_french", False)
                img_lang = analysis.get("language", "unknown")
                detected_txt = analysis.get("detected_text", "")
                french_trans = analysis.get("french_translation", "")

                print(f"      Has text: {has_txt} | Language: {img_lang}")
                if detected_txt:
                    print(f"      Original text: {detected_txt[:120]}...")
                if french_trans:
                    print(f"      French marketing translation: {french_trans[:120]}...")

                if not has_txt or is_fr:
                    # Universal / French image: keep raw as-is
                    raw_dest = prod_dir / f"ad_{slot}_raw{source_file.suffix}"
                    shutil.copy2(source_file, raw_dest)
                    ad_record["action"] = "direct_download_raw"
                    ad_record["output_image"] = str(raw_dest)
                    ad_record["reason"] = "No text or already in French (preserved raw)"
                    print(f"      ✅ Image is French/Universal. Preserved raw: {raw_dest.name}")
                else:
                    # Non-French Image: OpenRouter Image translation
                    ad_record["action"] = "openrouter_image_translate"
                    ad_record["reason"] = f"Image text in {img_lang}; routed to OpenRouter Image API ({image_model})"
                    ad_record["detected_text"] = detected_txt
                    ad_record["french_translation"] = french_trans

                    if dry_run:
                        ad_record["status"] = "dry_run_skipped"
                        print(f"      ⚠️ Dry Run: Would translate text to French via OpenRouter ({image_model})")
                    else:
                        print(f"      🎨 Translating image via OpenRouter ({image_model})...")
                        translated_dest = prod_dir / f"ad_{slot}_translated_fr.png"
                        or_res = translate_image_openrouter(
                            image_source=str(source_file),
                            output_path=translated_dest,
                            media_url=media_url,
                            model=image_model,
                            french_text_guide=french_trans,
                        )
                        ad_record["output_image"] = str(translated_dest)
                        ad_record["openrouter_result"] = or_res
                        print(f"      ✅ OpenRouter image translation completed: {translated_dest.name}")

            ad_record["status"] = "success"

        except Exception as e:
            print(f"      ❌ Error processing Ad #{idx}: {e}")
            ad_record["status"] = "error"
            ad_record["error"] = str(e)

        manifest["processed_ads"].append(ad_record)

        # 5. Incremental Persistence: Save manifest and sync to Google Drive after EACH ad
        manifest_file = prod_dir / "manifest.json"
        with open(manifest_file, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)

        if not dry_run and not skip_sync:
            print(f"      ☁️ Persisting Ad #{idx} (Slot AD{slot}) to Google Drive (gdrive:AdCreatives/{p_slug})...")
            trigger_sync(product_slug=p_slug)

    print(f"\n📄 Saved final manifest: {manifest_file}")

    # Final verification sync
    if not dry_run and not skip_sync:
        print(f"\n☁️ Final one-way synchronization check for {p_slug}...")
        trigger_sync(product_slug=p_slug)

    print(f"\n🎉 Finished processing product: {p_name}\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Autonomous Ad Creative Repurposing & Localization Pipeline"
    )
    parser.add_argument(
        "--row", "-r", type=int, required=True,
        help="Google Sheet row number (must be a ticked row)"
    )
    parser.add_argument(
        "--max-ads", "-m", type=int, default=3,
        help="Maximum number of competitor ads to process (default: 3)"
    )
    parser.add_argument(
        "--ads", "-a", type=int, nargs="+", default=None,
        help="Explicit AD slot numbers to process exclusively (e.g. --ads 3 for only AD3), overriding --max-ads"
    )
    parser.add_argument(
        "--output-dir", "-o", default="/home/ubuntu/AdCreatives",
        help="Target base directory for AdCreatives (default: /home/ubuntu/AdCreatives)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Evaluate decision tree and language detection without launching paid translations"
    )
    parser.add_argument(
        "--image-model", default=DEFAULT_IMAGE_MODEL,
        help=f"OpenRouter image generation model for ad localization (default: {DEFAULT_IMAGE_MODEL})"
    )
    parser.add_argument(
        "--skip-sync", action="store_true",
        help="Skip triggering rclone cloud synchronization"
    )
    parser.add_argument(
        "--video-color-temp", type=int, default=DEFAULT_COLOR_TEMP,
        help=f"Color temperature in Kelvin for FFmpeg night filter on video ads (default: {DEFAULT_COLOR_TEMP})"
    )

    args = parser.parse_args()

    try:
        manifest = process_product_ads(
            row_number=args.row,
            max_ads=args.max_ads,
            base_output_dir=Path(args.output_dir),
            dry_run=args.dry_run,
            skip_sync=args.skip_sync,
            ad_slots=args.ads,
            image_model=args.image_model,
            video_color_temp=args.video_color_temp,
        )
        print(json.dumps(manifest, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"[Pipeline Error] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
