import json
import math
import os
import requests
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request, session
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv

load_dotenv()

try:
    from twilio.rest import Client
except ImportError:
    Client = None

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SAFETY_POINTS_PATH = DATA_DIR / "safety_points.json"
USER_REPORTS_PATH  = DATA_DIR / "user_reports.jsonl"

FAST2SMS_API_KEY = os.getenv("FAST2SMS_API_KEY", "")
TOMTOM_API_KEY   = os.getenv("TOMTOM_API_KEY", "OfDU2Qgiw5VbIld0HdbAaJ9xNnWYTE0w")

_ROUTES_CACHE:  dict = {}
_TRAFFIC_CACHE: dict = {}
_CACHE_TTL_S         = 60
_TRAFFIC_CACHE_TTL_S = 120   # 2 min

DEFAULT_SAFETY_WEIGHTS = {
    "street_lighting":  0.25,
    "crowd_density":    0.15,
    "police_proximity": 0.10,
    "cctv_coverage":    0.10,
    "road_visibility":  0.10,
    "traffic_density":  0.10,
    "crime_rate":       0.15,
    "incident_reports": 0.05,
}

SAFETY_PERCENT_THRESHOLDS = {"safe": 70.0, "moderate": 40.0}

try:
    import pymysql  # noqa: F401
    SQLALCHEMY_DATABASE_URI = "mysql+pymysql://root:Qazqaz12%23@localhost/saferoute"
except ImportError:
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{BASE_DIR / 'saferoute.db'}"

db = SQLAlchemy()


class EmergencyContact(db.Model):
    __tablename__ = "emergency_contacts"
    id           = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id      = db.Column(db.Integer, nullable=False, default=1)
    contact_name = db.Column(db.String(100), nullable=True)
    phone        = db.Column(db.String(15), nullable=False)
    created_at   = db.Column(db.DateTime, server_default=db.func.now())


# ══════════════════════════════════════════════════════════════
# Generic helpers
# ══════════════════════════════════════════════════════════════

def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _kmh_to_ms(kmh: float) -> float:
    return max(0.1, float(kmh) / 3.6)


