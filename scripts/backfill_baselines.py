"""Give the twin a memory: per-cell, per-month rainfall and heat baselines.

``twin_cell_history`` starts empty and fills at five-minute intervals, which
means a twin deployed today cannot say whether 62 mm of rain is unusual until
it has watched a monsoon go by. That is the wrong way round -- the judgement is
needed on day one.

So the baselines come from an **archive, not from polling**: Open-Meteo's
historical reanalysis (keyless, ERA5-derived) serves hourly precipitation and
temperature back many years. This script samples it across the city, derives
the distribution of 1-hour, 3-hour and 24-hour rainfall totals per calendar
month, and writes the percentiles into ``twin_baseline``.

Two decisions worth knowing about:

* **Sampled on a lattice, not per cell.** A city is ~900 H3 cells and the
  archive is one HTTP call per point per window; at 5 km spacing a city is
  ~40 points. Reanalysis is itself ~10 km-resolution data, so a per-cell fetch
  would be 900 calls returning interpolated copies of the same ~40 numbers --
  more load on a free public API for no extra information. Each cell takes the
  nearest lattice point's distribution.

* **Percentiles, not just mean and sigma.** Rain is zero most hours and
  extreme in a few, so its standard deviation understates the tail badly.
  p90/p95/p99 are what ``twin/anomaly.py`` prefers; mean and sigma are stored
  as the fallback for metrics that are roughly normal, like temperature.

Usage::

    python scripts/backfill_baselines.py                  # both cities, 5 years
    python scripts/backfill_baselines.py --city bengaluru --years 3
    python scripts/backfill_baselines.py --spacing-km 8   # fewer API calls
"""

import argparse
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import requests  # noqa: E402

#: Open-Meteo asks for no key but does rate-limit; one second between calls
#: keeps a full two-city backfill comfortably inside the free tier.
PAUSE_S = 1.0


