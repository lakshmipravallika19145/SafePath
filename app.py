import json
import math
import os
import requests
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template, request, session
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash

load_dotenv()

try:
    from twilio.rest import Client
except ImportError:
    Client = None

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SAFETY_POINTS_PATH = DATA_DIR / "safety_points.json"
USER_REPORTS_PATH  = DATA_DIR / "user_reports.jsonl"

MSG91_API_KEY   = os.getenv("MSG91_API_KEY",   "501984ANen7Xhbtj69bd9eeeP1")
MSG91_SENDER_ID = os.getenv("MSG91_SENDER_ID", "india")
TOMTOM_API_KEY  = os.getenv("TOMTOM_API_KEY",  "OfDU2Qgiw5VbIld0HdbAaJ9xNnWYTE0w")

_ROUTES_CACHE:  dict = {}
_TRAFFIC_CACHE: dict = {}
_CACHE_TTL_S         = 60
_TRAFFIC_CACHE_TTL_S = 120

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

db = SQLAlchemy()


def _build_db_uri():
    db_host = os.getenv("DB_HOST")
    db_user = os.getenv("DB_USER")
    db_pass = os.getenv("DB_PASSWORD")
    db_name = os.getenv("DB_NAME")
    db_port = os.getenv("DB_PORT", "20477")
    if db_host and db_user and db_pass and db_name:
        try:
            import pymysql
            encoded_pass = urllib.parse.quote_plus(db_pass)
            return (
                f"mysql+pymysql://{db_user}:{encoded_pass}"
                f"@{db_host}:{db_port}/{db_name}"
                f"?charset=utf8mb4"
                f"&ssl_ca=/etc/ssl/certs/ca-certificates.crt"
                f"&ssl_check_hostname=false"
                f"&ssl_verify_cert=false"
            )
        except ImportError:
            pass
    try:
        import pymysql
        return "mysql+pymysql://root:Qazqaz12%23@localhost/saferoute"
    except ImportError:
        pass
    return f"sqlite:///{BASE_DIR / 'saferoute.db'}"



# ══════════════════════════════════════════════════════════════
# Models
# ══════════════════════════════════════════════════════════════

class User(db.Model):
    """
    TABLE: users
    CREATE TABLE users (
        id            INT AUTO_INCREMENT PRIMARY KEY,
        name          VARCHAR(100)  NOT NULL,
        phone         VARCHAR(15)   NOT NULL UNIQUE,
        email         VARCHAR(150)  DEFAULT NULL,
        password_hash VARCHAR(255)  NOT NULL DEFAULT '',
        contact1      VARCHAR(15)   DEFAULT NULL,
        contact2      VARCHAR(15)   DEFAULT NULL,
        contact3      VARCHAR(15)   DEFAULT NULL,
        created_at    DATETIME      DEFAULT CURRENT_TIMESTAMP,
        updated_at    DATETIME      DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
    );
    """
    __tablename__ = "users"
    id            = db.Column(db.Integer,     primary_key=True, autoincrement=True)
    name          = db.Column(db.String(100), nullable=False)
    phone         = db.Column(db.String(15),  nullable=False, unique=True)
    email         = db.Column(db.String(150), nullable=True)
    password_hash = db.Column(db.String(255), nullable=False, default="")
    contact1      = db.Column(db.String(15),  nullable=True)
    contact2      = db.Column(db.String(15),  nullable=True)
    contact3      = db.Column(db.String(15),  nullable=True)
    created_at    = db.Column(db.DateTime,    server_default=db.func.now())
    updated_at    = db.Column(db.DateTime,    server_default=db.func.now(), onupdate=db.func.now())

    def to_dict(self):
        return {
            "id":       self.id,
            "name":     self.name,
            "phone":    self.phone,
            "email":    self.email or "",
            "contact1": self.contact1 or "",
            "contact2": self.contact2 or "",
            "contact3": self.contact3 or "",
        }

    def get_contacts(self):
        return [c for c in [self.contact1, self.contact2, self.contact3] if c]


