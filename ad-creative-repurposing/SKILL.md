---
name: ad-creative-repurposing
description: "Use when fetching and transforming a competitor's ad video."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [Ads, Creative, Video, FFmpeg, TrendTrack, Facebook]
    related_skills: [recherche-produit-trendtrack]
---

# Ad creative: retrieve and transform

Get a competitor's ad video as a file, then produce a modified copy in one
ffmpeg pass. Used when a creative found during product research needs to be
saved or reworked.

## Procedure

### 1. Locate the ad where the video is actually downloadable

A TrendTrack share link (`app.trendtrack.io/share/ads/...`) is a viewer, not
a file endpoint — fetching it directly returns an API error, so don't burn
time trying to turn it into a download URL. Find the same ad in the
**Facebook Ad Library** instead: its page source exposes the creative as a
direct `.mp4` on the Facebook CDN (`video-yyz1-1.xx.fbcdn.net` or a sibling
`*.fbcdn.net` host).

### 2. Pull the CDN URL out of the DOM, then download

```python
# inside browser_exec, after the player has loaded
code = """(() => {
  const v = document.querySelector('video');
  return v ? (v.currentSrc || v.src) : null;
})()"""
```

Then fetch that URL with `curl -L -o out.mp4 '<url>'`. Query all `video`
elements, not just the first — Ad Library pages often hold a thumbnail
preview video ahead of the real creative. Wait for load before reading
`currentSrc`; an early query returns an empty string while the source is
still resolving.

### 3. Transform in a single pass

Order is load-bearing: upscale first (later filters resample the upscaled
frame rather than interpolating a small one), mirror second, colour grade
last.

```bash
ffmpeg -i in.mp4 \
  -vf "scale=iw*2:ih*2,hflip,eq=brightness=-0.15:contrast=0.9:saturation=0.85" \
  -c:v libx264 -crf 18 -preset slow -c:a copy out.mp4
```

`-c:a copy` leaves audio untouched, which also keeps the pass fast.

### 4. Verify the output, don't assume it

```bash
ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=p=0 out.mp4
```

Expected: exactly 2× the input's width and height.

## Pitfalls

- **`scale=iw*2:iw*2` squares the frame.** Both dimensions read `iw`, so a
  640×360 source comes out 1280×1280 — visibly stretched, and easy to miss
  because ffmpeg exits 0. Height must read `ih` (`scale=iw*2:ih*2`).
- **To hit a target width instead of a fixed 2×**, use
  `scale=1920:-2` — the `-2` keeps the aspect ratio and forces an even
  height, which the H.264 encoder requires (a bare `-1` can emit an odd
  height and fail).
- **Chaining filters in one `-vf` avoids generation loss** — separate
  ffmpeg passes re-encode at every step. Mirror and grade belong in the
  same command as the scale.
- **Confirm which file you transformed.** Ad Library pages serve several
  videos per ad (static preview, thumbnail loop, the creative); check the
  duration of the URL you grabbed before downloading a 3-second bumper by
  mistake.
