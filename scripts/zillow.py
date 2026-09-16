#!/usr/bin/env python3
"""
Zillow county land search, as a second source alongside Redfin.

Zillow's county land pages embed their search results as JSON in a
<script id="__NEXT_DATA__"> block, including each listing's coordinates, so no
per-listing page fetch is needed. Filters and paging go through the
searchQueryState query parameter; the SEO-style path filters are ignored.

Zillow rate-limits hard. Pages are fetched sequentially with a pause, and a
county stops cleanly at the first page that fails - we keep what we got.
"""

import json
import re
import sys
import time
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

COUNTY_SLUGS = {
    "Knox": "knox-county-tn", "Blount": "blount-county-tn",
    "Loudon": "loudon-county-tn", "Anderson": "anderson-county-tn",
    "Union": "union-county-tn", "Grainger": "grainger-county-tn",
    "Jefferson": "jefferson-county-tn", "Sevier": "sevier-county-tn",
    "Roane": "roane-county-tn", "Monroe": "monroe-county-tn",
    "Campbell": "campbell-county-tn", "Morgan": "morgan-county-tn",
}

PAGE_PAUSE = 4.0
PHOTO_SIZE = "cc_ft_768"     # zillowstatic size suffix; p_e is the small card
GALLERY_MAX = 8
MAX_PAGES = 20          # Zillow itself stops at 20
NEXT_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
ACRES_RE = re.compile(r"([\d.,]+)\s*(acres?|sqft|sq\.? ?ft)", re.I)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def _url(slug, page, max_price, min_lot_sqft):
    state = {
        "pagination": {"currentPage": page},
        "isMapVisible": False,
        "filterState": {
            "price": {"max": max_price},
            "lot": {"min": min_lot_sqft},
            "sort": {"value": "days"},
        },
    }
    q = urllib.parse.quote(json.dumps(state, separators=(",", ":")))
    return f"https://www.zillow.com/{slug}/land/?searchQueryState={q}"


def _get(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "text/html,*/*",
        "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def _listings(html):
    """Every object in __NEXT_DATA__ that looks like a listing card."""
    m = NEXT_RE.search(html)
    if not m:
        return [], None
    data = json.loads(m.group(1))
    out = []

    def walk(o):
        if isinstance(o, dict):
            if "latLong" in o and "price" in o and "zpid" in o:
                out.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(data)
    tot = re.search(r'"totalResultCount":(\d+)', html)
    return out, int(tot.group(1)) if tot else None


def _acres(card):
    info = (card.get("hdpData") or {}).get("homeInfo") or {}
    v, u = info.get("lotAreaValue"), (info.get("lotAreaUnit") or "").lower()
    if v:
        return round(v / 43560.0, 2) if u.startswith("sq") else round(float(v), 2)
    m = ACRES_RE.search(card.get("lotAreaString") or "")
    if m:
        n = float(m.group(1).replace(",", ""))
        return round(n / 43560.0, 2) if m.group(2).lower().startswith("sq") else round(n, 2)
    return None


def _price(card):
    p = card.get("unformattedPrice")
    if p is None:
        info = (card.get("hdpData") or {}).get("homeInfo") or {}
        p = info.get("price")
    if p is None:
        m = re.search(r"[\d,]+", str(card.get("price") or ""))
        p = m.group(0).replace(",", "") if m else None
    try:
        return int(float(p))
    except (TypeError, ValueError):
        return None


def to_tract(card):
    """Normalise a Zillow card. Returns None for anything not a live land sale."""
    if card.get("statusType") not in ("FOR_SALE", None):
        return None
    info = (card.get("hdpData") or {}).get("homeInfo") or {}
    if info.get("homeType") and info["homeType"] not in ("LOT", "LAND"):
        return None
    ll = card.get("latLong") or {}
    lat, lon = ll.get("latitude"), ll.get("longitude")
    price, acres = _price(card), _acres(card)
    if lat is None or lon is None or not price or not acres:
        return None
    url = card.get("detailUrl") or ""
    if url.startswith("/"):
        url = "https://www.zillow.com" + url
    img = card.get("imgSrc") or None
    if img and "logo" in img.lower():
        img = None
    # the card carries the whole carousel as photo keys - keep a gallery
    car = card.get("carouselPhotosComposable") or {}
    base = car.get("baseUrl") or ""
    gallery = [base.replace("{photoKey}", p["photoKey"])
               for p in (car.get("photoData") or []) if p.get("photoKey")]
    gallery = [g.replace("-p_e.jpg", f"-{PHOTO_SIZE}.jpg") for g in gallery[:GALLERY_MAX]]
    if img:
        img = img.replace("-p_e.jpg", f"-{PHOTO_SIZE}.jpg")
    return {
        "zpid": str(card.get("zpid")),
        "address": (card.get("addressStreet") or card.get("address") or "").strip(),
        "town": (card.get("addressCity") or "").strip(),
        "zip": (card.get("addressZipcode") or "").strip(),
        "price": price,
        "acres": acres,
        "lat": round(float(lat), 6),
        "lon": round(float(lon), 6),
        "url": url,
        "photo": img,
        "gallery": gallery,
        "dom": info.get("daysOnZillow"),
        "hoa": info.get("hoaFee") or info.get("monthlyHoaFee") or 0,
    }


def fetch_county(name, max_price, min_lot_sqft=4356):
    """All live land listings Zillow shows for a county, under max_price."""
    slug = COUNTY_SLUGS[name]
    seen, rows, total = set(), [], None
    for page in range(1, MAX_PAGES + 1):
        try:
            html = _get(_url(slug, page, max_price, min_lot_sqft))
        except Exception as e:                       # noqa: BLE001
            log(f"  zillow {name} p{page}: stopped ({getattr(e, 'code', e)})")
            break
        cards, tot = _listings(html)
        total = total or tot
        fresh = 0
        for c in cards:
            t = to_tract(c)
            if t and t["zpid"] not in seen:
                seen.add(t["zpid"])
                rows.append(t)
                fresh += 1
        if not cards or fresh == 0:
            break
        if total is not None and len(seen) >= total:
            break
        time.sleep(PAGE_PAUSE)
    log(f"  zillow {name:<10} {len(rows):>4} live land listings"
        f"{'' if total is None else f' of {total} reported'}")
    return rows


if __name__ == "__main__":
    county = sys.argv[1] if len(sys.argv) > 1 else "Morgan"
    rows = fetch_county(county, 250_000)
    for r in rows[:8]:
        print(f"  {r['acres']:>7} ac  ${r['price']:>8,}  {r['town']:<15} {r['address'][:34]:<34} "
              f"({r['lat']}, {r['lon']})  photo={'y' if r['photo'] else '-'}")
    print(f"... {len(rows)} total")
