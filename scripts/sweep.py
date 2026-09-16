#!/usr/bin/env python3
"""
Knoxville Land Scout - daily sweep.

Pulls active vacant-land listings from Redfin's CSV export endpoint for the 12
target counties, filters to the search criteria, assigns each parcel to a county
by point-in-polygon, merges against the previous run to work out what is new /
cut / gone, downloads any photos we don't already have, and writes:

    data/tracts.json   the dataset the site reads (also the archive)
    data/report.json   what changed this run

Run:  python3 scripts/sweep.py            (writes files)
      python3 scripts/sweep.py --dry-run  (no writes, prints summary)

No third-party packages - stdlib only, so it runs on a bare GitHub Actions box.
"""

import csv
import io
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
PHOTOS = os.path.join(ROOT, "photos")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# ---------------------------------------------------------------- criteria ---

MAX_PRICE = 250_000
MIN_ACRES = 0.1
SQFT_PER_ACRE = 43_560.0

# Redfin region ids for the 12 in-scope counties.
COUNTIES = {
    "Knox": 2591, "Blount": 2549, "Loudon": 2597, "Anderson": 2545,
    "Union": 2631, "Grainger": 2573, "Jefferson": 2589, "Sevier": 2622,
    "Roane": 2617, "Monroe": 2606, "Campbell": 2551, "Morgan": 2609,
}

KNOXVILLE = (35.9606, -83.9207)

# A tract absent from this many consecutive sweeps is retired from the list.
MISS_LIMIT = 3

# Straight-line miles -> estimated drive minutes. Calibrated to the ring labels
# already on the map (13 / 26 / 38 mi == 15 / 30 / 45 min).
DRIVE_FACTOR = 1.15

# Local errand roads wind more than the run into Knoxville, so the fallback
# estimate uses a heavier factor (~37 mph effective over straight-line distance).
# Real road-network times from OSRM replace both estimates whenever available.
ERRAND_FACTOR = 1.60
OSRM = "https://router.project-osrm.org/table/v1/driving/"
# The public OSRM server runs ~15% slower than real-world times on these
# roads (checked: Maryville 29 vs ~25, Lenoir City 35 vs ~30, Madisonville
# 65 vs ~55, Maynardville 41 vs ~35 - every ratio 0.85-0.86). Raw road
# minutes are kept as driveRoad / shopRoad; the displayed figure is calibrated.
ROAD_CAL = 0.86
SHOP_CANDIDATES = 5          # nearest anchor stores (by air) to route to

# A listing with a monthly HOA fee is excluded outright.
EXCLUDE_HOA = True

# Hard limits on routed road time. Applied only when the figure is a real
# routed one (never to a straight-line estimate), and remembered in
# data/excluded.json so the tract is not routed again every morning.
PAGE_FETCH_CAP = int(os.environ.get("PAGE_FETCH_CAP", "20"))  # Redfin blocks bursts
PAGE_FETCH_PAUSE = 5.0  # seconds between them

MAX_DRIVE_MIN = 60      # to downtown Knoxville
MAX_SHOP_MIN = 15       # to the nearest anchor store

# Terrain. Slope is averaged over a 3x3 grid 150 m apart around the listing's
# point, so it describes the hillside the parcel sits on rather than any one
# spot on it. Measured across the current set: median 3.8 deg, max 23.2 deg.
# 15-20 deg is ordinary East TN "steep"; above 20 is mountainside, and dropped.
MAX_SLOPE_DEG = 20.0
SLOPE_GRID_M = 150.0

# $/acre bands: (upper_bound_exclusive, label, colour)
BANDS = [
    (5_000, "under $5k", "#1F5C40"),
    (8_000, "$5-8k", "#4F8F5E"),
    (12_000, "$8-12k", "#C9A227"),
    (17_000, "$12-17k", "#B4652A"),
    (float("inf"), "$17k+", "#8C3B3B"),
]

# Narrative words that mean "there is a dwelling here" -> exclude.
DWELLING_RE = re.compile(
    r"\b(house|home|cabin|residence|dwelling|mobile home|manufactured|"
    r"double[- ]?wide|single[- ]?wide|duplex|apartment)\b", re.I)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ------------------------------------------------------------------ fetch ---

def fetch(url, tries=3, timeout=45, binary=False):
    """GET with retries. Returns bytes/str, or None on give-up."""
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "text/csv,text/html,application/xhtml+xml,*/*",
                "Accept-Language": "en-US,en;q=0.9",
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            return raw if binary else raw.decode("utf-8", "replace")
        except Exception as e:                      # noqa: BLE001
            last = e
            if attempt < tries - 1:
                time.sleep(1.5 * (attempt + 1))
    log(f"  ! fetch failed {url[:90]} :: {last}")
    return None


def redfin_csv(region_id, page=1):
    return ("https://www.redfin.com/stingray/api/gis-csv"
            f"?al=1&max_price={MAX_PRICE}&num_homes=350"
            f"&ord=days-on-redfin-asc&page_number={page}"
            f"&region_id={region_id}&region_type=5&sf=1,2,3,5,6,7"
            "&status=9&uipt=5&v=8")


# -------------------------------------------------------------- geography ---

def haversine_mi(a, b):
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 3958.8 * math.asin(math.sqrt(h))


def _rings(geom):
    """Yield every exterior ring of a Polygon / MultiPolygon."""
    if geom["type"] == "Polygon":
        yield geom["coordinates"][0]
    elif geom["type"] == "MultiPolygon":
        for poly in geom["coordinates"]:
            yield poly[0]


def point_in_ring(lon, lat, ring):
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat):
            x_at = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lon < x_at:
                inside = not inside
        j = i
    return inside


def load_counties():
    path = os.path.join(DATA, "counties.json")
    with open(path) as f:
        gj = json.load(f)
    out = []
    for feat in gj["features"]:
        name = feat["properties"].get("n")
        rings = list(_rings(feat["geometry"]))
        # bbox for a cheap pre-filter
        xs = [p[0] for r in rings for p in r]
        ys = [p[1] for r in rings for p in r]
        out.append({"name": name, "rings": rings,
                    "bbox": (min(xs), min(ys), max(xs), max(ys))})
    return out


