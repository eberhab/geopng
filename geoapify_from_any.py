#!/usr/bin/env python3
# Version: v2025.08.29
"""
===============================================================================
geoapify_from_any.py — unified converter (tracks + markers) for Geoapify Static Maps
===============================================================================

Features (quick reference)
--------------------------
- Inputs (multi-file, any mix): .gpx .kml .kmz .trc .nma .nmea .pos
- Tracks output (default **geojson** FeatureCollection). Also supports
  **polyline** or **polyline6** under "geometries".
- Markers from:
  • .pos files (NMEA-like WPL/HOM)
  • auto-extracted points/waypoints from GPX/KML/KMZ/TRC/NMEA
- KML/KMZ smart handling: if a KML/KMZ contains a LineString/gx:Track *and* a
  very large number of Point Placemarks (typically track samples duplicated as
  markers), KML-derived markers are skipped to avoid the Geoapify 100-marker cap.
- Converter-side bbox padding: `--pad-frac` (default 0.20) relative per-side.
  `--pad-min-deg` default is **0.0** (disabled) and the padded bbox is clamped
  to world bounds to avoid pole/antimeridian errors.
- Optional converter-side marker thinning: `--max-markers` (default 100) and
  `--thin-markers`; keeps first/last markers and samples evenly.
- Robustness: per-file try/except → problem files are logged as `[SKIP] …`,
  unless `--strict` is specified.

Notes on labels & sizes
-----------------------
- We never emit {"type":"plain","text":...} overlays; instead, marker text
  is embedded in the pin via "text" and "textsize" (pixel integer). The
  renderer will normalize text size to "small"|"medium"|"large" as required
  by the API when needed.
- Marker "size" can be a keyword ("small"|"medium"|"large") or explicit pixel
  integer (the latter is supported by the converter and normalized in the
  renderer for compatibility).

Output schema
-------------
- For geojson mode: {"style","width","height","format","geojson","markers"?,"area","meta"?}
- For geometries mode: {"style","width","height","format","geometries","markers"?,"area","meta"?}
- "area" is an object rectangle: {"type":"rect","value":{"lon1","lat1","lon2","lat2"}}
  The converter sets "meta.padApplied": true with parameters to signal that
  downstream tools should not re-pad.
===============================================================================
"""
import argparse, os, io, json, re, zipfile, sys
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import List, Tuple, Optional

ISO = "%Y-%m-%d"

# ---------------- Helpers ----------------
def _local(tag: str) -> str:
    """Return the local (namespace-stripped) XML tag name.

    Parameters
    ----------
    tag : str
        XML tag possibly including a namespace like '{ns}Tag'.

    Returns
    -------
    str
        The local tag without the namespace part.
    """
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else tag

def _parse_iso_date(s: str):
    """Parse various ISO-like timestamps to `date`.

    Accepts 'YYYY-MM-DD', and 'YYYY-MM-DDTHH:MM:SS[Z|+..]'.

    Returns `datetime.date` on success, else None.
    """
    try:
        s = s.strip()
        if "T" in s:
            s2 = s.replace("Z","").split("+")[0]
            return datetime.fromisoformat(s2).date()
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None

def _fallback_mtime(path: str) -> Optional[str]:
    """Return file mtime as YYYY-MM-DD or None on failure."""
    try:
        return datetime.fromtimestamp(os.path.getmtime(path)).strftime(ISO)
    except Exception:
        return None

# ---------------- Polyline encoder ----------------
def encode_polyline(points: List[Tuple[float,float]], precision=6) -> str:
    """Google Encoded Polyline Algorithm for a list of (lat,lon) points.

    Parameters
    ----------
    points : list[(float,float)]
        Latitude/longitude pairs.
    precision : int
        Decimal places (5 or 6; we default to 6).

    Returns
    -------
    str
        Encoded polyline string.
    """
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

def clamp_bbox(lat1: float, lat2: float, lon1: float, lon2: float):
    """Clamp and order a rectangle to world bounds.

    Ensures:
      -90 ≤ lat1 ≤ lat2 ≤ 90
     -180 ≤ lon1 ≤ lon2 ≤ 180

    Returns the clamped (lat1, lat2, lon1, lon2).
    """
    lat1 = max(-90.0, min(90.0, lat1))
    lat2 = max(-90.0, min(90.0, lat2))
    lon1 = max(-180.0, min(180.0, lon1))
    lon2 = max(-180.0, min(180.0, lon2))
    if lat1 > lat2: lat1, lat2 = lat2, lat1
    if lon1 > lon2: lon1, lon2 = lon2, lon1
    return lat1, lat2, lon1, lon2