def _http_get_json(url: str, headers: dict | None = None, timeout_s: int = 12):
    hdrs = {"User-Agent": "Mozilla/5.0 SafeRoute/1.0", "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    r = requests.get(url, headers=hdrs, timeout=timeout_s)
    r.raise_for_status()
    return r.json()


# ══════════════════════════════════════════════════════════════
# Safety scoring
# ══════════════════════════════════════════════════════════════

def _safety_point_score(p: dict, weights: dict | None = None) -> float:
    lighting   = float(p.get("street_lighting",  5))
    crowd      = float(p.get("crowd_density",    5))
    crime      = float(p.get("crime_rate",       5))
    police     = float(p.get("police_proximity", 5))
    cctv       = float(p.get("cctv_coverage",    5))
    visibility = float(p.get("road_visibility",  5))
    traffic    = float(p.get("traffic_density",  5))
    incidents  = float(p.get("incident_reports", 3))
    w = dict(DEFAULT_SAFETY_WEIGHTS)
    if weights:
        for k, v in weights.items():
            if k in w:
                try: w[k] = float(v)
                except: pass
    return float(
        w["street_lighting"]   * lighting
        + w["crowd_density"]   * crowd
        + w["police_proximity"]* police
        + w["cctv_coverage"]   * cctv
        + w["road_visibility"] * visibility
        + w["traffic_density"] * traffic
        - w["crime_rate"]      * crime
        - w["incident_reports"]* incidents
    )


def _normalize_safety_percent(raw: float) -> float:
    return float(_clamp((raw - (-1.2)) / (7.85 - (-1.2)) * 100.0, 0.0, 100.0))


def _zone_label_from_percent(pct: float) -> str:
    if pct >= SAFETY_PERCENT_THRESHOLDS["safe"]:     return "safe"
    if pct >= SAFETY_PERCENT_THRESHOLDS["moderate"]: return "moderate"
    return "unsafe"


# ══════════════════════════════════════════════════════════════
# Road-type speeds  (India-realistic km/h)
# ══════════════════════════════════════════════════════════════

# OSM highway tag → internal road class
_OSM_CLASS_MAP = {
    "motorway": "motorway", "motorway_link": "motorway",
    "trunk": "trunk",       "trunk_link": "trunk",
    "primary": "primary",   "primary_link": "primary",
    "secondary": "secondary","secondary_link": "secondary",
    "tertiary": "tertiary", "tertiary_link": "tertiary",
    "residential": "residential", "living_street": "residential",
    "unclassified": "residential", "service": "service",
    "track": "track",       "path": "track",
}

# Free-flow speeds per road class per mode (km/h)
_FREE_FLOW_KMH: dict[str, dict[str, float]] = {
    #               car   truck  bike  walk
    "motorway":  {"car":100, "truck":70,  "bike": 0,  "walk": 0 },
    "trunk":     {"car": 80, "truck":60,  "bike":25,  "walk": 0 },
    "primary":   {"car": 60, "truck":45,  "bike":22,  "walk": 5 },
    "secondary": {"car": 50, "truck":35,  "bike":20,  "walk": 5 },
    "tertiary":  {"car": 40, "truck":30,  "bike":18,  "walk": 5 },
    "residential":{"car":25, "truck":20,  "bike":15,  "walk": 5 },
    "service":   {"car": 15, "truck":10,  "bike":10,  "walk": 5 },
    "track":     {"car": 15, "truck":10,  "bike": 8,  "walk": 4 },  # kachha
}
_GHAT_SPEEDS = {"car": 25, "truck": 15, "bike": 10, "walk": 3}


def _osm_road_class(step: dict) -> str:
    name     = (step.get("name") or "").lower()
    ref      = (step.get("ref")  or "").lower()
    combined = name + " " + ref

    if "ghat" in combined:
        return "ghat"

    # OSRM intersection classes
    for inter in (step.get("intersections") or []):
        for cls in (inter.get("classes") or []):
            mapped = _OSM_CLASS_MAP.get(cls.lower())
            if mapped:
                return mapped

    # Name-based heuristics
    if any(x in combined for x in ("nh ", "nh-", "national highway", "expressway")):
        return "trunk"
    if any(x in combined for x in ("sh ", "sh-", "state highway")):
        return "primary"
    if any(x in combined for x in ("highway", "motorway")):
        return "motorway"
    if any(x in combined for x in ("track", "dirt", "kachha", "kutcha")):
        return "track"
    return "residential"


def _extract_road_segments(osrm_route: dict) -> list[dict]:
    segments = []
    for leg in (osrm_route.get("legs") or []):
        for step in (leg.get("steps") or []):
            dist = float(step.get("distance") or 0)
            if dist < 1:
                continue
            segments.append({"distance_m": dist, "road_class": _osm_road_class(step)})
    return segments


def _segments_to_seconds(segments: list[dict], mode: str,
                          traffic_factor: float = 1.0,
                          crowd_factor:   float = 1.0) -> float:
    total = 0.0
    for seg in segments:
        dist_m = seg["distance_m"]
        rclass = seg["road_class"]
        speed_kmh = (_GHAT_SPEEDS.get(mode, 10.0) if rclass == "ghat"
                     else _FREE_FLOW_KMH.get(rclass, _FREE_FLOW_KMH["residential"]).get(mode, 5.0))
        speed_kmh = max(1.0, speed_kmh)
        speed_ms  = _kmh_to_ms(speed_kmh)

        if mode in ("car", "truck"):
            speed_ms /= (traffic_factor * crowd_factor)
        elif mode == "bike":
            speed_ms /= max(1.0, crowd_factor * 0.6)

        speed_ms = max(0.3, speed_ms)
        total   += dist_m / speed_ms
    return float(total)


# ══════════════════════════════════════════════════════════════
# TomTom live traffic
# ══════════════════════════════════════════════════════════════

def _tomtom_traffic_factor(mid_lat: float, mid_lng: float) -> float:
    key = f"{round(mid_lat,3)}:{round(mid_lng,3)}"
    cached = _TRAFFIC_CACHE.get(key)
    if cached:
        ts, val = cached
        if (datetime.now(tz=timezone.utc).timestamp() - ts) < _TRAFFIC_CACHE_TTL_S:
            return val

    if not TOMTOM_API_KEY:
        return 1.0
    try:
        url = (f"https://api.tomtom.com/traffic/services/4/flowSegmentData"
               f"/relative0/10/json?point={mid_lat},{mid_lng}"
               f"&key={TOMTOM_API_KEY}&unit=KMPH")
        data    = _http_get_json(url, timeout_s=5)
        flow    = data.get("flowSegmentData") or {}
        cur     = float(flow.get("currentSpeed",  0) or 0)
        free    = float(flow.get("freeFlowSpeed", 0) or 0)
        if cur > 0 and free > 0:
            factor = _clamp(free / cur, 1.0, 4.0)
            print(f"[TomTom] ✅ factor={factor:.2f} (cur={cur}kmh, free={free}kmh)")
            _TRAFFIC_CACHE[key] = (datetime.now(tz=timezone.utc).timestamp(), factor)
            return factor
    except Exception as e:
        print(f"[TomTom] ⚠️  {type(e).__name__}: {str(e)[:80]}")
    return 1.0


# ══════════════════════════════════════════════════════════════
# Crowd density model  (area-type × hour-of-day)
# ══════════════════════════════════════════════════════════════

_AREA_CROWD_BASE = {
    "market":     0.90, "bazaar":    0.90,
    "bus_stand":  0.85, "railway":   0.85,
    "hospital":   0.70, "school":    0.75,
    "college":    0.75, "temple":    0.70,
    "mosque":     0.70, "church":    0.60,
    "park":       0.50, "residential":0.40,
    "industrial": 0.30, "highway":   0.20,
    "default":    0.45,
}


def _hour_crowd_mult(hour_ist: int) -> float:
    if  0 <= hour_ist <  5: return 0.10
    if  5 <= hour_ist <  6: return 0.20
    if  6 <= hour_ist <  7: return 0.50
    if  7 <= hour_ist <  9: return 0.90   # morning rush
    if  9 <= hour_ist < 11: return 0.75
    if 11 <= hour_ist < 13: return 0.80
    if 13 <= hour_ist < 15: return 0.65   # lunch lull
    if 15 <= hour_ist < 17: return 0.75
    if 17 <= hour_ist < 20: return 1.00   # evening peak
    if 20 <= hour_ist < 22: return 0.80
    return 0.35


def _detect_area_type(pts: list) -> str:
    kw = {
        "market":     ["market","bazaar","shopping","mall"],
        "bus_stand":  ["bus","stand","depot","isbt"],
        "railway":    ["railway","station","junction","metro"],
        "hospital":   ["hospital","clinic","medical"],
        "school":     ["school","high school"],
        "college":    ["college","university","institute","iit","nit"],
        "temple":     ["temple","mandir"],
        "mosque":     ["mosque","masjid","dargah"],
        "park":       ["park","garden","lake"],
        "industrial": ["industrial","factory","warehouse"],
    }
    text = " ".join((p.get("area") or p.get("name") or "").lower() for p in pts)
    for atype, words in kw.items():
        if any(w in text for w in words):
            return atype
    return "default"


def _crowd_factor(pts: list, departure_ts: float | None = None) -> float:
    now     = (datetime.now(timezone.utc) if departure_ts is None
               else datetime.fromtimestamp(departure_ts, tz=timezone.utc))
    ist_h   = ((now.hour + 5) % 24 + (1 if now.minute >= 30 else 0)) % 24
    weekday = now.weekday()
    atype   = _detect_area_type(pts)
    base    = _AREA_CROWD_BASE.get(atype, _AREA_CROWD_BASE["default"])
    hmult   = _hour_crowd_mult(ist_h)
    if weekday >= 5:
        hmult = min(1.0, hmult * 1.2) if atype in ("market","temple","park") else hmult * 0.3 if atype in ("school","college","industrial") else hmult
    level  = _clamp(base * hmult, 0.05, 1.0)
    factor = 1.0 + level * 0.6   # max +60% travel time due to crowd
    print(f"[Crowd]  area={atype}, ist_h={ist_h}, level={level:.2f}, factor={factor:.2f}")
    return float(factor)


# ══════════════════════════════════════════════════════════════
# Historical rush-hour multiplier (Indian cities, IST)
# ══════════════════════════════════════════════════════════════

def _historical_traffic_multiplier(departure_ts: float | None = None) -> float:
    now     = (datetime.now(timezone.utc) if departure_ts is None
               else datetime.fromtimestamp(departure_ts, tz=timezone.utc))
    ist_h   = ((now.hour + 5) % 24 + (1 if now.minute >= 30 else 0)) % 24
    weekday = now.weekday()
    if weekday < 5:   # weekday
        if  7 <= ist_h < 10: return 1.45
        if 10 <= ist_h < 12: return 1.15
        if 12 <= ist_h < 14: return 1.20
        if 14 <= ist_h < 17: return 1.10
        if 17 <= ist_h < 20: return 1.50   # heaviest
        if 20 <= ist_h < 22: return 1.25
        if  0 <= ist_h <  5: return 0.85
        return 1.0
    # weekend
    if 10 <= ist_h < 20: return 1.15
    if  0 <= ist_h <  5: return 0.85
    return 1.0


# ══════════════════════════════════════════════════════════════
# Master ETA calculator
# ══════════════════════════════════════════════════════════════

def _estimate_route_durations(osrm_route: dict,
                               safety_points_nearby: list,
                               departure_ts: float | None = None) -> dict:
    """
    Accurate per-mode ETAs:

    Strategy (calibrated to match Google Maps within ~10%):
    ─────────────────────────────────────────────────────
    • CAR / TRUCK:
        base = OSRM duration (already road-calibrated for Indian roads)
        × single traffic factor (TomTom live if available, else historical)
        NO crowd factor — crowd doesn't slow cars on roads
        truck = car × 1.15 (trucks slower than cars)

    • BIKE:
        base = distance / bike_speed_for_road_class
        × mild crowd factor (0.3 weight only, bikes weave through traffic)

    • WALK:
        base = distance / 5 km/h flat
        × crowd factor (pedestrians slowed by crowd significantly)

    This avoids the double/triple stacking that caused 2× overestimation.
    """
    osrm_car_s  = float(osrm_route.get("duration") or 0.0)
    total_dist_m = float(osrm_route.get("distance") or 0.0)

    # ── Traffic factor (single, not stacked) ──────────────────
    coords  = (osrm_route.get("geometry") or {}).get("coordinates") or []
    live_tf = 1.0
    if coords:
        mid     = coords[len(coords) // 2]
        live_tf = _tomtom_traffic_factor(mid[1], mid[0])

    hist = _historical_traffic_multiplier(departure_ts)

    # Use whichever gives more info:
    # live > 1.05 means TomTom returned real data → trust it more
    if live_tf > 1.05:
        # TomTom already reflects current conditions; hist is redundant
        # Use live as primary with a small hist blend for robustness
        traffic_factor = _clamp(live_tf * 0.85 + hist * 0.15, 0.85, 2.5)
    else:
        # No live data — historical only, but keep it mild
        # OSRM duration is already a good base; rush hour adds ~20-40%
        traffic_factor = _clamp(hist, 0.85, 1.5)

    print(f"[ETA]  live={live_tf:.2f}, hist={hist:.2f}, "
          f"traffic_factor={traffic_factor:.2f}")

    # ── CAR: OSRM base × traffic factor ───────────────────────
    car_s = osrm_car_s * traffic_factor

    # ── TRUCK: car + 15% (heavier, slower acceleration) ───────
    truck_s = car_s * 1.15

    # ── BIKE: road-type aware speeds, mild crowd impact ────────
    segments = _extract_road_segments(osrm_route)
    if not segments and total_dist_m > 0:
        segments = [{"distance_m": total_dist_m, "road_class": "secondary"}]

    crowd = _crowd_factor(safety_points_nearby, departure_ts)
    # Bikes only partially affected by crowd (they filter through traffic)
    bike_crowd = 1.0 + (crowd - 1.0) * 0.35

    bike_s = 0.0
    for seg in segments:
        dist_m    = seg["distance_m"]
        rclass    = seg["road_class"]
        speed_kmh = (_GHAT_SPEEDS.get("bike", 10.0) if rclass == "ghat"
                     else _FREE_FLOW_KMH.get(rclass, _FREE_FLOW_KMH["residential"]).get("bike", 12.0))
        speed_kmh = max(1.0, speed_kmh)
        speed_ms  = _kmh_to_ms(speed_kmh) / bike_crowd
        speed_ms  = max(0.5, speed_ms)
        bike_s   += dist_m / speed_ms

    # ── WALK: flat 5 km/h × full crowd factor ─────────────────
    walk_speed_ms = _kmh_to_ms(5.0) / crowd
    walk_speed_ms = max(0.5, walk_speed_ms)
    walk_s        = total_dist_m / walk_speed_ms if total_dist_m > 0 else 0.0

    # ── Road breakdown for display ─────────────────────────────
    rb: dict[str, float] = {}
    for seg in segments:
        rb[seg["road_class"]] = rb.get(seg["road_class"], 0.0) + seg["distance_m"]

    return {
        "car":               round(car_s,   1),
        "truck":             round(truck_s, 1),
        "bike":              round(bike_s,  1),
        "walk":              round(walk_s,  1),
        "_road_breakdown":   {k: round(v/1000, 2) for k, v in rb.items()},
        "_traffic_factor":   round(traffic_factor, 2),
        "_crowd_factor":     round(crowd, 2),
        "_live_traffic":     round(live_tf, 2),
    }


# ══════════════════════════════════════════════════════════════
# OSRM helpers
# ══════════════════════════════════════════════════════════════

_OSRM_MIRRORS = [
    "https://router.project-osrm.org/route/v1/driving/",
    "https://routing.openstreetmap.de/routed-car/route/v1/driving/",
]


def _osrm_routes(s_lat, s_lng, e_lat, e_lng, timeout_s=12):
    coords = f"{s_lng},{s_lat};{e_lng},{e_lat}"
    qs = urllib.parse.urlencode({
        "overview": "full", "geometries": "geojson",
        "alternatives": "true", "steps": "true",
    })
    last_err = None
    for mirror in _OSRM_MIRRORS:
        try:
            print(f"[OSRM] trying {mirror[:50]}...")
            data = _http_get_json(mirror + coords + "?" + qs, timeout_s=timeout_s)
            if data.get("code") == "Ok" and data.get("routes"):
                print(f"[OSRM] ✅ {len(data['routes'])} route(s)")
                return data
        except Exception as e:
            last_err = e
            print(f"[OSRM] ❌ {type(e).__name__}: {str(e)[:80]}")
    raise RuntimeError(f"All OSRM mirrors failed: {last_err}")


def _cache_key_for_route(s_lat, s_lng, e_lat, e_lng):
    return f"{round(s_lat,5)}:{round(s_lng,5)}->{round(e_lat,5)}:{round(e_lng,5)}"


def _cache_get(key):
    item = _ROUTES_CACHE.get(key)
    if not item: return None
    ts, val = item
    if (datetime.now(tz=timezone.utc).timestamp() - ts) > _CACHE_TTL_S:
        _ROUTES_CACHE.pop(key, None); return None
    return val


def _cache_set(key, value):
    _ROUTES_CACHE[key] = (datetime.now(tz=timezone.utc).timestamp(), value)


def _interpolate_route(a_lat, a_lng, b_lat, b_lng, n=80):
    n = max(2, int(n))
    return [[a_lng+(b_lng-a_lng)*i/(n-1), a_lat+(b_lat-a_lat)*i/(n-1)] for i in range(n)]


def _haversine_m(a_lat, a_lng, b_lat, b_lng) -> float:
    R    = 6_371_000
    dlat = math.radians(b_lat - a_lat)
    dlng = math.radians(b_lng - a_lng)
    a    = (math.sin(dlat/2)**2
            + math.cos(math.radians(a_lat))*math.cos(math.radians(b_lat))*math.sin(dlng/2)**2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


def _fallback_routes(s_lat, s_lng, e_lat, e_lng):
    ml, mg = (s_lat+e_lat)/2, (s_lng+e_lng)/2
    paths  = [
        _interpolate_route(s_lat, s_lng, e_lat, e_lng, n=90),
        (_interpolate_route(s_lat, s_lng, ml+0.003, mg-0.003, n=45)[:-1]
         + _interpolate_route(ml+0.003, mg-0.003, e_lat, e_lng, n=45)),
        (_interpolate_route(s_lat, s_lng, ml-0.003, mg+0.003, n=45)[:-1]
         + _interpolate_route(ml-0.003, mg+0.003, e_lat, e_lng, n=45)),
    ]
    out = []
    for cs in paths:
        dist = sum(_haversine_m(cs[i][1],cs[i][0],cs[i+1][1],cs[i+1][0])
                   for i in range(len(cs)-1))
        out.append({"distance":dist,"duration":dist/6.94 if dist>0 else 0,
                    "geometry":{"type":"LineString","coordinates":cs},"legs":[]})
    return out


# ══════════════════════════════════════════════════════════════
# Route proximity + safety scoring helpers
# ══════════════════════════════════════════════════════════════

def _meters_per_degree_lng(at_lat):
    return 111_320.0 * math.cos(math.radians(at_lat))


def _point_to_segment_distance_m(lat, lng, a_lat, a_lng, b_lat, b_lng):
    ref_lat  = (a_lat+b_lat)/2
    mx, my   = _meters_per_degree_lng(ref_lat), 111_320.0
    px, py   = lng*mx, lat*my
    ax, ay   = a_lng*mx, a_lat*my
    bx, by   = b_lng*mx, b_lat*my
    abx, aby = bx-ax, by-ay
    apx, apy = px-ax, py-ay
    ab2 = abx*abx+aby*aby
    if ab2 <= 1e-9: return math.hypot(px-ax, py-ay)
    t = _clamp((apx*abx+apy*aby)/ab2, 0.0, 1.0)
    return math.hypot(px-(ax+t*abx), py-(ay+t*aby))


def _route_nearby_points(route_coords, safety_points, max_distance_m=280.0, weights=None):
    if not route_coords or len(route_coords) < 2: return []
    step   = 2 if len(route_coords) > 400 else 1
    coords = route_coords[::step]
    if coords[-1] != route_coords[-1]: coords.append(route_coords[-1])
    segs   = [(coords[i][1],coords[i][0],coords[i+1][1],coords[i+1][0])
               for i in range(len(coords)-1)]
    nearby = []
    for p in safety_points:
        lat, lng = float(p.get("lat")), float(p.get("lng"))
        min_d = float("inf")
        for a_lat,a_lng,b_lat,b_lng in segs:
            d = _point_to_segment_distance_m(lat,lng,a_lat,a_lng,b_lat,b_lng)
            if d < min_d: min_d = d
            if min_d <= max_distance_m: break
        if min_d <= max_distance_m:
            p2  = dict(p)
            raw = _safety_point_score(p2, weights=weights)
            pct = _normalize_safety_percent(raw)
            p2["safety_raw"]          = round(raw, 4)
            p2["safety_percent"]      = round(pct, 1)
            p2["zone"]                = _zone_label_from_percent(pct)
            p2["distance_to_route_m"] = round(min_d, 1)
            nearby.append(p2)
    return nearby


def _route_safety_score(nearby, fallback_points=None):
    def _wavg(pts, denom):
        ws = wt = 0.0
        for p in pts:
            w  = 1.0/(1.0+float(p.get("distance_to_route_m",0))/denom)
            ws += float(p.get("safety_percent",50.0))*w
            wt += w
        return ws/wt if wt > 0 else None
    v = _wavg(nearby, 80.0) if nearby else None
    if v is not None: return v
    v = _wavg(fallback_points, 150.0) if fallback_points else None
    if v is not None: return v
    return 50.0


# ══════════════════════════════════════════════════════════════
# Flask factory
# ══════════════════════════════════════════════════════════════

def create_app():
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", SQLALCHEMY_DATABASE_URI)
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    with app.app_context():
        db.create_all()
    CORS(app)

    @app.route("/")
    def home():
        return render_template("dashboard.html")

    @app.route("/api/test")
    def test_api():
        return jsonify({"message": "SafeRoute API working"})

    @app.route("/api/safety_points")
    def safety_points():
        points = _read_json(SAFETY_POINTS_PATH, default=[])
        enriched = []
        for p in points:
            p2  = dict(p)
            raw = _safety_point_score(p2)
            pct = _normalize_safety_percent(raw)
            p2["safety_raw"]     = round(raw,4)
            p2["safety_percent"] = round(pct,1)
            p2["zone"]           = _zone_label_from_percent(pct)
            enriched.append(p2)
        return jsonify({"count": len(enriched), "points": enriched})

    # ── 3 labeled routes ──────────────────────────────────────
    @app.route("/api/routes", methods=["POST"])
    def routes():
        body  = request.get_json(silent=True) or {}
        start = body.get("start") or {}
        end   = body.get("end")   or {}
        try:
            s_lat = float(start["lat"]); s_lng = float(start["lng"])
            e_lat = float(end["lat"]);   e_lng = float(end["lng"])
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": "Invalid start/end coordinates"}), 400

        weights      = body.get("weights") or None
        max_dist_m   = float(body.get("max_distance_m", 280))
        departure_ts = body.get("departure_ts")
        sp           = _read_json(SAFETY_POINTS_PATH, default=[])

        # Fetch OSRM
        cache_key = _cache_key_for_route(s_lat, s_lng, e_lat, e_lng)
        raw_osrm  = _cache_get(cache_key)
        osrm_ok   = False
        if raw_osrm is None:
            try:
                data = _osrm_routes(s_lat, s_lng, e_lat, e_lng)
                if data.get("code") == "Ok" and data.get("routes"):
                    raw_osrm = data["routes"]
                    _cache_set(cache_key, raw_osrm)
                    osrm_ok = True
            except Exception:
                raw_osrm = None
        else:
            osrm_ok = True

        if not raw_osrm:
            print("[ROUTES] ⚠️  fallback")
            raw_osrm = _fallback_routes(s_lat, s_lng, e_lat, e_lng)
        else:
            print(f"[ROUTES] ✅ {len(raw_osrm)} OSRM route(s)")

        # Pad to 3 via waypoints
        if osrm_ok and len(raw_osrm) < 3:
            ml, mg = (s_lat+e_lat)/2, (s_lng+e_lng)/2
            dl, dg = e_lat-s_lat, e_lng-s_lng
            rlen   = math.hypot(dl, dg) or 1.0
            pl, pg = -dg/rlen, dl/rlen

            for off in [0.004, -0.004, 0.007, -0.007]:
                if len(raw_osrm) >= 3: break
                wl, wg = ml+pl*off, mg+pg*off
                for mb in _OSRM_MIRRORS:
                    if len(raw_osrm) >= 3: break
                    try:
                        vd = _http_get_json(
                            f"{mb}{s_lng},{s_lat};{wg},{wl};{e_lng},{e_lat}"
                            "?overview=full&geometries=geojson&steps=true",
                            timeout_s=10)
                        if vd.get("code") == "Ok" and vd.get("routes"):
                            vr   = vd["routes"][0]
                            vdst = float(vr.get("distance") or 0)
                            exst = [float(r.get("distance") or 0) for r in raw_osrm]
                            if all(abs(vdst-d) > 100 for d in exst):
                                raw_osrm.append(vr)
                                print(f"[ROUTES] ✅ via route {len(raw_osrm)}: {vdst:.0f}m")
                    except Exception as e:
                        print(f"[ROUTES] ⚠️  via: {e}")

        # Synthetic pad
        so = [(0.004,-0.004),(-0.004,0.004),(0.006,0.006)]
        si = 0
        while len(raw_osrm) < 3:
            dlo, dgo = so[si%len(so)]
            ml2, mg2 = (s_lat+e_lat)/2+dlo, (s_lng+e_lng)/2+dgo
            cs = (_interpolate_route(s_lat,s_lng,ml2,mg2,n=45)[:-1]
                  + _interpolate_route(ml2,mg2,e_lat,e_lng,n=45))
            dist = sum(_haversine_m(cs[i][1],cs[i][0],cs[i+1][1],cs[i+1][0])
                       for i in range(len(cs)-1))
            bd   = float(raw_osrm[0].get("distance") or 1)
            bu   = float(raw_osrm[0].get("duration") or 0)
            raw_osrm.append({
                "distance": dist,
                "duration": bu*(dist/bd) if bd > 0 else dist/6.94,
                "geometry": {"type":"LineString","coordinates":cs},
                "legs": [], "_synthetic": f"s{si}",
            })
            si += 1
        raw_osrm = raw_osrm[:3]

        # Score + accurate ETA
        scored = []
        for r in raw_osrm:
            dist_m = float(r.get("distance") or 0)
            coords = (r.get("geometry") or {}).get("coordinates") or []

            nearby = _route_nearby_points(coords, sp,
                                          max_distance_m=max_dist_m, weights=weights)
            fb_pts = (None if nearby
                      else _route_nearby_points(coords, sp, max_distance_m=500, weights=weights))
            score_pct = _route_safety_score(nearby, fb_pts)
            zone      = _zone_label_from_percent(score_pct)

            durations = _estimate_route_durations(
                osrm_route=r,
                safety_points_nearby=nearby or (fb_pts or []),
                departure_ts=departure_ts,
            )

            # Accurate car ETA as canonical duration
            accurate_dur_s = durations.get("car", float(r.get("duration") or 0))
            rb  = durations.pop("_road_breakdown", {})
            tf  = durations.pop("_traffic_factor", 1.0)
            cf  = durations.pop("_crowd_factor",   1.0)
            lt  = durations.pop("_live_traffic",   1.0)
            road_str = ", ".join(f"{k} {v}km"
                                 for k, v in sorted(rb.items(), key=lambda x:-x[1])) or "mixed roads"

            if score_pct >= 70:
                ai_msg = f"Safe route ({round(score_pct)}%) via {road_str}."
            elif score_pct >= 40:
                ai_msg = f"Moderate safety ({round(score_pct)}%) — stay alert."
            else:
                ai_msg = f"Higher risk ({round(score_pct)}%) — consider alternatives."
            if lt > 1.05:
                ai_msg += f" Live traffic {round((lt-1)*100)}% slower than usual."
            if cf > 1.2:
                ai_msg += " Crowd delays expected."

            worst = sorted(nearby, key=lambda p: p.get("safety_percent",100))[:8]
            scored.append({
                "distance_m":          round(dist_m, 1),
                "duration_s":          round(accurate_dur_s, 1),
                "duration_by_mode_s":  {k: round(v,1) for k,v in durations.items()},
                "route_score":         round(score_pct, 1),
                "zone":                zone,
                "geometry":            r.get("geometry"),
                "legs":                r.get("legs") or [],
                "nearby_count":        len(nearby),
                "worst_points":        worst,
                "ai_message":          ai_msg,
                "traffic_factor":      round(tf, 2),
                "crowd_factor":        round(cf, 2),
                "road_types":          rb,
            })

        # Label assignment
        scores  = [s["route_score"] for s in scored]
        durs    = [s["duration_s"]  for s in scored]
        s_rng   = (max(scores)-min(scores)) or 1.0
        d_rng   = (max(durs)  -min(durs))   or 1.0
        for s in scored:
            s["_bal"] = (0.5*(s["route_score"]-min(scores))/s_rng
                         + 0.5*(max(durs)-s["duration_s"])/d_rng)

        safest_idx  = max(range(3), key=lambda i: scored[i]["route_score"])
        fastest_idx = min(range(3), key=lambda i: scored[i]["duration_s"])
        remaining   = [i for i in range(3) if i not in (safest_idx, fastest_idx)]
        balanced_idx= (max(remaining, key=lambda i: scored[i]["_bal"])
                       if remaining else fastest_idx)
        label_map   = {safest_idx:"Safest Route",
                       balanced_idx:"Balanced Route",
                       fastest_idx:"Fastest Route"}

        final_routes, seen = [], set()
        for idx in [safest_idx, balanced_idx, fastest_idx]:
            if idx in seen: continue
            seen.add(idx)
            r = dict(scored[idx])
            r["route_label"] = label_map[idx]
            r.pop("_bal", None)
            final_routes.append(r)
        for idx in range(3):
            if len(final_routes) == 3: break
            if idx not in seen:
                r = dict(scored[idx])
                r["route_label"] = "Route"
                r.pop("_bal", None)
                final_routes.append(r)

        top = final_routes[0]
        return jsonify({
            "routes":           final_routes,
            "ai_recommendation":(f"SafePath recommends the {top['route_label']} "
                                 f"({top['route_score']}% safe, "
                                 f"~{round(top['duration_s']/60)} min by car)."),
            "source":           "osrm" if osrm_ok else "fallback",
            "count":            len(final_routes),
        })

    # ── Save profile ──────────────────────────────────────────
    @app.route("/api/save_profile", methods=["POST"])
    def save_profile():
        payload = request.get_json(silent=True)
        if not payload or not isinstance(payload, dict):
            return jsonify({"error": "Invalid JSON payload"}), 400
        ud = DATA_DIR / "user_data"
        ud.mkdir(parents=True, exist_ok=True)
        uf = ud / "users.json"
        users = []
        if uf.exists():
            try: users = json.loads(uf.read_text(encoding="utf-8"))
            except: users = []
        if not isinstance(users, list): users = []
        users.append(payload)
        uf.write_text(json.dumps(users, indent=4), encoding="utf-8")
        contacts = payload.get("contacts") if isinstance(payload, dict) else None
        if contacts and isinstance(contacts, list):
            try:
                EmergencyContact.query.filter_by(user_id=1).delete()
                db.session.commit()
                for i, phone in enumerate(contacts[:3]):
                    if not phone: continue
                    db.session.add(EmergencyContact(
                        user_id=1, contact_name=f"Contact {i+1}", phone=str(phone).strip()))
                db.session.commit()
            except Exception as e:
                app.logger.error("contacts DB: %s", e)
        return jsonify({"status": "saved"})

    # ── Get contacts ──────────────────────────────────────────
    @app.route("/api/get_contacts")
    def get_contacts():
        uid = request.args.get("user_id", type=int) or 1
        try:
            res = EmergencyContact.query.filter_by(user_id=uid).limit(3).all()
            if res: return jsonify({"contacts": [c.phone for c in res if c.phone]})
        except Exception as e:
            app.logger.error("get_contacts DB: %s", e)
        contacts = []
        try:
            uf = DATA_DIR / "user_data" / "users.json"
            if uf.exists():
                users = json.loads(uf.read_text(encoding="utf-8"))
                if isinstance(users, list) and users:
                    p = users[-1]
                    if isinstance(p, dict): contacts = p.get("contacts", [])
        except: pass
        return jsonify({"contacts": contacts})

    # ── SOS alert ─────────────────────────────────────────────
    @app.route("/api/sos_alert", methods=["POST"])
    def sos_alert():
        data = request.get_json(silent=True)
        if not data or not isinstance(data, dict):
            return jsonify({"error": "Invalid JSON payload"}), 400
        name = data.get("name"); lat = data.get("lat"); lng = data.get("lng")
        contacts = data.get("contacts", [])
        if not name or lat is None or lng is None or not contacts:
            return jsonify({"error": "Invalid SOS data"}), 400
        clean = []
        for n in contacts:
            s = str(n).strip()
            if s.startswith("+91"): s = s[3:]
            if s.startswith("91") and len(s) == 12: s = s[2:]
            if len(s) == 10 and s.isdigit(): clean.append(s)
        if not clean: return jsonify({"error": "No valid 10-digit numbers"}), 400
        msg = (f"EMERGENCY ALERT!\n\n{name} may be in danger.\n\n"
               f"Location: https://maps.google.com/?q={lat},{lng}")
        pl  = {"route":"q","sender_id":"TXTIND","message":msg,
               "language":"english","flash":0,"numbers":",".join(clean)}
        hd  = {"authorization":FAST2SMS_API_KEY,"Content-Type":"application/json"}
        rj  = None
        try:
            rj = requests.post("https://www.fast2sms.com/dev/bulkV2",
                               json=pl, headers=hd, timeout=30).json()
        except Exception as e:
            rj = {"error": str(e)}
        if isinstance(rj, dict) and rj.get("status_code") == 990:
            try:
                rj = requests.post("https://www.fast2sms.com/dev/bulkV2", data=pl,
                    headers={**hd,"Content-Type":"application/x-www-form-urlencoded"},
                    timeout=30).json()
            except Exception as e:
                rj = {"error": str(e)}
        sf = DATA_DIR / "user_data" / "sos_logs.json"
        sf.parent.mkdir(parents=True, exist_ok=True)
        logs = []
        if sf.exists():
            try: logs = json.loads(sf.read_text(encoding="utf-8"))
            except: logs = []
        if not isinstance(logs, list): logs = []
        logs.append(data)
        sf.write_text(json.dumps(logs, indent=4), encoding="utf-8")
        return jsonify({"status": "SMS sent", "response": rj})

    # ── WhatsApp ──────────────────────────────────────────────
    @app.route("/api/send_whatsapp", methods=["POST"])
    def send_whatsapp():
        data = request.get_json(silent=True)
        if not data or not isinstance(data, dict):
            return jsonify({"error": "Invalid JSON payload"}), 400
        contacts = data.get("contacts") or []
        lat = data.get("lat"); lng = data.get("lng"); name = data.get("name","SOS User")
        if not contacts or lat is None or lng is None:
            return jsonify({"error": "Missing contacts or location"}), 400
        if Client is None: return jsonify({"error": "Twilio not installed"}), 500
        sid = os.getenv("TWILIO_ACCOUNT_SID")
        tok = os.getenv("TWILIO_AUTH_TOKEN")
        fwa = os.getenv("TWILIO_WHATSAPP_FROM")
        if not all([sid,tok,fwa]): return jsonify({"error":"Twilio not configured"}),500
        client = Client(sid, tok)
        txt = (f"🚨 SOS ALERT\n{name} may be in danger.\n"
               f"Live location: https://www.google.com/maps?q={lat},{lng}")
        results = []
        for phone in contacts[:3]:
            if not phone: continue
            n = str(phone).strip().lstrip("+")
            if n.startswith("91") and len(n) >= 12: n = n[2:]
            if len(n) != 10 or not n.isdigit():
                results.append({"phone":phone,"status":"invalid"}); continue
            try:
                m = client.messages.create(from_=fwa, body=txt, to=f"whatsapp:+91{n}")
                results.append({"phone":phone,"status":"sent","sid":m.sid})
            except Exception as ex:
                results.append({"phone":phone,"status":"error","error":str(ex)})
        return jsonify({"status":"done","results":results})

    # ── Fast2SMS wallet ───────────────────────────────────────
    @app.route("/api/wallet_status")
    def wallet_status():
        try:
            r = requests.get(
                f"https://www.fast2sms.com/dev/wallet?authorization={FAST2SMS_API_KEY}",
                timeout=20)
            return jsonify({"status_code":r.status_code,"wallet":r.json()}), r.status_code
        except Exception as e:
            return jsonify({"error":str(e)}), 500

    # ── Geocode ───────────────────────────────────────────────
    @app.route("/api/geocode")
    def geocode():
        q = (request.args.get("q") or "").strip()
        if len(q) < 2: return jsonify({"error":"Query too short"}), 400
        url = ("https://nominatim.openstreetmap.org/search?"
               + urllib.parse.urlencode({"format":"json","q":q,"limit":"1","addressdetails":"1"}))
        try:
            res = _http_get_json(url,
                headers={"User-Agent":"SafeRoute/1.0","Accept-Language":"en"}, timeout_s=10)
            if not res: return jsonify({"error":"Location not found"}), 404
            return jsonify({"lat":float(res[0]["lat"]),"lng":float(res[0]["lon"]),
                            "display_name":res[0].get("display_name","")})
        except Exception as e:
            return jsonify({"error":str(e)}), 500

    # ── Autocomplete ──────────────────────────────────────────
    @app.route("/api/autocomplete")
    def autocomplete():
        q = (request.args.get("q") or "").strip()
        if len(q) < 2: return jsonify({"results":[]})
        limit = 10
        try:
            nl = float(request.args.get("near_lat")) if request.args.get("near_lat") else None
            ng = float(request.args.get("near_lng")) if request.args.get("near_lng") else None
        except ValueError:
            nl = ng = None
        nh = {"User-Agent":"SafeRoute/1.0","Accept-Language":"en"}

        def run_search(query, bounded, viewbox=None):
            p = {"format":"json","q":query,"limit":str(limit),
                 "addressdetails":"1","namedetails":"1","extratags":"1",
                 "bounded":"1" if bounded else "0","featuretype":"city"}
            if viewbox: p["viewbox"] = viewbox
            return _http_get_json(
                "https://nominatim.openstreetmap.org/search?"+urllib.parse.urlencode(p),
                headers=nh, timeout_s=12)

        merged, seen = [], set()
        def add_items(items):
            for item in (items or []):
                k = item.get("place_id") or item.get("display_name")
                if k and k not in seen: seen.add(k); merged.append(item)

        if nl is not None and ng is not None:
            d = 0.5
            try: add_items(run_search(q, bounded=True,
                viewbox=f"{ng-d},{nl+d},{ng+d},{nl-d}"))
            except: pass
        try: add_items(run_search(q, bounded=False))
        except: pass

        if nl is not None:
            merged.sort(key=lambda it: math.hypot(
                (float(it.get("lat",0))-nl)*111320,
                (float(it.get("lon",0))-ng)*111320*math.cos(math.radians(nl))))

        results, ql, has_exact = [], q.lower(), False
        for item in merged[:limit]:
            try:
                disp  = item.get("display_name") or ""
                exact = bool(ql and ql in disp.lower())
                has_exact = has_exact or exact
                addr  = item.get("address") or {}
                results.append({
                    "display_name": disp,
                    "name": ((item.get("namedetails") or {}).get("name")
                             or (disp.split(",")[0].strip() if disp else "")),
                    "address": {
                        "road":     addr.get("road"),
                        "suburb":   addr.get("suburb") or addr.get("neighbourhood"),
                        "city":     addr.get("city") or addr.get("town") or addr.get("village"),
                        "state":    addr.get("state"),
                        "postcode": addr.get("postcode"),
                    },
                    "lat": float(item.get("lat")), "lng": float(item.get("lon")),
                    "type": item.get("type"), "class": item.get("class"),
                    "importance": item.get("importance"), "exact_like": exact,
                })
            except: continue

        for p in _read_json(SAFETY_POINTS_PATH, default=[]):
            try:
                area = (p.get("area") or "").strip()
                if not area or ql not in area.lower(): continue
                results.append({
                    "display_name":area,"name":area,
                    "address":{"road":None,"suburb":None,"city":None,"state":None,"postcode":None},
                    "lat":float(p["lat"]),"lng":float(p["lng"]),
                    "type":"safety_point","class":"poi","importance":0.0,"exact_like":True,
                })
                has_exact = True
            except: continue

        return jsonify({"results":results,
                        "message":None if has_exact else "No exact match — showing nearby."})

    # ── User report ───────────────────────────────────────────
    @app.route("/api/report", methods=["POST"])
    def report():
        payload = request.get_json(silent=True) or {}
        lat = payload.get("lat"); lng = payload.get("lng")
        if lat is None or lng is None:
            return jsonify({"error":"lat and lng required"}), 400
        record = {
            "id":         f"rep_{int(datetime.now(tz=timezone.utc).timestamp()*1000)}",
            "created_at": datetime.now(tz=timezone.utc).isoformat(),
            "lat": float(lat), "lng": float(lng),
            "place_name":  (payload.get("place_name") or "").strip()[:120],
            "description": (payload.get("description") or "").strip()[:300],
            "rating": int(payload["rating"]) if payload.get("rating") else None,
        }
        _append_jsonl(USER_REPORTS_PATH, record)
        return jsonify({"ok": True, "report": record})

    # ── Score route standalone ────────────────────────────────
    @app.route("/api/score_route", methods=["POST"])
    def score_route():
        payload = request.get_json(silent=True) or {}
        coords  = payload.get("coordinates")
        if not isinstance(coords, list) or len(coords) < 2:
            return jsonify({"error":"coordinates must be list of [lng,lat] len>=2"}), 400
        weights = payload.get("weights")
        points  = _read_json(SAFETY_POINTS_PATH, default=[])
        nearby  = _route_nearby_points(coords, points,
                    max_distance_m=float(payload.get("max_distance_m",280.0)),
                    weights=weights)
        fb = ([] if nearby
              else _route_nearby_points(coords, points, max_distance_m=500.0, weights=weights))
        score = float(_clamp(_route_safety_score(nearby, fb or None), 0.0, 100.0))
        worst = sorted(nearby, key=lambda p: float(p.get("safety_percent",0)))[:10]
        return jsonify({
            "route_score":         round(score,1),
            "zone":                _zone_label_from_percent(score),
            "nearby_points_count": len(nearby),
            "nearby_points":       nearby,
            "worst_points":        worst,
        })

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(host="127.0.0.1", port=5000, debug=True)