def load_stores():
    """Grocery stores from OpenStreetMap, captured once into data/stores.json."""
    path = os.path.join(DATA, "stores.json")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return json.load(f)


def nearest_store(lat, lon, stores):
    """(miles, minutes, name) to the closest grocery store."""
    if not stores:
        return None, None, None
    best, bestd = None, 1e9
    for st in stores:
        # cheap planar approximation first - we only need the minimum
        d = (st["lat"] - lat) ** 2 + ((st["lon"] - lon) * 0.81) ** 2
        if d < bestd:
            bestd, best = d, st
    mi = haversine_mi((lat, lon), (best["lat"], best["lon"]))
    return round(mi, 1), max(3, int(round(mi * ERRAND_FACTOR))), best["n"]


def county_for(lat, lon, polys):
    for c in polys:
        x0, y0, x1, y1 = c["bbox"]
        if not (x0 <= lon <= x1 and y0 <= lat <= y1):
            continue
        for ring in c["rings"]:
            if point_in_ring(lon, lat, ring):
                return c["name"]
    return None


# ---------------------------------------------------------------- routing ---

def load_anchors():
    """Walmart / Kroger / Food City / Ingles / Publix / ALDI / Target / Food Lion.
    A store from one of these chains means a real town with proper shopping,
    which a rural IGA or Dollar General Market does not."""
    path = os.path.join(DATA, "anchors.json")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return json.load(f)


def _osrm_table(sources, dests, tries=4):
    """Road-network minutes, sources x dests. None on failure."""
    pts = sources + dests
    coords = ";".join(f"{p[1]:.5f},{p[0]:.5f}" for p in pts)       # lon,lat
    url = (OSRM + coords
           + "?sources=" + ";".join(str(i) for i in range(len(sources)))
           + "&destinations=" + ";".join(str(len(sources) + i) for i in range(len(dests)))
           + "&annotations=duration")
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=90) as r:
                d = json.load(r)
            if d.get("code") == "Ok":
                return [[(v / 60.0 if v is not None else None) for v in row]
                        for row in d["durations"]]
            log(f"  ! osrm: {d.get('code')} {d.get('message', '')[:80]}")
        except Exception as e:                               # noqa: BLE001
            code = getattr(e, "code", None)
            if attempt == tries - 1:
                log(f"  ! osrm failed: {e}")
            time.sleep(20 if code == 429 else 4 * (attempt + 1))
    return None


def add_drive_times(tracts, batch=99):
    """Real minutes to Knoxville for tracts that only have the estimate."""
    todo = [t for t in tracts if not t.get("driveReal")
            and t.get("lat") is not None and t.get("geo") == "parcel"]
    if not todo:
        return 0
    log(f"knoxville routing needed: {len(todo)}")
    done = 0
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        m = _osrm_table([(t["lat"], t["lon"]) for t in chunk], [KNOXVILLE])
        if m is None:
            break
        for t, row in zip(chunk, m):
            if row and row[0] is not None:
                t["driveRoad"] = round(row[0], 1)
                t["drive"] = max(5, int(round(row[0] * ROAD_CAL)))
                t["driveReal"] = True
                done += 1
        time.sleep(1.5)
    return done


def add_shop_times(tracts, anchors, cap=100):
    """Real minutes to the nearest anchor store, batched to stay under OSRM's
    coordinate limit: each request carries a set of tracts plus the union of
    their nearest candidate stores."""
    todo = [t for t in tracts if t.get("shopMin") is None
            and t.get("lat") is not None and t.get("geo") == "parcel"]
    if not todo or not anchors:
        return 0
    log(f"shopping routing needed: {len(todo)}")

    def nearest(t):
        return sorted(range(len(anchors)), key=lambda i:
                      (anchors[i]["lat"] - t["lat"]) ** 2
                      + ((anchors[i]["lon"] - t["lon"]) * 0.81) ** 2)[:SHOP_CANDIDATES]

    batches, cur_t, cur_a = [], [], set()
    for t in todo:
        cand = set(nearest(t))
        if cur_t and len(cur_t) + 1 + len(cur_a | cand) > cap:
            batches.append((cur_t, sorted(cur_a)))
            cur_t, cur_a = [], set()
        cur_t.append(t)
        cur_a |= cand
    if cur_t:
        batches.append((cur_t, sorted(cur_a)))

    done = 0
    for ts, ai in batches:
        m = _osrm_table([(t["lat"], t["lon"]) for t in ts],
                        [(anchors[i]["lat"], anchors[i]["lon"]) for i in ai])
        if m is None:
            break
        for t, row in zip(ts, m):
            best = None
            for j, mins in enumerate(row):
                if mins is not None and (best is None or mins < best[0]):
                    best = (mins, anchors[ai[j]])
            if best:
                mins, a = best
                t["shopRoad"] = round(mins, 1)
                t["shopMin"] = max(2, int(round(mins * ROAD_CAL)))
                t["shopName"] = a["n"]
                t["shopCity"] = a.get("city") or ""
                t["shopMi"] = round(haversine_mi((t["lat"], t["lon"]),
                                                 (a["lat"], a["lon"])), 1)
                done += 1
        time.sleep(1.5)
    return done


# ----------------------------------------------------------- convenience ---

def load_json(name, default):
    path = os.path.join(DATA, name)
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


def _within(lat, lon, pts, miles):
    """Points of `pts` within a straight-line radius, nearest first."""
    dl = miles / 69.0
    out = []
    for p in pts:
        if abs(p["lat"] - lat) > dl or abs(p["lon"] - lon) > dl * 1.3:
            continue
        d = haversine_mi((lat, lon), (p["lat"], p["lon"]))
        if d <= miles:
            out.append((d, p))
    out.sort(key=lambda x: x[0])
    return out


