#!/usr/bin/env python3
# Version: v2025.08.27.2
"""
===============================================================================
geoapify_from_any.py — unified, robust converter (with extensive commentary)
===============================================================================

What this does
--------------
Turn a mix of GPS-ish files into a single **Geoapify Static Maps** POST body
(JSON). Supported inputs: **.gpx .kml .kmz .trc .nma .nmea .pos** (multiple
files; any mix).

Outputs
-------
- **Lines/tracks** (default): GeoJSON FeatureCollection with styled LineStrings.
  * Switch with `--out-type polyline|polyline6`.
  * For polyline/polyline6, the earliest date across files is stored in `meta.date`.
- **Markers/positions**:
  * *.POS files → material markers with **in-pin labels**.
  * **Auto-positions from all inputs** (default ON):
    - GPX `<wpt>` (and named `<rtept>` as fallback)
    - KML/KMZ `Placemark > Point > coordinates` with `<name>`
    - TRC/NMEA `$..WPL` / `$..HOM` sentences

Key design decisions (lessons learned)
--------------------------------------
- ❌ Avoid `type:"plain"` label markers.  
  The Geoapify Static Maps API can return **HTTP 400** when posting bodies
  containing `{"type":"plain","text":"..."}` markers in certain mixes. To
  keep requests reliable, we **always** embed the label **inside** the icon
  marker: `{ "type":"material", "text":"...", "textsize": N }`.
- 🧨 One bad file should not break the batch.  
  Every file is processed inside `try/except`. We log and **skip** failures by
  default: `[SKIP] path/file.gpx: ParseError: ...`. Add `--strict` to abort on
  first error.
- 🧵 1‑point GPX segments are common.  
  By default, segments with `< 2` points are ignored. If a GPX has only
  singletons, `--gpx-merge-singletons` merges all points into one LineString.
  The renderer supports `GEOAPIFY_GPX_MERGE_SINGLETONS=1` to pass this flag.
- 🧭 Auto-positions are globally de-duplicated.  
  We dedupe on `(round(lat,7), round(lon,7), name)` across all files.
- 🗓️ Dates
  - GPX: `<time>`; KML/KMZ: `<when>`/`<TimeStamp>`; NMEA: `$..ZDA`/`$..RMC`;
    otherwise fallback to file mtime.
  - GeoJSON features get `properties.date` (per segment). For polyline/polyline6
    we set `meta.date = min(all dates)`.

CLI quick reference
-------------------
- Geometry: `--out-type geojson|polyline|polyline6`, `--thin N`
- Robustness: `--strict`, `--gpx-merge-singletons`
- Positions: `--auto-positions` (default), `--no-auto-positions`
- Markers: `--marker-color`, `--marker-size` or `--marker-size-px`, `--no-text`,
  `--contentsize`, `--max-name-len`
- Output: `-o geoapify_body.json`; style/size knobs: `--style --width --height`

Examples
--------
- Mixed inputs, defaults (GeoJSON + auto markers):
    python3 geoapify_from_any.py run.gpx tour.kmz notes.trc -o body.json
- Merge GPX singletons:
    python3 geoapify_from_any.py --gpx-merge-singletons weird.gpx -o body.json
- Lines only (disable auto marker extraction):
    python3 geoapify_from_any.py --no-auto-positions *.kml -o body.json

===============================================================================
"""
import argparse, os, io, json, re, zipfile, sys
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import List, Tuple, Optional

ISO = "%Y-%m-%d"

# ---------------- General helpers ----------------
def _parse_iso_date(s: str):
    try:
        s = s.strip()
        if "T" in s:
            s2 = s.replace("Z","").split("+")[0]
            return datetime.fromisoformat(s2).date()
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None

def _fallback_mtime(path: str) -> Optional[str]:
    try:
        return datetime.fromtimestamp(os.path.getmtime(path)).strftime(ISO)
    except Exception:
        return None

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else tag

