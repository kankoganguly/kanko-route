#!/usr/bin/env python3
"""
Route Calculator — Bay Area → Lake Tahoe
Geocoding: Nominatim (OSM) · Routing: OSRM · Live closures: Caltrans QuickMap KML
No external packages required.
"""

import json
import os
import sys
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs, urlencode
from urllib.request import urlopen, Request

PORT = int(os.environ.get("PORT", 7824))
UA   = "SmartTodos-RouteApp/1.0 (local personal use)"

# ── Allowed regions ───────────────────────────────────────────────────────────

BAY_AREA = {
    "label":   "Bay Area",
    "hint":    "Alameda · Contra Costa · Marin · Napa · San Francisco · San Mateo · Santa Clara · Solano · Sonoma",
    "counties": {
        "Alameda County", "Contra Costa County", "Marin County", "Napa County",
        "San Francisco County", "San Mateo County", "Santa Clara County",
        "Solano County", "Sonoma County",
    },
    "extra_cities": {"San Francisco"},          # SF is its own city+county
    "viewbox": "-123.6,39.1,-121.2,36.8",       # Nominatim: left,top,right,bottom
    "zips_prefix": {"940", "941", "942", "943", "944", "945", "946", "947",
                    "948", "949", "950", "951"},
}

LAKE_TAHOE = {
    "label":   "Lake Tahoe",
    "hint":    "El Dorado · Placer · Nevada · Alpine (CA) · Douglas · Washoe (NV)",
    "counties": {
        "El Dorado County", "Placer County", "Nevada County", "Alpine County",
        "Douglas County", "Washoe County",
    },
    "extra_cities": {
        "South Lake Tahoe", "Tahoe City", "Truckee", "Kings Beach",
        "Incline Village", "Stateline", "Meyers", "Tahoma", "Homewood",
        "Carnelian Bay", "Tahoe Vista", "Crystal Bay",
    },
    "viewbox": "-121.2,39.6,-119.5,38.3",
    "zips_prefix": {"957", "895", "894"},
}

CALTRANS_KML = "https://quickmap.dot.ca.gov/data/lcs2way.kml"
NOMINATIM    = "https://nominatim.openstreetmap.org/search"
OSRM         = "https://router.project-osrm.org/route/v1/driving"


# ── Network ───────────────────────────────────────────────────────────────────

def fetch(url, timeout=13):
    try:
        req = Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en"})
        with urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        return None


# ── Geocoding ─────────────────────────────────────────────────────────────────

def geocode(query, region_key):
    region = BAY_AREA if region_key == "origin" else LAKE_TAHOE
    qs = urlencode({
        "q": query, "format": "jsonv2", "addressdetails": "1",
        "limit": "7", "countrycodes": "us",
        "viewbox": region["viewbox"], "bounded": "1",
    })
    raw = fetch(f"{NOMINATIM}?{qs}")
    if not raw:
        return []
    try:
        results = json.loads(raw)
    except Exception:
        return []

    out = []
    for r in results:
        addr     = r.get("address", {})
        county   = addr.get("county", "")
        city     = addr.get("city", addr.get("town", addr.get("village", "")))
        state    = addr.get("state", "")
        postcode = addr.get("postcode", "")

        valid = (county in region["counties"] or
                 city in region.get("extra_cities", set()) or
                 any(postcode.startswith(p) for p in region.get("zips_prefix", set())))

        # Build a short human-readable label
        parts = []
        for k in ("amenity", "tourism", "leisure", "historic", "building"):
            if addr.get(k):
                parts.append(addr[k])
                break
        if not parts:
            for k in ("road", "pedestrian", "path"):
                if addr.get(k):
                    parts.append(addr[k])
                    break
        for k in ("city", "town", "village", "hamlet", "suburb"):
            if addr.get(k):
                parts.append(addr[k])
                break
        state_abbr = {"California": "CA", "Nevada": "NV"}
        if state in state_abbr:
            parts.append(state_abbr[state])

        short = ", ".join(parts) if parts else r.get("display_name", "")[:70]

        out.append({
            "display":  r.get("display_name", ""),
            "short":    short,
            "lat":      float(r.get("lat", 0)),
            "lng":      float(r.get("lon", 0)),
            "valid":    valid,
            "county":   county,
            "state":    state,
            "postcode": postcode,
        })
    return out


# ── Routing ───────────────────────────────────────────────────────────────────