def nearest_town(lat, lon, towns):
    """Closest named place, preferring somewhere with people in it: a town
    beats a village at the same distance, and a village 3 mi off beats a
    city 30 mi off. Returns (town, miles)."""
    best, bestscore = None, 1e9
    # a real town first: 1,500 people or more (or tagged city/town) within 25
    # miles; only if there is none does a village or hamlet count
    real = [t for t in towns if (t.get("pop") or 0) >= 1500 or t.get("kind") in ("city", "town")]
    pool = [t for t in real if haversine_mi((lat, lon), (t["lat"], t["lon"])) <= 25] or towns
    for t in pool:
        d = haversine_mi((lat, lon), (t["lat"], t["lon"]))
        if d > 25:
            continue
        # a place with more people is 'closer' for this purpose
        pop = t.get("pop") or 200
        score = d / (1 + math.log10(pop) / 2)
        if score < bestscore:
            best, bestscore = t, score
    if not best:
        return None, None
    return best, round(haversine_mi((lat, lon), (best["lat"], best["lon"])), 1)


def convenience(t, towns, pois):
    """A 1-5 rating of everyday convenience, and the facts behind it.
    Counts are straight-line, so they say what is *around*, not the drive."""
    lat, lon = t["lat"], t["lon"]
    town, tmi = nearest_town(lat, lon, towns)
    near10 = _within(lat, lon, pois, 10)
    near5 = [(d, p) for d, p in near10 if d <= 5]
    kinds10 = {}
    for d, p in near10:
        kinds10.setdefault(p["k"], []).append(d)
    def n(k, lst=near10):
        return sum(1 for d, p in lst if p["k"] == k)
    def nearest(k):
        ds = kinds10.get(k)
        return round(ds[0], 1) if ds else None
    facts = {
        "townName": town["n"] if town else None,
        "townPop": town.get("pop") if town else None,
        "townKind": town.get("kind") if town else None,
        "townMi": tmi,
        "hospitalMi": min([x for x in (nearest("hospital"),) if x] or [None]) if nearest("hospital") else None,
        "clinicMi": nearest("clinic") or nearest("doctors"),
        "pharmacyMi": nearest("pharmacy"),
        "hardwareMi": nearest("hardware") or nearest("doityourself"),
        "farmMi": nearest("farm"),
        "fuelMi": nearest("fuel"),
        "restaurants10": n("restaurant") + n("fast_food") + n("cafe"),
        "schools10": n("school"),
        "fuel5": n("fuel", near5),
    }
    # --- score: shopping is the anchor, then health, then everyday errands.
    # Graded finely because the list is already filtered to decent shopping;
    # the stars have to separate "5 minutes from a Walmart and a hospital"
    # from "15 minutes from anything".
    pts = 0.0
    sm = t.get("shopMin")
    if sm is not None:
        pts += 2.0 if sm <= 5 else 1.5 if sm <= 10 else 1.0 if sm <= 15 else 0.4 if sm <= 25 else 0
    pm = facts["pharmacyMi"]
    if pm is not None:
        pts += 1.0 if pm <= 3 else 0.6 if pm <= 8 else 0.3
    hm = facts["hospitalMi"]
    if hm is not None:
        pts += 1.0 if hm <= 10 else 0.5
    elif facts["clinicMi"] is not None and facts["clinicMi"] <= 8:
        pts += 0.4
    hw = facts["hardwareMi"] if facts["hardwareMi"] is not None else facts["farmMi"]
    if hw is not None:
        pts += 0.5 if hw <= 8 else 0.25
    fm = facts["fuelMi"]
    if fm is not None:
        pts += 0.5 if fm <= 3 else 0.25
    r = facts["restaurants10"]
    pts += 1.0 if r >= 40 else 0.7 if r >= 15 else 0.4 if r >= 5 else 0.15 if r >= 1 else 0
    if facts["schools10"] >= 3:
        pts += 0.3
    score = max(1, min(5, int(round(pts / 6.3 * 5))))
    facts["convenience"] = score
    facts["convenienceLabel"] = {1: "remote", 2: "quiet", 3: "workable",
                                 4: "convenient", 5: "very convenient"}[score]
    return facts


CONV_VERSION = 3


def add_convenience(tracts, towns, pois):
    if not towns:
        return 0
    todo = [t for t in tracts if t.get("geo") == "parcel"
            and (t.get("convenience") is None or t.get("shopMin") != t.get("_convShop")
                 or t.get("_convV") != CONV_VERSION)]
    for t in todo:
        t.update(convenience(t, towns, pois))
        t["_convShop"] = t.get("shopMin")
        t["_convV"] = CONV_VERSION
    return len(todo)


# ------------------------------------------------------------------ flood ---

NFHL = ("https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"
        "?geometryType=esriGeometryPoint&inSR=4326&spatialRel=esriSpatialRelIntersects"
        "&outFields=FLD_ZONE,ZONE_SUBTY,SFHA_TF&returnGeometry=false&f=json&geometry=")
FLOOD_PAUSE = 0.6
FLOOD_CAP = int(os.environ.get("FLOOD_CAP", "150"))   # per run; FEMA is slow


def flood_at(lat, lon, tries=3):
    """FEMA flood zone at a point. Returns (class, zone, subtype) or None on
    failure. class: 'sfha' (100-yr floodplain), 'x500' (moderate), 'none'."""
    url = NFHL + f"{lon:.6f},{lat:.6f}"
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=40) as r:
                d = json.load(r)
            if "error" in d:
                raise RuntimeError(d["error"].get("message", "error"))
            feats = d.get("features") or []
            if not feats:
                return ("none", "", "")           # unmapped or outside any zone
            zone = sub = ""
            cls = "none"
            for f in feats:
                a = f["attributes"]
                z, st, sf = (a.get("FLD_ZONE") or ""), (a.get("ZONE_SUBTY") or ""), a.get("SFHA_TF")
                if sf == "T":
                    return ("sfha", z, st)
                if z == "X" and "0.2" in st.upper():
                    cls, zone, sub = "x500", z, st
                elif cls == "none":
                    zone, sub = z, st
            return (cls, zone, sub)
        except Exception as e:                               # noqa: BLE001
            if attempt == tries - 1:
                log(f"  ! nfhl failed at {lat},{lon}: {e}")
                return None
            time.sleep(3 * (attempt + 1))


