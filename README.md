# Knoxville Land Scout

Raw land near Knoxville, Tennessee — 10+ acres, under $250,000, roughly 45 minutes out.
Swept from Redfin every morning, mapped, and published free on GitHub Pages.

**Live site:** https://msaade12.github.io/knoxville-land/

---

## What it does

Every day at 11:15 UTC (~6:15am Central) a GitHub Action runs `scripts/sweep.py`, which:

1. Pulls active vacant-land listings from **two sources**: Redfin's CSV export for the
   12 counties, then Zillow's county land pages (`scripts/zillow.py`). Zillow's page
   embeds every listing's coordinates and photo carousel, so no per-listing fetch is
   needed. The two are matched as one parcel when county, price and acreage (±0.06)
   agree: Zillow then supplies coordinates for a Redfin tract we only knew to the town,
   revives an archived tract Redfin's export dropped, or — if Redfin never had it — adds
   it with a `z…` id. Redfin's export omits whole MLS boards (Sevier County came back with
   3 of 16); Zillow fills that gap.
2. Filters to the criteria: 10+ acres, ≤ $250k, ACTIVE, no building square footage.
3. Assigns each parcel to a county by point-in-polygon against `data/counties.json`.
4. Diffs against the previous run — what's new, what changed price, what vanished.
5. Finds each listing's photo on Redfin's CDN straight from its MLS number (no page
   fetch — Redfin serves a bot challenge to page bursts) and downloads it into `photos/`.
6. Commits `data/tracts.json`, `data/report.json` and any new photos.

The commit republishes the site. The whole run takes about 15 seconds. `lastSeen` is
stamped on every tract each run, so there is normally one small commit a day even when
nothing moved — read the commit message (`sweep 2026-09-16: 0 new, 78 active`) or the
run summary to see whether anything actually changed.

A written summary of each run lands in the **Actions** tab, under the run's Summary.

## Search criteria

| Rule | Value |
|---|---|
| Counties | Knox, Blount, Loudon, Anderson, Union, Grainger, Jefferson, Sevier, Roane, Monroe, Campbell, Morgan |
| Acreage | Collected from 0.1 acre so the slider can go that low; the site defaults to 10 |
| Price | $250,000 maximum |
| Status | Active only |
| Type | Vacant land — anything with a listed building square footage is dropped |
| HOA | Excluded outright. Any listing with a monthly HOA fee is dropped (`EXCLUDE_HOA`) |
| Shopping | Real road-network drive time to the nearest Walmart / Kroger / Food City / Ingles / Publix / ALDI / Target. The site filters to 15 minutes by default — land *around* a real town, not deep in the hollows |
| Drive | Real road-network minutes to downtown Knoxville, not a straight-line estimate |

Tracts beyond 45 minutes are still collected; the site just filters them out by default.

---

## The map

- **Four basemaps** — satellite, terrain, streets, plain. Satellite is the default and
  carries a place-label overlay.
- **Pins** are coloured by price per acre, sized by acreage, and the number inside each
  one is the estimated drive time from Knoxville.
- **A solid pin** sits on the real parcel coordinates. **A dashed pin** is a tract we
  only know to the town, placed at the town centre.
- **Dashed rings** mark straight-line 15/30/45-minute distances. They are not road
  distances and the legend says so.
- **Click any photo** — on a card or in a popup — to open the full-size viewer, then use
  the arrow keys to page through every tract currently filtered in.

## Hiding listings

Hit **Hide** on any card or popup. Hides save to this browser immediately.