# ---------------- Date extraction ----------------
def _date_from_gpx(path: str) -> Optional[str]:
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return None
    times = []
    for el in root.iter():
        if _local(el.tag) == "time" and el.text:
            dt = _parse_iso_date(el.text)
            if dt: times.append(dt)
    return min(times).strftime(ISO) if times else None

def _date_from_kml_root(root: ET.Element) -> Optional[str]:
    times = []
    for el in root.iter():
        if _local(el.tag) == "when" and el.text:
            dt = _parse_iso_date(el.text)
            if dt: times.append(dt)
    if not times:
        for ts in root.iter():
            if _local(ts.tag) == "TimeStamp":
                for w in ts:
                    if _local(w.tag) == "when" and w.text:
                        dt = _parse_iso_date(w.text)
                        if dt: times.append(dt)
    return min(times).strftime(ISO) if times else None

def _date_from_kml(path: str) -> Optional[str]:
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return None
    return _date_from_kml_root(root)

def _date_from_kmz(path: str) -> Optional[str]:
    try:
        with zipfile.ZipFile(path, "r") as zf:
            name = "doc.kml" if "doc.kml" in zf.namelist() else None
            if not name:
                for n in zf.namelist():
                    if n.lower().endswith(".kml"):
                        name = n; break
            if not name: return None
            data = zf.read(name)
            root = ET.parse(io.BytesIO(data)).getroot()
            return _date_from_kml_root(root)
    except Exception:
        return None

_ZDA_RE = re.compile(r'^\$(?:GP|GN|GL)ZDA,(?:[^,]*),([0-3]\d),([01]\d),(\d{4})')
_RMC_RE = re.compile(r'^\$(?:GP|GN|GL)RMC,[^,]*,[AV],[^,]*,[NS],[^,]*,[EW],[^,]*,[^,]*,([0-3]\d)([01]\d)(\d{2})')

def _date_from_nmea_like(path: str) -> Optional[str]:
    best = None
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                s = line.strip()
                m = _ZDA_RE.match(s)
                if m:
                    d = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1))).date()
                    best = d if best is None or d < best else best
                    continue
                m = _RMC_RE.match(s)
                if m:
                    d = datetime(2000+int(m.group(3)), int(m.group(2)), int(m.group(1))).date()
                    best = d if best is None or d < best else best
    except Exception:
        return None
    return best.strftime(ISO) if best else None

# ---------------- Polyline6 encoder ----------------
def encode_polyline(points: List[Tuple[float,float]], precision=6) -> str:
    factor = 10 ** precision
    prev_lat = 0; prev_lon = 0; out = []
    for lat, lon in points:
        ilat = int(round(lat * factor)); ilon = int(round(lon * factor))
        dlat = ilat - prev_lat; dlon = ilon - prev_lon
        prev_lat, prev_lon = ilat, ilon
        for d in (dlat, dlon):
            s = (~(d << 1)) if d < 0 else (d << 1)
            while s >= 0x20:
                out.append(chr((0x20 | (s & 0x1f)) + 63)); s >>= 5
            out.append(chr(s + 63))
    return "".join(out)

# ---------------- Parsers ----------------
def parse_gpx_segments(gpx_path: str) -> List[List[Tuple[float,float]]]:
    """Return a list of LineString-like segments from GPX trk/trkseg and rte."""
    try:
        root = ET.parse(gpx_path).getroot()
    except Exception:
        return []
    segs = []
    # trk/trkseg/trkpt
    for trk in root.iter():
        if _local(trk.tag) != "trk": continue
        for seg in trk:
            if _local(seg.tag) != "trkseg": continue
            pts = []
            for pt in seg:
                if _local(pt.tag) != "trkpt": continue
                try:
                    lat = float(pt.get("lat")); lon = float(pt.get("lon"))
                    pts.append((lat, lon))
                except Exception:
                    pass
            if len(pts) >= 2: segs.append(pts)
    # rte/rtept
    for rte in root.iter():
        if _local(rte.tag) != "rte": continue
        pts = []
        for pt in rte:
            if _local(pt.tag) != "rtept": continue
            try:
                lat = float(pt.get("lat")); lon = float(pt.get("lon"))
                pts.append((lat, lon))
            except Exception:
                pass
        if len(pts) >= 2: segs.append(pts)
    return segs