def percentile(sorted_values, fraction):
    """Linear-interpolated percentile of an already-sorted list."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = fraction * (len(sorted_values) - 1)
    low = int(position)
    high = min(low + 1, len(sorted_values) - 1)
    weight = position - low
    return sorted_values[low] * (1 - weight) + sorted_values[high] * weight


def rolling_sums(values, window):
    """Every `window`-long rolling total of an hourly series.

    None entries break a window rather than being treated as zero: a gap in
    the archive is missing data, and silently reading it as "no rain" would
    drag every percentile down and make real storms look ordinary.
    """
    sums = []
    for i in range(len(values) - window + 1):
        chunk = values[i:i + window]
        if any(v is None for v in chunk):
            continue
        sums.append(sum(chunk))
    return sums


def fetch_archive(session, lat, lon, start, end, timeout_s=60):
    """Hourly precipitation + temperature for one point over one window."""
    from twin import config

    response = session.get(config.OPEN_METEO_ARCHIVE_URL, params={
        "latitude": round(lat, 4),
        "longitude": round(lon, 4),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "hourly": "precipitation,temperature_2m",
        "timezone": "UTC",
    }, timeout=timeout_s)
    response.raise_for_status()
    return response.json().get("hourly") or {}


def distributions_for_point(session, lat, lon, years, verbose=True):
    """{month: {metric: [values...]}} for one lattice point.

    The archive is pulled a year at a time: one request for five years of
    hourly data is a large response that the API will sometimes refuse, and a
    failure then costs the whole point rather than one year of it.
    """
    import time

    by_month = defaultdict(lambda: defaultdict(list))
    today = date.today()

    for year_offset in range(1, years + 1):
        end = date(today.year - year_offset + 1, today.month, 1) - timedelta(days=1)
        start = date(end.year - 1, end.month, 1)
        try:
            hourly = fetch_archive(session, lat, lon, start, end)
        except Exception as exc:  # noqa: BLE001 - one bad year must not sink the point
            if verbose:
                print("      ! %s..%s failed (%s)" % (start, end, exc))
            time.sleep(PAUSE_S)
            continue

        times = hourly.get("time") or []
        rain = hourly.get("precipitation") or []
        temp = hourly.get("temperature_2m") or []

        # Group hour indexes by calendar month before taking rolling sums, so
        # a window never straddles two months' worth of climate.
        by_month_indexes = defaultdict(list)
        for index, stamp in enumerate(times):
            try:
                month = int(stamp[5:7])
            except (TypeError, ValueError, IndexError):
                continue
            by_month_indexes[month].append(index)

        for month, indexes in by_month_indexes.items():
            month_rain = [rain[i] if i < len(rain) else None for i in indexes]
            month_temp = [temp[i] if i < len(temp) else None for i in indexes]

            by_month[month]["rain_1h"].extend(v for v in month_rain if v is not None)
            by_month[month]["rain_3h"].extend(rolling_sums(month_rain, 3))
            by_month[month]["rain_24h"].extend(rolling_sums(month_rain, 24))
            by_month[month]["temp_max"].extend(v for v in month_temp if v is not None)

        time.sleep(PAUSE_S)

    return by_month


def summarise(values):
    """mean/stddev/percentiles for one metric-month sample."""
    if not values:
        return None
    ordered = sorted(values)
    count = len(ordered)
    mean = sum(ordered) / count
    variance = sum((v - mean) ** 2 for v in ordered) / count
    return {
        "mean": mean,
        "stddev": variance ** 0.5,
        "p50": percentile(ordered, 0.50),
        "p90": percentile(ordered, 0.90),
        "p95": percentile(ordered, 0.95),
        "p99": percentile(ordered, 0.99),
        "maximum": ordered[-1],
        "sample_count": count,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", default="all")
    parser.add_argument("--years", type=int, default=None,
                        help="years of archive to sample (default: TWIN_BASELINE_YEARS)")
    parser.add_argument("--spacing-km", type=float, default=8.0,
                        help="lattice spacing; larger means fewer API calls")
    parser.add_argument("--database", default=None,
                        help="SQLAlchemy URL (default: instance/site.db)")
    args = parser.parse_args()

    database_uri = args.database or (
        "sqlite:///" + os.path.abspath(os.path.join("instance", "site.db")))

    from tests.twin.twin_test_host import create_app, db

    app = create_app(database_uri=database_uri)

    with app.app_context():
        db.create_all()
        from twin import config, geo
        from twin import models as m

        years = args.years or config.BASELINE_YEARS
        session = requests.Session()
        session.headers["User-Agent"] = "sentinel-twin/0.1 (+digital-twin-module)"

        cities = db.session.query(m.TwinCity).filter_by(is_active=True)
        if args.city != "all":
            cities = cities.filter_by(slug=args.city)
        cities = cities.all()
        if not cities:
            print("No matching city. Run scripts/seed_twin.py first.")
            return

        for city in cities:
            cells = db.session.query(m.TwinCell).filter_by(city_id=city.id).all()
            if not cells:
                print("%s has no cells; run scripts/seed_twin.py first." % city.slug)
                continue

            bbox = (city.bbox_min_lon, city.bbox_min_lat,
                    city.bbox_max_lon, city.bbox_max_lat)
            lattice = geo.build_lattice(bbox, spacing_km=args.spacing_km)
            print("=" * 72)
            print("%s: %d cells, %d lattice points, %d years of archive"
                  % (city.slug, len(cells), len(lattice), years))

            point_distributions = []
            for index, (lat, lon) in enumerate(lattice, start=1):
                print("  [%d/%d] %.3f, %.3f" % (index, len(lattice), lat, lon))
                point_distributions.append(
                    ((lat, lon), distributions_for_point(session, lat, lon, years)))

            usable = [(point, dist) for point, dist in point_distributions if dist]
            if not usable:
                print("  ! no archive data retrieved; nothing written")
                continue

            # Summarise once per lattice point, then attach each cell to its
            # nearest point -- 40 summaries instead of 900 identical ones.
            summaries_by_point = {}
            for point, distribution in usable:
                summaries_by_point[point] = {
                    (metric, month): summarise(values)
                    for month, metrics in distribution.items()
                    for metric, values in metrics.items()
                }

            points = [point for point, _ in usable]
            window_end = datetime.now(timezone.utc)
            window_start = window_end - timedelta(days=365 * years)

            existing = {
                (row.cell_id, row.metric, row.month): row
                for row in db.session.query(m.TwinBaseline).filter(
                    m.TwinBaseline.city_id == city.id).all()
            }

            written = 0
            for cell in cells:
                nearest = points[geo.nearest_point_index(
                    None, points, cell.center_latitude, cell.center_longitude)]
                for (metric, month), stats in summaries_by_point[nearest].items():
                    if not stats:
                        continue
                    row = existing.get((cell.id, metric, month))
                    if row is None:
                        row = m.TwinBaseline(cell_id=cell.id, metric=metric, month=month)
                        db.session.add(row)
                        existing[(cell.id, metric, month)] = row

                    row.city_id = city.id
                    row.source = "open_meteo_archive"
                    row.window_start = window_start
                    row.window_end = window_end
                    row.computed_at = m.utcnow()
                    for field, value in stats.items():
                        setattr(row, field, value)
                    written += 1

                # Commit per cell batch rather than once at the end: ~900 cells
                # x 4 metrics x 12 months is ~43,000 rows, and one giant
                # transaction on SQLite is where this would otherwise stall.
                if written % 2000 == 0:
                    db.session.commit()

            db.session.commit()
            print("  wrote %d baseline rows for %s" % (written, city.slug))

    print("Done. twin/anomaly.py can now judge what is unusual for each cell.")


if __name__ == "__main__":
    main()
