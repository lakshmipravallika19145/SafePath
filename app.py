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

load_dotenv()  # allow setting Twilio creds in .env

try:
    from twilio.rest import Client
except ImportError:
    Client = None

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SAFETY_POINTS_PATH = DATA_DIR / "safety_points.json"
USER_REPORTS_PATH = DATA_DIR / "user_reports.jsonl"

FAST2SMS_API_KEY = "EUa8staznIqxRxDYi7k1ZhK8FiRaLUbdShtv7SZJhGvNQwFoLy6e4qnvZpHa"

_ROUTES_CACHE: dict = {}
_CACHE_TTL_S = 60

DEFAULT_SAFETY_WEIGHTS = {
    # Matches the formula you specified (positive factors)
    "street_lighting": 0.25,
    "crowd_density": 0.15,
    "police_proximity": 0.10,
    "cctv_coverage": 0.10,
    "road_visibility": 0.10,
    "traffic_density": 0.10,
    # Negative factors (applied as subtraction in scoring)
    "crime_rate": 0.15,
    "incident_reports": 0.05,
}

SAFETY_PERCENT_THRESHOLDS = {
    "safe": 70.0,
    "moderate": 40.0,
}

# Flask-SQLAlchemy emergency contacts support
# (prefers MySQL saferoute, falls back to SQLite if connector isn't installed)

try:
    import pymysql  # noqa: F401
    SQLALCHEMY_DATABASE_URI = "mysql+pymysql://root:Qazqaz12%23@localhost/saferoute"
except ImportError:
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{BASE_DIR / 'saferoute.db'}"

db = SQLAlchemy()

class EmergencyContact(db.Model):
    __tablename__ = "emergency_contacts"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, nullable=False, default=1)
    contact_name = db.Column(db.String(100), nullable=True)
    phone = db.Column(db.String(15), nullable=False)
    created_at = db.Column(db.DateTime, server_default=db.func.now())


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        return default


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _safety_point_score(p: dict, weights: dict | None = None) -> float:
    """
    Returns the RAW safety score for a single location point.
    Input metrics assumed to be 1..10 (incident_reports may be 0..10).
    """
    lighting = float(p.get("street_lighting", 5))
    crowd = float(p.get("crowd_density", 5))
    crime = float(p.get("crime_rate", 5))
    police = float(p.get("police_proximity", 5))
    cctv = float(p.get("cctv_coverage", 5))
    visibility = float(p.get("road_visibility", 5))
    traffic = float(p.get("traffic_density", 5))
    incidents = float(p.get("incident_reports", 3))

    # If the frontend doesn't pass weights, we use your required formula weights.
    w = dict(DEFAULT_SAFETY_WEIGHTS)
    if weights:
        for k, v in weights.items():
            if k in w:
                try:
                    w[k] = float(v)
                except (TypeError, ValueError):
                    pass

    w_lighting = float(w["street_lighting"])
    w_crowd = float(w["crowd_density"])
    w_police = float(w["police_proximity"])
    w_cctv = float(w["cctv_coverage"])
    w_visibility = float(w["road_visibility"])
    w_traffic = float(w["traffic_density"])
    w_crime = float(w["crime_rate"])
    w_incidents = float(w["incident_reports"])

    raw = (
        w_lighting * lighting
        + w_crowd * crowd
        + w_police * police
        + w_cctv * cctv
        + w_visibility * visibility
        + w_traffic * traffic
        - w_crime * crime
        - w_incidents * incidents
    )

    return float(raw)


def _normalize_safety_percent(raw_score: float) -> float:
    """
    Normalize raw score to 0..100.

    With the required weights and metric ranges:
    - raw_min ≈ 0.8 - (0.15*10 + 0.05*10) = -1.2
    - raw_max ≈ 8.0 - (0.15*1 + 0.05*0)  = 7.85
    """
    raw_min = -1.2
    raw_max = 7.85
    if raw_max <= raw_min:
        return 50.0
    pct = (raw_score - raw_min) / (raw_max - raw_min) * 100.0
    return float(_clamp(pct, 0.0, 100.0))


def _zone_label_from_percent(pct_0_100: float) -> str:
    if pct_0_100 >= SAFETY_PERCENT_THRESHOLDS["safe"]:
        return "safe"
    if pct_0_100 >= SAFETY_PERCENT_THRESHOLDS["moderate"]:
        return "moderate"
    return "unsafe"


def _zone_label(score_or_pct: float) -> str:
    """
    Backwards compatible helper:
    - if value <= 10, treat it as 0..10 then convert to percent
    - else treat as percent
    """
    v = float(score_or_pct)
    pct = v * 10.0 if v <= 10.0 else v
    return _zone_label_from_percent(pct)


def _classify_road_type(step_name: str) -> str:
    if not step_name or not isinstance(step_name, str):
        return "local"
    n = step_name.lower()
    if "ghat" in n:
        return "ghat"
    if "highway" in n or "express" in n or "nh" in n or "sh" in n or "motorway" in n:
        return "highway"
    return "local"


_SPEED_M_S_BY_MODE = {
    "car": {"highway": 27.78, "ghat": 13.89, "local": 11.11},
    "truck": {"highway": 19.44, "ghat": 9.72, "local": 8.33},
    "bike": {"highway": 6.94, "ghat": 4.17, "local": 5.56},
    "walk": {"highway": 1.39, "ghat": 0.97, "local": 1.39},
}


