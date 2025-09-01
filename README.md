# geopng

**geopng** turns your GPS traces and waypoints into clean, shareable map images—fast.  
Point it at your GPX/KML/KMZ/TRC/POS files and get a PNG with your path, markers, and a neat date stamp in the corner. It’s a friendly wrapper around Geoapify Static Maps plus a smart converter that speaks lots of quirky GPS formats.

This is a **vibe coding** project: built iteratively with real-world files, pragmatic choices, and tiny quality-of-life tweaks—so you can spend less time fiddling and more time sharing where you’ve been.

## Why you might like it
- **One command to map**: `./geoapify_render.sh file.gpx` ⇒ `file.png`
- **Understands many formats**: GPX, KML, KMZ, TRC/NMEA logs, POS waypoints
- **Looks good by default**: sane styles, line colors, and a date overlay
- **Handles messy data**: skips broken files, caps huge tracks, and avoids API limits
- **Smart framing**: automatic bounding box with padding (no cut-off corners)

## Requirements

**Ubuntu/Debian packages** (no Python pip deps needed):
```bash
sudo apt update
sudo apt install -y python3 curl jq imagemagick
# (Optional) If you prefer GraphicsMagick for the date overlay:
# sudo apt install -y graphicsmagick
```

**Why these?**
- `python3` runs the converter (`geoapify_from_any.py`)
- `curl` sends the request to Geoapify
- `jq` tidies the JSON and helps with safe fallbacks
- `imagemagick` (or `graphicsmagick`) adds the bottom-right date overlay

## Quick start

1) **Get geoapify API key**:
https://myprojects.geoapify.com/projects

2) **Set your Geoapify API key (required)**:
```bash
export GEOAPIFY_KEY="YOUR_GEOAPIFY_KEY"
```

3) **Render a single track**:
```bash
./geoapify_render.sh MyRun.gpx
```

4) **Combine formats and keep the request JSON for debugging**:
```bash
./geoapify_render.sh -K trip.kmz waypoints.pos track.trc
```

5) **Set a custom output name**:
```bash
./geoapify_render.sh -o holiday.png Spanien09.kmz
```

> Tip: you can also inline the key per call:
```bash
GEOAPIFY_KEY="YOUR_GEOAPIFY_KEY" ./geoapify_render.sh MyRun.gpx
```

## How it works (high level)
- A Python converter (`geoapify_from_any.py`) unifies all inputs into one Geoapify Static Maps request.
- A small Bash wrapper (`geoapify_render.sh`) sends it, retries sensibly, and adds the date overlay.
- It quietly thins overly dense tracks and trims marker labels to keep things smooth.

## Notes
- You’ll need a **Geoapify API key** (set `GEOAPIFY_KEY` as shown above).
- This is open, hackable, and welcoming to tweaks—bring your vibe.
