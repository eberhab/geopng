#!/usr/bin/env bash
# Version: v2025.08.27.6
# ============================================================================
# geoapify_render.sh — robust, documented renderer for Geoapify Static Maps
# ============================================================================
#
# What this does
#  1) Calls the unified converter (geoapify_from_any.py) on any number of inputs
#     (.gpx .kml .kmz .trc .nma .nmea .pos), producing a POST body (JSON).
#  2) Normalizes the payload for reliability:
#       - marker size: coerced to "small|medium|large" if numeric
#       - area bbox: auto-added if missing so the view is explicit
#       - scaleFactor: injected into body (and URL) when provided
#  3) POSTs the body to the Static Maps API and writes the returned image.
#  4) Adds a date overlay (bottom-right) using the earliest date in the body:
#       geojson.features[].properties.date, geojson.properties.date, meta.date
#
# Why these safeguards?
#  - We explicitly provide an "area" bbox: some payloads render more reliably
#    when the map view is specified (esp. markers-only requests).
#  - We avoid pixel marker sizes by default and normalize to keywords
#    ("small|medium|large") which are what the examples use.
#  - If the server still returns 400/500, we retry without marker labels
#    (strip text/textsize) and, if it's a markers-only map, fall back to GET.
#
# Flags
#  -o OUTPUT.png  : output image filename (default: next to first input)
#  -K             : keep the final JSON (prints its path) for debugging
#
# Environment variables (optional)
#  GEOAPIFY_KEY     : API key (required)
#  GEOAPIFY_STYLE   : map style (default osm-carto)
#  GEOAPIFY_LANG    : language (default de)
#  GEOAPIFY_SCALE   : scale factor (default 2)
#  GEOAPIFY_WIDTH   : width in px (default 1280)
#  GEOAPIFY_HEIGHT  : height in px (default 800)
#  GEOAPIFY_FORMAT  : png|jpeg (default png)
#  GEOAPIFY_OUTTYPE : geojson|polyline|polyline6 (default geojson)
#  GEOAPIFY_GPX_MERGE_SINGLETONS : 1/0 merge single-point GPX segments (default 1)
#  GEOAPIFY_LINECOLOR : track line color (default #0066ff)
#  GEOAPIFY_LINEWIDTH : track line width px (default 5)
#  GEOAPIFY_THIN      : keep every Nth point (default 1)
#  POS_MARKER_COLOR   : marker color (default #D32F2F)
#  POS_MARKER_SIZE    : small|medium|large (default medium)
#  POS_MARKER_SIZE_PX : explicit px size (overrides POS_MARKER_SIZE)
#  POS_NO_TEXT        : 1 to remove in-pin labels
#  POS_TEXTSIZE       : in-pin label size (px, default 18)
#  POS_MAX_NAME_LEN   : truncate long labels (default 40)
#  DATE_FMT           : overlay date format (default %Y-%m-%d)
#  LABEL_PAD          : overlay padding (default 18)
#
# Usage
#   GEOAPIFY_KEY=... ./geoapify_render.sh [-o out.png] [-K] input1 [input2 ...]
#   # Many users create an alias: alias geopng='~/.../geoapify_render.sh'
#
# Notes
#  - The converter (geoapify_from_any.py) already avoids "type: plain" labels.
#  - If your payload remains too large/complex, try OUTTYPE=polyline6:
#       GEOAPIFY_OUTTYPE=polyline6 ./geoapify_render.sh input.gpx
# ============================================================================

set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage: geoapify_render.sh [-o output.png] [-K] <input1> [input2 ...]
USAGE
  exit 2
}

# ---- CLI options ----
OUTFILE=""
KEEP_JSON=0
while getopts ":o:Kh" opt; do
  case "$opt" in
    o) OUTFILE="$OPTARG" ;;
    K) KEEP_JSON=1 ;;
    h) usage ;;
    \?) echo "Invalid option: -$OPTARG" >&2; usage ;;
    :)  echo "Option -$OPTARG requires an argument." >&2; usage ;;
  esac