def fetch_osrm(olat, olng, dlat, dlng):
    url = f"{OSRM}/{olng},{olat};{dlng},{dlat}?steps=true&overview=full&geometries=geojson"
    raw = fetch(url)
    if not raw:
        return None
    try:
        d = json.loads(raw)
        return d if d.get("code") == "Ok" else None
    except Exception:
        return None


def derive_segments(steps):
    """
    Reduce OSRM steps to key named-highway segments.
    Groups all consecutive steps on the same highway ref into one entry.
    Unnamed 'Road' ramp steps are absorbed into adjacent named segments
    unless they carry an exit number or meaningful destination.
    """
    def norm_ref(step):
        ref = (step.get("ref") or "").replace(" ", "-").strip()
        # Strip combined refs like "I-80;-CA-12" -> "I-80"
        if ";" in ref:
            ref = ref.split(";")[0].strip("-")
        return ref

    def clean_dest(dest, label):
        """Remove the highway name from dest if it already appears in label."""
        if not dest:
            return ""
        # Trim very long destination strings
        if len(dest) > 60:
            dest = dest[:57] + "..."
        return dest

    # Pass 1: flatten steps into (ref, mtype, dist_m, dur_s, dest, exits, loc)
    flat = []
    for step in steps:
        ref   = norm_ref(step)
        name  = (step.get("name") or "").strip()
        label = ref or name         # may be empty string = unnamed road
        mtype = step.get("maneuver", {}).get("type", "")
        mod   = step.get("maneuver", {}).get("modifier", "")
        dist  = step.get("distance", 0)
        dur   = step.get("duration", 0)
        dest  = (step.get("destinations") or "").strip()
        exits = (step.get("exits") or "").strip()
        loc   = step.get("maneuver", {}).get("location", [])
        flat.append((label, mtype, mod, dist, dur, dest, exits, loc))

    # Pass 2: group by named highway, absorbing unnamed ramp steps
    groups   = []       # list of {highway, mtype, mod, dist_m, dur_s, dest, exits, loc}
    pending  = None     # accumulator for current highway

    def flush(g):
        if g and g["dist_m"] > 10:   # ignore sub-10m phantom steps
            groups.append(g)

    for (label, mtype, mod, dist, dur, dest, exits, loc) in flat:
        is_unnamed = (label == "" or label == "Road")

        if mtype == "depart":
            flush(pending)
            pending = None
            groups.append({"highway": label or "Local street", "mtype": mtype,
                           "mod": mod, "dist_m": dist, "dur_s": dur,
                           "dest": dest, "exits": exits, "loc": loc})
            continue

        if mtype == "arrive":
            flush(pending)
            pending = None
            groups.append({"highway": "Destination", "mtype": mtype,
                           "mod": mod, "dist_m": dist, "dur_s": dur,
                           "dest": dest, "exits": exits, "loc": loc})
            continue

        # Unnamed ramp/road: only break out if it has an exit number;
        # otherwise absorb distance into adjacent named segment.
        if is_unnamed:
            if exits:
                flush(pending)
                pending = None
                groups.append({"highway": dest.split(":")[0][:25] if dest else "Exit ramp",
                               "mtype": "off ramp", "mod": mod,
                               "dist_m": dist, "dur_s": dur,
                               "dest": dest, "exits": exits, "loc": loc})
            else:
                if pending:
                    pending["dist_m"] += dist
                    pending["dur_s"]  += dur
                    if dest and not pending["dest"]:
                        pending["dest"] = dest
            continue

        # Named highway
        if pending and pending["highway"] == label and pending["mtype"] not in ("depart",):
            # Same highway — accumulate
            pending["dist_m"] += dist
            pending["dur_s"]  += dur
            if dest and not pending["dest"]:
                pending["dest"] = dest
            if exits and not pending["exits"]:
                pending["exits"] = exits
        else:
            flush(pending)
            pending = {"highway": label, "mtype": mtype, "mod": mod,
                       "dist_m": dist, "dur_s": dur,
                       "dest": dest, "exits": exits, "loc": loc}

    flush(pending)

    # Merge consecutive groups on the same highway (e.g. Depart + Continue)
    merged = []
    for g in groups:
        if (merged and merged[-1]["highway"] == g["highway"]
                and merged[-1]["mtype"] != "arrive"
                and g["mtype"] not in ("depart", "arrive", "off ramp")):
            merged[-1]["dist_m"] += g["dist_m"]
            merged[-1]["dur_s"]  += g["dur_s"]
            if g["dest"] and not merged[-1]["dest"]:
                merged[-1]["dest"] = g["dest"]
        else:
            merged.append(g)
    groups = merged

    # Pass 3: convert groups to output segments
    segments = []
    for g in groups:
        hwy   = g["highway"]
        mtype = g["mtype"]
        mod   = g["mod"]
        dist  = round(g["dist_m"] / 1609.34, 1)
        dur   = round(g["dur_s"] / 60)
        dest  = clean_dest(g["dest"], hwy)
        exits = g["exits"]
        loc   = g["loc"]

        if mtype == "depart":
            action = "Depart"
            detail = dest or ""
        elif mtype == "arrive":
            action = "Arrive at destination"
            detail = ""
        elif mtype == "off ramp":
            action = f"Exit {exits}" if exits else f"Take exit toward {dest.split(':')[0]}" if dest else "Take exit"
            detail = dest
        elif mtype in ("merge", "on ramp"):
            action = f"Merge onto {hwy}"
            detail = f"toward {dest}" if dest else ""
        elif mtype == "fork":
            action = f"Keep {mod} for {hwy}"
            detail = f"toward {dest}" if dest else ""
        elif mtype == "end of road":
            action = f"Turn {mod} onto {hwy}"
            detail = dest
        else:
            action = f"Continue on {hwy}"
            detail = f"toward {dest}" if dest else ""

        segments.append({
            "highway": hwy,
            "action":  action,
            "detail":  detail,
            "dist_mi": dist,
            "dur_min": dur,
            "exit":    exits,
            "loc":     loc,
        })

    return segments


