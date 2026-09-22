"""Derive LGD district codes from the SACHET feed itself.

IMD writes an alert's area two ways. Sometimes it names the districts
("Chamarajanagara,Chikkaballapura,Kolar districts of Karnataka") and lists
their ``cap:geocode`` LGD codes alongside. Far more often it says "23 districts
of Telangana" and names none of them -- and then the code list is the only way
to know whether this city is inside the warning.

So: harvest the alerts that give both, pair name to code by position, and keep
a pairing only once several independent alerts agree on it. The result is
written to ``data/twin/lgd_districts.json`` and merged into
``twin.config.CITY_LGD_DISTRICT_CODES`` at import, *below* anything set
explicitly in .env -- an inference never overrides a human.

This exists instead of hard-coding a table because a wrong district code
silently attributes another district's warning to this city, and the LGD
directory is not openly queryable. Run it periodically::

    python scripts/learn_lgd_codes.py
    python scripts/learn_lgd_codes.py --states karnataka,telangana --max-alerts 60

Confidence is just "how many alerts agreed", and pairings below the threshold
are written to a ``rejected`` section rather than thrown away, so an operator
can see what was nearly learned and confirm it by hand against
https://lgdirectory.gov.in.
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import requests  # noqa: E402

from twin import config  # noqa: E402
from twin.ingest import sachet  # noqa: E402

#: "Chamarajanagara,Chikkaballapura,Kolar districts of Karnataka"
NAMED_AREA = re.compile(r"^(?P<names>.+?)\s+districts?\s+of\s+(?P<state>.+)$", re.I)
#: "23 districts of Telangana" -- no names, nothing to learn from.
COUNTED_AREA = re.compile(r"^\d+\s+districts?\s+of\s+", re.I)


def harvest(states, max_alerts, timeout_s=20):
    """{(state, district_name): {code: agreement_count}} from live feeds."""
    session = requests.Session()
    session.headers["User-Agent"] = "sentinel-twin/0.1 (+digital-twin-module)"

    pairings = defaultdict(lambda: defaultdict(int))
    examined = matched = 0

    for state in states:
        feed_url = config.SACHET_RSS_URL % state
        try:
            response = session.get(feed_url, timeout=timeout_s)
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            print("  ! %s feed unavailable: %s" % (state, exc))
            continue

        for item in sachet.parse_rss(response.text)[:max_alerts]:
            link = item.get("link")
            if not link:
                continue
            try:
                document = session.get(link, timeout=timeout_s)
                document.raise_for_status()
                alert = sachet.parse_cap(document.text)
            except Exception as exc:  # noqa: BLE001
                print("  ! CAP fetch failed for %s: %s" % (item.get("guid"), exc))
                continue

            examined += 1
            if _learn_from(alert, state, pairings):
                matched += 1

    return pairings, examined, matched


def _learn_from(alert, state, pairings):
    area_desc = (alert.get("area_desc") or "").strip()
    if not area_desc or COUNTED_AREA.match(area_desc):
        return False

    match = NAMED_AREA.match(area_desc)
    if not match:
        return False

    names = [name.strip() for name in match.group("names").split(",") if name.strip()]
    codes = []
    for value_name, values in (alert.get("geocodes") or {}).items():
        if "district" in value_name.lower() or "lgd" in value_name.lower():
            codes.extend(str(v).strip() for v in
                         (values if isinstance(values, (list, tuple)) else [values]))

    # Pair by position only when the two lists line up exactly. A mismatch
    # means the order is not guaranteed and any pairing would be a guess.
    if not names or len(names) != len(codes):
        return False

    for name, code in zip(names, codes):
        pairings[(state, name.lower())][code] += 1
    return True


def resolve(pairings, min_agreement):
    """Accepted {(state, name): code} plus everything that fell short."""
    accepted, rejected = {}, {}
    for key, counts in pairings.items():
        code, agreement = max(counts.items(), key=lambda kv: kv[1])
        contested = len([c for c in counts.values() if c == agreement]) > 1
        if agreement >= min_agreement and not contested:
            accepted[key] = {"code": code, "agreement": agreement}
        else:
            rejected[key] = {"candidates": dict(counts), "contested": contested}
    return accepted, rejected


def cities_from(accepted):
    """Map each modelled city to the accepted codes of its own districts."""
    cities = {}
    for slug, aliases in config.CITY_ALERT_ALIASES.items():
        codes = []
        for (_state, name), entry in accepted.items():
            if any(alias in name for alias in aliases):
                codes.append(entry["code"])
        if codes:
            cities[slug] = sorted(set(codes))
    return cities


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--states", default=",".join(config.SACHET_STATES))
    parser.add_argument("--max-alerts", type=int, default=40)
    parser.add_argument("--min-agreement", type=int, default=2,
                        help="alerts that must agree before a pairing is accepted")
    parser.add_argument("--out", default=config.LGD_LEARNED_FILE)
    args = parser.parse_args()

    states = [s.strip().lower() for s in args.states.split(",") if s.strip()]
    print("Harvesting SACHET feeds: %s" % ", ".join(states))

    pairings, examined, matched = harvest(states, args.max_alerts)
    print("  examined %d alerts, %d carried district names" % (examined, matched))

    accepted, rejected = resolve(pairings, args.min_agreement)
    cities = cities_from(accepted)

    # Merge rather than overwrite: each run sees only the alerts live at the
    # time, so a month of runs learns far more than any single one.
    existing = {}
    if os.path.exists(args.out):
        try:
            with open(args.out, "r", encoding="utf-8") as handle:
                existing = json.load(handle)
        except (OSError, ValueError):
            existing = {}

    merged_districts = dict(existing.get("districts") or {})
    for (state, name), entry in accepted.items():
        merged_districts["%s|%s" % (state, name)] = entry

    merged_cities = {slug: sorted(set(existing.get("cities", {}).get(slug, []))
                                  | set(codes))
                     for slug, codes in cities.items()}
    for slug, codes in (existing.get("cities") or {}).items():
        merged_cities.setdefault(slug, sorted(set(codes)))

    payload = {
        "districts": merged_districts,
        "cities": merged_cities,
        "rejected": {"%s|%s" % key: value for key, value in rejected.items()},
        "note": ("Inferred from SACHET CAP alerts that listed district names and "
                 "LGD codes together. Verify against https://lgdirectory.gov.in "
                 "before relying on a code for evacuation decisions."),
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)

    print("  accepted %d district codes, %d still uncertain"
          % (len(accepted), len(rejected)))
    for slug, codes in merged_cities.items():
        print("    %s -> %s" % (slug, ", ".join(codes) or "(none yet)"))
    print("  written to %s" % args.out)


if __name__ == "__main__":
    main()