done
shift $((OPTIND - 1))
[[ $# -ge 1 ]] || usage

# ---- Environment / defaults ----
# Embedded default API key per user request (override with GEOAPIFY_KEY env):
KEY="${GEOAPIFY_KEY:-4f57159fee49457e96715cea917cc6d4}"
STYLE="${GEOAPIFY_STYLE:-osm-carto}"
LANG="${GEOAPIFY_LANG:-de}"
SCALE="${GEOAPIFY_SCALE:-2}"
WIDTH="${GEOAPIFY_WIDTH:-1280}"
HEIGHT="${GEOAPIFY_HEIGHT:-800}"
FORMAT="${GEOAPIFY_FORMAT:-png}"
OUTTYPE="${GEOAPIFY_OUTTYPE:-geojson}"           # geojson | polyline6 | polyline
MERGE_SINGLETONS="${GEOAPIFY_GPX_MERGE_SINGLETONS:-1}" # 1=merge singletons
LINECOLOR="${GEOAPIFY_LINECOLOR:-#0066ff}"
LINEWIDTH="${GEOAPIFY_LINEWIDTH:-5}"
THIN="${GEOAPIFY_THIN:-1}"

# Marker/label passthrough (converter uses these)
POS_MARKER_COLOR="${POS_MARKER_COLOR:-#D32F2F}"
POS_MARKER_SIZE="${POS_MARKER_SIZE:-medium}"
POS_MARKER_SIZE_PX="${POS_MARKER_SIZE_PX:-}"
POS_NO_TEXT="${POS_NO_TEXT:-0}"
POS_TEXTSIZE="${POS_TEXTSIZE:-18}"
POS_MAX_NAME_LEN="${POS_MAX_NAME_LEN:-40}"

# Overlay
DATE_FMT="${DATE_FMT:-%Y-%m-%d}"
LABEL_PAD="${LABEL_PAD:-18}"
PAD_FRAC="${GEOAPIFY_AREA_PAD_FRAC:-0.12}"
MIN_PAD_DEG="${GEOAPIFY_AREA_PAD_MIN_DEG:-0.03}"

# ---- Paths and output naming ----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONVERTER="$SCRIPT_DIR/geoapify_from_any.py"
[[ -f "$CONVERTER" ]] || { echo "Missing converter: $CONVERTER" >&2; exit 3; }

first="$1"; first_dir="$(cd "$(dirname "$first")" && pwd)"
first_base="$(basename "$first")"; first_stem="${first_base%.*}"
PNG_OUT="${OUTFILE:-$first_dir/$first_stem.png}"

# ---- Temps and cleanup ----
RESP="$(mktemp -t geoapify_resp.XXXXXX)"
# If -K is set, keep JSON in the target dir with a stable name
if [[ $KEEP_JSON -eq 1 ]]; then
  if [[ -n "$OUTFILE" ]]; then
    target_dir="$(cd "$(dirname "$OUTFILE")" && pwd)"; target_stem="$(basename "$OUTFILE")"; target_stem="${target_stem%.*}"
  else
    target_dir="$first_dir"; target_stem="$first_stem"
  fi
  RUN_ID="$(date +%Y%m%d-%H%M%S).$$"
  FINAL="$target_dir/${target_stem}.geoapify_body.${RUN_ID}.json"
else
  FINAL="$(mktemp -t geoapify_body.final.XXXXXX.json)"
fi
cleanup() { [[ $KEEP_JSON -eq 0 ]] && rm -f "$FINAL" "$RESP" || rm -f "$RESP"; }
trap cleanup EXIT

# ---- Build converter args ----
ARGS=( --style "$STYLE" --width "$WIDTH" --height "$HEIGHT" --format "$FORMAT"
       --out-type "$OUTTYPE" --linecolor "$LINECOLOR" --linewidth "$LINEWIDTH" --thin "$THIN"
       --marker-color "$POS_MARKER_COLOR" --marker-size "$POS_MARKER_SIZE" --contentsize "$POS_TEXTSIZE"
       --max-name-len "$POS_MAX_NAME_LEN" -o "$FINAL" )
[[ -n "$POS_MARKER_SIZE_PX" ]] && ARGS+=( --marker-size-px "$POS_MARKER_SIZE_PX" )
if [[ "$POS_NO_TEXT" == "1" ]]; then ARGS+=(--no-text); fi
case "$MERGE_SINGLETONS" in 1|true|TRUE|yes|YES) ARGS+=(--gpx-merge-singletons);; esac

# ---- Run converter on all inputs ----
python3 "$CONVERTER" "${ARGS[@]}" "$@" >/dev/null

# ---- Normalize payload for reliability ----
#  - ensure area (bbox) if missing
#  - coerce marker.size to keyword when numeric
#  - inject scaleFactor into body (also passed in URL)

tmp_norm="$(mktemp -t geoapify_body.norm.XXXXXX.json)"
jq --argjson sc "$SCALE" --argjson pf "$PAD_FRAC" --argjson pm "$MIN_PAD_DEG" '
  def size_to_keyword(s): if (s|type)=="string" then s else (if s>=64 then "large" elif s>=48 then "medium" else "small" end) end;
  def textsize_to_keyword(s): if (s|type)=="string" then s else (if s>=24 then "large" elif s>=16 then "medium" else "small" end) end;
  def bbox_from_points:
    reduce .[] as $p ({minlat:  1e9, minlon:  1e9, maxlat: -1e9, maxlon: -1e9};
      {minlat: (if $p.lat < .minlat then $p.lat else .minlat end),
       minlon: (if $p.lon < .minlon then $p.lon else .minlon end),
       maxlat: (if $p.lat > .maxlat then $p.lat else .maxlat end),
       maxlon: (if $p.lon > .maxlon then $p.lon else .maxlon end)});
  def expand_bbox(b; pf; pm):
    (b.maxlat - b.minlat) as $dlat |
    (b.maxlon - b.minlon) as $dlon |
    ( ( ($dlat*pf) | tonumber ) as $plat0 | (if $plat0 < pm then pm else $plat0 end) ) as $plat |
    ( ( ($dlon*pf) | tonumber ) as $plon0 | (if $plon0 < pm then pm else $plon0 end) ) as $plon |
    {minlat: (b.minlat - $plat), minlon: (b.minlon - $plon), maxlat: (b.maxlat + $plat), maxlon: (b.maxlon + $plon)};
  def ensure_area:
    if has("area") then . else (
      (
        (if has("markers") then (.markers | map({lat,lon})) else [] end)
      ) as $pts
      | if ($pts|length)>0 then
          ($pts | bbox_from_points) as $b
          | .area = {type:"rect", value:{lon1:$b.minlon,lat1:$b.minlat,lon2:$b.maxlon,lat2:$b.maxlat}}
        else . end
    ) end;
  def pad_area(pf; pm):
    if has("area") and (.area|type=="object") and (.area.type=="rect") and (.area.value|type=="object") then
      (.area.value) as $v |
      {minlat:$v.lat1, minlon:$v.lon1, maxlat:$v.lat2, maxlon:$v.lon2} as $b |
      (expand_bbox($b; pf; pm)) as $e |
      .area.value.lon1 = $e.minlon |
      .area.value.lat1 = $e.minlat |
      .area.value.lon2 = $e.maxlon |
      .area.value.lat2 = $e.maxlat
    else . end;
  .scaleFactor = $sc
  | (if has("markers") then .markers |= map(
        .size = size_to_keyword(.size // "medium")
      | ( if has("textsize") then (.textsize = textsize_to_keyword(.textsize)) else . end )
    ) else . end)
  | ensure_area
  | pad_area($pf; $pm)
' "$FINAL" > "$tmp_norm" && mv "$tmp_norm" "$FINAL"

# ---- POST to Geoapify ----
[[ -n "$KEY" ]] || { echo "GEOAPIFY_KEY is required" >&2; exit 2; }
url="https://maps.geoapify.com/v1/staticmap?apiKey=${KEY}&lang=${LANG}&scaleFactor=${SCALE}"

HTTP_CODE="$(curl -sS -o "$RESP" -w "%{http_code}" -X POST "$url" -H 'Content-Type: application/json' --data-binary @"$FINAL")"
if [[ "$HTTP_CODE" != "200" ]]; then
  echo "POST failed with HTTP $HTTP_CODE" >&2
  echo "--- request body (first 200 lines) ---" >&2; jq . "$FINAL" 2>/dev/null | head -200 >&2 || head -200 "$FINAL" >&2
  echo "--- server response (first 200 lines) ---" >&2; head -200 "$RESP" >&2

  # Retry without marker labels (strip text/textsize)
  STRIPPED="$(mktemp -t geoapify_body.stripped.XXXXXX.json)"
  jq 'if has("markers") then .markers |= map( del(.text) | del(.textsize) ) else . end' "$FINAL" > "$STRIPPED" || STRIPPED="$FINAL"

  HTTP_CODE2="$(curl -sS -o "$RESP" -w "%{http_code}" -X POST "$url" -H 'Content-Type: application/json' --data-binary @"$STRIPPED")"
  if [[ "$HTTP_CODE2" != "200" ]]; then
    echo "POST (retry, no labels) failed with HTTP $HTTP_CODE2" >&2
    head -200 "$RESP" >&2

    # If markers-only, try GET fallback building marker= strings
    ONLY_MARKERS="$(jq -r '(has("markers") and ((has("geometries")|not) and (has("geojson")|not)))|tostring' "$STRIPPED" 2>/dev/null || echo false)"
    if [[ "$ONLY_MARKERS" == "true" ]]; then
      echo "Trying GET fallback for markers-only ..." >&2
      MSTR="$(jq -r '
        .markers
        | map(
            "lonlat:" + (.lon|tostring) + "," + (.lat|tostring)
            + ";color:" + ((.color // "#ff0000")|gsub("#";"%23"))
            + (if .type then ";type:" + .type else "" end)
            + ";size:" + (.size // "medium")
          )
        | join("|")
      ' "$STRIPPED")"
      get_url="https://maps.geoapify.com/v1/staticmap?style=$STYLE&width=$WIDTH&height=$HEIGHT&marker=$MSTR&scaleFactor=$SCALE&lang=$LANG&apiKey=$KEY"
      HTTP_CODE3="$(curl -sS -o "$PNG_OUT" -w "%{http_code}" "$get_url")"
      if [[ "$HTTP_CODE3" != "200" ]]; then
        echo "GET fallback failed with HTTP $HTTP_CODE3" >&2
        exit 4
      fi
    else
      exit 4
    fi
  else
    mv "$RESP" "$PNG_OUT"
  fi
else
  mv "$RESP" "$PNG_OUT"
fi

# ---- Date overlay (best-effort) ----
DATE_RAW=""
if command -v jq >/dev/null 2>&1; then
  DATE_RAW="$(jq -r '
    (
      ((.geojson.features // []) | map(.properties.date // empty)) +
      [(.geojson.properties.date // empty)] +
      [(.meta.date // empty)]
    ) | map(select(. != null and . != "")) | (min // empty)
  ' "$FINAL" 2>/dev/null || true)"
fi

if [[ -n "$DATE_RAW" ]]; then
  if date -d "$DATE_RAW" +"$DATE_FMT" >/dev/null 2>&1; then DATE_PRINT="$(date -d "$DATE_RAW" +"$DATE_FMT")"; else DATE_PRINT="$DATE_RAW"; fi
  if command -v magick >/dev/null 2>&1; then
    magick "$PNG_OUT" -gravity southeast -fill white -undercolor '#00000080' -pointsize 28 -annotate +${LABEL_PAD}+${LABEL_PAD} "$DATE_PRINT" "$PNG_OUT"
  elif convert -version 2>/dev/null | grep -qi "ImageMagick"; then
    convert "$PNG_OUT" -gravity southeast -fill white -undercolor '#00000080' -pointsize 28 -annotate +${LABEL_PAD}+${LABEL_PAD} "$DATE_PRINT" "$PNG_OUT"
  elif command -v gm >/dev/null 2>&1; then
    gm convert "$PNG_OUT" -font DejaVu-Sans -pointsize 28 -fill white -stroke black -strokewidth 1 -draw "gravity southeast text ${LABEL_PAD},${LABEL_PAD} \"$DATE_PRINT\"" "$PNG_OUT"
  else
    echo "Note: ImageMagick/GraphicsMagick not found; overlay skipped." >&2
  fi
fi

# ---- Debug output ----
if [[ $KEEP_JSON -eq 1 ]]; then
  echo "Kept body: $FINAL"
fi
echo "Wrote: $PNG_OUT"