def _compute_route_distances_by_road_type(route):
    distances = {"highway": 0.0, "ghat": 0.0, "local": 0.0}
    legs = route.get("legs") or []
    for leg in legs:
        for step in leg.get("steps", []):
            dist = float(step.get("distance", 0.0))
            rtype = _classify_road_type(step.get("name", ""))
            if rtype not in distances:
                rtype = "local"
            distances[rtype] += dist
    # Fallback all to local if no step-based classification exists
    total = sum(distances.values())
    if total <= 1e-3:
        distances["local"] = float(route.get("distance", 0.0) or 0.0)
    return distances


def _road_speed_limit_kmh(road_type: str) -> float:
    limits = {"highway": 100.0, "ghat": 40.0, "local": 50.0}
    return float(limits.get(road_type, 50.0))


def _mode_speed_factor(mode: str) -> float:
    factors = {"car": 0.9, "truck": 0.7, "bike": 0.4, "walk": 0.2}
    return float(factors.get(mode, 0.5))


def _kmh_to_ms(kmh: float) -> float:
    return max(0.1, float(kmh) / 3.6)


def _historical_traffic_multiplier(departure_ts: float | None = None) -> float:
    if departure_ts is None:
        now = datetime.now(timezone.utc)
    else:
        now = datetime.fromtimestamp(departure_ts, tz=timezone.utc)

    hour = now.hour
    weekday = now.weekday()  # 0=Monday..6=Sunday

    # Peak weekday morning and evening:
    if weekday < 5 and (7 <= hour < 10):
        return 1.25
    if weekday < 5 and (17 <= hour < 20):
        return 1.3
    # Light weekend traffic
    if weekday >= 5 and (10 <= hour < 18):
        return 1.1
    return 1.0


def _realtime_traffic_multiplier(realtime_traffic_factor: float | None, report_level: float | None) -> float:
    if realtime_traffic_factor is None:
        realtime_traffic_factor = 1.0
    if report_level is None:
        report_level = 1.0
    return max(0.5, min(3.0, float(realtime_traffic_factor) * float(report_level)))


def _compute_mode_durations(distances):
    result = {}
    for mode in _SPEED_M_S_BY_MODE.keys():
        total_seconds = 0.0
        for road_type, dist in distances.items():
            limit = _road_speed_limit_kmh(road_type)
            base_speed_ms = _kmh_to_ms(limit * _mode_speed_factor(mode))
            if base_speed_ms > 0:
                total_seconds += dist / base_speed_ms
        result[mode] = float(total_seconds)
    return result


def _estimate_route_durations(route, historical_factor=1.0, realtime_factor=1.0):
    # Use route distance as the canonical base for mode duration comparisons
    total_dist_m = float(route.get("distance") or 0.0)

    # realistic average speeds (km/h)
    base_speeds_kmh = {
        "car": 60.0,
        "truck": 45.0,
        "bike": 15.0,
        "walk": 5.0,
    }

    # Convert to m/s
    base_speeds_ms = {m: _kmh_to_ms(v) for m, v in base_speeds_kmh.items()}

    car = total_dist_m / base_speeds_ms["car"] if base_speeds_ms["car"] > 0 else 0.0
    truck = total_dist_m / base_speeds_ms["truck"] if base_speeds_ms["truck"] > 0 else 0.0
    bike = total_dist_m / base_speeds_ms["bike"] if base_speeds_ms["bike"] > 0 else 0.0
    walk = total_dist_m / base_speeds_ms["walk"] if base_speeds_ms["walk"] > 0 else 0.0

    # apply traffic multipliers only for motor vehicles
    car *= historical_factor * realtime_factor
    truck *= historical_factor * realtime_factor

    return {
        "car": float(car),
        "truck": float(truck),
        "bike": float(bike),
        "walk": float(walk),
    }


def _http_get_json(url: str, headers: dict | None = None, timeout_s: int = 12):
    """
    Small HTTP JSON helper with timeouts.
    Raises on network failures; callers should handle and return a clean API error.
    """
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        data = resp.read().decode("utf-8", errors="ignore")
    return json.loads(data)


def _osrm_routes(start_lat: float, start_lng: float, end_lat: float, end_lng: float, timeout_s: int = 12):
    return _osrm_routes_by_profile(start_lat, start_lng, end_lat, end_lng, "driving", timeout_s=timeout_s)


def _osrm_routes_by_profile(start_lat: float, start_lng: float, end_lat: float, end_lng: float, profile: str, timeout_s: int = 12):
    # OSRM expects lng,lat
    base = f"https://router.project-osrm.org/route/v1/{profile}/"
    coords = f"{start_lng},{start_lat};{end_lng},{end_lat}"

    # Note: not all public OSRM profiles may support steps/alternatives; using flags safely.
    qs = urllib.parse.urlencode(
        {
            "overview": "full",
            "geometries": "geojson",
            "alternatives": "true" if profile == "driving" else "false",
            "steps": "true",
        }
    )
    return _http_get_json(base + coords + "?" + qs, timeout_s=timeout_s)


def _cache_key_for_route(start_lat: float, start_lng: float, end_lat: float, end_lng: float) -> str:
    # Round to reduce cache fragmentation; good enough for hackathon demo.
    return f"{round(start_lat,5)}:{round(start_lng,5)}->{round(end_lat,5)}:{round(end_lng,5)}"


