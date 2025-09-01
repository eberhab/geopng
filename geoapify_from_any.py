#!/usr/bin/env python3
# Version: v2025.09.01
"""
================================================================================
geoapify_from_any.py
================================================================================
Unified converter that ingests multiple geodata formats and emits a single
Geoapify Static Maps POST body JSON.

Supported inputs (in any mix, multiple files):
  • GPX (.gpx) — tracks (trk/trkseg/trkpt), routes (rte/rtept), waypoints (wpt)
  • KML (.kml) and KMZ (.kmz) — LineString, gx:Track, Point Placemarks
  • TRC/NMEA-like logs (.trc .nma .nmea .log .txt) — numeric lon/lat or NMEA
  • POS (.pos) — $..WPL and $..HOM waypoints

Output body features:
  • Tracks/Routes:
      - Default: GeoJSON FeatureCollection of LineStrings in WGS84
      - Alternatives: "geometries" with type "polyline" or "polyline6"
      - Per-track styling: linecolor, linewidth
      - Earliest per-file date stored in Feature.properties.date (GeoJSON mode),
        or in body.meta.date (polyline modes)
  • Markers:
      - Extracted from POS, GPX (wpt/rtept names), KML/KMZ (Placemark names),
        TRC/NMEA ($..WPL/$..HOM)
      - Labels are included INSIDE the pin (text/textsize) to comply with
        Geoapify’s marker schema.
      - Optional converter-side marker thinning (respect API caps).
  • Area/BBox:
      - We compute an explicit "area" rect around all points (tracks+markers),
        apply relative padding (default 20% per side), then clamp to world
        bounds ([-90..90], [-180..180]) and ensure ordering.
        Minimum-degree padding defaults to 0.0 (disabled).

Robustness & UX:
  • Per-file try/except — bad files log as [SKIP] and we continue unless --strict.
  • Smart KML/KMZ: when a path exists and Placemark Points are numerous (likely
    track samples as markers), we skip those markers to avoid API limits.
  • NMEA date extraction: earliest date from $..ZDA / $..RMC if present.
  • GPX fallback (--gpx-merge-singletons): merge isolated trkpt/rtept into one
    line when no trkseg/rte present.
  • Downsampling (--thin) and global capping (--cap-track-points) to mitigate
    timeouts with huge inputs.
================================================================================
"""
import argparse, os, io, json, re, zipfile, sys
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import List, Tuple, Optional

ISO = "%Y-%m-%d"

