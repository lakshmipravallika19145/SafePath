"""
Run this file in your project folder:
    python test_osrm.py

It will tell you exactly what's happening with the routing.
"""
import requests
import json

s_lat, s_lng = 16.5062, 80.6480
e_lat, e_lng = 16.5193, 80.6305

print("=" * 50)
print("Testing OSRM routing...")
print("=" * 50)

url = "https://router.project-osrm.org/route/v1/driving/{},{};{},{}".format(
    s_lng, s_lat, e_lng, e_lat
)
params = {
    "overview": "full",
    "geometries": "geojson",
    "alternatives": "true",
    "steps": "true",
}

try:
    r = requests.get(url, params=params, timeout=15)
    print("HTTP Status:", r.status_code)
    data = r.json()
    print("OSRM code:", data.get("code"))
    routes = data.get("routes", [])
    print("Routes returned:", len(routes))
    for i, route in enumerate(routes):
        coords = route.get("geometry", {}).get("coordinates", [])
        print(f"  Route {i}: {route.get('distance')}m, {route.get('duration')}s, {len(coords)} coords")
    if routes:
        print("\n✅ OSRM is working! The straight lines are a different bug.")
    else:
        print("\n❌ OSRM returned no routes.")
except requests.exceptions.Timeout:
    print("❌ OSRM timed out - server too slow")
except requests.exceptions.ConnectionError as e:
    print("❌ Cannot reach OSRM:", str(e)[:100])
except Exception as e:
    print("❌ Error:", type(e).__name__, str(e)[:100])