def _cache_get(key: str):
    item = _ROUTES_CACHE.get(key)
    if not item:
        return None
    ts, value = item
    if (datetime.now(tz=timezone.utc).timestamp() - ts) > _CACHE_TTL_S:
        _ROUTES_CACHE.pop(key, None)
        return None
    return value


def _cache_set(key: str, value):
    _ROUTES_CACHE[key] = (datetime.now(tz=timezone.utc).timestamp(), value)


def _interpolate_route(a_lat: float, a_lng: float, b_lat: float, b_lng: float, n: int = 80):
    """
    Build a simple GeoJSON LineString coordinates array [[lng,lat],...].
    Used as a fallback when OSRM demo server times out.
    """
    n = max(2, int(n))
    coords = []
    for i in range(n):
        t = i / (n - 1)
        lat = a_lat + (b_lat - a_lat) * t
        lng = a_lng + (b_lng - a_lng) * t
        coords.append([lng, lat])
    return coords


def _haversine_m(a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> float:
    """Haversine distance between two points in meters."""
    R = 6_371_000
    dlat = math.radians(b_lat - a_lat)
    dlng = math.radians(b_lng - a_lng)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(a_lat)) * math.cos(math.radians(b_lat)) * math.sin(dlng / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def _fallback_routes(start_lat: float, start_lng: float, end_lat: float, end_lng: float):
    """
    Return 3 simple alternative routes (direct + two detours).
    Geometry shape is demo-only; scoring still uses your safety dataset.
    Distance and duration use haversine for accuracy.
    """
    base = _interpolate_route(start_lat, start_lng, end_lat, end_lng, n=90)

    # Build two gentle detours by offsetting the midpoint a bit
    mid_lat = (start_lat + end_lat) / 2.0
    mid_lng = (start_lng + end_lng) / 2.0

    # offsets in degrees (~0.003 ≈ 300m)
    det1 = _interpolate_route(start_lat, start_lng, mid_lat + 0.003, mid_lng - 0.003, n=45)[:-1] + _interpolate_route(mid_lat + 0.003, mid_lng - 0.003, end_lat, end_lng, n=45)
    det2 = _interpolate_route(start_lat, start_lng, mid_lat - 0.003, mid_lng + 0.003, n=45)[:-1] + _interpolate_route(mid_lat - 0.003, mid_lng + 0.003, end_lat, end_lng, n=45)

    def approx_distance_m(coords):
        """Sum haversine distances along route segments. Accurate for fallback."""
        if len(coords) < 2:
            return 0.0
        total = 0.0
        for i in range(1, len(coords)):
            a_lng, a_lat = coords[i - 1]
            b_lng, b_lat = coords[i]
            total += _haversine_m(a_lat, a_lng, b_lat, b_lng)
        return float(max(0.0, total))

    routes = []
    for coords in [base, det1, det2]:
        dist = approx_distance_m(coords)
        # Urban average ~25 km/h => 6.94 m/s for ETA
        dur = dist / 6.94 if dist > 0 else 0.0
        routes.append(
            {
                "distance": dist,
                "duration": dur,
                "geometry": {"type": "LineString", "coordinates": coords},
                "legs": [],
            }
        )
    return routes


def _meters_per_degree_lat() -> float:
    return 111_320.0


def _meters_per_degree_lng(at_lat: float) -> float:
    return 111_320.0 * math.cos(math.radians(at_lat))


def _point_to_segment_distance_m(lat: float, lng: float, a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> float:
    """
    Fast approximation: project lat/lng to local meters using equirectangular scaling,
    then compute point-to-segment distance in 2D.
    """
    ref_lat = (a_lat + b_lat) / 2.0
    mx = _meters_per_degree_lng(ref_lat)
    my = _meters_per_degree_lat()

    px, py = (lng * mx, lat * my)
    ax, ay = (a_lng * mx, a_lat * my)
    bx, by = (b_lng * mx, b_lat * my)

    abx, aby = (bx - ax, by - ay)
    apx, apy = (px - ax, py - ay)
    ab_len2 = abx * abx + aby * aby
    if ab_len2 <= 1e-9:
        dx, dy = (px - ax, py - ay)
        return math.hypot(dx, dy)

    t = (apx * abx + apy * aby) / ab_len2
    t = _clamp(t, 0.0, 1.0)
    cx, cy = (ax + t * abx, ay + t * aby)
    return math.hypot(px - cx, py - cy)


def _route_nearby_points(
    route_coords: list,
    safety_points: list,
    max_distance_m: float = 280.0,
    weights: dict | None = None,
) -> list:
    """
    route_coords: list of [lng, lat] as returned by OSRM GeoJSON.
    Returns list of safety points that are within max_distance_m of any route segment.
    """
    if not route_coords or len(route_coords) < 2:
        return []

    # Light downsample to keep accuracy while reducing cost (was too aggressive)
    step = 2 if len(route_coords) > 400 else 1
    coords = route_coords[::step]
    if coords[-1] != route_coords[-1]:
        coords.append(route_coords[-1])

    segments = []
    for i in range(len(coords) - 1):
        a_lng, a_lat = coords[i]
        b_lng, b_lat = coords[i + 1]
        segments.append((a_lat, a_lng, b_lat, b_lng))

    nearby = []
    for p in safety_points:
        lat = float(p.get("lat"))
        lng = float(p.get("lng"))
        min_d = float("inf")
        for a_lat, a_lng, b_lat, b_lng in segments:
            d = _point_to_segment_distance_m(lat, lng, a_lat, a_lng, b_lat, b_lng)
            if d < min_d:
                min_d = d
            if min_d <= max_distance_m:
                break
        if min_d <= max_distance_m:
            p2 = dict(p)
            raw = _safety_point_score(p2, weights=weights)
            pct = _normalize_safety_percent(raw)
            p2["safety_raw"] = round(raw, 4)
            p2["safety_percent"] = round(pct, 1)
            p2["zone"] = _zone_label_from_percent(pct)
            p2["distance_to_route_m"] = round(min_d, 1)
            nearby.append(p2)

    return nearby


def _route_safety_score(nearby: list, fallback_points: list | None = None) -> float:
    """
    Compute route safety score with distance-weighted average.
    Points closer to the route contribute more than points at the edge.
    Uses inverse-distance weighting: w = 1 / (1 + d/80) so close points dominate.
    If no nearby points, uses fallback_points (nearest within 500m) with strong penalty.
    """
    if nearby:
        weighted_sum = 0.0
        weight_total = 0.0
        for p in nearby:
            d = float(p.get("distance_to_route_m", 0))
            pct = float(p.get("safety_percent", 50.0))
            # Inverse-distance weight: closer = higher influence
            w = 1.0 / (1.0 + d / 80.0)
            weighted_sum += pct * w
            weight_total += w
        if weight_total > 0:
            return weighted_sum / weight_total

    # No points within max_distance: use nearest points with distance penalty
    if fallback_points:
        weighted_sum = 0.0
        weight_total = 0.0
        for p in fallback_points:
            d = float(p.get("distance_to_route_m", 500))
            pct = float(p.get("safety_percent", 50.0))
            w = 1.0 / (1.0 + d / 150.0)  # Strong distance penalty
            weighted_sum += pct * w
            weight_total += w
        if weight_total > 0:
            return weighted_sum / weight_total

    return 50.0  # Unknown area default

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

    @app.route("/api/test", methods=["GET"])
    def test_api():
        # Simple health-check endpoint
        return jsonify({"message": "SafeRoute API working"})

    @app.route("/api/safety_points", methods=["GET"])
    def safety_points():
        points = _read_json(SAFETY_POINTS_PATH, default=[])
        # Add computed score/zone for frontend visualization
        enriched = []
        for p in points:
            p2 = dict(p)
            raw = _safety_point_score(p2)
            pct = _normalize_safety_percent(raw)
            p2["safety_raw"] = round(raw, 4)
            p2["safety_percent"] = round(pct, 1)
            p2["zone"] = _zone_label_from_percent(pct)
            enriched.append(p2)
        return jsonify({"count": len(enriched), "points": enriched})

    @app.route("/api/save_profile", methods=["POST"])
    def save_profile():
        payload = request.get_json(silent=True)
        if not payload or not isinstance(payload, dict):
            return jsonify({"error": "Invalid JSON payload"}), 400

        user_data_dir = DATA_DIR / "user_data"
        user_data_dir.mkdir(parents=True, exist_ok=True)

        users_file = user_data_dir / "users.json"
        users = []
        if users_file.exists():
            try:
                with users_file.open("r", encoding="utf-8") as f:
                    users = json.load(f)
            except (json.JSONDecodeError, IOError):
                users = []

        if not isinstance(users, list):
            users = []

        users.append(payload)

        with users_file.open("w", encoding="utf-8") as f:
            json.dump(users, f, indent=4)

        # Persist emergency contacts into SQL table (user_id defaults to 1 for this demo)
        contacts = payload.get("contacts") if isinstance(payload, dict) else None
        if contacts and isinstance(contacts, list):
            try:
                EmergencyContact.query.filter_by(user_id=1).delete()
                db.session.commit()
                for i, phone in enumerate(contacts[:3]):
                    if not phone:
                        continue
                    entry = EmergencyContact(user_id=1, contact_name=f"Contact {i + 1}", phone=str(phone).strip())
                    db.session.add(entry)
                db.session.commit()
            except Exception as e:
                app.logger.error("Failed to save contacts to DB: %s", e)

        return jsonify({"status": "saved"})

    @app.route("/api/get_contacts", methods=["GET"])
    def get_contacts():
        user_id = request.args.get("user_id", type=int) or 1
        try:
            results = EmergencyContact.query.filter_by(user_id=user_id).limit(3).all()
            if results:
                phones = [c.phone for c in results if c.phone]
                return jsonify({"contacts": phones})
        except Exception as e:
            app.logger.error("Error fetching contacts from DB: %s", e)

        # fallback to local user file
        contacts = []
        try:
            user_data_dir = DATA_DIR / "user_data"
            users_file = user_data_dir / "users.json"
            if users_file.exists():
                with users_file.open("r", encoding="utf-8") as f:
                    users = json.load(f)
                if isinstance(users, list) and users:
                    profile = users[-1]
                    if isinstance(profile, dict):
                        contacts = profile.get("contacts", [])
        except Exception:
            contacts = []

        return jsonify({"contacts": contacts})

    @app.route("/api/sos_alert", methods=["POST"])
    def sos_alert():
        data = request.get_json(silent=True)
        if not data or not isinstance(data, dict):
            return jsonify({"error": "Invalid JSON payload"}), 400

        name = data.get("name")
        lat = data.get("lat")
        lng = data.get("lng")
        contacts = data.get("contacts", [])

        if not name or lat is None or lng is None or not contacts:
            return jsonify({"error": "Invalid SOS data"}), 400

        # sanitize/validate contacts as 10-digit numbers (Fast2SMS requirement)
        clean_numbers = []
        for n in contacts:
            s = str(n).strip()
            if s.startswith("+91"):
                s = s[3:]
            if s.startswith("91") and len(s) == 12:
                s = s[2:]
            if len(s) == 10 and s.isdigit():
                clean_numbers.append(s)

        if not clean_numbers:
            return jsonify({"error": "No valid 10-digit contact numbers"}), 400

        numbers = ",".join(clean_numbers)

        message = f"EMERGENCY ALERT!\n\n{name} may be in danger.\n\nLocation: https://maps.google.com/?q={lat},{lng}"

        url = "https://www.fast2sms.com/dev/bulkV2"

        payload = {
            "route": "q",
            "sender_id": "TXTIND",
            "message": message,
            "language": "english",
            "flash": 0,
            "numbers": numbers,
        }

        headers = {
            "authorization": FAST2SMS_API_KEY,
            "Content-Type": "application/json",
        }

        response_json = None
        try:
            # Primary: send JSON payload (newer Fast2SMS API expectations)
            response = requests.post(url, json=payload, headers=headers, timeout=30)
            print("Fast2SMS response text (JSON):", response.text)
            response_json = response.json()
        except Exception as e:
            print("Fast2SMS request failed (JSON):", e)
            response_json = {"error": str(e)}

        # Fallback for the 990 'old API' message (some accounts still require form encoding)
        if isinstance(response_json, dict) and response_json.get("status_code") == 990:
            try:
                fallback_headers = {
                    "authorization": FAST2SMS_API_KEY,
                    "Content-Type": "application/x-www-form-urlencoded",
                }
                response = requests.post(url, data=payload, headers=fallback_headers, timeout=30)
                print("Fast2SMS response text (form fallback):", response.text)
                response_json = response.json()
            except Exception as e:
                print("Fast2SMS request failed (form fallback):", e)
                response_json = {"error": str(e)}

        # still persist SOS log for audit/history
        user_data_dir = DATA_DIR / "user_data"
        user_data_dir.mkdir(parents=True, exist_ok=True)
        sos_file = user_data_dir / "sos_logs.json"

        sos_logs = []
        if sos_file.exists():
            try:
                with sos_file.open("r", encoding="utf-8") as f:
                    sos_logs = json.load(f)
            except (json.JSONDecodeError, IOError):
                sos_logs = []

        if not isinstance(sos_logs, list):
            sos_logs = []

        sos_logs.append(data)

        with sos_file.open("w", encoding="utf-8") as f:
            json.dump(sos_logs, f, indent=4)

        return jsonify({"status": "SMS sent", "response": response_json})

    @app.route("/api/send_whatsapp", methods=["POST"])
    def send_whatsapp():
        data = request.get_json(silent=True)
        if not data or not isinstance(data, dict):
            return jsonify({"error": "Invalid JSON payload"}), 400

        contacts = data.get("contacts") or []
        lat = data.get("lat")
        lng = data.get("lng")
        name = data.get("name", "SOS User")

        if not contacts or lat is None or lng is None:
            return jsonify({"error": "Missing contacts or location"}), 400

        if Client is None:
            return jsonify({"error": "Twilio package is not installed"}), 500

        sid = os.getenv("TWILIO_ACCOUNT_SID")
        token = os.getenv("TWILIO_AUTH_TOKEN")
        from_whatsapp = os.getenv("TWILIO_WHATSAPP_FROM")

        if not sid or not token or not from_whatsapp:
            return jsonify({"error": "Twilio credentials not configured"}), 500

        client = Client(sid, token)
        link = f"https://www.google.com/maps?q={lat},{lng}"
        message_text = f"🚨 SOS ALERT\n{name} may be in danger.\nLive location: {link}"

        results = []
        for phone in contacts[:3]:
            if not phone:
                continue
            normalized = str(phone).strip().lstrip("+")
            if normalized.startswith("91") and len(normalized) >= 12:
                normalized = normalized[2:]
            if len(normalized) != 10 or not normalized.isdigit():
                results.append({"phone": phone, "status": "invalid"})
                continue

            to = f"whatsapp:+91{normalized}"
            try:
                msg = client.messages.create(from_=from_whatsapp, body=message_text, to=to)
                results.append({"phone": phone, "status": "sent", "sid": msg.sid})
            except Exception as ex:
                results.append({"phone": phone, "status": "error", "error": str(ex)})

        return jsonify({"status": "done", "results": results})

    @app.route("/api/wallet_status", methods=["GET"])
    def wallet_status():
        """Check Fast2SMS wallet status using query param authorization (docs requirement)."""
        wallet_url = f"https://www.fast2sms.com/dev/wallet?authorization={FAST2SMS_API_KEY}"
        try:
            r = requests.get(wallet_url, timeout=20)
            return jsonify({"status_code": r.status_code, "wallet": r.json()}), r.status_code
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/geocode", methods=["GET"])
    def geocode():
        """
        Convert address string to coordinates using Nominatim.
        Used when user types an address and clicks Find without selecting from dropdown.
        """
        q = (request.args.get("q") or "").strip()
        if len(q) < 2:
            return jsonify({"error": "Query too short"}), 400

        headers = {
            "User-Agent": "SafeRoute-Hackathon/1.0 (Flask backend proxy)",
            "Accept-Language": "en",
        }
        params = {
            "format": "json",
            "q": q,
            "limit": "1",
            "addressdetails": "1",
        }
        url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(params)
        try:
            results = _http_get_json(url, headers=headers, timeout_s=10)
            if not results:
                return jsonify({"error": "Location not found"}), 404
            r = results[0]
            return jsonify({
                "lat": float(r.get("lat")),
                "lng": float(r.get("lon")),
                "display_name": r.get("display_name", ""),
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/autocomplete", methods=["GET"])
    def autocomplete():
        """
        Nominatim autocomplete proxy.
        Frontend uses this to avoid CORS/UA issues and keep API calls consistent.
        Supports global search + optional proximity prioritization.
        """
        q = (request.args.get("q") or "").strip()
        if len(q) < 2:
            return jsonify({"results": []})

        limit = 10

        try:
            near_lat = float(request.args.get("near_lat")) if request.args.get("near_lat") else None
            near_lng = float(request.args.get("near_lng")) if request.args.get("near_lng") else None
        except ValueError:
            near_lat = None
            near_lng = None

        headers = {
            # Nominatim policy: identify the application
            "User-Agent": "SafeRoute-Hackathon/1.0 (Flask backend proxy)",
            "Accept-Language": "en",
        }

        def run_search(query: str, bounded: bool, viewbox: str | None = None):
            params = {
                "format": "json",
                "q": query,
                "limit": str(limit),
                "addressdetails": "1",
                "namedetails": "1",
                "extratags": "1",
                "bounded": "1" if bounded else "0",
                "featuretype": "city",
            }
            if viewbox:
                params["viewbox"] = viewbox

            url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(params)
            return _http_get_json(url, headers=headers, timeout_s=12)

        # 1) If client gave location, do local bounded search first
        merged = []
        seen = set()

        def add_items(items):
            for item in items or []:
                key = item.get("place_id") or item.get("osm_id") or item.get("display_name")
                if not key or key in seen:
                    continue
                seen.add(key)
                merged.append(item)

        if near_lat is not None and near_lng is not None:
            delta = 0.5  # degrees (~50km) around current location
            viewbox = f"{near_lng - delta},{near_lat + delta},{near_lng + delta},{near_lat - delta}"
            try:
                local_data = run_search(q, bounded=True, viewbox=viewbox)
                add_items(local_data)
            except Exception:
                local_data = []

        # 2) Global search (always fallback for broad coverage)
        try:
            global_data = run_search(q, bounded=False, viewbox=None)
            add_items(global_data)
        except Exception:
            global_data = []

        # If partial query and short, also try fallback with country fixed or full text by default
        if len(merged) < limit and " " not in q:
            # to avoid queries like "New" being too local; include global best effort
            try:
                fallback = run_search(q, bounded=False, viewbox=None)
                add_items(fallback)
            except Exception:
                pass

        # Sort by distance if near location is known
        if near_lat is not None and near_lng is not None:
            def item_distance(item):
                try:
                    lat = float(item.get("lat", 0.0))
                    lng = float(item.get("lon", 0.0))
                    return math.hypot((lat - near_lat) * 111320, (lng - near_lng) * 111320 * math.cos(math.radians(near_lat)))
                except Exception:
                    return float("inf")

            merged.sort(key=item_distance)

        results = []
        ql = q.lower()
        has_exact_like = False

        # Primary: Nominatim results (same behavior as before)
        for item in merged[:limit]:
            try:
                disp = item.get("display_name") or ""
                disp_l = disp.lower()
                exact_like = bool(ql and ql in disp_l)
                has_exact_like = has_exact_like or exact_like

                addr = item.get("address") or {}
                results.append(
                    {
                        "display_name": disp,
                        "name": (item.get("namedetails") or {}).get("name")
                        or (disp.split(",")[0].strip() if disp else ""),
                        "address": {
                            "road": addr.get("road"),
                            "suburb": addr.get("suburb") or addr.get("neighbourhood"),
                            "city": addr.get("city") or addr.get("town") or addr.get("village"),
                            "state": addr.get("state"),
                            "postcode": addr.get("postcode"),
                        },
                        "lat": float(item.get("lat")),
                        "lng": float(item.get("lon")),
                        "type": item.get("type"),
                        "class": item.get("class"),
                        "importance": item.get("importance"),
                        "exact_like": exact_like,
                    }
                )
            except (TypeError, ValueError):
                continue

        # Secondary: local safety dataset points from safety_points.json
        # These are appended so they appear in the same dropdown and behave
        # exactly like other suggestions for the frontend.
        try:
            safety_points = _read_json(SAFETY_POINTS_PATH, default=[])
        except Exception:
            safety_points = []

        for p in safety_points:
            try:
                area = (p.get("area") or "").strip()
                if not area:
                    continue
                if ql not in area.lower():
                    continue
                lat = float(p.get("lat"))
                lng = float(p.get("lng"))
            except (TypeError, ValueError):
                continue

            disp = area
            results.append(
                {
                    "display_name": disp,
                    "name": area,
                    "address": {
                        "road": None,
                        "suburb": None,
                        "city": None,
                        "state": None,
                        "postcode": None,
                    },
                    "lat": lat,
                    "lng": lng,
                    "type": "safety_point",
                    "class": "poi",
                    "importance": 0.0,
                    "exact_like": True,
                }
            )
            has_exact_like = True

        message = None
        if results and not has_exact_like:
            message = "No exact match found — showing nearby results."

        return jsonify({"results": results, "message": message})

    @app.route("/api/routes", methods=["POST"])
    def routes():
        """
        Google-Maps-like endpoint:
        - generates up to 3 OSRM routes
        - scores each route using the safety dataset
        - returns distance, ETA, geometry, score, zone, and worst points
        """
        payload = request.get_json(silent=True) or {}
        start = payload.get("start") or {}
        end = payload.get("end") or {}
        weights = payload.get("weights")
        if weights is not None and not isinstance(weights, dict):
            return jsonify({"error": "weights must be an object"}), 400

        try:
            start_lat = float(start.get("lat"))
            start_lng = float(start.get("lng"))
            end_lat = float(end.get("lat"))
            end_lng = float(end.get("lng"))
        except (TypeError, ValueError):
            return jsonify({"error": "start/end must include lat,lng"}), 400

        cache_key = _cache_key_for_route(start_lat, start_lng, end_lat, end_lng)
        cached = _cache_get(cache_key)
        osrm = None

        if cached:
            osrm = cached
        else:
            try:
                osrm = _osrm_routes(start_lat, start_lng, end_lat, end_lng, timeout_s=12)
                _cache_set(cache_key, osrm)
            except Exception:
                return jsonify({
                    "error": "Routing service unavailable. Please try again.",
                    "routes": [],
                }), 503

        if not osrm or osrm.get("code") != "Ok":
            return jsonify({
                "error": "No routes found between the selected locations.",
                "routes": [],
            }), 404

        points = _read_json(SAFETY_POINTS_PATH, default=[])
        max_distance_m = float(payload.get("max_distance_m", 280.0))

        departure = payload.get("departure_datetime")
        departure_ts = None
        if departure:
            try:
                departure_ts = datetime.fromisoformat(departure).replace(tzinfo=timezone.utc).timestamp()
            except Exception:
                departure_ts = None

        hist_factor = _historical_traffic_multiplier(departure_ts)
        realtime_factor = _realtime_traffic_multiplier(
            payload.get("realtime_traffic_factor"),
            payload.get("realtime_report_level"),
        )

        # Use only OSRM routes (real road-following paths); no straight-line fallbacks
        osrm_routes = list((osrm.get("routes") or [])[:3])

        # Individual mode assessments (bicycle, foot) from OSRM profile routes if available
        bike_osrm = None
        walk_osrm = None
        try:
            bike_osrm = _osrm_routes_by_profile(start_lat, start_lng, end_lat, end_lng, "bicycle", timeout_s=12)
        except Exception:
            bike_osrm = None
        try:
            walk_osrm = _osrm_routes_by_profile(start_lat, start_lng, end_lat, end_lng, "foot", timeout_s=12)
        except Exception:
            walk_osrm = None

        bike_routes = list((bike_osrm.get("routes") if bike_osrm else [])[:3])
        walk_routes = list((walk_osrm.get("routes") if walk_osrm else [])[:3])

        routes_out = []
        for idx, r in enumerate(osrm_routes):
            coords = (r.get("geometry") or {}).get("coordinates") or []
            nearby = _route_nearby_points(coords, points, max_distance_m=max_distance_m, weights=weights)
            fallback_pts = []
            if not nearby:
                fallback_pts = _route_nearby_points(coords, points, max_distance_m=500.0, weights=weights)

            route_score = _route_safety_score(nearby, fallback_pts if fallback_pts else None)
            route_score = float(_clamp(route_score, 0.0, 100.0))
            zone = _zone_label_from_percent(route_score)
            worst = sorted(nearby, key=lambda p: float(p.get("safety_percent", 0.0)))[:10]

            # Per-route AI message for navigation panel
            route_ai_messages = {
                "safe": "This route is well-lit and frequently used. It is recommended for safer travel.",
                "moderate": "This route has moderate safety conditions. Stay aware of surroundings.",
                "unsafe": "This route passes through higher-risk areas. Consider taking precautions such as staying alert or carrying personal safety tools.",
            }
            route_ai_msg = route_ai_messages.get(zone, route_ai_messages["moderate"])

            road_type_distances = _compute_route_distances_by_road_type(r)

            def route_duration_or_fallback(route_list, default_speed_m_s):
                if idx < len(route_list):
                    return float((route_list[idx].get("duration") or 0.0)) * (hist_factor * realtime_factor)
                dist_m = float(r.get("distance") or 0.0)
                return dist_m / default_speed_m_s if default_speed_m_s > 0 else 0.0

            car_duration = float(r.get("duration") or 0.0) * hist_factor * realtime_factor
            truck_duration = car_duration * 1.25

            # For bike and walk, use mode-specific comfortable speed (not driving speed)
            dist_m = float(r.get("distance") or 0.0)
            bike_duration = dist_m / _kmh_to_ms(15) if _kmh_to_ms(15) > 0 else 0.0
            walk_duration = dist_m / _kmh_to_ms(5) if _kmh_to_ms(5) > 0 else 0.0

            routes_out.append(
                {
                    "distance_m": float(r.get("distance") or 0.0),
                    "duration_s": float(r.get("duration") or 0.0),
                    "geometry": r.get("geometry"),
                    # leg/step info for live navigation UI
                    "legs": r.get("legs") or [],
                    # road type split
                    "road_type_distance_m": {
                        "highway": round(road_type_distances["highway"], 1),
                        "ghat": round(road_type_distances["ghat"], 1),
                        "local": round(road_type_distances["local"], 1),
                    },
                    "duration_by_mode_s": {
                        "car": round(car_duration, 1),
                        "truck": round(truck_duration, 1),
                        "bike": round(bike_duration, 1),
                        "walk": round(walk_duration, 1),
                    },
                    "historical_traffic_multiplier": round(hist_factor, 2),
                    "realtime_traffic_multiplier": round(realtime_factor, 2),
                    # 0..100
                    "route_score": round(route_score, 1),
                    "zone": zone,
                    "ai_message": route_ai_msg,
                    "nearby_points_count": len(nearby),
                    "worst_points": worst,
                }
            )

        # Determine route categories: safest, fastest, balanced
        safest_route = max(routes_out, key=lambda x: x["route_score"]) if routes_out else None
        fastest_route = min(routes_out, key=lambda x: x["duration_s"]) if routes_out else None

        balanced_route = None
        if routes_out:
            min_d = min(route["duration_s"] for route in routes_out)
            max_d = max(route["duration_s"] for route in routes_out)
            # Combine safety and speed into balanced score
            best_balance = -1.0
            for r in routes_out:
                speed_score = 1.0 if max_d <= min_d else 1.0 - (r["duration_s"] - min_d) / (max_d - min_d)
                bou = float(r.get("route_score", 0.0)) / 100.0
                balance_score = (speed_score + bou) / 2.0
                if balance_score > best_balance:
                    best_balance = balance_score
                    balanced_route = r

        ranked = sorted(routes_out, key=lambda x: x["route_score"], reverse=True)
        ai_best = ranked[0] if ranked else None

        # AI safety recommendation messages based on zone
        AI_RECOMMENDATIONS = {
            "safe": "This route is well-lit and frequently used. It is recommended for safer travel.",
            "moderate": "This route has moderate safety conditions. Stay aware of surroundings.",
            "unsafe": "This route passes through higher-risk areas. Consider taking precautions such as staying alert or carrying personal safety tools.",
        }
        ai_message = AI_RECOMMENDATIONS.get(ai_best["zone"], AI_RECOMMENDATIONS["moderate"]) if ai_best else None
        if ai_best:
            ai_best = {**ai_best, "ai_message": ai_message}

        return jsonify(
            {
                "routes": routes_out,
                "safest_route": safest_route,
                "fastest_route": fastest_route,
                "balanced_route": balanced_route,
                "ai_recommendation": ai_best,
                "ai_message": ai_message,
            }
        )

    @app.route("/api/report", methods=["POST"])
    def report():
        payload = request.get_json(silent=True) or {}
        lat = payload.get("lat")
        lng = payload.get("lng")
        place_name = (payload.get("place_name") or "").strip()[:120]
        description = (payload.get("description") or "").strip()
        rating = payload.get("rating")  # 1..5 (user perception)

        if lat is None or lng is None:
            return jsonify({"error": "lat and lng are required"}), 400

        record = {
            "id": f"rep_{int(datetime.now(tz=timezone.utc).timestamp() * 1000)}",
            "created_at": datetime.now(tz=timezone.utc).isoformat(),
            "lat": float(lat),
            "lng": float(lng),
            "place_name": place_name,
            "description": description[:300],
            "rating": int(rating) if rating is not None else None,
        }
        _append_jsonl(USER_REPORTS_PATH, record)
        return jsonify({"ok": True, "report": record})

    @app.route("/api/score_route", methods=["POST"])
    def score_route():
        payload = request.get_json(silent=True) or {}
        coords = payload.get("coordinates")  # expected: [[lng, lat], ...]
        if not isinstance(coords, list) or len(coords) < 2:
            return jsonify({"error": "coordinates must be a list of [lng, lat] with length >= 2"}), 400

        max_distance_m = float(payload.get("max_distance_m", 280.0))
        weights = payload.get("weights")
        if weights is not None and not isinstance(weights, dict):
            return jsonify({"error": "weights must be an object"}), 400
        points = _read_json(SAFETY_POINTS_PATH, default=[])
        nearby = _route_nearby_points(coords, points, max_distance_m=max_distance_m, weights=weights)
        fallback_pts = []
        if not nearby:
            fallback_pts = _route_nearby_points(coords, points, max_distance_m=500.0, weights=weights)

        route_score = _route_safety_score(nearby, fallback_pts if fallback_pts else None)
        route_score = float(_clamp(route_score, 0.0, 100.0))
        label = _zone_label_from_percent(route_score)

        # Surface the most risky nearby points
        worst = sorted(nearby, key=lambda p: float(p.get("safety_percent", 0.0)))[:10]

        return jsonify(
            {
                "route_score": round(route_score, 1),
                "zone": label,
                "nearby_points_count": len(nearby),
                "nearby_points": nearby,
                "worst_points": worst,
            }
        )

    return app


if __name__ == "__main__":
    app = create_app()
    # Runs on http://127.0.0.1:5000 with debug enabled
    app.run(host="127.0.0.1", port=5000, debug=True)