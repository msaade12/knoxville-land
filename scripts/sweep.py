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
MIN_ACRES = 10.0
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

# Local errand roads wind more than the run into Knoxville, so the grocery
# estimate uses a heavier factor (~37 mph effective over straight-line distance).
ERRAND_FACTOR = 1.60

# A listing with a monthly HOA fee is excluded outright.
EXCLUDE_HOA = True

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

    county = county_for(lat, lon, polys)
    if county not in COUNTIES:
        return None

    miles = haversine_mi(KNOXVILLE, (lat, lon))
    gmi, gmin, gname = nearest_store(lat, lon, stores)
    ppa = int(round(price / acres))
    band = next(i for i, (hi, _, _) in enumerate(BANDS) if ppa < hi)

    return {
        "id": "rf" + m.group(1),
        "mls": (r.get("MLS#") or "").strip() or None,
        "acres": acres,
        "price": price,
        "ppa": ppa,
        "address": (r.get("ADDRESS") or "").strip(),
        "town": (r.get("CITY") or "").strip(),
        "county": county,
        "zip": (r.get("ZIP OR POSTAL CODE") or "").strip(),
        "url": url,
        "photo": None,
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "miles": round(miles, 1),
        "drive": max(5, int(round(miles * DRIVE_FACTOR))),
        "hoa": hoa or 0,
        "groceryMi": gmi,
        "groceryMin": gmin,
        "groceryName": gname,
        "domSource": num(r.get("DAYS ON MARKET"), int),
        "source": (r.get("SOURCE") or "Redfin").strip(),
        "status": "active",
        "bandIdx": band,
        "color": BANDS[band][2],
        "bandLabel": BANDS[band][1],
    }


# ----------------------------------------------------------------- photos ---

OG_RE = re.compile(r'<meta property="og:image" content="([^"]+)"')


def find_photo(t):
    html = fetch(t["url"], tries=2, timeout=30)
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
    log(f"grocery stores loaded: {len(stores)}")
    prev = load_previous()
    log(f"previous dataset: {len(prev)} tracts")

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
            if t and t["id"] not in found:
                found[t["id"]] = t
                kept += 1
        log(f"  {name:<10} {len(rows):>4} rows -> {kept:>3} kept "
            f"(running total {len(found)})")
        time.sleep(1.0)

    if not found:
        log("FATAL: swept zero tracts - refusing to overwrite the archive.")
        return 2

    # ---- merge with the archive -----------------------------------------
    # Redfin's CSV export carries the notice "some MLS listings are not
    # included in the download", and we have confirmed it omits listings that
    # are still active. So a tract missing from one sweep is NOT proof it
    # sold. We mark it unconfirmed and only retire it after MISS_LIMIT
    # consecutive misses.
    new_ids, cuts, bumps = [], [], []
    for tid, t in found.items():
        old = prev.get(tid)
        t["geo"] = "parcel"          # real Redfin parcel coordinates
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
        t["lastSeen"] = today

    # ---- tracts the sweep did not return --------------------------------
    stale, retired, rejected = [], [], []
    for tid, old in prev.items():
        if tid in found:
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
        f"rejected {len(rejected)} | retired {len(retired)}")

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
    log("wrote data/tracts.json and data/report.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