def parse_kml(raw, bbox):
    incidents = []
    try:
        root = ET.fromstring(raw)
        ns = root.tag[:root.tag.index("}") + 1] if root.tag.startswith("{") else ""
        for pm in root.iter(f"{ns}Placemark"):
            ne = pm.find(f"{ns}name")
            de = pm.find(f"{ns}description")
            ce = pm.find(f".//{ns}coordinates")
            if ce is None:
                continue
            parts = ce.text.strip().split(",")
            if len(parts) < 2:
                continue
            try:
                lng, lat = float(parts[0]), float(parts[1])
            except ValueError:
                continue
            if (bbox["min_lat"] <= lat <= bbox["max_lat"] and
                    bbox["min_lng"] <= lng <= bbox["max_lng"]):
                incidents.append({
                    "name": (ne.text or "").strip() if ne is not None else "Incident",
                    "desc": (de.text or "").strip() if de is not None else "",
                    "lat": lat, "lng": lng,
                })
    except ET.ParseError:
        pass
    return incidents


def build_route(olat, olng, dlat, dlng):
    osrm = fetch_osrm(olat, olng, dlat, dlng)
    if osrm:
        route    = osrm["routes"][0]
        dist_mi  = round(route["distance"] / 1609.34, 1)
        dur_min  = round(route["duration"] / 60)
        h, m     = divmod(dur_min, 60)
        dur_str  = f"{h}h {m}m" if h else f"{m}m"
        geometry = route["geometry"]
        steps    = [s for leg in route.get("legs", []) for s in leg.get("steps", [])]
        segments = derive_segments(steps)
        osrm_ok  = True
    else:
        dist_mi  = "—"
        dur_str  = "—"
        geometry = {"type": "LineString", "coordinates": [[olng, olat], [dlng, dlat]]}
        segments = []
        osrm_ok  = False

    pad  = 0.4
    bbox = {
        "min_lat": min(olat, dlat) - pad, "max_lat": max(olat, dlat) + pad,
        "min_lng": min(olng, dlng) - pad, "max_lng": max(olng, dlng) + pad,
    }
    kml_raw   = fetch(CALTRANS_KML)
    incidents = parse_kml(kml_raw, bbox) if kml_raw else []

    return {
        "dist_mi":       dist_mi,
        "dur_str":       dur_str,
        "osrm_live":     osrm_ok,
        "caltrans_live": bool(incidents),
        "segments":      segments,
        "incidents":     incidents,
        "geometry":      geometry,
        "origin_coords": [olat, olng],
        "dest_coords":   [dlat, dlng],
    }


# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Bay Area → Lake Tahoe Route</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; overflow: hidden; }

  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: #0f0f0f;
    color: #ddd;
    display: flex;
    flex-direction: column;
    height: 100vh;
  }

  /* ── Header ── */
  header {
    background: #141414;
    border-bottom: 1px solid #222;
    padding: 12px 18px;
    display: flex;
    align-items: center;
    gap: 10px;
    flex-shrink: 0;
  }
  header h1 { font-size: 0.95rem; font-weight: 600; color: #fff; flex: 1; }
  header .sub { font-size: 0.75rem; color: #aaa; }

  /* ── Layout ── */
  .main { display: flex; flex: 1; overflow: hidden; }

  /* ── Side panel ── */
  .panel {
    width: 360px;
    flex-shrink: 0;
    background: #111;
    border-right: 1px solid #1e1e1e;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  .panel-scroll {
    flex: 1;
    overflow-y: auto;
    padding: 14px 16px;
  }
  .panel-scroll::-webkit-scrollbar { width: 4px; }
  .panel-scroll::-webkit-scrollbar-thumb { background: #2a2a2a; border-radius: 2px; }

  /* ── Address form ── */
  .addr-block { margin-bottom: 12px; }

  .addr-label {
    font-size: 0.68rem;
    font-weight: 700;
    letter-spacing: 0.07em;
    text-transform: uppercase;
    color: #aaa;
    margin-bottom: 5px;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .badge {
    font-size: 0.6rem;
    background: #1e2e1e;
    color: #4ade80;
    border-radius: 4px;
    padding: 1px 5px;
    font-weight: 600;
    letter-spacing: 0;
  }
  .badge.tahoe { background: #1e2040; color: #60a5fa; }

  .addr-wrap { position: relative; }

  .addr-input {
    width: 100%;
    padding: 9px 12px;
    background: #1a1a1a;
    border: 1px solid #2a2a2a;
    border-radius: 8px;
    color: #e0e0e0;
    font-size: 0.88rem;
    outline: none;
    transition: border-color 0.15s;
  }
  .addr-input::placeholder { color: #666; }
  .addr-input:focus { border-color: #3a3a3a; }
  .addr-input.valid   { border-color: #2a4a2a; }
  .addr-input.invalid { border-color: #4a2a2a; }

  /* ── Suggestions dropdown ── */
  .suggestions {
    display: none;
    position: absolute;
    top: calc(100% + 4px);
    left: 0; right: 0;
    background: #1c1c1c;
    border: 1px solid #2a2a2a;
    border-radius: 8px;
    overflow: hidden;
    z-index: 1000;
    box-shadow: 0 8px 24px rgba(0,0,0,0.5);
    max-height: 220px;
    overflow-y: auto;
  }
  .sug-item {
    padding: 9px 12px;
    cursor: pointer;
    border-bottom: 1px solid #222;
    transition: background 0.1s;
  }
  .sug-item:last-child { border-bottom: none; }
  .sug-item:hover, .sug-item.active { background: #252525; }
  .sug-item.sug-outside { opacity: 0.45; }
  .sug-main { display: block; font-size: 0.83rem; color: #fff; }
  .sug-sub  { display: block; font-size: 0.72rem; color: #aaa; margin-top: 2px; }
  .sug-tag  {
    font-size: 0.62rem;
    background: #3a1a1a;
    color: #f87171;
    border-radius: 3px;
    padding: 1px 5px;
    margin-left: 6px;
    vertical-align: middle;
  }
  .sug-empty { padding: 10px 12px; font-size: 0.8rem; color: #aaa; }

  /* ── Validation status ── */
  .addr-status {
    font-size: 0.72rem;
    margin-top: 4px;
    min-height: 16px;
    padding-left: 2px;
    color: #aaa;
  }
  .addr-status.ok       { color: #4ade80; }
  .addr-status.error    { color: #f87171; }
  .addr-status.searching { color: #aaa; }

  .addr-hint {
    font-size: 0.68rem;
    color: #888;
    margin-top: 3px;
    line-height: 1.4;
    padding-left: 2px;
  }

  /* ── Calculate button ── */
  .calc-row {
    margin-top: 14px;
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .btn-calc {
    flex: 1;
    padding: 10px;
    background: #fff;
    color: #000;
    border: none;
    border-radius: 8px;
    font-size: 0.9rem;
    font-weight: 600;
    cursor: pointer;
    transition: opacity 0.15s;
  }
  .btn-calc:hover:not(:disabled) { opacity: 0.85; }
  .btn-calc:disabled { opacity: 0.3; cursor: default; }

  .spinner {
    display: none;
    width: 16px; height: 16px;
    border: 2px solid #333;
    border-top-color: #aaa;
    border-radius: 50%;
    animation: spin 0.65s linear infinite;
    flex-shrink: 0;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* ── Divider ── */
  .divider {
    height: 1px;
    background: #1e1e1e;
    margin: 14px 0;
  }

  /* ── Summary ── */
  .summary {
    display: none;
    background: #0f1f0f;
    border: 1px solid #1e3a1e;
    border-radius: 8px;
    padding: 12px 14px;
    margin-bottom: 14px;
  }
  .summary.show { display: block; }
  .sum-row { display: flex; gap: 28px; margin-bottom: 8px; }
  .sum-val   { font-size: 1.5rem; font-weight: 700; color: #4ade80; line-height: 1; }
  .sum-lbl   { font-size: 0.65rem; color: #aaa; text-transform: uppercase; letter-spacing: 0.07em; margin-top: 3px; }
  .sum-meta  { font-size: 0.71rem; color: #bbb; line-height: 1.6; }
  .dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%; margin-right: 4px; vertical-align: middle; }
  .dot.on  { background: #4ade80; }
  .dot.off { background: #3a3a3a; }

  /* ── Section label ── */
  .sec { font-size: 0.67rem; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; color: #888; margin: 14px 0 8px; }

  /* ── Segments ── */
  .seg-list { list-style: none; }
  .seg {
    display: flex;
    gap: 10px;
    padding: 9px 0;
    border-bottom: 1px solid #191919;
  }
  .seg:last-child { border-bottom: none; }

  .seg-icon {
    width: 30px; height: 30px;
    border-radius: 6px;
    background: #162030;
    display: flex; align-items: center; justify-content: center;
    font-size: 0.62rem; font-weight: 700;
    color: #60a5fa;
    flex-shrink: 0;
    margin-top: 1px;
    text-align: center;
    line-height: 1.2;
    padding: 2px;
  }
  .seg-icon.arrive { background: #102010; color: #4ade80; }
  .seg-icon.exit   { background: #201a08; color: #fbbf24; }

  .seg-body { flex: 1; }
  .seg-hwy    { font-size: 0.85rem; font-weight: 600; color: #fff; }
  .seg-action { font-size: 0.74rem; color: #ccc; margin-top: 2px; line-height: 1.35; }
  .seg-meta   { font-size: 0.68rem; color: #999; margin-top: 2px; }

  /* ── Incidents ── */
  .incident {
    background: #1a1208;
    border: 1px solid #2e2208;
    border-radius: 6px;
    padding: 8px 11px;
    margin-bottom: 6px;
  }
  .inc-name { font-size: 0.8rem; font-weight: 600; color: #fbbf24; }
  .inc-desc { font-size: 0.72rem; color: #ccc; margin-top: 2px; line-height: 1.35; }
  .no-inc   { font-size: 0.8rem; color: #888; padding: 4px 0; }

  /* ── Error ── */
  .err-bar {
    display: none;
    background: #1e1010;
    color: #f87171;
    font-size: 0.8rem;
    border-radius: 6px;
    padding: 8px 12px;
    margin-bottom: 12px;
  }

  /* ── Map ── */
  #map { flex: 1; background: #1a1a1a; }
</style>
</head>
<body>

<header>
  <h1>Route Calculator</h1>
  <span class="sub">Bay Area → Lake Tahoe · Live routing + Caltrans data</span>
</header>

<div class="main">

  <!-- ── Left panel ── -->
  <div class="panel">
    <div class="panel-scroll">

      <!-- Origin -->
      <div class="addr-block">
        <div class="addr-label">
          Origin <span class="badge">Bay Area</span>
        </div>
        <div class="addr-wrap">
          <input class="addr-input" id="origin-input"
                 placeholder="e.g. Cupertino Memorial Park, Cupertino"
                 autocomplete="off" spellcheck="false">
          <div class="suggestions" id="origin-sugg"></div>
        </div>
        <div class="addr-status" id="origin-status"></div>
        <div class="addr-hint" id="origin-hint">
          Alameda · Contra Costa · Marin · Napa · San Francisco · San Mateo · Santa Clara · Solano · Sonoma
        </div>
      </div>

      <!-- Destination -->
      <div class="addr-block">
        <div class="addr-label">
          Destination <span class="badge tahoe">Lake Tahoe</span>
        </div>
        <div class="addr-wrap">
          <input class="addr-input" id="dest-input"
                 placeholder="e.g. Granlibakken Tahoe, Tahoe City"
                 autocomplete="off" spellcheck="false">
          <div class="suggestions" id="dest-sugg"></div>
        </div>
        <div class="addr-status" id="dest-status"></div>
        <div class="addr-hint" id="dest-hint">
          El Dorado · Placer · Nevada · Alpine (CA) · Douglas · Washoe (NV)
        </div>
      </div>

      <div class="calc-row">
        <button class="btn-calc" id="calc-btn" disabled onclick="calculate()">Calculate</button>
        <div class="spinner" id="spinner"></div>
      </div>

      <div class="err-bar" id="err"></div>

      <!-- Results (hidden until calc) -->
      <div id="results" style="display:none">
        <div class="divider"></div>

        <div class="summary" id="summary">
          <div class="sum-row">
            <div><div class="sum-val" id="s-dist">—</div><div class="sum-lbl">miles</div></div>
            <div><div class="sum-val" id="s-dur">—</div><div class="sum-lbl">est. drive time</div></div>
          </div>
          <div class="sum-meta" id="s-meta"></div>
        </div>

        <div class="sec">Key Highways &amp; Exits</div>
        <ul class="seg-list" id="seg-list"></ul>

        <div class="sec">Caltrans — Active Lane Closures on Corridor</div>
        <div id="incidents"></div>
      </div>

    </div>
  </div>

  <!-- ── Map ── -->
  <div id="map"></div>
</div>

<script>
// ── Map setup ────────────────────────────────────────────────────────────────
const map = L.map('map').setView([38.3, -121.2], 7);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '© OpenStreetMap contributors', maxZoom: 18
}).addTo(map);

let routeLayer = null, incidentLayer = null;
let originMarker = null, destMarker = null;

function mkIcon(color, glow) {
  return L.divIcon({
    html: `<div style="width:13px;height:13px;background:${color};border:2px solid #fff;border-radius:50%;box-shadow:0 0 7px ${glow}"></div>`,
    iconSize: [13,13], iconAnchor: [6,6], className: ''
  });
}
const incIcon = L.divIcon({
  html: '<div style="width:9px;height:9px;background:#fbbf24;border:2px solid #fff;border-radius:50%"></div>',
  iconSize: [9,9], iconAnchor: [4,4], className: ''
});

// ── Address autocomplete ──────────────────────────────────────────────────────
class AddrInput {
  constructor(inputId, suggId, statusId, type) {
    this.el     = document.getElementById(inputId);
    this.sugg   = document.getElementById(suggId);
    this.status = document.getElementById(statusId);
    this.type   = type;
    this.sel    = null;
    this._timer = null;
    this._idx   = -1;

    this.el.addEventListener('input',  () => this._onInput());
    this.el.addEventListener('keydown', e => this._onKey(e));
    this.el.addEventListener('focus',  () => { if (this.sugg.children.length) this.sugg.style.display = 'block'; });
    this.el.addEventListener('blur',   () => setTimeout(() => this.sugg.style.display = 'none', 180));
  }

  _onInput() {
    this.sel = null;
    this.el.classList.remove('valid','invalid');
    updateBtn();
    clearTimeout(this._timer);
    const q = this.el.value.trim();
    if (q.length < 3) { this.sugg.style.display = 'none'; this._setStatus(''); return; }
    this._setStatus('Searching…', 'searching');
    this._timer = setTimeout(() => this._search(q), 310);
  }

  async _search(q) {
    try {
      const r = await fetch(`/api/geocode?q=${encodeURIComponent(q)}&type=${this.type}`);
      const d = await r.json();
      this._render(d);
    } catch { this._setStatus('Search unavailable', 'error'); }
  }

  _render(results) {
    this.sugg.innerHTML = '';
    this._idx = -1;
    if (!results.length) {
      this.sugg.innerHTML = '<div class="sug-empty">No results in this area</div>';
      this.sugg.style.display = 'block';
      this._setStatus('No addresses found in allowed area', 'error');
      return;
    }
    results.forEach(r => {
      const div = document.createElement('div');
      div.className = 'sug-item' + (r.valid ? '' : ' sug-outside');
      const tag = r.valid ? '' : '<span class="sug-tag">outside area</span>';
      const sub = [r.county, r.state].filter(Boolean).join(', ');
      div.innerHTML = `<span class="sug-main">${esc(r.short)}${tag}</span><span class="sug-sub">${esc(sub)}</span>`;
      div.addEventListener('mousedown', () => this._select(r));
      this.sugg.appendChild(div);
    });
    this.sugg.style.display = 'block';
    this._setStatus('');
  }

  _select(r) {
    this.el.value = r.short;
    this.sugg.style.display = 'none';
    if (r.valid) {
      this.sel = r;
      this.el.classList.add('valid'); this.el.classList.remove('invalid');
      const loc = [r.county, r.state].filter(Boolean).join(', ');
      this._setStatus('✓ ' + loc, 'ok');
      // Preview marker on map
      this._placeMarker(r.lat, r.lng);
    } else {
      this.sel = null;
      this.el.classList.add('invalid'); this.el.classList.remove('valid');
      this._setStatus('Address is outside the allowed area', 'error');
    }
    updateBtn();
  }

  _placeMarker(lat, lng) {
    if (this.type === 'origin') {
      if (originMarker) map.removeLayer(originMarker);
      originMarker = L.marker([lat, lng], {icon: mkIcon('#4ade80','#4ade8099')})
        .bindPopup('<b>Origin</b>').addTo(map);
      map.setView([lat, lng], 12);
    } else {
      if (destMarker) map.removeLayer(destMarker);
      destMarker = L.marker([lat, lng], {icon: mkIcon('#60a5fa','#60a5fa99')})
        .bindPopup('<b>Destination</b>').addTo(map);
    }
  }

  _setStatus(msg, cls = '') {
    this.status.textContent = msg;
    this.status.className = 'addr-status ' + cls;
  }

  _onKey(e) {
    const items = [...this.sugg.querySelectorAll('.sug-item')];
    if (!items.length) return;
    if (e.key === 'ArrowDown')  { e.preventDefault(); this._idx = Math.min(this._idx+1, items.length-1); }
    else if (e.key === 'ArrowUp')   { e.preventDefault(); this._idx = Math.max(this._idx-1, 0); }
    else if (e.key === 'Escape')    { this.sugg.style.display = 'none'; return; }
    else if (e.key === 'Enter' && this._idx >= 0) { items[this._idx].dispatchEvent(new Event('mousedown')); return; }
    else return;
    items.forEach((el, i) => el.classList.toggle('active', i === this._idx));
  }

  get isValid() { return this.sel !== null; }
  get coords()  { return this.sel ? {lat: this.sel.lat, lng: this.sel.lng} : null; }
}

const originInput = new AddrInput('origin-input', 'origin-sugg', 'origin-status', 'origin');
const destInput   = new AddrInput('dest-input',   'dest-sugg',   'dest-status',   'dest');

function updateBtn() {
  document.getElementById('calc-btn').disabled = !(originInput.isValid && destInput.isValid);
}

// ── Calculate ─────────────────────────────────────────────────────────────────
async function calculate() {
  const oc = originInput.coords, dc = destInput.coords;
  if (!oc || !dc) return;

  document.getElementById('err').style.display = 'none';
  document.getElementById('spinner').style.display = 'block';
  document.getElementById('calc-btn').disabled = true;
  document.getElementById('calc-btn').textContent = 'Loading…';

  try {
    const url = `/api/route?olat=${oc.lat}&olng=${oc.lng}&dlat=${dc.lat}&dlng=${dc.lng}`;
    const res = await fetch(url);
    if (!res.ok) throw new Error('Server error ' + res.status);
    const d = await res.json();

    // Summary
    document.getElementById('s-dist').textContent = d.dist_mi;
    document.getElementById('s-dur').textContent  = d.dur_str;
    document.getElementById('s-meta').innerHTML =
      `<span class="dot ${d.osrm_live ? 'on':'off'}"></span>${d.osrm_live ? 'Live routing (OSRM)' : 'Routing unavailable'}&nbsp;&nbsp;` +
      `<span class="dot ${d.caltrans_live ? 'on':'off'}"></span>${d.caltrans_live ? d.incidents.length + ' closure(s) from Caltrans QuickMap' : 'Caltrans feed unavailable'}`;
    document.getElementById('summary').classList.add('show');

    // Route on map
    if (routeLayer)   map.removeLayer(routeLayer);
    if (incidentLayer) map.removeLayer(incidentLayer);
    routeLayer = L.geoJSON(d.geometry, { style: {color:'#60a5fa', weight:4, opacity:0.85} }).addTo(map);
    map.fitBounds(routeLayer.getBounds(), {padding:[28,28]});

    // Re-place markers on top
    if (originMarker) { map.removeLayer(originMarker); }
    if (destMarker)   { map.removeLayer(destMarker); }
    originMarker = L.marker(d.origin_coords, {icon: mkIcon('#4ade80','#4ade8099')})
      .bindPopup(`<b>Origin</b><br>${esc(originInput.el.value)}`).addTo(map);
    destMarker = L.marker(d.dest_coords, {icon: mkIcon('#60a5fa','#60a5fa99')})
      .bindPopup(`<b>Destination</b><br>${esc(destInput.el.value)}`).addTo(map);

    // Incident markers
    incidentLayer = L.layerGroup();
    d.incidents.forEach(inc =>
      L.marker([inc.lat, inc.lng], {icon: incIcon})
        .bindPopup(`<b>${esc(inc.name)}</b><br>${esc(inc.desc)}`)
        .addTo(incidentLayer));
    incidentLayer.addTo(map);

    // Segments
    const segList = document.getElementById('seg-list');
    segList.innerHTML = '';
    (d.segments || []).forEach(seg => {
      const hwy = seg.highway || '';
      const m   = hwy.match(/(?:I|US|CA|SR|HWY)-?\d+[A-Z]?/);
      const lbl = m ? m[0].replace('-',' ') : hwy.slice(0,7);
      const cls = seg.action.startsWith('Arrive') ? 'arrive' : (seg.exit ? 'exit' : '');
      const meta = [
        seg.dist_mi > 0 ? seg.dist_mi + ' mi' : '',
        seg.dur_min > 0 ? seg.dur_min + ' min' : '',
      ].filter(Boolean).join(' · ');
      const li = document.createElement('li');
      li.className = 'seg';
      li.innerHTML = `
        <div class="seg-icon ${cls}">${esc(lbl)}</div>
        <div class="seg-body">
          <div class="seg-hwy">${esc(seg.highway)}</div>
          <div class="seg-action">${esc(seg.action)}</div>
          ${seg.detail ? `<div class="seg-meta">${esc(seg.detail)}</div>` : ''}
          ${meta ? `<div class="seg-meta">${meta}</div>` : ''}
        </div>`;
      segList.appendChild(li);
    });

    // Incidents
    const incDiv = document.getElementById('incidents');
    if (!d.incidents.length) {
      incDiv.innerHTML = '<div class="no-inc">No active lane closures on this corridor.</div>';
    } else {
      incDiv.innerHTML = d.incidents.map(inc =>
        `<div class="incident"><div class="inc-name">${esc(inc.name)}</div>` +
        `<div class="inc-desc">${esc(inc.desc) || 'Lane closure in effect.'}</div></div>`
      ).join('');
    }

    document.getElementById('results').style.display = 'block';

  } catch(e) {
    const eb = document.getElementById('err');
    eb.textContent = 'Could not load route: ' + e.message;
    eb.style.display = 'block';
  } finally {
    document.getElementById('spinner').style.display = 'none';
    document.getElementById('calc-btn').disabled = false;
    document.getElementById('calc-btn').textContent = 'Calculate';
  }
}

function esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
</script>
</body>
</html>
"""


# ── HTTP server ────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def _json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path
        qs     = parse_qs(parsed.query)

        if path == "/":
            self._html(HTML)

        elif path == "/api/geocode":
            q    = qs.get("q", [""])[0].strip()
            kind = qs.get("type", ["origin"])[0]
            if len(q) < 2:
                self._json(400, {"error": "query too short"})
                return
            self._json(200, geocode(q, kind))

        elif path == "/api/route":
            try:
                olat = float(qs["olat"][0])
                olng = float(qs["olng"][0])
                dlat = float(qs["dlat"][0])
                dlng = float(qs["dlng"][0])
            except (KeyError, ValueError):
                self._json(400, {"error": "missing coords"})
                return
            self._json(200, build_route(olat, olng, dlat, dlng))

        else:
            self._json(404, {"error": "not found"})


def main():
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Route Calculator -> http://localhost:{PORT}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()