def add_flood(tracts):
    """Fill the FEMA flood class on located tracts that don't have it."""
    todo = [t for t in tracts if t.get("flood") is None
            and t.get("lat") is not None and t.get("geo") == "parcel"]
    if not todo:
        return 0
    log(f"flood lookups needed: {len(todo)} (doing up to {FLOOD_CAP})")
    done, streak = 0, 0
    for t in todo[:FLOOD_CAP]:
        r = flood_at(t["lat"], t["lon"])
        if r is None:
            streak += 1                # one bad point: leave it unset, move on
            if streak >= 5:
                log("  nfhl: five failures running - service down, stopping")
                break
            continue
        streak = 0
        t["flood"], t["floodZone"], t["floodSub"] = r
        done += 1
        time.sleep(FLOOD_PAUSE)
    log(f"flood lookups done: {done}/{len(todo)}")
    return done


# ---------------------------------------------------------------- terrain ---

def _slope_grid(lat, lon):
    dlat = SLOPE_GRID_M / 111320.0
    dlon = SLOPE_GRID_M / (111320.0 * math.cos(math.radians(lat)))
    return [(lat + dy * dlat, lon + dx * dlon)
            for dy in (1, 0, -1) for dx in (-1, 0, 1)]     # north row first


def _elevations(points, tries=6):
    """Open-Meteo returns plain JSON numbers - no image decoding needed."""
    url = ("https://api.open-meteo.com/v1/elevation?latitude="
           + ",".join(f"{p[0]:.6f}" for p in points)
           + "&longitude=" + ",".join(f"{p[1]:.6f}" for p in points))
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)["elevation"]
        except Exception as e:                               # noqa: BLE001
            code = getattr(e, "code", None)
            if attempt == tries - 1:
                log(f"  ! elevation lookup failed: {e}")
                return None
            time.sleep(30 if code == 429 else 3 * (attempt + 1))


def _slope_deg(z):
    """Horn's method over a 3x3 grid, z1..z9, north row first."""
    z1, z2, z3, z4, z5, z6, z7, z8, z9 = z
    dzdx = ((z3 + 2 * z6 + z9) - (z1 + 2 * z4 + z7)) / (8 * SLOPE_GRID_M)
    dzdy = ((z1 + 2 * z2 + z3) - (z7 + 2 * z8 + z9)) / (8 * SLOPE_GRID_M)
    return math.degrees(math.atan(math.hypot(dzdx, dzdy)))


def add_terrain(tracts, batch=50):
    """Fill slope/elev/relief on tracts that don't have it yet."""
    todo = [t for t in tracts if t.get("slope") is None
            and t.get("lat") is not None and t.get("geo") == "parcel"]
    if not todo:
        return 0
    log(f"terrain lookups needed: {len(todo)}")
    pts, owner = [], []
    for t in todo:
        for p in _slope_grid(t["lat"], t["lon"]):
            pts.append(p)
            owner.append(t["id"])
    elevs = []
    for i in range(0, len(pts), batch):
        chunk = _elevations(pts[i:i + batch])
        if chunk is None:
            return 0                 # leave slope unset; nothing gets dropped
        elevs.extend(chunk)
        time.sleep(5.0)
    by = {}
    for tid, e in zip(owner, elevs):
        by.setdefault(tid, []).append(e)
    done = 0
    for t in todo:
        z = by.get(t["id"], [])
        if len(z) != 9 or any(v is None for v in z):
            continue
        t["slope"] = round(_slope_deg(z), 1)
        t["elev"] = round(z[4])
        t["relief"] = round(max(z) - min(z))
        done += 1
    return done


# ---------------------------------------------------------------- parsing ---

def parse_csv(text):
    """Redfin prepends a disclaimer line before the real rows sometimes."""
    if not text:
        return []
    lines = text.splitlines()
    start = 0
    for i, ln in enumerate(lines[:5]):
        if ln.startswith("SALE TYPE,"):
            start = i
            break
    body = "\n".join(lines[start:])
    rows = []
    for r in csv.DictReader(io.StringIO(body)):
        if r.get("SALE TYPE"):
            rows.append(r)
    return rows


