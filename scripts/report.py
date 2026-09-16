#!/usr/bin/env python3
"""Render data/report.json as Markdown - used for the Actions run summary."""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = "https://msaade12.github.io/knoxville-land/"


def money(n):
    return "${:,}".format(int(n))


def row(t):
    return (f"| {t['acres']} ac | {money(t['price'])} | "
            f"{money(round(t['price'] / t['acres']))}/ac | "
            f"{t['town']}, {t['county']} | [listing]({t['url']}) |")


def commit_message(r):
    bits = ["%d new" % len(r["new"])]
    if r.get("priceCuts"):
        bits.append("%d cut" % len(r["priceCuts"]))
    if r.get("retired"):
        bits.append("%d retired" % len(r["retired"]))
    bits.append("%d active" % r.get("confirmed", r["count"]))
    return "sweep %s: %s" % (r["date"], ", ".join(bits))


def main():
    path = os.path.join(ROOT, "data", "report.json")
    if not os.path.exists(path):
        print("No report.json - sweep did not run.")
        return 0
    with open(path) as f:
        r = json.load(f)

    if "--commit-message" in sys.argv:
        print(commit_message(r))
        return 0

    out = [f"## Knoxville Land Scout — {r['date']}", ""]
    out.append(f"**{r.get('confirmed', r['count'])} confirmed active tracts** "
               f"of {r['count']} tracked. [Open the map]({SITE})")
    out.append("")

    hdr = ["| Acres | Price | $/acre | Where | |", "|---|---|---|---|---|"]

    if r["new"]:
        out += [f"### {len(r['new'])} new since last run", ""] + hdr
        out += [row(t) for t in r["new"]] + [""]
    else:
        out += ["### No new listings", ""]

    if r["priceCuts"]:
        out += [f"### {len(r['priceCuts'])} price cut(s)", ""]
        for c in r["priceCuts"]:
            out.append(f"- **{c['acres']} ac, {c['town']}** — "
                       f"{money(c['from'])} → {money(c['to'])} "
                       f"({money(c['delta'])}) · [listing]({c['url']})")
        out.append("")

    if r.get("gone"):
        out += [f"### {len(r['gone'])} confirmed gone (status read off the listing page)", ""]
        for g in r["gone"]:
            out.append(f"- {g['acres']} ac, {money(g['price'])}, {g['town']} — {g['status']}")
        out.append("")

    if r.get("tooFar"):
        out += [f"### {len(r['tooFar'])} dropped as too far by road", "",
                "_Over 60 min to Knoxville or over 15 min to a real town. Remembered "
                "in `data/excluded.json`; delete the entry to reconsider one._", ""]
        for g in r["tooFar"][:12]:
            out.append(f"- {g['acres']} ac, {money(g['price'])}, {g['town']} — "
                       f"Knoxville {g['drive']} min, shops {g['shopMin']} min · [listing]({g['url']})")
        if len(r["tooFar"]) > 12:
            out.append(f"- …and {len(r['tooFar']) - 12} more")
        out.append("")

    if r.get("tooSteep"):
        out += [f"### {len(r['tooSteep'])} dropped as too steep", ""]
        for g in r["tooSteep"]:
            out.append(f"- {g['acres']} ac, {money(g['price'])}, {g['town']} — {g['slope']}°")
        out.append("")

    if r.get("unconfirmed"):
        out += [f"### {len(r['unconfirmed'])} not seen this run",
                "", "_Redfin's export omits some MLS listings, so these are kept "
                "and re-checked rather than deleted. They retire after 3 misses._", ""]
        for g in r["unconfirmed"][:15]:
            out.append(f"- {g['acres']} ac, {money(g['price'])}, "
                       f"{g['town']} ({g['missCount']}/3) · [listing]({g['url']})")
        if len(r["unconfirmed"]) > 15:
            out.append(f"- …and {len(r['unconfirmed']) - 15} more")
        out.append("")

    if r.get("retired"):
        out += [f"### {len(r['retired'])} retired (gone 3 runs running)", ""]
        for g in r["retired"]:
            out.append(f"- {g['acres']} ac, {money(g['price'])}, {g['town']}")
        out.append("")

    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