class SosLog(db.Model):
    """
    TABLE: sos_logs
    CREATE TABLE sos_logs (
        id           INT AUTO_INCREMENT PRIMARY KEY,
        user_id      INT           NOT NULL,
        user_name    VARCHAR(100)  DEFAULT NULL,
        user_phone   VARCHAR(15)   DEFAULT NULL,
        latitude     DECIMAL(10,7) NOT NULL,
        longitude    DECIMAL(10,7) NOT NULL,
        map_link     VARCHAR(300)  DEFAULT NULL,
        contacts_str VARCHAR(300)  DEFAULT NULL,
        success      TINYINT(1)    DEFAULT 0,
        triggered_at DATETIME      DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    """
    __tablename__ = "sos_logs"
    id           = db.Column(db.Integer,       primary_key=True, autoincrement=True)
    user_id      = db.Column(db.Integer,       db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    user_name    = db.Column(db.String(100),   nullable=True)
    user_phone   = db.Column(db.String(15),    nullable=True)
    latitude     = db.Column(db.Numeric(10,7), nullable=False)
    longitude    = db.Column(db.Numeric(10,7), nullable=False)
    map_link     = db.Column(db.String(300),   nullable=True)
    contacts_str = db.Column(db.String(300),   nullable=True)
    success      = db.Column(db.Boolean,       default=False)
    triggered_at = db.Column(db.DateTime,      server_default=db.func.now())


class EmergencyContact(db.Model):
    __tablename__ = "emergency_contacts"
    id           = db.Column(db.Integer,     primary_key=True, autoincrement=True)
    user_id      = db.Column(db.Integer,     nullable=False, default=1)
    contact_name = db.Column(db.String(100), nullable=True)
    phone        = db.Column(db.String(15),  nullable=False)
    created_at   = db.Column(db.DateTime,    server_default=db.func.now())


# ══════════════════════════════════════════════════════════════
# Auto-migration: safely add missing columns to existing tables
# SQLAlchemy's db.create_all() does NOT alter existing tables,
# so we do it manually here on every startup (safe, idempotent).
# ══════════════════════════════════════════════════════════════

def _migrate_tables(engine):
    """Add any missing columns to the users table without losing existing data."""
    # Columns to add: (column_name, ALTER TABLE SQL)
    user_columns = [
        ("email",         "ALTER TABLE users ADD COLUMN email         VARCHAR(150)  DEFAULT NULL"),
        ("password_hash", "ALTER TABLE users ADD COLUMN password_hash VARCHAR(255)  NOT NULL DEFAULT ''"),
        ("contact1",      "ALTER TABLE users ADD COLUMN contact1      VARCHAR(15)   DEFAULT NULL"),
        ("contact2",      "ALTER TABLE users ADD COLUMN contact2      VARCHAR(15)   DEFAULT NULL"),
        ("contact3",      "ALTER TABLE users ADD COLUMN contact3      VARCHAR(15)   DEFAULT NULL"),
        ("updated_at",    "ALTER TABLE users ADD COLUMN updated_at    DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
    ]

    with engine.connect() as conn:
        # Check if users table exists at all
        try:
            result = conn.execute(db.text("SHOW COLUMNS FROM users"))
            existing_cols = {row[0].lower() for row in result}
        except Exception:
            # Table doesn't exist yet — db.create_all() will create it fresh
            print("[Migration] users table not found, will be created by db.create_all()")
            return

        # Add each missing column
        for col_name, alter_sql in user_columns:
            if col_name.lower() not in existing_cols:
                try:
                    conn.execute(db.text(alter_sql))
                    conn.commit()
                    print(f"[Migration] ✅ Added column: users.{col_name}")
                except Exception as e:
                    print(f"[Migration] ⚠️  Could not add {col_name}: {e}")
            else:
                print(f"[Migration] ✓ Column already exists: users.{col_name}")


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


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _kmh_to_ms(kmh):
    return max(0.1, float(kmh) / 3.6)


def _http_get_json(url, headers=None, timeout_s=12):
    hdrs = {"User-Agent": "Mozilla/5.0 SafeRoute/1.0", "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    r = requests.get(url, headers=hdrs, timeout=timeout_s)
    r.raise_for_status()
    return r.json()


def _normalise_indian_mobile(raw):
    s = str(raw).strip().replace(" ", "").replace("-", "")
    if s.startswith("+91"):  s = s[3:]
    elif s.startswith("91") and len(s) == 12: s = s[2:]
    return s if (len(s) == 10 and s.isdigit()) else None


def _msg91_send(numbers, message):
    results = []
    success_count = 0
    for number in numbers:
        mobile = f"91{number}"
        try:
            params = {
                "authkey": MSG91_API_KEY, "mobiles": mobile,
                "message": message, "sender": MSG91_SENDER_ID,
                "route": "4", "country": "91", "unicode": "0",
            }
            r = requests.get("https://api.msg91.com/api/sendhttp.php", params=params, timeout=15)
            resp_text = r.text.strip()
            print(f"[MSG91] {mobile}: {resp_text}")
            if r.status_code == 200 and resp_text and not resp_text.lower().startswith("error"):
                results.append({"number": number, "status": "sent", "request_id": resp_text})
                success_count += 1
            else:
                results.append({"number": number, "status": "failed", "response": resp_text})
        except Exception as e:
            results.append({"number": number, "status": "error", "error": str(e)})
    return {"return": success_count > 0, "success_count": success_count,
            "total": len(numbers), "results": results}


# ══════════════════════════════════════════════════════════════
# Safety scoring
# ══════════════════════════════════════════════════════════════

def _safety_point_score(p, weights=None):
    w = dict(DEFAULT_SAFETY_WEIGHTS)
    if weights:
        for k, v in weights.items():
            if k in w:
                try: w[k] = float(v)
                except: pass
    return float(
        w["street_lighting"]   * float(p.get("street_lighting",  5))
        + w["crowd_density"]   * float(p.get("crowd_density",    5))
        + w["police_proximity"]* float(p.get("police_proximity", 5))
        + w["cctv_coverage"]   * float(p.get("cctv_coverage",    5))
        + w["road_visibility"] * float(p.get("road_visibility",  5))
        + w["traffic_density"] * float(p.get("traffic_density",  5))
        - w["crime_rate"]      * float(p.get("crime_rate",       5))
        - w["incident_reports"]* float(p.get("incident_reports", 3))
    )


def _normalize_safety_percent(raw):
    return float(_clamp((raw - (-1.2)) / (7.85 - (-1.2)) * 100.0, 0.0, 100.0))


def _zone_label_from_percent(pct):
    if pct >= SAFETY_PERCENT_THRESHOLDS["safe"]:     return "safe"
    if pct >= SAFETY_PERCENT_THRESHOLDS["moderate"]: return "moderate"
    return "unsafe"


# ══════════════════════════════════════════════════════════════
# Road-type speeds
# ══════════════════════════════════════════════════════════════

_OSM_CLASS_MAP = {
    "motorway": "motorway", "motorway_link": "motorway",
    "trunk": "trunk", "trunk_link": "trunk",
    "primary": "primary", "primary_link": "primary",
    "secondary": "secondary", "secondary_link": "secondary",
    "tertiary": "tertiary", "tertiary_link": "tertiary",
    "residential": "residential", "living_street": "residential",
    "unclassified": "residential", "service": "service",
    "track": "track", "path": "track",
}
_FREE_FLOW_KMH = {
    "motorway":   {"car":100,"truck":70, "bike":0,  "walk":0 },
    "trunk":      {"car":80, "truck":60, "bike":25, "walk":0 },
    "primary":    {"car":60, "truck":45, "bike":22, "walk":5 },
    "secondary":  {"car":50, "truck":35, "bike":20, "walk":5 },
    "tertiary":   {"car":40, "truck":30, "bike":18, "walk":5 },
    "residential":{"car":25, "truck":20, "bike":15, "walk":5 },
    "service":    {"car":15, "truck":10, "bike":10, "walk":5 },
    "track":      {"car":15, "truck":10, "bike":8,  "walk":4 },
}
_GHAT_SPEEDS = {"car":25,"truck":15,"bike":10,"walk":3}


def _osm_road_class(step):
    name = (step.get("name") or "").lower()
    ref  = (step.get("ref")  or "").lower()
    combined = name + " " + ref
    if "ghat" in combined: return "ghat"
    for inter in (step.get("intersections") or []):
        for cls in (inter.get("classes") or []):
            mapped = _OSM_CLASS_MAP.get(cls.lower())
            if mapped: return mapped
    if any(x in combined for x in ("nh ","nh-","national highway","expressway")): return "trunk"
    if any(x in combined for x in ("sh ","sh-","state highway")): return "primary"
    if any(x in combined for x in ("highway","motorway")): return "motorway"
    if any(x in combined for x in ("track","dirt","kachha","kutcha")): return "track"
    return "residential"


def _extract_road_segments(osrm_route):
    segments = []
    for leg in (osrm_route.get("legs") or []):
        for step in (leg.get("steps") or []):
            dist = float(step.get("distance") or 0)
            if dist < 1: continue
            segments.append({"distance_m": dist, "road_class": _osm_road_class(step)})
    return segments


# ══════════════════════════════════════════════════════════════
# Traffic / crowd helpers
# ══════════════════════════════════════════════════════════════

def _tomtom_traffic_factor(mid_lat, mid_lng):
    key = f"{round(mid_lat,3)}:{round(mid_lng,3)}"
    cached = _TRAFFIC_CACHE.get(key)
    if cached:
        ts, val = cached
        if (datetime.now(tz=timezone.utc).timestamp() - ts) < _TRAFFIC_CACHE_TTL_S:
            return val
    if not TOMTOM_API_KEY: return 1.0
    try:
        url = (f"https://api.tomtom.com/traffic/services/4/flowSegmentData"
               f"/relative0/10/json?point={mid_lat},{mid_lng}&key={TOMTOM_API_KEY}&unit=KMPH")
        data = _http_get_json(url, timeout_s=5)
        flow = data.get("flowSegmentData") or {}
        cur  = float(flow.get("currentSpeed", 0) or 0)
        free = float(flow.get("freeFlowSpeed",0) or 0)
        if cur > 0 and free > 0:
            factor = _clamp(free / cur, 1.0, 4.0)
            _TRAFFIC_CACHE[key] = (datetime.now(tz=timezone.utc).timestamp(), factor)
            return factor
    except Exception as e:
        print(f"[TomTom] {type(e).__name__}: {str(e)[:60]}")
    return 1.0

_AREA_CROWD_BASE = {
    "market":0.90,"bazaar":0.90,"bus_stand":0.85,"railway":0.85,
    "hospital":0.70,"school":0.75,"college":0.75,"temple":0.70,
    "mosque":0.70,"church":0.60,"park":0.50,"residential":0.40,
    "industrial":0.30,"highway":0.20,"default":0.45,
}

def _hour_crowd_mult(h):
    if  0<=h< 5: return 0.10
    if  5<=h< 6: return 0.20
    if  6<=h< 7: return 0.50
    if  7<=h< 9: return 0.90
    if  9<=h<11: return 0.75
    if 11<=h<13: return 0.80
    if 13<=h<15: return 0.65
    if 15<=h<17: return 0.75
    if 17<=h<20: return 1.00
    if 20<=h<22: return 0.80
    return 0.35

def _detect_area_type(pts):
    kw = {
        "market":["market","bazaar","shopping","mall"],
        "bus_stand":["bus","stand","depot","isbt"],
        "railway":["railway","station","junction","metro"],
        "hospital":["hospital","clinic","medical"],
        "school":["school","high school"],
        "college":["college","university","institute","iit","nit"],
        "temple":["temple","mandir"],
        "mosque":["mosque","masjid","dargah"],
        "park":["park","garden","lake"],
        "industrial":["industrial","factory","warehouse"],
    }
    text = " ".join((p.get("area") or p.get("name") or "").lower() for p in pts)
    for atype, words in kw.items():
        if any(w in text for w in words): return atype
    return "default"

def _crowd_factor(pts, departure_ts=None):
    now   = datetime.now(timezone.utc) if departure_ts is None else datetime.fromtimestamp(departure_ts, tz=timezone.utc)
    ist_h = ((now.hour+5)%24+(1 if now.minute>=30 else 0))%24
    atype = _detect_area_type(pts)
    base  = _AREA_CROWD_BASE.get(atype, _AREA_CROWD_BASE["default"])
    hmult = _hour_crowd_mult(ist_h)
    if now.weekday()>=5:
        if atype in ("market","temple","park"): hmult=min(1.0,hmult*1.2)
        elif atype in ("school","college","industrial"): hmult*=0.3
    return float(1.0+_clamp(base*hmult,0.05,1.0)*0.6)

def _historical_traffic_multiplier(departure_ts=None):
    now   = datetime.now(timezone.utc) if departure_ts is None else datetime.fromtimestamp(departure_ts, tz=timezone.utc)
    ist_h = ((now.hour+5)%24+(1 if now.minute>=30 else 0))%24
    wd    = now.weekday()
    if wd<5:
        if  7<=ist_h<10: return 1.45
        if 10<=ist_h<12: return 1.15
        if 12<=ist_h<14: return 1.20
        if 14<=ist_h<17: return 1.10
        if 17<=ist_h<20: return 1.50
        if 20<=ist_h<22: return 1.25
        if  0<=ist_h< 5: return 0.85
        return 1.0
    if 10<=ist_h<20: return 1.15
    if  0<=ist_h< 5: return 0.85
    return 1.0

def _estimate_route_durations(osrm_route, safety_points_nearby, departure_ts=None):
    osrm_car_s   = float(osrm_route.get("duration") or 0.0)
    total_dist_m = float(osrm_route.get("distance") or 0.0)
    coords = (osrm_route.get("geometry") or {}).get("coordinates") or []
    live_tf = 1.0
    if coords:
        mid = coords[len(coords)//2]
        live_tf = _tomtom_traffic_factor(mid[1], mid[0])
    hist = _historical_traffic_multiplier(departure_ts)
    tf   = _clamp(live_tf*0.85+hist*0.15,0.85,2.5) if live_tf>1.05 else _clamp(hist,0.85,1.5)
    car_s=osrm_car_s*tf; truck_s=car_s*1.15
    segs = _extract_road_segments(osrm_route)
    if not segs and total_dist_m>0: segs=[{"distance_m":total_dist_m,"road_class":"secondary"}]
    crowd=_crowd_factor(safety_points_nearby,departure_ts)
    bc=1.0+(crowd-1.0)*0.35
    bike_s=sum(s["distance_m"]/max(0.5,_kmh_to_ms(max(1.0,
        _GHAT_SPEEDS.get("bike",10) if s["road_class"]=="ghat"
        else _FREE_FLOW_KMH.get(s["road_class"],_FREE_FLOW_KMH["residential"]).get("bike",12)))/bc)
        for s in segs)
    walk_s=total_dist_m/max(0.5,_kmh_to_ms(5.0)/crowd) if total_dist_m>0 else 0.0
    rb={}
    for s in segs: rb[s["road_class"]]=rb.get(s["road_class"],0.0)+s["distance_m"]
    return {"car":round(car_s,1),"truck":round(truck_s,1),"bike":round(bike_s,1),"walk":round(walk_s,1),
            "_road_breakdown":{k:round(v/1000,2) for k,v in rb.items()},
            "_traffic_factor":round(tf,2),"_crowd_factor":round(crowd,2),"_live_traffic":round(live_tf,2)}


# ══════════════════════════════════════════════════════════════
# OSRM helpers
# ══════════════════════════════════════════════════════════════

_OSRM_MIRRORS = [
    "https://router.project-osrm.org/route/v1/driving/",
    "https://routing.openstreetmap.de/routed-car/route/v1/driving/",
]

def _osrm_routes(s_lat,s_lng,e_lat,e_lng,timeout_s=12):
    coords=f"{s_lng},{s_lat};{e_lng},{e_lat}"
    qs=urllib.parse.urlencode({"overview":"full","geometries":"geojson","alternatives":"true","steps":"true"})
    last_err=None
    for mirror in _OSRM_MIRRORS:
        try:
            data=_http_get_json(mirror+coords+"?"+qs,timeout_s=timeout_s)
            if data.get("code")=="Ok" and data.get("routes"): return data
        except Exception as e: last_err=e
    raise RuntimeError(f"All OSRM mirrors failed: {last_err}")

def _cache_key_for_route(s_lat,s_lng,e_lat,e_lng):
    return f"{round(s_lat,5)}:{round(s_lng,5)}->{round(e_lat,5)}:{round(e_lng,5)}"

def _cache_get(key):
    item=_ROUTES_CACHE.get(key)
    if not item: return None
    ts,val=item
    if (datetime.now(tz=timezone.utc).timestamp()-ts)>_CACHE_TTL_S:
        _ROUTES_CACHE.pop(key,None); return None
    return val

def _cache_set(key,value):
    _ROUTES_CACHE[key]=(datetime.now(tz=timezone.utc).timestamp(),value)

def _interpolate_route(a_lat,a_lng,b_lat,b_lng,n=80):
    n=max(2,int(n))
    return [[a_lng+(b_lng-a_lng)*i/(n-1),a_lat+(b_lat-a_lat)*i/(n-1)] for i in range(n)]

def _haversine_m(a_lat,a_lng,b_lat,b_lng):
    R=6_371_000; dlat=math.radians(b_lat-a_lat); dlng=math.radians(b_lng-a_lng)
    a=math.sin(dlat/2)**2+math.cos(math.radians(a_lat))*math.cos(math.radians(b_lat))*math.sin(dlng/2)**2
    return R*2*math.atan2(math.sqrt(a),math.sqrt(1-a))

def _fallback_routes(s_lat,s_lng,e_lat,e_lng):
    ml,mg=(s_lat+e_lat)/2,(s_lng+e_lng)/2
    paths=[
        _interpolate_route(s_lat,s_lng,e_lat,e_lng,n=90),
        _interpolate_route(s_lat,s_lng,ml+0.003,mg-0.003,n=45)[:-1]+_interpolate_route(ml+0.003,mg-0.003,e_lat,e_lng,n=45),
        _interpolate_route(s_lat,s_lng,ml-0.003,mg+0.003,n=45)[:-1]+_interpolate_route(ml-0.003,mg+0.003,e_lat,e_lng,n=45),
    ]
    out=[]
    for cs in paths:
        dist=sum(_haversine_m(cs[i][1],cs[i][0],cs[i+1][1],cs[i+1][0]) for i in range(len(cs)-1))
        out.append({"distance":dist,"duration":dist/6.94 if dist>0 else 0,"geometry":{"type":"LineString","coordinates":cs},"legs":[]})
    return out

def _meters_per_degree_lng(at_lat):
    return 111_320.0*math.cos(math.radians(at_lat))

def _point_to_segment_distance_m(lat,lng,a_lat,a_lng,b_lat,b_lng):
    ref_lat=(a_lat+b_lat)/2; mx,my=_meters_per_degree_lng(ref_lat),111_320.0
    px,py=lng*mx,lat*my; ax,ay=a_lng*mx,a_lat*my; bx,by=b_lng*mx,b_lat*my
    abx,aby=bx-ax,by-ay; apx,apy=px-ax,py-ay; ab2=abx*abx+aby*aby
    if ab2<=1e-9: return math.hypot(px-ax,py-ay)
    t=_clamp((apx*abx+apy*aby)/ab2,0.0,1.0)
    return math.hypot(px-(ax+t*abx),py-(ay+t*aby))

def _route_nearby_points(route_coords,safety_points,max_distance_m=280.0,weights=None):
    if not route_coords or len(route_coords)<2: return []
    step=2 if len(route_coords)>400 else 1
    coords=route_coords[::step]
    if coords[-1]!=route_coords[-1]: coords.append(route_coords[-1])
    segs=[(coords[i][1],coords[i][0],coords[i+1][1],coords[i+1][0]) for i in range(len(coords)-1)]
    nearby=[]
    for p in safety_points:
        lat,lng=float(p.get("lat")),float(p.get("lng")); min_d=float("inf")
        for a_lat,a_lng,b_lat,b_lng in segs:
            d=_point_to_segment_distance_m(lat,lng,a_lat,a_lng,b_lat,b_lng)
            if d<min_d: min_d=d
            if min_d<=max_distance_m: break
        if min_d<=max_distance_m:
            p2=dict(p); raw=_safety_point_score(p2,weights=weights); pct=_normalize_safety_percent(raw)
            p2["safety_raw"]=round(raw,4); p2["safety_percent"]=round(pct,1)
            p2["zone"]=_zone_label_from_percent(pct); p2["distance_to_route_m"]=round(min_d,1)
            nearby.append(p2)
    return nearby

def _route_safety_score(nearby,fallback_points=None):
    def _wavg(pts,denom):
        ws=wt=0.0
        for p in pts:
            w=1.0/(1.0+float(p.get("distance_to_route_m",0))/denom)
            ws+=float(p.get("safety_percent",50.0))*w; wt+=w
        return ws/wt if wt>0 else None
    v=_wavg(nearby,80.0) if nearby else None
    if v is not None: return v
    v=_wavg(fallback_points,150.0) if fallback_points else None
    if v is not None: return v
    return 50.0


# ══════════════════════════════════════════════════════════════
# Flask factory
# ══════════════════════════════════════════════════════════════

def create_app():
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["SQLALCHEMY_DATABASE_URI"] = _build_db_uri()
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "saferoute-secret-change-in-prod")
    db.init_app(app)

    with app.app_context():
        try:
            _migrate_tables(db.engine)
            db.create_all()
            print("[DB] ✅ Connected and tables ready")
        except Exception as e:
            print(f"[DB] ⚠️ Could not connect on startup: {e}")
            print("[DB] App will start anyway, DB operations will retry on request")           # create any brand-new tables

    CORS(app)

    @app.route("/")
    def home():
        return render_template("dashboard.html")

    @app.route("/api/test")
    def test_api():
        return jsonify({"message": "SafeRoute API working"})

    # ── Auth ──────────────────────────────────────────────────

    @app.route("/api/signup", methods=["POST"])
    def signup():
        data = request.get_json(silent=True) or {}
        name     = (data.get("name") or "").strip()
        phone    = (data.get("phone") or "").strip()
        password = (data.get("password") or "").strip()
        email    = (data.get("email") or "").strip() or None
        c1 = _normalise_indian_mobile(data.get("contact1") or "")
        c2 = _normalise_indian_mobile(data.get("contact2") or "")
        c3 = _normalise_indian_mobile(data.get("contact3") or "")
        if not name or not phone or not password:
            return jsonify({"error": "name, phone and password are required"}), 400
        if len(password) < 6:
            return jsonify({"error": "Password must be at least 6 characters"}), 400
        if User.query.filter_by(phone=phone).first():
            return jsonify({"error": "Phone number already registered"}), 409
        user = User(name=name, phone=phone, email=email,
                    password_hash=generate_password_hash(password),
                    contact1=c1, contact2=c2, contact3=c3)
        db.session.add(user)
        db.session.commit()
        session["user_id"] = user.id
        session["user_name"] = user.name
        session["user_phone"] = user.phone
        return jsonify({"status": "created", "user": user.to_dict()}), 201

    @app.route("/api/login", methods=["POST"])
    def login():
        data = request.get_json(silent=True) or {}
        phone    = (data.get("phone") or "").strip()
        password = (data.get("password") or "").strip()
        if not phone or not password:
            return jsonify({"error": "phone and password are required"}), 400
        user = User.query.filter_by(phone=phone).first()
        if not user or not check_password_hash(user.password_hash, password):
            return jsonify({"error": "Invalid phone or password"}), 401
        session["user_id"] = user.id
        session["user_name"] = user.name
        session["user_phone"] = user.phone
        return jsonify({"status": "ok", "user": user.to_dict()})

    @app.route("/api/logout", methods=["POST"])
    def logout():
        session.clear()
        return jsonify({"status": "logged_out"})

    @app.route("/api/me")
    def me():
        uid = session.get("user_id")
        if not uid: return jsonify({"logged_in": False}), 200
        user = User.query.get(uid)
        if not user: session.clear(); return jsonify({"logged_in": False}), 200
        return jsonify({"logged_in": True, "user": user.to_dict()})

    @app.route("/api/edit_profile", methods=["POST"])
    def edit_profile():
        uid = session.get("user_id")
        if not uid: return jsonify({"error": "Not logged in"}), 401
        user = User.query.get(uid)
        if not user: return jsonify({"error": "User not found"}), 404
        data = request.get_json(silent=True) or {}
        if data.get("name"):    user.name  = data["name"].strip()
        if "email" in data:     user.email = (data["email"] or "").strip() or None
        if "contact1" in data:  user.contact1 = _normalise_indian_mobile(data["contact1"] or "") or None
        if "contact2" in data:  user.contact2 = _normalise_indian_mobile(data["contact2"] or "") or None
        if "contact3" in data:  user.contact3 = _normalise_indian_mobile(data["contact3"] or "") or None
        new_pw  = (data.get("new_password") or "").strip()
        curr_pw = (data.get("current_password") or "").strip()
        if new_pw:
            if not curr_pw:
                return jsonify({"error": "current_password required to change password"}), 400
            if not check_password_hash(user.password_hash, curr_pw):
                return jsonify({"error": "Current password is incorrect"}), 401
            if len(new_pw) < 6:
                return jsonify({"error": "New password must be at least 6 characters"}), 400
            user.password_hash = generate_password_hash(new_pw)
        db.session.commit()
        session["user_name"] = user.name
        return jsonify({"status": "updated", "user": user.to_dict()})

    @app.route("/api/get_contacts")
    def get_contacts():
        uid = session.get("user_id")
        if uid:
            user = User.query.get(uid)
            if user: return jsonify({"contacts": user.get_contacts()})
        return jsonify({"contacts": []})

    # ── SOS ───────────────────────────────────────────────────

    @app.route("/api/sos_alert", methods=["POST"])
    def sos_alert():
        data = request.get_json(silent=True)
        if not data: return jsonify({"error": "Invalid JSON payload"}), 400
        name=data.get("name"); lat=data.get("lat"); lng=data.get("lng")
        contacts=data.get("contacts",[])
        if not name or lat is None or lng is None or not contacts:
            return jsonify({"error": "Invalid SOS data"}), 400
        clean=[n for c in contacts if (n:=_normalise_indian_mobile(str(c)))]
        if not clean: return jsonify({"error":"No valid 10-digit Indian numbers","received":contacts}),400
        msg=(f"🚨 SOS ALERT!\n\n{name} is in danger and needs immediate help!\n\n"
             f"📍 Location: https://maps.google.com/?q={lat},{lng}\n\nPlease respond immediately!")
        resp=_msg91_send(clean,msg); success=isinstance(resp,dict) and resp.get("return") is True
        uid=session.get("user_id")
        if uid:
            try:
                db.session.add(SosLog(user_id=uid,user_name=name,user_phone=data.get("phone"),
                    latitude=float(lat),longitude=float(lng),
                    map_link=f"https://maps.google.com/?q={lat},{lng}",
                    contacts_str=",".join(clean),success=success))
                db.session.commit()
            except Exception as e: app.logger.error("sos_logs DB: %s",e)
        return jsonify({"status":"sent" if success else "failed","sent_to":clean,"response":resp})

    @app.route("/api/send_whatsapp", methods=["POST"])
    def send_whatsapp():
        data=request.get_json(silent=True)
        if not data: return jsonify({"error":"Invalid JSON payload"}),400
        contacts=data.get("contacts") or []; lat=data.get("lat"); lng=data.get("lng")
        name=data.get("name","SOS User")
        if not contacts or lat is None or lng is None: return jsonify({"error":"Missing data"}),400
        clean=[n for c in contacts if (n:=_normalise_indian_mobile(str(c)))]
        if not clean: return jsonify({"error":"No valid numbers"}),400
        msg=f"📍 LIVE LOCATION UPDATE\n{name} is in danger!\nLocation: https://maps.google.com/?q={lat},{lng}"
        resp=_msg91_send(clean,msg)
        return jsonify({"status":"sent" if resp.get("return") else "failed","response":resp})

    @app.route("/api/wallet_status")
    def wallet_status():
        try:
            r=requests.get(f"https://api.msg91.com/api/balance.php?authkey={MSG91_API_KEY}&type=2",timeout=10)
            return jsonify({"status_code":r.status_code,"wallet":r.json()}),r.status_code
        except Exception as e: return jsonify({"error":str(e)}),500

    # ── Safety / routing ──────────────────────────────────────

    @app.route("/api/safety_points")
    def safety_points():
        points=_read_json(SAFETY_POINTS_PATH,default=[])
        enriched=[]
        for p in points:
            p2=dict(p); raw=_safety_point_score(p2); pct=_normalize_safety_percent(raw)
            p2["safety_raw"]=round(raw,4); p2["safety_percent"]=round(pct,1)
            p2["zone"]=_zone_label_from_percent(pct); enriched.append(p2)
        return jsonify({"count":len(enriched),"points":enriched})

    @app.route("/api/routes", methods=["POST"])
    def routes():
        body=request.get_json(silent=True) or {}
        start=body.get("start") or {}; end=body.get("end") or {}
        try:
            s_lat=float(start["lat"]); s_lng=float(start["lng"])
            e_lat=float(end["lat"]);   e_lng=float(end["lng"])
        except (KeyError,TypeError,ValueError):
            return jsonify({"error":"Invalid start/end coordinates"}),400
        weights=body.get("weights") or None
        max_dist_m=float(body.get("max_distance_m",280))
        departure_ts=body.get("departure_ts")
        sp=_read_json(SAFETY_POINTS_PATH,default=[])
        cache_key=_cache_key_for_route(s_lat,s_lng,e_lat,e_lng)
        raw_osrm=_cache_get(cache_key); osrm_ok=False
        if raw_osrm is None:
            try:
                data=_osrm_routes(s_lat,s_lng,e_lat,e_lng)
                if data.get("code")=="Ok" and data.get("routes"):
                    raw_osrm=data["routes"]; _cache_set(cache_key,raw_osrm); osrm_ok=True
            except Exception: raw_osrm=None
        else: osrm_ok=True
        if not raw_osrm: raw_osrm=_fallback_routes(s_lat,s_lng,e_lat,e_lng)
        if osrm_ok and len(raw_osrm)<3:
            ml,mg=(s_lat+e_lat)/2,(s_lng+e_lng)/2; dl,dg=e_lat-s_lat,e_lng-s_lng
            rlen=math.hypot(dl,dg) or 1.0; pl,pg=-dg/rlen,dl/rlen
            for off in [0.004,-0.004,0.007,-0.007]:
                if len(raw_osrm)>=3: break
                wl,wg=ml+pl*off,mg+pg*off
                for mb in _OSRM_MIRRORS:
                    if len(raw_osrm)>=3: break
                    try:
                        vd=_http_get_json(f"{mb}{s_lng},{s_lat};{wg},{wl};{e_lng},{e_lat}?overview=full&geometries=geojson&steps=true",timeout_s=10)
                        if vd.get("code")=="Ok" and vd.get("routes"):
                            vr=vd["routes"][0]
                            if all(abs(float(vr.get("distance",0))-float(r.get("distance",0)))>100 for r in raw_osrm):
                                raw_osrm.append(vr)
                    except: pass
        so=[(0.004,-0.004),(-0.004,0.004),(0.006,0.006)]; si=0
        while len(raw_osrm)<3:
            dlo,dgo=so[si%len(so)]; ml2,mg2=(s_lat+e_lat)/2+dlo,(s_lng+e_lng)/2+dgo
            cs=_interpolate_route(s_lat,s_lng,ml2,mg2,n=45)[:-1]+_interpolate_route(ml2,mg2,e_lat,e_lng,n=45)
            dist=sum(_haversine_m(cs[i][1],cs[i][0],cs[i+1][1],cs[i+1][0]) for i in range(len(cs)-1))
            bd=float(raw_osrm[0].get("distance") or 1); bu=float(raw_osrm[0].get("duration") or 0)
            raw_osrm.append({"distance":dist,"duration":bu*(dist/bd) if bd>0 else dist/6.94,
                              "geometry":{"type":"LineString","coordinates":cs},"legs":[]}); si+=1
        raw_osrm=raw_osrm[:3]
        scored=[]
        for r in raw_osrm:
            dist_m=float(r.get("distance") or 0)
            coords=(r.get("geometry") or {}).get("coordinates") or []
            nearby=_route_nearby_points(coords,sp,max_distance_m=max_dist_m,weights=weights)
            fb_pts=None if nearby else _route_nearby_points(coords,sp,max_distance_m=500,weights=weights)
            score_pct=_route_safety_score(nearby,fb_pts); zone=_zone_label_from_percent(score_pct)
            durations=_estimate_route_durations(r,nearby or (fb_pts or []),departure_ts)
            accurate_dur_s=durations.get("car",float(r.get("duration") or 0))
            rb=durations.pop("_road_breakdown",{}); tf=durations.pop("_traffic_factor",1.0)
            cf=durations.pop("_crowd_factor",1.0);  lt=durations.pop("_live_traffic",1.0)
            road_str=", ".join(f"{k} {v}km" for k,v in sorted(rb.items(),key=lambda x:-x[1])) or "mixed roads"
            if score_pct>=70:   ai_msg=f"Safe route ({round(score_pct)}%) via {road_str}."
            elif score_pct>=40: ai_msg=f"Moderate safety ({round(score_pct)}%) — stay alert."
            else:               ai_msg=f"Higher risk ({round(score_pct)}%) — consider alternatives."
            if lt>1.05: ai_msg+=f" Live traffic {round((lt-1)*100)}% slower than usual."
            if cf>1.2:  ai_msg+=" Crowd delays expected."
            scored.append({"distance_m":round(dist_m,1),"duration_s":round(accurate_dur_s,1),
                "duration_by_mode_s":{k:round(v,1) for k,v in durations.items()},
                "route_score":round(score_pct,1),"zone":zone,"geometry":r.get("geometry"),
                "legs":r.get("legs") or [],"nearby_count":len(nearby),
                "worst_points":sorted(nearby,key=lambda p:p.get("safety_percent",100))[:8],
                "ai_message":ai_msg,"traffic_factor":round(tf,2),"crowd_factor":round(cf,2),"road_types":rb})
        scores=[s["route_score"] for s in scored]; durs=[s["duration_s"] for s in scored]
        s_rng=(max(scores)-min(scores)) or 1.0; d_rng=(max(durs)-min(durs)) or 1.0
        for s in scored:
            s["_bal"]=0.5*(s["route_score"]-min(scores))/s_rng+0.5*(max(durs)-s["duration_s"])/d_rng
        si2=max(range(3),key=lambda i:scored[i]["route_score"])
        fi=min(range(3),key=lambda i:scored[i]["duration_s"])
        rem=[i for i in range(3) if i not in (si2,fi)]
        bi=max(rem,key=lambda i:scored[i]["_bal"]) if rem else fi
        lmap={si2:"Safest Route",bi:"Balanced Route",fi:"Fastest Route"}
        final_routes,seen=[],set()
        for idx in [si2,bi,fi]:
            if idx in seen: continue
            seen.add(idx); r=dict(scored[idx]); r["route_label"]=lmap[idx]; r.pop("_bal",None)
            final_routes.append(r)
        for idx in range(3):
            if len(final_routes)==3: break
            if idx not in seen:
                r=dict(scored[idx]); r["route_label"]="Route"; r.pop("_bal",None); final_routes.append(r)
        top=final_routes[0]
        return jsonify({"routes":final_routes,
            "ai_recommendation":f"SafePath recommends the {top['route_label']} ({top['route_score']}% safe, ~{round(top['duration_s']/60)} min by car).",
            "source":"osrm" if osrm_ok else "fallback","count":len(final_routes)})

    @app.route("/api/geocode")
    def geocode():
        q=(request.args.get("q") or "").strip()
        if len(q)<2: return jsonify({"error":"Query too short"}),400
        url="https://nominatim.openstreetmap.org/search?"+urllib.parse.urlencode({"format":"json","q":q,"limit":"1","addressdetails":"1"})
        try:
            res=_http_get_json(url,headers={"User-Agent":"SafeRoute/1.0","Accept-Language":"en"},timeout_s=10)
            if not res: return jsonify({"error":"Location not found"}),404
            return jsonify({"lat":float(res[0]["lat"]),"lng":float(res[0]["lon"]),"display_name":res[0].get("display_name","")})
        except Exception as e: return jsonify({"error":str(e)}),500

    @app.route("/api/autocomplete")
    def autocomplete():
        q=(request.args.get("q") or "").strip()
        if len(q)<2: return jsonify({"results":[]})
        limit=10
        try:
            nl=float(request.args.get("near_lat")) if request.args.get("near_lat") else None
            ng=float(request.args.get("near_lng")) if request.args.get("near_lng") else None
        except ValueError: nl=ng=None
        nh={"User-Agent":"SafeRoute/1.0","Accept-Language":"en"}
        def run_search(query,bounded,viewbox=None):
            p={"format":"json","q":query,"limit":str(limit),"addressdetails":"1","namedetails":"1",
               "extratags":"1","bounded":"1" if bounded else "0","featuretype":"city"}
            if viewbox: p["viewbox"]=viewbox
            return _http_get_json("https://nominatim.openstreetmap.org/search?"+urllib.parse.urlencode(p),headers=nh,timeout_s=12)
        merged,seen=[],set()
        def add_items(items):
            for item in (items or []):
                k=item.get("place_id") or item.get("display_name")
                if k and k not in seen: seen.add(k); merged.append(item)
        if nl is not None and ng is not None:
            d=0.5
            try: add_items(run_search(q,bounded=True,viewbox=f"{ng-d},{nl+d},{ng+d},{nl-d}"))
            except: pass
        try: add_items(run_search(q,bounded=False))
        except: pass
        if nl is not None:
            merged.sort(key=lambda it:math.hypot((float(it.get("lat",0))-nl)*111320,
                (float(it.get("lon",0))-ng)*111320*math.cos(math.radians(nl))))
        results,ql,has_exact=[],q.lower(),False
        for item in merged[:limit]:
            try:
                disp=item.get("display_name") or ""; exact=bool(ql and ql in disp.lower())
                has_exact=has_exact or exact; addr=item.get("address") or {}
                results.append({"display_name":disp,
                    "name":((item.get("namedetails") or {}).get("name") or (disp.split(",")[0].strip() if disp else "")),
                    "address":{"road":addr.get("road"),"suburb":addr.get("suburb") or addr.get("neighbourhood"),
                               "city":addr.get("city") or addr.get("town") or addr.get("village"),
                               "state":addr.get("state"),"postcode":addr.get("postcode")},
                    "lat":float(item.get("lat")),"lng":float(item.get("lon")),
                    "type":item.get("type"),"class":item.get("class"),
                    "importance":item.get("importance"),"exact_like":exact})
            except: continue
        for p in _read_json(SAFETY_POINTS_PATH,default=[]):
            try:
                area=(p.get("area") or "").strip()
                if not area or ql not in area.lower(): continue
                results.append({"display_name":area,"name":area,"address":{"road":None,"suburb":None,"city":None,"state":None,"postcode":None},
                    "lat":float(p["lat"]),"lng":float(p["lng"]),"type":"safety_point","class":"poi","importance":0.0,"exact_like":True})
                has_exact=True
            except: continue
        return jsonify({"results":results,"message":None if has_exact else "No exact match — showing nearby."})

    @app.route("/api/report", methods=["POST"])
    def report():
        payload=request.get_json(silent=True) or {}
        lat=payload.get("lat"); lng=payload.get("lng")
        if lat is None or lng is None: return jsonify({"error":"lat and lng required"}),400
        record={"id":f"rep_{int(datetime.now(tz=timezone.utc).timestamp()*1000)}",
            "created_at":datetime.now(tz=timezone.utc).isoformat(),"lat":float(lat),"lng":float(lng),
            "place_name":(payload.get("place_name") or "").strip()[:120],
            "description":(payload.get("description") or "").strip()[:300],
            "rating":int(payload["rating"]) if payload.get("rating") else None}
        _append_jsonl(USER_REPORTS_PATH,record)
        return jsonify({"ok":True,"report":record})

    @app.route("/api/score_route", methods=["POST"])
    def score_route():
        payload=request.get_json(silent=True) or {}; coords=payload.get("coordinates")
        if not isinstance(coords,list) or len(coords)<2:
            return jsonify({"error":"coordinates must be list of [lng,lat] len>=2"}),400
        points=_read_json(SAFETY_POINTS_PATH,default=[])
        nearby=_route_nearby_points(coords,points,max_distance_m=float(payload.get("max_distance_m",280.0)),weights=payload.get("weights"))
        fb=[] if nearby else _route_nearby_points(coords,points,max_distance_m=500.0,weights=payload.get("weights"))
        score=float(_clamp(_route_safety_score(nearby,fb or None),0.0,100.0))
        worst=sorted(nearby,key=lambda p:float(p.get("safety_percent",0)))[:10]
        return jsonify({"route_score":round(score,1),"zone":_zone_label_from_percent(score),
            "nearby_points_count":len(nearby),"nearby_points":nearby,"worst_points":worst})

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(host="127.0.0.1", port=5000, debug=True)