def num(v, cast=float):
    try:
        return cast(str(v).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


URL_ID_RE = re.compile(r"/home/(\d+)")


def row_url(r):
    """Redfin's URL column has a long, brittle header - match it by prefix."""
    for k, v in r.items():
        if k and k.startswith("URL"):
            return (v or "").strip()
    return ""


def row_to_tract(r, polys, stores=()):
    """Turn one CSV row into a tract dict, or None if it fails the criteria."""
    url = row_url(r)
    m = URL_ID_RE.search(url)
    if not m:
        return None

    status = (r.get("STATUS") or "").strip().lower()
    if "active" not in status:
        return None
    ptype = (r.get("PROPERTY TYPE") or "").strip().lower()
    if ptype not in ("vacant land", "land"):
        return None

    price = num(r.get("PRICE"), int)
    lot = num(r.get("LOT SIZE"), float)
    lat = num(r.get("LATITUDE"))
    lon = num(r.get("LONGITUDE"))
    if not price or not lot or lat is None or lon is None:
        return None
    if price > MAX_PRICE:
        return None

    acres = round(lot / SQFT_PER_ACRE, 2)
    if acres < MIN_ACRES:
        return None

    # A listed square footage means a building is on it.
    if num(r.get("SQUARE FEET"), float):
        return None

    # No HOA - a fee means covenants and a subdivision.
    hoa = num(r.get("HOA/MONTH"), float)
    if EXCLUDE_HOA and hoa:
        return None
    if DWELLING_RE.search(r.get("ADDRESS") or ""):
        return None

    return finish_tract({
        "id": "rf" + m.group(1),
        "mls": (r.get("MLS#") or "").strip() or None,
        "acres": acres,
        "price": price,
        "address": (r.get("ADDRESS") or "").strip(),
        "town": (r.get("CITY") or "").strip(),
        "zip": (r.get("ZIP OR POSTAL CODE") or "").strip(),
        "url": url,
        "photo": None,
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "hoa": hoa or 0,
        "domSource": num(r.get("DAYS ON MARKET"), int),
        "source": (r.get("SOURCE") or "Redfin").strip(),
        "geo": "parcel",
    }, polys, stores)


def finish_tract(t, polys, stores):
    """County, distances, price band - everything derived from lat/lon/price.
    Returns None if the point falls outside the 12 counties."""
    lat, lon = t["lat"], t["lon"]
    county = county_for(lat, lon, polys)
    if county not in COUNTIES:
        return None
    miles = haversine_mi(KNOXVILLE, (lat, lon))
    gmi, gmin, gname = nearest_store(lat, lon, stores)
    ppa = int(round(t["price"] / t["acres"]))
    band = next(i for i, (hi, _, _) in enumerate(BANDS) if ppa < hi)
    t.update({
        "county": county, "ppa": ppa, "miles": round(miles, 1),
        "drive": max(5, int(round(miles * DRIVE_FACTOR))),
        "groceryMi": gmi, "groceryMin": gmin, "groceryName": gname,
        "status": "active", "bandIdx": band,
        "color": BANDS[band][2], "bandLabel": BANDS[band][1],
    })
    return t


def same_parcel(a, b):
    """The dedupe rule: one parcel syndicated to two sites shows the same
    price and (to a tenth of an acre) the same size in the same county."""
    tol = max(0.06, 0.02 * max(a["acres"], b["acres"]))
    return (a["price"] == b["price"] and a.get("county") == b.get("county")
            and abs(a["acres"] - b["acres"]) <= tol)


def merge_zillow(found, prev, excluded, polys, stores):
    """Second source. A Zillow listing either (a) puts a Redfin tract we only
    knew to the town onto its real parcel, (b) revives an archived tract the
    Redfin export stopped returning, or (c) is a listing Redfin never had."""
    import zillow
    upgraded, revived, added = 0, 0, 0
    for name in COUNTIES:
        try:
            rows = zillow.fetch_county(name, MAX_PRICE, int(MIN_ACRES * 43560))
        except Exception as e:                       # noqa: BLE001
            log(f"  ! zillow {name}: {e}")
            continue
        for z in rows:
            if z["price"] > MAX_PRICE or z["acres"] < MIN_ACRES:
                continue
            if EXCLUDE_HOA and z.get("hoa"):
                continue
            zt = finish_tract({
                "id": "z" + z["zpid"], "mls": None, "acres": z["acres"],
                "price": z["price"], "address": z["address"], "town": z["town"],
                "zip": z["zip"], "url": z["url"], "photo": z["photo"],
                "lat": z["lat"], "lon": z["lon"], "hoa": z.get("hoa") or 0,
                "domSource": z.get("dom"), "source": "Zillow",
                "gallery": z.get("gallery") or [], "geo": "parcel",
            }, polys, stores)
            if not zt or zt["id"] in excluded:
                continue
            # (a) same parcel already in this run's set
            hit = next((t for t in found.values() if same_parcel(t, zt)), None)
            if hit:
                if hit.get("geo") != "parcel" and not hit.get("coordSource"):
                    hit["lat"], hit["lon"] = zt["lat"], zt["lon"]
                    hit["geo"] = "parcel"
                    hit["coordSource"] = "Zillow"
                    hit["status"] = "active"
                    hit["missCount"] = 0
                    hit.pop("missDate", None)
                    for k in ("driveReal", "driveRoad", "shopMin", "shopRoad",
                              "shopName", "shopCity", "shopMi",
                              "slope", "elev", "relief"):
                        hit.pop(k, None)
                    hit["miles"], hit["drive"] = zt["miles"], zt["drive"]
                    upgraded += 1
                if not hit.get("photo") and zt.get("photo"):
                    hit["photo"] = zt["photo"]
                if zt.get("gallery") and not hit.get("gallery"):
                    hit["gallery"] = zt["gallery"]
                continue
            # (b) an archived tract Redfin's export dropped
            old = next((p for pid, p in prev.items() if pid not in found
                        and pid not in excluded and same_parcel(p, zt)), None)
            if old:
                t = dict(old)
                t.update({k: zt[k] for k in ("lat", "lon", "miles", "drive",
                                              "groceryMi", "groceryMin",
                                              "groceryName", "county")})
                t["geo"], t["coordSource"] = "parcel", "Zillow"
                t["status"], t["missCount"] = "active", 0
                t.pop("missDate", None)
                for k in ("driveReal", "driveRoad", "shopMin", "shopRoad",
                          "shopName", "shopCity", "shopMi",
                          "slope", "elev", "relief"):
                    t.pop(k, None)
                if not t.get("photo"):
                    t["photo"] = zt.get("photo")
                if zt.get("gallery"):
                    t["gallery"] = zt["gallery"]
                found[t["id"]] = t
                revived += 1
                continue
            # (c) genuinely new to us
            found[zt["id"]] = zt
            added += 1
        time.sleep(2.0)
    log(f"zillow: {upgraded} tracts placed on parcel, {revived} revived, "
        f"{added} only on Zillow")



# ------------------------------------------------------ listing-page check ---

LAT_RE = re.compile(r'"latitude"\s*:\s*(-?\d+\.\d+)')
LON_RE = re.compile(r'"longitude"\s*:\s*(-?\d+\.\d+)')
STATUS_RE = re.compile(r'"mlsStatus"\s*:\s*"([^"]{2,30})"')


def check_listing(t):
    """Fetch the listing page. Returns (id, lat, lon, status) - any may be None."""
    html = fetch(t["url"], tries=2, timeout=40)
    if not html:
        return t["id"], None, None, None
    la = LAT_RE.search(html)
    lo = LON_RE.search(html)
    st = STATUS_RE.search(html)
    return (t["id"],
            float(la.group(1)) if la else None,
            float(lo.group(1)) if lo else None,
            st.group(1).strip().lower() if st else None)


def refine_from_pages(found, polys, today):
    """Two jobs, one fetch each:
    - a tract we only know to the town gets its real parcel coordinates;
    - a tract Redfin's export stopped returning gets its status read off the
      page, so 'unconfirmed' resolves the same day instead of after 3 misses.
    Returns the list of tracts confirmed gone."""
    todo = sorted([t for t in found.values()
                   if t.get("geo") != "parcel" or t.get("status") == "unconfirmed"],
                  key=lambda t: t.get("drive", 999))
    if not todo:
        return []
    log(f"listing pages to check: {len(todo)}")
    gone, parsed = [], 0
    results = []
    for i, t in enumerate(todo[:PAGE_FETCH_CAP]):
        results.append(check_listing(t))
        time.sleep(PAGE_FETCH_PAUSE)
    for tid, la, lo, st in results:
            t = found[tid]
            if st is None and la is None:
                continue          # blocked or unparsable - leave it alone
            parsed += 1
            if st and st != "active":
                t["pageStatus"] = st
                gone.append(found.pop(tid))
                continue
            if st == "active" and t.get("status") == "unconfirmed":
                t["status"] = "active"
                t["missCount"] = 0
                t.pop("missDate", None)
                t["lastSeen"] = today
            if la is not None and lo is not None and t.get("geo") != "parcel":
                county = county_for(la, lo, polys)
                if county not in COUNTIES:
                    t["pageStatus"] = f"outside area ({county})"
                    gone.append(found.pop(tid))
                    continue
                t["lat"], t["lon"] = round(la, 6), round(lo, 6)
                t["county"] = county
                t["geo"] = "parcel"
                t["miles"] = round(haversine_mi(KNOXVILLE, (la, lo)), 1)
                # everything measured from the town centre is now wrong
                for k in ("driveReal", "driveRoad", "shopMin", "shopRoad",
                          "shopName", "shopCity", "shopMi",
                          "slope", "elev", "relief"):
                    t.pop(k, None)
                t["drive"] = max(5, int(round(t["miles"] * DRIVE_FACTOR)))
    log(f"listing pages parsed: {parsed}/{min(len(todo), PAGE_FETCH_CAP)}"
        + (f" | confirmed gone: {len(gone)}" if gone else ""))
    return gone


# ----------------------------------------------------------------- photos ---

OG_RE = re.compile(r'<meta property="og:image" content="([^"]+)"')


# Redfin photo-set ids seen so far for these MLS boards. The primary photo
# lives at a URL derivable from the MLS number, so no page fetch is needed.
PHOTO_MARKETS = ("177", "255", "145")


def _head_ok(url):
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except Exception:                                    # noqa: BLE001
        return False


def find_photo(t):
    mls = t.get("mls") or ""
    if mls.isdigit():
        for mk in PHOTO_MARKETS:
            u = (f"https://ssl.cdn-redfin.com/photo/{mk}/islphoto/{mls[-3:]}"
                 f"/genIslnoResize.{mls}_0.webp")
            if _head_ok(u):
                return t["id"], u
    html = fetch(t["url"], tries=1, timeout=30)
    if not html:
        return t["id"], None
    m = OG_RE.search(html)
    if not m:
        return t["id"], None
    u = m.group(1)
    if "logo" in u.lower() or "placeholder" in u.lower():
        return t["id"], None
    return t["id"], u


def ext_for(url):
    e = os.path.splitext(url.split("?")[0])[1].lower()
    return e if e in (".jpg", ".jpeg", ".png", ".webp") else ".jpg"


def download_gallery(t):
    urls = [u for u in (t.get("gallery") or []) if u]
    if not urls:
        return 0
    have = t.get("imgs") or []
    if len(have) >= len(urls):
        return 0
    out = []
    for i, u in enumerate(urls):
        local = os.path.join(PHOTOS, f"{t['id']}_{i}{ext_for(u)}")
        if os.path.exists(local) and os.path.getsize(local) > 2000:
            out.append("photos/" + os.path.basename(local))
            continue
        raw = fetch(u, tries=1, timeout=30, binary=True)
        if raw and len(raw) > 2000:
            with open(local, "wb") as f:
                f.write(raw)
            out.append("photos/" + os.path.basename(local))
    t["imgs"] = out
    return len(out)


def download_photo(t):
    """Save the photo locally so the site never depends on Redfin hotlinking."""
    if not t.get("photo"):
        return False
    local = os.path.join(PHOTOS, t["id"] + ext_for(t["photo"]))
    if os.path.exists(local) and os.path.getsize(local) > 2000:
        t["img"] = "photos/" + os.path.basename(local)
        return True
    raw = fetch(t["photo"], tries=2, timeout=30, binary=True)
    if not raw or len(raw) < 2000:
        return False
    with open(local, "wb") as f:
        f.write(raw)
    t["img"] = "photos/" + os.path.basename(local)
    return True


# ------------------------------------------------------------------- main ---

def load_excluded():
    """Tracts we have already measured and thrown out (too steep). Remembered
    so the next run doesn't route and survey them all over again."""
    path = os.path.join(DATA, "excluded.json")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def load_previous():
    path = os.path.join(DATA, "tracts.json")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        prev = json.load(f)
    return {t["id"]: t for t in prev.get("tracts", [])}


def main():
    dry = "--dry-run" in sys.argv
    today = date.today().isoformat()
    polys = load_counties()
    stores = load_stores()
    anchors = load_anchors()
    log(f"grocery stores: {len(stores)} | anchor stores: {len(anchors)}")
    prev = load_previous()
    excluded = load_excluded()
    log(f"previous dataset: {len(prev)} tracts | remembered exclusions: {len(excluded)}")

    # ---- sweep ----------------------------------------------------------
    found = {}
    returned = set()        # every id Redfin gave back, passing or not
    for name, rid in COUNTIES.items():
        text = fetch(redfin_csv(rid))
        rows = parse_csv(text)
        kept = 0
        for r in rows:
            m = URL_ID_RE.search(row_url(r))
            if m:
                returned.add("rf" + m.group(1))
            t = row_to_tract(r, polys, stores)
            if t and t["id"] in excluded:
                continue
            if t and t["id"] not in found:
                found[t["id"]] = t
                kept += 1
        log(f"  {name:<10} {len(rows):>4} rows -> {kept:>3} kept "
            f"(running total {len(found)})")
        time.sleep(1.0)

    if not found:
        log("FATAL: swept zero tracts - refusing to overwrite the archive.")
        return 2

    # ---- second source ---------------------------------------------------
    if os.environ.get("SWEEP_SKIP_ZILLOW"):
        log("zillow: skipped (SWEEP_SKIP_ZILLOW set)")
    else:
        merge_zillow(found, prev, excluded, polys, stores)

    # ---- merge with the archive -----------------------------------------
    # Redfin's CSV export carries the notice "some MLS listings are not
    # included in the download", and we have confirmed it omits listings that
    # are still active. So a tract missing from one sweep is NOT proof it
    # sold. We mark it unconfirmed and only retire it after MISS_LIMIT
    # consecutive misses.
    new_ids, cuts, bumps = [], [], []
    for tid, t in found.items():
        old = prev.get(tid)
        t.setdefault("geo", "parcel")   # Redfin CSV rows carry parcel coords
        t["missCount"] = 0
        t.pop("missDate", None)
        t["status"] = "active"
        if not old:
            t["firstSeen"] = today
            t["priceHistory"] = [{"date": today, "price": t["price"]}]
            if prev:
                new_ids.append(tid)
            else:
                # First run ever: everything is "found", nothing is news.
                t["baseline"] = True
        else:
            t["firstSeen"] = old.get("firstSeen", today)
            hist = list(old.get("priceHistory") or [])
            oldprice = hist[-1]["price"] if hist else old.get("price")
            if oldprice != t["price"]:
                hist.append({"date": today, "price": t["price"]})
                delta = t["price"] - oldprice
                rec = {"id": tid, "from": oldprice, "to": t["price"],
                       "delta": delta}
                (cuts if delta < 0 else bumps).append(rec)
            t["priceHistory"] = hist
            # carry the photo forward - no refetch when we already have one
            if old.get("photo"):
                t["photo"] = old["photo"]
            if old.get("img"):
                t["img"] = old["img"]
            if old.get("baseline"):
                t["baseline"] = True
            # Terrain and routing never change and cost API calls - carry them.
            for k in ("gallery", "imgs", "flood", "floodZone", "floodSub",
                      "slope", "elev", "relief", "shopRoad",
                      "shopMin", "shopName", "shopCity", "shopMi", "driveRoad"):
                if old.get(k) is not None:
                    t[k] = old[k]
            if old.get("driveReal"):
                t["drive"] = old["drive"]
                t["driveReal"] = True
        t["lastSeen"] = today

    # ---- tracts the sweep did not return --------------------------------
    stale, retired, rejected = [], [], []
    skip_z = bool(os.environ.get("SWEEP_SKIP_ZILLOW"))
    for tid, old in prev.items():
        if tid in found:
            continue
        if skip_z and old.get("source") == "Zillow":
            # Zillow wasn't consulted this run, so its absence means nothing.
            found[tid] = dict(old)
            continue
        if tid in returned:
            # Redfin still lists it, but it no longer meets the criteria
            # (an HOA appeared, price rose, acreage was corrected). Drop it
            # now - there is nothing uncertain about this one.
            rejected.append(old)
            continue
        # Re-running on the same day must not double-count a miss.
        if old.get("missDate") == today:
            miss = int(old.get("missCount") or 1)
        else:
            miss = int(old.get("missCount") or 0) + 1
        if miss >= MISS_LIMIT:
            retired.append(old)
            continue
        carry = dict(old)
        carry["missCount"] = miss
        carry["missDate"] = today
        carry["status"] = "unconfirmed"
        carry.setdefault("geo", "town")
        carry["lastSeen"] = old.get("lastSeen", old.get("firstSeen", today))
        found[tid] = carry
        stale.append(carry)

    # carried-forward tracts predate the grocery data - fill it in once
    for t in found.values():
        if t.get("groceryMin") is None and t.get("lat") is not None:
            gmi, gmin, gname = nearest_store(t["lat"], t["lon"], stores)
            t["groceryMi"], t["groceryMin"], t["groceryName"] = gmi, gmin, gname

    # ---- listing pages: real coordinates, and the actual MLS status -------
    page_gone = refine_from_pages(found, polys, today)
    stale = [g for g in stale if g["id"] in found]

    # ---- real road times: to Knoxville, and to the nearest real town ------
    add_drive_times(list(found.values()))
    add_shop_times(list(found.values()), anchors)

    # ---- too far: by real road time, from a real town or from Knoxville --
    far = []
    for tid in list(found):
        t = found[tid]
        why = None
        if t.get("geo") != "parcel":
            continue          # a town-centre pin can't be judged on distance
        if t.get("driveReal") and t["drive"] > MAX_DRIVE_MIN:
            why = f"{t['drive']} min to Knoxville"
        elif t.get("shopMin") is not None and t["shopMin"] > MAX_SHOP_MIN:
            why = f"{t['shopMin']} min to {t.get('shopName')}, {t.get('shopCity')}"
        if why:
            far.append(found.pop(tid))
            excluded[tid] = {"reason": "too far", "detail": why,
                             "on": today, "town": t.get("town")}
    if far:
        log(f"dropped as too far by road: {len(far)}")
        dropped = {g["id"] for g in far}
        new_ids = [i for i in new_ids if i not in dropped]
        cuts = [c for c in cuts if c["id"] not in dropped]
        bumps = [b for b in bumps if b["id"] not in dropped]

    # ---- terrain: slope decides whether it is buildable at all -----------
    add_terrain(list(found.values()))
    steep = []
    for tid in list(found):
        sl = found[tid].get("slope")
        if sl is not None and sl > MAX_SLOPE_DEG:
            steep.append(found.pop(tid))
    if steep:
        log(f"dropped as too steep (>{MAX_SLOPE_DEG} deg): {len(steep)}")
        for g in steep:
            excluded[g["id"]] = {"reason": "too steep", "slope": g.get("slope"),
                                 "on": today, "town": g.get("town")}
        # A dropped tract must not linger in the new/price-change lists,
        # which are looked up against `found` when the report is built.
        dropped = {g["id"] for g in steep}
        new_ids = [i for i in new_ids if i not in dropped]
        cuts = [c for c in cuts if c["id"] not in dropped]
        bumps = [b for b in bumps if b["id"] not in dropped]

    # ---- flood: FEMA zone at the pin --------------------------------------
    add_flood(list(found.values()))

    # ---- nearest town and everyday convenience (local data, recomputed) ----
    n = add_convenience(list(found.values()),
                        load_json("towns.json", []), load_json("pois.json", []))
    log(f"convenience computed: {n}")

    # ---- photos: only look up the ones we don't have ---------------------
    need = [t for t in found.values() if not t.get("photo")]
    log(f"photo lookups needed: {len(need)}")
    if need:
        with ThreadPoolExecutor(6) as ex:
            for tid, url in ex.map(find_photo, need):
                if url:
                    found[tid]["photo"] = url

    os.makedirs(PHOTOS, exist_ok=True)
    todl = [t for t in found.values() if t.get("photo") and not t.get("img")]
    if todl and not dry:
        with ThreadPoolExecutor(6) as ex:
            got = sum(1 for ok in ex.map(download_photo, todl) if ok)
        log(f"photos downloaded: {got}/{len(todl)}")
    # Galleries are hotlinked from Zillow's CDN rather than committed - eight
    # 768px frames per listing would add ~100 MB to the repo. The primary
    # photo is always stored locally, so a card never goes blank.
    else:
        # still resolve img paths for anything already on disk
        for t in found.values():
            if t.get("photo") and not t.get("img"):
                cand = os.path.join(PHOTOS, t["id"] + ext_for(t["photo"]))
                if os.path.exists(cand):
                    t["img"] = "photos/" + os.path.basename(cand)

    # ---- days on list ----------------------------------------------------
    for t in found.values():
        seen = (date.today()
                - datetime.fromisoformat(t["firstSeen"]).date()).days
        t["daysListed"] = t["domSource"] if t["domSource"] is not None else seen

    tracts = sorted(found.values(), key=lambda t: t["ppa"])

    report = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "date": today,
        "count": len(tracts),
        "confirmed": sum(1 for t in tracts if t.get("status") == "active"),
        "new": [
            {k: found[i][k] for k in
             ("id", "acres", "price", "ppa", "town", "county", "url")}
            for i in new_ids
        ],
        "priceCuts": [
            {**c, **{k: found[c["id"]][k] for k in
                     ("acres", "town", "county", "url")}} for c in cuts
        ],
        "priceIncreases": [
            {**b, **{k: found[b["id"]][k] for k in
                     ("acres", "town", "county", "url")}} for b in bumps
        ],
        "unconfirmed": [
            {**{k: g.get(k) for k in
                ("id", "acres", "price", "town", "county", "url")},
             "missCount": g.get("missCount")} for g in stale
        ],
        "retired": [
            {k: g.get(k) for k in
             ("id", "acres", "price", "town", "county", "url")}
            for g in retired
        ],
        "gone": [
            {**{k: g.get(k) for k in
                ("id", "acres", "price", "town", "county", "url")},
             "status": g.get("pageStatus")} for g in page_gone
        ],
        "tooFar": [
            {**{k: g.get(k) for k in
                ("id", "acres", "price", "town", "county", "url")},
             "drive": g.get("drive"), "shopMin": g.get("shopMin")} for g in far
        ],
        "tooSteep": [
            {**{k: g.get(k) for k in
                ("id", "acres", "price", "town", "county", "url")},
             "slope": g.get("slope")} for g in steep
        ],
        "rejected": [
            {k: g.get(k) for k in
             ("id", "acres", "price", "town", "county", "url")}
            for g in rejected
        ],
        "baseline": not prev,
    }

    confirmed = sum(1 for t in tracts if t["status"] == "active")
    log(f"\n{len(tracts)} tracts | {confirmed} confirmed active | "
        f"{len(stale)} unconfirmed | new {len(new_ids)} | cuts {len(cuts)} | "
        f"rejected {len(rejected)} | gone {len(page_gone)} | far {len(far)} | "
        f"steep {len(steep)} | "
        f"retired {len(retired)}")

    if dry:
        log("(dry run - nothing written)")
        return 0

    os.makedirs(DATA, exist_ok=True)
    with open(os.path.join(DATA, "tracts.json"), "w") as f:
        json.dump({"generated": report["generated"], "date": today,
                   "count": len(tracts), "criteria": {
                       "maxPrice": MAX_PRICE, "minAcres": MIN_ACRES,
                       "counties": sorted(COUNTIES)},
                   "tracts": tracts}, f, indent=1)
    with open(os.path.join(DATA, "report.json"), "w") as f:
        json.dump(report, f, indent=1)
    with open(os.path.join(DATA, "excluded.json"), "w") as f:
        json.dump(excluded, f, indent=1)
    log("wrote data/tracts.json and data/report.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
