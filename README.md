# Knoxville Land Scout

Raw land near Knoxville, Tennessee — 10+ acres, under $250,000, roughly 45 minutes out.
Swept from Redfin every morning, mapped, and published free on GitHub Pages.

**Live site:** https://msaade12.github.io/knoxville-land/

---

## What it does

Every day at 11:15 UTC (~6:15am Central) a GitHub Action runs `scripts/sweep.py`, which:

1. Pulls active vacant-land listings from Redfin's CSV export for 12 counties.
2. Filters to the criteria: 10+ acres, ≤ $250k, ACTIVE, no building square footage.
3. Assigns each parcel to a county by point-in-polygon against `data/counties.json`.
4. Diffs against the previous run — what's new, what changed price, what vanished.
5. Downloads any photo it doesn't already have into `photos/`.
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
| Acreage | 10 acres minimum |
| Price | $250,000 maximum |
| Status | Active only |
| Type | Vacant land — anything with a listed building square footage is dropped |

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
photos/rf*.webp         one photo per listing, served same-origin
scripts/sweep.py        the daily sweep — stdlib only, no dependencies
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

## Known gaps

- **Redfin only.** LandSearch, LandWatch, Land.com, Craigslist FSBO and the auction
  houses are not swept. Owner-financing and by-owner flags came from those sources, so
  those filters are not on the site.
- **Drive times are estimates** — straight-line distance × 1.15, not routed. Mountain
  roads in Monroe and Morgan counties will read optimistic.
- **Photos are one per listing.** Redfin's CDN serves the same image at every photo index
  for these land listings, so there is no gallery to pull.
- **No parcel boundaries.** Redfin gives a point, not a polygon.
