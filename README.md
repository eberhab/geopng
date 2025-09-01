# geopng

**geopng** turns your GPS traces and waypoints into clean, shareable map images—fast.  
Point it at your GPX/KML/KMZ/TRC/POS files and get a PNG with your path, markers, and a neat date stamp in the corner. It’s a friendly wrapper around Geoapify Static Maps plus a smart converter that speaks lots of quirky GPS formats.

This is a **vibe coding** project: built iteratively with real-world files, pragmatic choices, and tiny quality-of-life tweaks—so you can spend less time fiddling and more time sharing where you’ve been.

## Why you might like it
- **One command to map**: `geopng file.gpx` ⇒ `file.png`
- **Understands many formats**: GPX, KML, KMZ, TRC/NMEA logs, POS waypoints
- **Looks good by default**: sane styles, line colors, and a date overlay
- **Handles messy data**: skips broken files, caps huge tracks, and avoids API limits
- **Smart framing**: automatic bounding box with padding (no cut-off corners)

## Quick start
```bash
# 1) Set your Geoapify API key (required)
export GEOAPIFY_KEY="YOUR_GEOAPIFY_KEY"

# 2) Render a single track
geopng MyRun.gpx

# 3) Combine formats and keep the request JSON for debugging
geopng -K trip.kmz waypoints.pos track.trc

# 4) Set a custom output name
geopng -o holiday.png Spanien09.kmz

```

You’ll find the output PNG next to your input file. With `-K`, the exact POST body is kept for troubleshooting.

## How it works (high level)
- A Python converter unifies all inputs into one Geoapify Static Maps request.
- A tiny bash script sends it off, retries sensibly, and adds the date overlay.
- It quietly thins overly dense tracks and trims marker labels to keep things smooth.

## Notes
- You’ll need a **Geoapify API key** (set `GEOAPIFY_KEY` as shown above).
- This is open, hackable, and welcoming to tweaks—bring your vibe.