# ---------------- GPX/KML/KMZ date helpers ----------------
def _date_from_gpx(path: str) -> Optional[str]:
    """Extract earliest <time> from a GPX as YYYY-MM-DD (or None)."""
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
    """Extract earliest <when> (or <TimeStamp><when>) from a KML root."""
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
    """Open a KML and return earliest date string or None."""
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return None
    return _date_from_kml_root(root)

def _date_from_kmz(path: str) -> Optional[str]:
    """Open a KMZ (zip), find a KML inside, and return earliest date or None."""
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

# ---------------- NMEA date helpers ----------------
_ZDA_RE = re.compile(r'^\$(?:GP|GN|GL)ZDA,(?:[^,]*),([0-3]\d),([01]\d),(\d{4})')
_RMC_RE = re.compile(r'^\$(?:GP|GN|GL)RMC,[^,]*,[AV],[^,]*,[NS],[^,]*,[EW],[^,]*,[^,]*,([0-3]\d)([01]\d)(\d{2})')

def _date_from_nmea_like(path: str) -> Optional[str]:
    """Parse earliest date from NMEA ZDA/RMC sentences in a text file (if any)."""
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

# ---------------- Parsers ----------------
def parse_gpx_segments(gpx_path: str) -> List[List[Tuple[float,float]]]:
    """Return list of GPX line segments (each a list of (lat,lon))."""
    try:
        root = ET.parse(gpx_path).getroot()
    except Exception:
        return []
    segs = []
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
    """Open .kml or .kmz and return parsed XML root (raises on wrong type)."""
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
    """Parse KML/KMZ LineStrings and gx:Track coords into segments."""
    try:
        root = _parse_root_from_kml_or_kmz(path)
    except Exception:
        return []
    segments = []
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