# ---------------- Helpers ----------------
def _local(tag: str) -> str:
    """Return the local name of an XML tag regardless of namespace."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else tag

def _parse_iso_date(s: str):
    """Parse various ISO-like time strings into a date object, else None."""
    try:
        s = s.strip()
        if "T" in s:
            s2 = s.replace("Z","").split("+")[0]
            return datetime.fromisoformat(s2).date()
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None

def _fallback_mtime(path: str) -> Optional[str]:
    """Return file modification time as YYYY-MM-DD, or None on error."""
    try:
        return datetime.fromtimestamp(os.path.getmtime(path)).strftime(ISO)
    except Exception:
        return None

def encode_polyline(points: List[Tuple[float,float]], precision=6) -> str:
    """Encode (lat,lon) points as a Google polyline string."""
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
    """Clamp bbox to world bounds and ensure axis ordering."""
    lat1 = max(-90.0, min(90.0, lat1))
    lat2 = max(-90.0, min(90.0, lat2))
    lon1 = max(-180.0, min(180.0, lon1))
    lon2 = max(-180.0, min(180.0, lon2))
    if lat1 > lat2: lat1, lat2 = lat2, lat1
    if lon1 > lon2: lon1, lon2 = lon2, lon1
    return lat1, lat2, lon1, lon2

# ---------------- Date helpers ----------------
def _date_from_gpx(path: str) -> Optional[str]:
    """Extract earliest <time> from a GPX file as YYYY-MM-DD, else None."""
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
    """Extract earliest <when> or <TimeStamp><when> from KML root."""
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
    """Open .kml and delegate to _date_from_kml_root; returns YYYY-MM-DD or None."""
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return None
    return _date_from_kml_root(root)

def _date_from_kmz(path: str) -> Optional[str]:
    """Open .kmz, find contained .kml, and extract earliest timestamp; else None."""
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
    """Scan a TRC/NMEA-like text file for earliest ZDA/RMC date; returns YYYY-MM-DD or None."""
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
    """Parse GPX tracks and routes to a list of segments; each segment is [(lat,lon), ...]."""
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
    """Open a .kml or .kmz file and return the parsed XML root element."""
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
    """Parse KML/KMZ LineStrings and gx:Track coords into segments of (lat,lon)."""
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

def parse_nmea_line(line: str) -> Optional[Tuple[float,float]]:
    """Parse a single NMEA sentence of type RMC/GGA into (lat,lon) if possible."""
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

def find_pair(nums, force=None) -> Optional[Tuple[float,float]]:
    """From a list of floats, find an adjacent (lat,lon) or (lon,lat) pair."""
    n = len(nums)
    if force == "lonlat":
        for i in range(n-1):
            a,b = nums[i], nums[i+1]
            if -180.0 <= a <= 180.0 and -90.0 <= b <= 90.0: return (b,a)
        return None
    if force == "latlon":
        for i in range(n-1):
            a,b = nums[i], nums[i+1]
            if -90.0 <= a <= 90.0 and -180.0 <= b <= 180.0: return (a,b)
        return None
    for i in range(n-1):
        a,b = nums[i], nums[i+1]
        if -180.0 <= a <= 180.0 and -90.0 <= b <= 90.0: return (b,a)
        if -90.0 <= a <= 90.0 and -180.0 <= b <= 180.0: return (a,b)
    return None

def dm_to_dec(dm, hemi=None) -> Optional[float]:
    """Convert NMEA 'degrees+minutes' (DM) value to decimal degrees."""
    try:
        f = float(dm)
    except Exception:
        return None
    deg = int(f // 100.0); minutes = f - deg*100.0
    dec = deg + minutes/60.0
    if hemi in ("S","W"): dec = -dec
    return dec

def parse_trc_segments(path: str, order=None, split_on_empty=False) -> List[List[Tuple[float,float]]]:
    """Parse TRC/NMEA-like text into segments of (lat,lon)."""
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
    """Convert WPL/HOM DM value to decimal degrees (unsigned)."""
    try:
        v = float(dm)
    except Exception:
        return None
    deg = int(v // 100); minutes = v - 100*deg
    return deg + minutes/60.0

def parse_pos_file(path: str):
    """Parse a .pos file into [(lat,lon,name), ...]."""
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
    """Extract GPX waypoint and named route points into markers."""
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
    """Extract KML/KMZ Placemark Point coordinates and names into markers."""
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
    """Extract named waypoints from TRC/NMEA-like files ($..WPL/$..HOM)."""
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
    """Derive pixel size from keyword unless an explicit pixel value is given."""
    if size_px is not None and size_px > 0: return size_px
    return {"small":36,"medium":48,"large":64}.get(size_name, 48)

# ---------------- Main ----------------
def main():
    """CLI — Convert inputs to Geoapify Static Maps POST body JSON."""
    ap = argparse.ArgumentParser(description="Convert GPX/KML/KMZ/TRC/NMEA/POS (multi-file) to a single Geoapify Static Maps POST body.")
    ap.add_argument("inputs", nargs="+", help="Input files (.gpx .kml .kmz .trc .nma .nmea .pos)")
    ap.add_argument("-o","--output", default="geoapify_body.json", help="Output JSON file")
    ap.add_argument("--style", default="osm-carto", help="Map style")
    ap.add_argument("--width", type=int, default=1280, help="Image width")
    ap.add_argument("--height", type=int, default=800, help="Image height")
    ap.add_argument("--format", choices=["png","jpeg"], default="png", help="Image format")
    ap.add_argument("--pad-frac", type=float, default=0.20, help="BBox padding fraction per side")
    ap.add_argument("--pad-min-deg", type=float, default=0.0, help="Min padding per side in degrees (0.0 = disabled)")

    # Line styling
    ap.add_argument("--out-type", choices=["geojson","polyline","polyline6"], default="geojson", help="Geometry encoding for tracks/routes")
    ap.add_argument("--linecolor", default="#0066ff", help="Track line color")
    ap.add_argument("--linewidth", type=int, default=5, help="Track line width (px)")
    ap.add_argument("--thin", type=int, default=1, help="Downsample: keep every Nth point")
    ap.add_argument("--cap-track-points", type=int, default=10000,
                   help="Cap total number of track points across all segments; adaptive thinning is applied if exceeded (0 = unlimited).")
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
    ap.add_argument("--no-auto-positions", dest="auto_positions", action="store_false")

    # Robustness
    ap.add_argument("--strict", action="store_true", help="Abort on first bad file instead of skipping")

    args = ap.parse_args()

    features = []
    geometries = []
    markers = []
    seen_markers = set()
    dates = []
    collected_segments = []  # list of {'points': [(lat,lon),...], 'date': 'YYYY-MM-DD' or None}

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

            # Collect segments; update bounds
            if segs:
                for seg in segs:
                    for (lat,lon) in seg:
                        if lat < min_lat: min_lat = lat
                        if lat > max_lat: max_lat = lat
                        if lon < min_lon: min_lon = lon
                        if lon > max_lon: max_lon = lon
                    collected_segments.append({'points': seg, 'date': date})

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

    # ---- Track point capping + emission ----
    def _thin_by_step(seg, step):
        if step <= 1: return seg
        if len(seg) <= 2: return seg
        core = seg[1:-1:step]
        out = [seg[0]] + core + [seg[-1]]
        return out if len(out) >= 2 else seg

    total_pts = sum(len(s['points']) for s in collected_segments)
    print(f"[INFO] track points (before cap): {total_pts}", file=sys.stderr)
    if args.cap_track_points and total_pts > args.cap_track_points:
        step = (total_pts + (args.cap_track_points - 1)) // args.cap_track_points  # ceil
        new_segments = []
        for s in collected_segments:
            pts = _thin_by_step(s['points'], step)
            if len(pts) >= 2:
                new_segments.append({'points': pts, 'date': s['date']})
        collected_segments = new_segments
        total_pts = sum(len(s['points']) for s in collected_segments)
        print(f"[INFO] track points (after cap step={step}): {total_pts}", file=sys.stderr)

    # Emit to chosen geometry encoding
    if args.out_type == 'geojson' and collected_segments:
        for s in collected_segments:
            seg = s['points']; date = s['date']
            features.append({
                'type': 'Feature',
                'properties': {'linecolor': args.linecolor, 'linewidth': args.linewidth, **({'date': date} if date else {})},
                'geometry': {'type': 'LineString', 'coordinates': [[lon,lat] for (lat,lon) in seg]}
            })
    elif args.out_type == 'polyline6' and collected_segments:
        for s in collected_segments:
            seg = s['points']
            geometries.append({
                'type': 'polyline6',
                'value': encode_polyline(seg, precision=6),
                'linecolor': args.linecolor,
                'linewidth': args.linewidth
            })
    elif args.out_type != 'geojson' and collected_segments:
        for s in collected_segments:
            seg = s['points']
            geometries.append({
                'type': 'polyline',
                'value': [{'lat':lat, 'lon':lon} for (lat,lon) in seg],
                'linecolor': args.linecolor,
                'linewidth': args.linewidth
            })

    # Log marker count
    print(f"[INFO] markers: {len(markers)}", file=sys.stderr)

    # Recompute bounds after capping (for accurate area)
    if collected_segments:
        min_lat = min(min_lat, min(min(lat for lat,_ in s['points']) for s in collected_segments))
        max_lat = max(max_lat, max(max(lat for lat,_ in s['points']) for s in collected_segments))
        min_lon = min(min_lon, min(min(lon for _,lon in s['points']) for s in collected_segments))
        max_lon = max(max_lon, max(max(lon for _,lon in s['points']) for s in collected_segments))

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

    # Padded & clamped bbox (object form)
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
