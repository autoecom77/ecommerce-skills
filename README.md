# Ecommerce Skills for Hermes

A collection of autonomous agent skills and automated workflows for ecommerce and dropshipping operations.

## Included Skills

### 1. `recherche-produit-trendtrack`
Automated dropshipping product research workflow in the European market:
- Scans TrendTrack ads (Shopify reach growth, native ads, live ad volume).
- De-duplicates domains against existing Google Sheets.
- Computes COGS via reverse image search (1688 and AliExpress fallback).
- Updates Google Sheet with scoring and competitor data.

### 2. `aliexpress-image-search`
Visual search engine on AliExpress:
- Reverse image lookups using Scrapling and AliExpress search API.
- Extracts product pricing, seller ratings, shipping terms, and SKU variants.

### 3. `ad-creative-repurposing`
Competitor advertising creative ingestion and transformation:
- Downloads video creatives from TrendTrack / Facebook ad library.
- Automates single-pass FFmpeg alterations (crop, aspect ratio, audio/video filters).

### 4. `product-media-extraction`
High-resolution media harvesting from ecommerce product pages:
- Deterministic extraction of product hero images, gallery assets, and variant photos.
- Produces clean structured JSON manifests for Shopify import and creative pipelines.

### 5. `vozo-translate-dub`
Automated video localization, multilingual voice dubbing, and subtitle replacement:
- Uses the official `vozo-cli` tool (v1.0.3) and Vozo REST API with authenticated account `autoecom77@gmail.com`.
- **Enforced defaults for French dropshipping market**:
  - Source language: `auto`
  - Target language: `fr`
  - Voice cloning: `auto`
  - Diarization: `Auto` (speakers detected automatically)
  - Subtitles: `--enable-subtitles --remove-original-subtitle` (erases burnt-in original subtitles, adds French subtitles)
  - Timing: `--auto-align-video` (synchronizes video speed segment-by-segment with audio dubbing)
- **Autonomous agent command**:
  ```bash
  vozo-cli project translate_dub create \
    --from-file "/path/to/video.mp4" \
    --target-language fr \
    --original-language auto \
    --dub-preference auto \
    --speaker-number Auto \
    --enable-subtitles \
    --remove-original-subtitle \
    --auto-align-video
  ```
- Includes standalone Python helper `vozo_translate.py` for direct REST API scripting and automated download pipelines.