def _parse_root_from_kml_or_kmz(path: str) -> ET.Element:
    low = path.lower()
    if low.endswith(".kml"):
        return ET.parse(path).getroot()
    if low.endswith(".kmz"):
        with zipfile.ZipFile(path, "r") as zf:
            name = "doc.kml" if "doc.kml" in zf.namelist() else None
            if not name:
                for n in zf.namelist():
                    if n.lower().endswith(".kml"):
                        name = n; break
            if not name: raise RuntimeError("KMZ does not contain any .kml file")
            data = zf.read(name)
            return ET.parse(io.BytesIO(data)).getroot()
    raise RuntimeError("Input must be .kml or .kmz")

def parse_kmx_segments(path: str) -> List[List[Tuple[float,float]]]:
    """LineString-like segments from KML/KMZ gx:Track or <coordinates> blocks."""
    try:
        root = _parse_root_from_kml_or_kmz(path)
    except Exception:
        return []
    segments = []
    # gx:Track → gx:coord
    for tr in root.iter():
        if _local(tr.tag) != "Track": continue
        seg = []
        for el in tr.iter():
            if _local(el.tag) == "coord" and el.text:
                parts = el.text.strip().split()
                if len(parts) >= 2:
                    try:
                        lon = float(parts[0]); lat = float(parts[1])
                        seg.append((lat, lon))
                    except Exception:
                        pass
        if len(seg) >= 2: segments.append(seg)
    # Any coordinates text blocks (lon,lat[,alt] ...)
    for coords in root.iter():
        if _local(coords.tag) != "coordinates": continue
        txt = (coords.text or "").strip()
        if not txt: continue
        pts = []
        for tok in txt.replace("\n"," ").replace("\t"," ").split():
            parts = tok.split(",")
            if len(parts) >= 2:
                try:
                    lon = float(parts[0]); lat = float(parts[1])
                    pts.append((lat, lon))
                except Exception:
                    pass
        if len(pts) >= 2: segments.append(pts)
    return segments