To carry them between your phone and your desktop, press **Sync** and paste a GitHub
[fine-grained personal access token](https://github.com/settings/tokens?type=beta) with
**Contents: read & write** scoped to this one repository. Hides then round-trip through
`data/hidden.json`. The token is kept in your browser's local storage and is only ever
sent to `api.github.com` — it is never committed and never leaves your device otherwise.

Without a token everything still works; hides just stay on that one device.

## "New" listings

A tract is tagged **NEW** for 10 days after the first sweep that saw it. The 110 tracts
imported from the original archive are flagged `"baseline": true` so they are not all
falsely tagged new on day one.

---

## A caveat that matters

Redfin's CSV export prints this notice:

> In accordance with local MLS rules, some MLS listings are not included in the download

That is real. On the first run, 33 of the 110 archived tracts were missing from the export
even though they had been collected the same day. So **a tract vanishing from one sweep is
not proof it sold.**

The sweep therefore never deletes on a single miss. A missing tract is marked
`status: "unconfirmed"`, kept on the map with a dashed pin and an *unconfirmed* tag, and
only retired after **3 consecutive misses** (`MISS_LIMIT` in `scripts/sweep.py`). The run
summary lists them with their miss count so you can spot-check any that matter.

---

## Layout

```
index.html              the app
assets/app.css          styling
assets/app.js           map, filters, hides, photo viewer
data/tracts.json        the dataset — also the archive the next run diffs against
data/report.json        what changed on the last run
data/hidden.json        hidden listings, written by the page via the GitHub API
data/counties.json      East TN county polygons (54 counties, 12 flagged in scope)
data/anchors.json       174 anchor stores (Walmart, Kroger, Food City…) — what "shopping" means
data/stores.json        301 supermarkets of any kind — fallback when routing is unavailable
photos/rf*.webp         one photo per listing, served same-origin
scripts/sweep.py        the daily sweep — stdlib only, no dependencies
scripts/zillow.py       Zillow county search: coordinates, status, photo carousel
scripts/report.py       renders report.json as Markdown for the Actions summary
.github/workflows/daily.yml
```

## Running it by hand

```bash
python3 scripts/sweep.py            # sweep and write the data files
python3 scripts/sweep.py --dry-run  # sweep and print the summary, write nothing
python3 scripts/report.py           # re-print the last run as Markdown
python3 -m http.server 8000         # then open http://localhost:8000
```

You can also trigger the real thing from the **Actions** tab → *Daily land sweep* →
*Run workflow*.

## Changing the search

Everything tunable is at the top of `scripts/sweep.py`: `MAX_PRICE`, `MIN_ACRES`,
`COUNTIES` (name → Redfin region id), `MISS_LIMIT`, `DRIVE_FACTOR` and the `BANDS`
price-per-acre colour scale. The site reads the bands from `assets/app.js`, so change
both if you re-cut them.

## Drive times are real

Both drive numbers come from the OSRM road network, not from straight lines:

- `drive` — minutes to downtown Knoxville. This is the number inside every pin.
- `shopMin` / `shopName` / `shopCity` / `shopMi` — minutes to the nearest **anchor store**.

The public OSRM server runs about 15% slower than real-world times on these roads
(checked against four known routes; every ratio came out 0.85–0.86), so the raw road
minutes are kept as `driveRoad` / `shopRoad` and the displayed `drive` / `shopMin` are
calibrated by `ROAD_CAL = 0.86`.

Straight-line estimates were badly wrong in lake and ridge country: a Sharps Chapel tract
came out at "13 min to Food City" on a straight line and is **32 minutes** by road, because
Norris Lake sits in the way. Every tract now carries the routed figure. Routing is done
once per tract and carried forward, so the daily run only routes genuinely new listings.
If OSRM is unreachable the tract keeps its straight-line estimate and is marked as such
(`driveReal` absent) rather than being dropped.

### What counts as "shopping"

`data/anchors.json` holds 174 stores from chains whose presence means a real town:
Walmart, Kroger, Food City, Ingles, Publix, ALDI, Food Lion, Target. A rural IGA, a
Save-A-Lot or a Dollar General Market in a hamlet does **not** count — the owner wants land
on the fringe of a town, not deep in the hollows with a country store as the only option.
`data/stores.json` (all 301 supermarkets) is kept as the fallback when routing is
unavailable. Both were captured once from OpenStreetMap; anchor town names were filled from
Nominatim.

## Terrain

Every tract carries `slope` (degrees), `elev` (metres) and `relief`, computed from
Open-Meteo's elevation service over a 3×3 grid 150 m apart around the listing's point.
That describes the hillside the parcel sits on, not any single spot on it — Redfin gives
one coordinate, not a parcel boundary, so treat it as a strong hint rather than a survey.

Across the current set: median 3.8°, max 23.2°. **Anything above `MAX_SLOPE_DEG` (20°) is
dropped** as mountainside. 15–20° is ordinary East Tennessee "steep" and is kept, with a
*steep* tag on the card. Terrain is carried forward between runs, so the elevation service
is only called for genuinely new tracts.

## Verified vs awaiting check

A tract is **verified** when it is on a real parcel coordinate and Redfin's export returned
it this run. The site shows only verified tracts by default ("Verified only"). The rest —
inherited from the original archive on town-centre pins, or missing from the export — are
**awaiting page check**: each run reads up to 12 listing pages (Redfin blocks bursts) and
either places the tract on its real parcel, or confirms from the page's MLS status that it
is gone. Nothing on a town-centre pin is ever judged on distance.

## What gets dropped, and why

The sweep distinguishes three very different things:

- **too far** — by real road, over 60 min to Knoxville or over 15 min to an anchor
  store. Judged only on a real parcel coordinate, never a town pin. Remembered in
  `data/excluded.json` — delete an entry to reconsider that tract.
- **gone** — the listing page's own MLS status is not Active. Dropped the same day.
- **too steep** — average slope above 20°. Dropped every run; listed in the summary.
- **rejected** — Redfin still lists it, but it no longer meets the criteria (an HOA
  appeared, the price rose, the acreage was corrected). Dropped immediately; there is
  nothing uncertain about it.
- **unconfirmed** — Redfin did not return it at all. Might be sold, might be the export
  omitting it. Kept, tagged, retired after 3 consecutive misses.
- **retired** — unconfirmed three runs running. Gone.

## Known gaps

- **Redfin + Zillow only.** LandSearch, LandWatch, Land.com and LandsOfAmerica all
  return 403 to non-browser requests; Redfin's listing pages serve a bot challenge to
  anything beyond one request per session; Redfin's detail APIs are 403. Craigslist FSBO
  and the auction houses are not swept. Owner-financing and by-owner flags came from
  those sources, so those filters are not on the site.
- **Galleries are hotlinked.** A Zillow-matched tract shows its whole carousel in the
  viewer, served from `photos.zillowstatic.com`; only the primary photo is committed.
- **Photos are one per listing.** Redfin's CDN serves the same image at every photo index
  for these land listings, so there is no gallery to pull.
- **No parcel boundaries.** Redfin gives a point, not a polygon.