# ---- TRC/NMEA helpers ----
FLOAT_RE = re.compile(r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?')
def plausible_lon(x): 
    """Return True if x is a plausible longitude.""" 
    return -180.0 <= x <= 180.0
def plausible_lat(y): 
    """Return True if y is a plausible latitude.""" 
    return -90.0 <= y <= 90.0

def dm_to_dec(dm, hemi=None):
    """Convert NMEA degrees-minutes float to decimal degrees; apply hemisphere."""
    try: f = float(dm)
    except Exception: return None
    deg = int(f // 100.0); minutes = f - deg*100.0
    dec = deg + minutes/60.0
    if hemi in ("S","W"): dec = -dec
    return dec

def parse_nmea_line(line):
    """Parse a single NMEA RMC/GGA line into (lat,lon) or None."""
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
    """Heuristically find adjacent (lat,lon) or (lon,lat) in a numeric list.

    If `force` is 'lonlat' or 'latlon', restrict matching accordingly.
    """
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
    """Parse .trc/.nma/.nmea generic log into segments of (lat,lon)."""
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

# Positions (POS, GPX wpt/rtept, KML/KMZ Placemarks, TRC named points)
WPL_RE = re.compile(r'^\$(?:GP|GN|GL)WPL,([^,]+),([NS]),([^,]+),([EW]),([^,*]+)')
HOM_RE = re.compile(r'^\$(?:GP|GN|GL)HOM,([^,]+),([EW]),([^,]+),([NS])')

def dm_to_deg(dm: str) -> Optional[float]:
    """Convert NMEA ddmm.mmmm (float) to decimal degrees (positive)."""
    try: v = float(dm)
    except Exception: return None
    deg = int(v // 100); minutes = v - 100*deg
    return deg + minutes/60.0

def parse_pos_file(path: str):
    """Parse .pos (WPL/HOM) to a list of (lat,lon,name). Unnamed entries
    are auto-named using the filename stem + counter.
    """
    rows = []
    stem = os.path.splitext(os.path.basename(path))[0]
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#") or s.startswith(";"): continue
                if s.startswith("$GPRTE") or s.startswith("$GNRTE") or s.startswith("$GLRTE"):
                    continue
                m = WPL_RE.match(s)
                if m:
                    lat_dm, ns, lon_dm, ew, name = m.groups()
                    lat = dm_to_deg(lat_dm); lon = dm_to_deg(lon_dm)
                    if lat is None or lon is None: continue
                    if ns == "S": lat = -lat
                    if ew == "W": lon = -lon
                    rows.append((lat, lon, (name or "").strip())); continue
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
    """Extract (lat,lon,name) from GPX waypoints (<wpt>) or named route points."""
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
            if nm: out.append((float(lat), float(lon), nm))
    return out

def parse_positions_kmx(path: str):
    """Extract (lat,lon,name) points from KML/KMZ Placemarks with <Point>."""
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
                nm = ch.text.strip(); break
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
    """Extract (lat,lon,name) from NMEA-like WPL/HOM records in TRC/log files."""
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
                    out.append((lat, lon, (name or "").strip())); continue
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
    """Return pixel size for a marker from a keyword or explicit px override."""
    if size_px is not None and size_px > 0: return size_px
    return {"small":36,"medium":48,"large":64}.get(size_name, 48)

# ---------------- Main ----------------
def main():
    """CLI entry: build a Geoapify Static Maps POST body from inputs."""
    ap = argparse.ArgumentParser(description="Convert GPX/KML/KMZ/TRC/NMEA/POS (multi-file) to a single Geoapify Static Maps POST body.")
    ap.add_argument("inputs", nargs="+", help="Input files (.gpx .kml .kmz .trc .nma .nmea .pos)")
    ap.add_argument("-o","--output", default="geoapify_body.json", help="Output JSON file")
    ap.add_argument("--style", default="osm-carto", help="Map style")
    ap.add_argument("--width", type=int, default=1280, help="Image width")
    ap.add_argument("--height", type=int, default=800, help="Image height")
    ap.add_argument("--format", choices=["png","jpeg"], default="png", help="Image format")
    ap.add_argument("--pad-frac", type=float, default=0.20, help="BBox padding fraction per side")
    ap.add_argument("--pad-min-deg", type=float, default=0.0, help="Minimum padding per side in degrees (0.0 disables)")

    # Line styling
    ap.add_argument("--out-type", choices=["geojson","polyline","polyline6"], default="geojson", help="Geometry encoding for tracks/routes")
    ap.add_argument("--linecolor", default="#0066ff", help="Track line color")
    ap.add_argument("--linewidth", type=int, default=5, help="Track line width (px)")
    ap.add_argument("--thin", type=int, default=1, help="Downsample: keep every Nth point")
    ap.add_argument("--gpx-merge-singletons", action="store_true", help="If all GPX segments have <2 points, merge all trkpt/rtept into one segment")

    # TRC options
    ap.add_argument("--order", choices=["lonlat","latlon"], default=None, help="Force coordinate order for TRC numeric pairs")
    ap.add_argument("--split-on-empty", action="store_true", help="New segment at blank lines for TRC")

    # POS/labels
    ap.add_argument("--marker-color", default="#D32F2F", help="Marker color")
    ap.add_argument("--marker-size", choices=["small","medium","large"], default="medium", help="Marker named size")
    ap.add_argument("--marker-size-px", type=int, default=None, help="Marker size in pixels (overrides named size)")
    ap.add_argument("--no-text", action="store_true", help="Disable label text")
    ap.add_argument("--label-mode", choices=["inmarker","plain"], default="inmarker", help="(compat) plain is mapped to in-marker")
    ap.add_argument("--label-offset-m", type=float, default=60.0, help="(compat, ignored)")
    ap.add_argument("--contentsize", type=int, default=18, help="Label text size (renderer normalizes)")
    ap.add_argument("--max-name-len", type=int, default=40, help="Truncate names longer than this (0 = no limit)")

    # Marker limits (converter-side)
    ap.add_argument("--max-markers", type=int, default=100, help="Cap markers to this count (API limit). 0 = unlimited")
    ap.add_argument("--thin-markers", action="store_true", help="Thin markers down to <= max-markers if exceeded")

    # Positions auto-extract
    ap.add_argument("--auto-positions", action="store_true", default=True, help="Extract waypoint/point markers from all inputs")
    ap.add_argument("--no-auto-positions", dest="auto_positions", action="store_false", help="Disable auto position extraction")

    # Robustness
    ap.add_argument("--strict", action="store_true", help="Abort on first bad file instead of skipping")

    args = ap.parse_args()

    features = []
    geometries = []
    markers = []
    seen_markers = set()
    dates = []

    min_lat = float('inf'); min_lon = float('inf')
    max_lat = float('-inf'); max_lon = float('-inf')

    for path in args.inputs:
        try:
            if not os.path.isfile(path):
                print(f"[SKIP] {path}: not found", file=sys.stderr); 
                continue

            ext = os.path.splitext(path)[1].lower().lstrip(".")

            # POS → markers
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
                    if lat < min_lat: min_lat = lat
                    if lat > max_lat: max_lat = lat
                    if lon < min_lon: min_lon = lon
                    if lon > max_lon: max_lon = lon
                continue

            # Tracks/routes
            if ext == "gpx":
                segs = parse_gpx_segments(path); date = _date_from_gpx(path) or _fallback_mtime(path)
                if not segs and args.gpx_merge_singletons:
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
                segs = parse_trc_segments(path, order=args.order, split_on_empty=args.split_on_empty)
                date = _date_from_nmea_like(path) or _fallback_mtime(path)
            else:
                print(f"[SKIP] {path}: unsupported extension .{ext}", file=sys.stderr)
                continue

            if args.thin > 1 and segs:
                segs = [seg[::args.thin] for seg in segs if len(seg) >= 2]
            if date: dates.append(date)

            # Emit tracks
            if segs:
                if args.out_type == "geojson":
                    for seg in segs:
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
                else:
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

            # Auto-positions (markers) after we know if KML had segments
            if args.auto_positions and ext in ("gpx","kml","kmz","trc","nma","nmea","log","txt"):
                try:
                    if ext == "gpx":
                        pos_pts = parse_positions_gpx(path)
                    elif ext in ("kml","kmz"):
                        kml_positions = parse_positions_kmx(path)
                        has_kml_lines = bool(segs)
                        if has_kml_lines and len(kml_positions) > max(100, args.max_markers):
                            print(f"[INFO] {path}: KML has path + {len(kml_positions)} points; skipping KML markers.", file=sys.stderr)
                            pos_pts = []
                        else:
                            pos_pts = kml_positions
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
                            if lat < min_lat: min_lat = lat
                            if lat > max_lat: max_lat = lat
                            if lon < min_lon: min_lon = lon
                            if lon > max_lon: max_lon = lon
                except Exception:
                    pass

        except Exception as e:
            print(f"[SKIP] {path}: {e.__class__.__name__}: {e}", file=sys.stderr)
            if args.strict:
                raise
            continue

    # Build body
    body = {"style": args.style, "width": args.width, "height": args.height, "format": args.format}

    if args.out_type == "geojson" and features:
        body["geojson"] = {"type": "FeatureCollection", "features": features}
    if args.out_type != "geojson" and geometries:
        body["geometries"] = geometries
        if dates: body["meta"] = {"date": min(dates)}

    # Converter-side marker thinning (preferred)
    if args.max_markers and args.thin_markers and len(markers) > args.max_markers:
        n = len(markers); m = args.max_markers
        step = max(1, (n + (m-1)) // m)  # ceil(n/m)
        markers = [markers[0]] + [markers[i] for i in range(1, n-1, step)] + [markers[-1]]
        markers = markers[:m]
        print(f"[INFO] thinned markers: {n} → {len(markers)}", file=sys.stderr)

    if markers:
        body["markers"] = markers

    # Drop empty geojson if no features
    if "geojson" in body and body["geojson"].get("features") == []:
        del body["geojson"]

    # Padded bbox (object form) + clamp
    if min_lat != float('inf'):
        dlat = max_lat - min_lat; dlon = max_lon - min_lon
        plat = max(args.pad_frac * dlat, args.pad_min_deg)
        plon = max(args.pad_frac * dlon, args.pad_min_deg)
        elat1 = min_lat - plat; elon1 = min_lon - plon
        elat2 = max_lat + plat; elon2 = max_lon + plon
        lat1_c, lat2_c, lon1_c, lon2_c = clamp_bbox(elat1, elat2, elon1, elon2)
        body["area"] = {"type": "rect", "value": {"lon1": lon1_c, "lat1": lat1_c, "lon2": lon2_c, "lat2": lat2_c}}
        body.setdefault("meta", {})["padApplied"] = True
        body["meta"]["padParams"] = {"padFrac": args.pad_frac, "padMinDeg": args.pad_min_deg}

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False)
    print(args.output)

if __name__ == "__main__":
    main()