# TRC / NMEA
FLOAT_RE = re.compile(r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?')
def plausible_lon(x): return -180.0 <= x <= 180.0
def plausible_lat(y): return -90.0 <= y <= 90.0
def dm_to_dec(dm, hemi=None):
    try: f = float(dm)
    except Exception: return None
    deg = int(f // 100.0); minutes = f - deg*100.0
    dec = deg + minutes/60.0
    if hemi in ("S","W"): dec = -dec
    return dec

def parse_nmea_line(line):
    if not (line.startswith("$GP") or line.startswith("$GN") or line.startswith("$GL")):
        return None
    p = line.strip().split(",")
    talker = p[0][3:]
    try:
        if talker == "RMC":
            if len(p) >= 7 and p[2] in ("A","V"):
                lat = dm_to_dec(p[3], p[4]) if len(p)>4 else None
                lon = dm_to_dec(p[5], p[6]) if len(p)>6 else None
                if lat is not None and lon is not None: return (lat,lon)
        elif talker == "GGA":
            if len(p) >= 6:
                lat = dm_to_dec(p[2], p[3]) if len(p)>3 else None
                lon = dm_to_dec(p[4], p[5]) if len(p)>5 else None
                if lat is not None and lon is not None: return (lat,lon)
    except Exception:
        return None
    return None

def find_pair(nums, force=None):
    n = len(nums)
    if force == "lonlat":
        for i in range(n-1):
            a,b = nums[i], nums[i+1]
            if plausible_lon(a) and plausible_lat(b): return (b,a)
        return None
    if force == "latlon":
        for i in range(n-1):
            a,b = nums[i], nums[i+1]
            if plausible_lat(a) and plausible_lon(b): return (a,b)
        return None
    for i in range(n-1):
        a,b = nums[i], nums[i+1]
        if plausible_lon(a) and plausible_lat(b): return (b,a)
        if plausible_lat(a) and plausible_lon(b): return (a,b)
    return None

def parse_trc_segments(path: str, order=None, split_on_empty=False) -> List[List[Tuple[float,float]]]:
    """Parse generic TRC/NMEA-ish logs into segments by heuristic coordinate pairs and NMEA sentences."""
    segs = [[]]
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                s = line.strip()
                if not s:
                    if split_on_empty and segs[-1]: segs.append([])
                    continue
                nmea = parse_nmea_line(s)
                if nmea is not None:
                    segs[-1].append(nmea); continue
                if s.startswith("$GP") or s.startswith("$GN") or s.startswith("$GL"):
                    continue
                nums = [float(m.group(0)) for m in FLOAT_RE.finditer(s)]
                pair = find_pair(nums, force=order)
                if pair: segs[-1].append(pair)
    except Exception:
        return []
    segs = [seg for seg in segs if len(seg) >= 2]
    return segs

# POS + positions helpers
WPL_RE = re.compile(r'^\$(?:GP|GN|GL)WPL,([^,]+),([NS]),([^,]+),([EW]),([^,*]+)')
HOM_RE = re.compile(r'^\$(?:GP|GN|GL)HOM,([^,]+),([EW]),([^,]+),([NS])')

def dm_to_deg(dm: str) -> Optional[float]:
    try: v = float(dm)
    except Exception: return None
    deg = int(v // 100); minutes = v - 100*deg
    return deg + minutes/60.0

def parse_pos_file(path: str):
    """Parse *.POS into (lat,lon,name). Empty names are auto-filled from filename."""
    rows = []
    stem = os.path.splitext(os.path.basename(path))[0]
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#") or s.startswith(";"):
                    continue
                if s.startswith("$GPRTE") or s.startswith("$GNRTE") or s.startswith("$GLRTE"):
                    continue
                m = WPL_RE.match(s)
                if m:
                    lat_dm, ns, lon_dm, ew, name = m.groups()
                    lat = dm_to_deg(lat_dm); lon = dm_to_deg(lon_dm)
                    if lat is None or lon is None: continue
                    if ns == "S": lat = -lat
                    if ew == "W": lon = -lon
                    rows.append((lat, lon, (name or "").strip()))
                    continue
                m = HOM_RE.match(s)
                if m:
                    lon_dm, ew, lat_dm, ns = m.groups()
                    lat = dm_to_deg(lat_dm); lon = dm_to_deg(lon_dm)
                    if lat is None or lon is None: continue
                    if ns == "S": lat = -lat
                    if ew == "W": lon = -lon
                    rows.append((lat, lon, ""))
    except Exception:
        return []
    out = []
    unnamed = 1
    for (lat,lon,name) in rows:
        if not name:
            name = f"{stem}_{unnamed}"; unnamed += 1
        out.append((lat,lon,name))
    return out

def parse_positions_gpx(path: str):
    """Extract named positions from GPX: <wpt> (or named <rtept> fallback)."""
    out = []
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return out
    for wpt in root.iter():
        if _local(wpt.tag) != "wpt": continue
        lat = wpt.get("lat"); lon = wpt.get("lon")
        if lat is None or lon is None: continue
        name = ""
        for ch in wpt:
            if _local(ch.tag) == "name" and ch.text:
                name = ch.text.strip(); break
        out.append((float(lat), float(lon), name))
    if not out:
        for rtept in root.iter():
            if _local(rtept.tag) != "rtept": continue
            lat = rtept.get("lat"); lon = rtept.get("lon")
            if lat is None or lon is None: continue
            nm = ""
            for ch in rtept:
                if _local(ch.tag) == "name" and ch.text:
                    nm = ch.text.strip(); break
            if nm:
                out.append((float(lat), float(lon), nm))
    return out

def parse_positions_kmx(path: str):
    """Extract named positions from KML/KMZ: Point Placemarks with <name>."""
    out = []
    try:
        root = _parse_root_from_kml_or_kmz(path)
    except Exception:
        return out
    for pm in root.iter():
        if _local(pm.tag) != "Placemark": continue
        nm = ""
        for ch in pm:
            if _local(ch.tag) == "name" and ch.text:
                nm = ch.text.strip()
                break
        for ch in pm.iter():
            if _local(ch.tag) == "Point":
                for co in ch:
                    if _local(co.tag) == "coordinates" and co.text:
                        txt = co.text.strip()
                        parts = txt.split(",")
                        if len(parts) >= 2:
                            try:
                                lon = float(parts[0]); lat = float(parts[1])
                                out.append((lat, lon, nm))
                            except Exception:
                                pass
    return out

def parse_positions_trc(path: str):
    """Extract WPL/HOM waypoints embedded inside TRC/NMEA logs."""
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                s = line.strip()
                m = WPL_RE.match(s)
                if m:
                    lat_dm, ns, lon_dm, ew, name = m.groups()
                    lat = dm_to_deg(lat_dm); lon = dm_to_deg(lon_dm)
                    if lat is None or lon is None: continue
                    if ns == "S": lat = -lat
                    if ew == "W": lon = -lon
                    out.append((lat, lon, (name or "").strip()))
                    continue
                m = HOM_RE.match(s)
                if m:
                    lon_dm, ew, lat_dm, ns = m.groups()
                    lat = dm_to_deg(lat_dm); lon = dm_to_deg(lon_dm)
                    if lat is None or lon is None: continue
                    if ns == "S": lat = -lat
                    if ew == "W": lon = -lon
                    out.append((lat, lon, ""))
    except Exception:
        return out
    return out

def resolve_marker_size_px(size_name: str, size_px: Optional[int]) -> int:
    """Allow named sizes or pixel override."""
    if size_px is not None and size_px > 0: return size_px
    return {"small":36,"medium":48,"large":64}.get(size_name, 48)

# ---------------- Main ----------------
def main():
    ap = argparse.ArgumentParser(description="Convert GPX/KML/KMZ/TRC/NMEA/POS (multi-file) to a single Geoapify Static Maps POST body.")
    ap.add_argument("inputs", nargs="+", help="Input files (.gpx .kml .kmz .trc .nma .nmea .pos)")
    ap.add_argument("-o","--output", default="geoapify_body.json", help="Output JSON file")
    ap.add_argument("--style", default="osm-carto", help="Map style")
    ap.add_argument("--width", type=int, default=1280, help="Image width")
    ap.add_argument("--height", type=int, default=800, help="Image height")
    ap.add_argument("--format", choices=["png","jpeg"], default="png", help="Image format")

    # Line styling
    ap.add_argument("--out-type", choices=["geojson","polyline","polyline6"], default="geojson", help="Geometry encoding for tracks/routes")
    ap.add_argument("--linecolor", default="#0066ff", help="Track line color")
    ap.add_argument("--linewidth", type=int, default=5, help="Track line width (px)")
    ap.add_argument("--thin", type=int, default=1, help="Downsample: keep every Nth point")
    ap.add_argument("--gpx-merge-singletons", action="store_true", help="If all GPX segments have <2 points, merge all points into one segment")

    # TRC options
    ap.add_argument("--order", choices=["lonlat","latlon"], default=None, help="Force coordinate order for generic numeric TRC lines")
    ap.add_argument("--split-on-empty", action="store_true", help="New segment at blank lines for TRC")

    # POS/labels (NO 'plain' markers — labels go inside pin)
    ap.add_argument("--marker-color", default="#D32F2F", help="Marker color")
    ap.add_argument("--marker-size", choices=["small","medium","large"], default="medium", help="Marker named size")
    ap.add_argument("--marker-size-px", type=int, default=None, help="Marker size in pixels (overrides named size)")
    ap.add_argument("--no-text", action="store_true", help="Disable label text")
    ap.add_argument("--label-mode", choices=["inmarker","plain"], default="inmarker", help="(compat) labels inside marker (plain is mapped to in-marker)")
    ap.add_argument("--label-offset-m", type=float, default=60.0, help="(compat, ignored)")
    ap.add_argument("--contentsize", type=int, default=18, help="Label text size (POST: textsize)")
    ap.add_argument("--max-name-len", type=int, default=40, help="Truncate names longer than this (0 = no limit)")

    # Positions auto-extract
    ap.add_argument("--auto-positions", action="store_true", default=True, help="Extract waypoint/point markers from all inputs (GPX/KML/KMZ/TRC)")
    ap.add_argument("--no-auto-positions", dest="auto_positions", action="store_false", help="Disable auto position extraction")

    # Robustness
    ap.add_argument("--strict", action="store_true", help="Abort on first bad file instead of skipping")

    args = ap.parse_args()

    features = []
    geometries = []
    markers = []
    seen_markers = set()  # (lat_7, lon_7, name)
    dates = []

    # bbox accumulators (used to add 'area' rect in body for reliable view)
    min_lat = float('inf')
    min_lon = float('inf')
    max_lat = float('-inf')
    max_lon = float('-inf')

    for path in args.inputs:
        try:
            if not os.path.isfile(path):
                print(f"[SKIP] {path}: not found", file=sys.stderr); 
                continue

            ext = os.path.splitext(path)[1].lower().lstrip(".")

            # POS → markers (material + in-marker label only)
            if ext == "pos":
                pts = parse_pos_file(path)
                if not pts:
                    print(f"[SKIP] {path}: no positions", file=sys.stderr)
                    continue
                size_px = resolve_marker_size_px(args.marker_size, args.marker_size_px)
                size_field = args.marker_size if args.marker_size_px is None else size_px
                for lat,lon,name in pts:
                    nm = name
                    if args.max_name_len and len(nm) > args.max_name_len and args.max_name_len > 0:
                        nm = nm[:max(1,args.max_name_len-1)] + "…"
                    key = (round(lat,7), round(lon,7), nm)
                    if key in seen_markers: continue
                    seen_markers.add(key)
                    icon_marker = {"lat": lat, "lon": lon, "type": "material", "color": args.marker_color, "size": size_field}
                    if not args.no_text:
                        icon_marker.update({"text": nm, "textsize": args.contentsize})
                    markers.append(icon_marker)
                    # bbox
                    if lat < min_lat: min_lat = lat
                    if lat > max_lat: max_lat = lat
                    if lon < min_lon: min_lon = lon
                    if lon > max_lon: max_lon = lon
                continue

            # Auto-extract positions (markers) from non-POS files
            if args.auto_positions and ext in ("gpx","kml","kmz","trc","nma","nmea","log","txt"):
                try:
                    if ext == "gpx":
                        pos_pts = parse_positions_gpx(path)
                    elif ext in ("kml","kmz"):
                        pos_pts = parse_positions_kmx(path)
                    else:
                        pos_pts = parse_positions_trc(path)
                    if pos_pts:
                        size_px = resolve_marker_size_px(args.marker_size, args.marker_size_px)
                        size_field = args.marker_size if args.marker_size_px is None else size_px
                        stem = os.path.splitext(os.path.basename(path))[0]
                        counter = 1
                        for lat,lon,name in pos_pts:
                            nm = name.strip() if name else f"{stem}_{counter}"; 
                            if not name: counter += 1
                            if args.max_name_len and len(nm) > args.max_name_len and args.max_name_len > 0:
                                nm = nm[:max(1,args.max_name_len-1)] + "…"
                            key = (round(lat,7), round(lon,7), nm)
                            if key in seen_markers: continue
                            seen_markers.add(key)
                            icon_marker = {"lat": lat, "lon": lon, "type": "material", "color": args.marker_color, "size": size_field}
                            if not args.no_text:
                                icon_marker.update({"text": nm, "textsize": args.contentsize})
                            markers.append(icon_marker)
                            # bbox
                            if lat < min_lat: min_lat = lat
                            if lat > max_lat: max_lat = lat
                            if lon < min_lon: min_lon = lon
                            if lon > max_lon: max_lon = lon
                except Exception:
                    pass

            # Tracks/routes → segments
            if ext == "gpx":
                segs = parse_gpx_segments(path); date = _date_from_gpx(path) or _fallback_mtime(path)
                if not segs and args.gpx_merge_singletons:
                    # Fallback: collect all trkpt and rtept into one segment if available
                    try:
                        root_tmp = ET.parse(path).getroot()
                        pts_all = []
                        for el in root_tmp.iter():
                            tag = _local(el.tag)
                            if tag == "trkpt" or tag == "rtept":
                                lat = el.get("lat"); lon = el.get("lon")
                                if lat is not None and lon is not None:
                                    pts_all.append((float(lat), float(lon)))
                        if len(pts_all) >= 2:
                            segs = [pts_all]
                    except Exception:
                        pass
            elif ext in ("kml","kmz"):
                segs = parse_kmx_segments(path); date = (_date_from_kml(path) if ext=="kml" else _date_from_kmz(path)) or _fallback_mtime(path)
            elif ext in ("trc","nma","nmea","log","txt"):
                segs = parse_trc_segments(path, order=args.order, split_on_empty=args.split_on_empty); date = _date_from_nmea_like(path) or _fallback_mtime(path)
            else:
                print(f"[SKIP] {path}: unsupported extension .{ext}", file=sys.stderr)
                continue

            if not segs:
                print(f"[SKIP] {path}: no segments (tracks)", file=sys.stderr)
                continue

            if args.thin > 1:
                segs = [seg[::args.thin] for seg in segs if len(seg) >= 2]
            if date: dates.append(date)

            if args.out_type == "geojson":
                for seg in segs:
                    # bbox
                    for (lat,lon) in seg:
                        if lat < min_lat: min_lat = lat
                        if lat > max_lat: max_lat = lat
                        if lon < min_lon: min_lon = lon
                        if lon > max_lon: max_lon = lon
                    feat = {
                        "type": "Feature",
                        "properties": {"linecolor": args.linecolor, "linewidth": args.linewidth},
                        "geometry": {"type": "LineString", "coordinates": [[lon,lat] for (lat,lon) in seg]}
                    }
                    if date: feat["properties"]["date"] = date
                    features.append(feat)
            elif args.out_type == "polyline6":
                for seg in segs:
                    for (lat,lon) in seg:
                        if lat < min_lat: min_lat = lat
                        if lat > max_lat: max_lat = lat
                        if lon < min_lon: min_lon = lon
                        if lon > max_lon: max_lon = lon
                    geometries.append({
                        "type": "polyline6",
                        "value": encode_polyline(seg, precision=6),
                        "linecolor": args.linecolor,
                        "linewidth": args.linewidth
                    })
            else:  # polyline
                for seg in segs:
                    for (lat,lon) in seg:
                        if lat < min_lat: min_lat = lat
                        if lat > max_lat: max_lat = lat
                        if lon < min_lon: min_lon = lon
                        if lon > max_lon: max_lon = lon
                    geometries.append({
                        "type": "polyline",
                        "value": [{"lat":lat, "lon":lon} for (lat,lon) in seg],
                        "linecolor": args.linecolor,
                        "linewidth": args.linewidth
                    })

        except Exception as e:
            print(f"[SKIP] {path}: {e.__class__.__name__}: {e}", file=sys.stderr)
            if args.strict:
                raise
            continue

    body = {"style": args.style, "width": args.width, "height": args.height, "format": args.format}

    # geojson vs geometries
    if args.out_type == "geojson" and features:
        body["geojson"] = {"type": "FeatureCollection", "features": features}
    if args.out_type != "geojson" and geometries:
        body["geometries"] = geometries
        if dates: body["meta"] = {"date": min(dates)}

    if markers:
        body["markers"] = markers

    # avoid empty geojson when only markers present
    if "geojson" in body and body["geojson"].get("features") == []:
        del body["geojson"]

    # include a bbox area if we have any spatial content
    if min_lat != float('inf'):
        body["area"] = {"type": "rect", "value": {"lon1": min_lon, "lat1": min_lat, "lon2": max_lon, "lat2": max_lat}}

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False)
    print(args.output)

if __name__ == "__main__":
    main()
