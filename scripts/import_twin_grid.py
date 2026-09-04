"""Copy a pre-seeded twin grid from one SQLite database into another.

`scripts/seed_twin.py` builds the H3 grid from scratch: it clips ~1,700 cells
against each city boundary, then fills elevation from Open-Meteo and
infrastructure from Overpass. That is 5-20 minutes of rate-limited network
traffic per city, and it is entirely deterministic -- the same boundaries
produce the same cells every time.

So when a grid has already been built once (the digital-twin module was
developed and seeded standalone before being wired into this app), copying it
is strictly better than rebuilding it: same rows, no Overpass load, seconds
instead of tens of minutes.

    python -m scripts.import_twin_grid --source "digital twin/instance/sentinel_dev.db"

Cities and zones are matched by slug, never by row id, so this stays correct
even if the two databases were seeded in a different order. Existing grid rows
for a city are replaced wholesale; nothing outside the twin_* tables is
touched.
"""

import argparse
import os
import sqlite3
import sys

DEFAULT_SOURCE = os.path.join("digital twin", "instance", "sentinel_dev.db")
DEFAULT_TARGET = os.path.join("instance", "site.db")

#: Copied in this order -- twin_cell before the tables that reference it.
GRID_TABLES = ("twin_cell", "twin_cell_state", "twin_infrastructure")


def _columns(connection, table):
    return [row[1] for row in connection.execute("PRAGMA table_info(%s)" % table)]


def _slug_to_id(connection, table):
    return {row[0]: row[1] for row in connection.execute("SELECT slug, id FROM %s" % table)}


def _id_remap(src, dst, table):
    """{source_id: target_id} for one metadata table, matched on slug."""
    source_ids = {row[1]: row[0] for row in
                  src.execute("SELECT id, slug FROM %s" % table)}
    target_ids = _slug_to_id(dst, table)
    remap = {}
    for slug, source_id in source_ids.items():
        if slug in target_ids:
            remap[source_id] = target_ids[slug]
    return remap


def import_grid(source_path, target_path, cities=None, dry_run=False):
    if not os.path.exists(source_path):
        raise SystemExit("source database not found: %s" % source_path)
    if not os.path.exists(target_path):
        raise SystemExit(
            "target database not found: %s -- start the app once so "
            "db.create_all() builds the twin tables first" % target_path)

    src = sqlite3.connect(source_path)
    dst = sqlite3.connect(target_path)
    dst.execute("PRAGMA foreign_keys = OFF")

    city_remap = _id_remap(src, dst, "twin_city")
    zone_remap = _id_remap(src, dst, "twin_zone")
    if not city_remap:
        raise SystemExit("no cities in common between the two databases")

    slug_by_source_id = {row[0]: row[1] for row in
                         src.execute("SELECT id, slug FROM twin_city")}
    if cities:
        wanted = set(cities)
        city_remap = {sid: tid for sid, tid in city_remap.items()
                      if slug_by_source_id.get(sid) in wanted}
        if not city_remap:
            raise SystemExit("none of %s exist in both databases" % sorted(wanted))

    target_city_ids = sorted(city_remap.values())
    print("cities: %s" % ", ".join(
        "%s (%s->%s)" % (slug_by_source_id[sid], sid, tid)
        for sid, tid in sorted(city_remap.items())))

    # Zone boundaries are fetched from Overpass by scripts/fetch_boundaries.py
    # and are just as reusable as the grid itself.
    boundaries = 0
    for source_zone_id, target_zone_id in zone_remap.items():
        row = src.execute(
            "SELECT boundary_geojson, boundary_source, center_latitude, center_longitude "
            "FROM twin_zone WHERE id = ?", (source_zone_id,)).fetchone()
        if not row or row[0] is None:
            continue
        if not dry_run:
            dst.execute(
                "UPDATE twin_zone SET boundary_geojson = ?, boundary_source = ?, "
                "center_latitude = ?, center_longitude = ? WHERE id = ?",
                (row[0], row[1], row[2], row[3], target_zone_id))
        boundaries += 1
    print("zone boundaries copied: %d" % boundaries)

    placeholders = ",".join("?" * len(target_city_ids))
    cell_id_remap = {}
    counts = {}

    # Purge children before parents, and before any insert.
    #
    # Deleting inside each table's own copy loop looked equivalent and was
    # not: SQLite reuses rowids after a DELETE unless the column is declared
    # AUTOINCREMENT, so re-inserting 1,747 cells into a table just emptied
    # of 1,747 cells hands the new rows the exact same ids. The stale child
    # rows then still resolve against twin_cell, "delete the orphans"
    # deletes nothing, and the copy dies on a UNIQUE violation halfway
    # through -- observed exactly that way.
    if not dry_run:
        for table in reversed(GRID_TABLES):
            if "city_id" in _columns(dst, table):
                dst.execute("DELETE FROM %s WHERE city_id IN (%s)" % (table, placeholders),
                            target_city_ids)
            else:
                dst.execute(
                    "DELETE FROM %s WHERE cell_id IN "
                    "(SELECT id FROM twin_cell WHERE city_id IN (%s))" % (table, placeholders),
                    target_city_ids)

    for table in GRID_TABLES:
        columns = _columns(src, table)
        if columns != _columns(dst, table):
            raise SystemExit(
                "schema mismatch on %s -- the two databases are at different "
                "migrations; run `flask db upgrade` on both first" % table)

        if "city_id" in columns:
            # twin_cell and twin_infrastructure both carry city_id. Scoping
            # by it rather than by cell_id matters for infrastructure: ~16%
            # of Overpass assets land outside the clipped H3 grid and so
            # have cell_id NULL, and `/api/twin/<city>/infrastructure`
            # queries by city_id -- selecting on cell_id would silently drop
            # every hospital and substation just outside the boundary.
            source_rows = src.execute(
                "SELECT %s FROM %s WHERE city_id IN (%s)"
                % (",".join(columns), table, ",".join("?" * len(city_remap))),
                tuple(city_remap.keys())).fetchall()
        elif cell_id_remap:
            source_rows = src.execute(
                "SELECT %s FROM %s WHERE cell_id IN (%s)"
                % (",".join(columns), table, ",".join("?" * len(cell_id_remap))),
                tuple(cell_id_remap.keys())).fetchall()
        else:
            source_rows = []

        index = {name: position for position, name in enumerate(columns)}
        insert_sql = "INSERT INTO %s (%s) VALUES (%s)" % (
            table, ",".join(columns), ",".join("?" * len(columns)))

        for row in source_rows:
            values = list(row)
            if "city_id" in index and values[index["city_id"]] is not None:
                values[index["city_id"]] = city_remap[values[index["city_id"]]]
            if "zone_id" in index and values[index["zone_id"]] is not None:
                values[index["zone_id"]] = zone_remap.get(values[index["zone_id"]])
            source_row_id = values[index["id"]]
            if table != "twin_cell" and "cell_id" in index:
                # NULL cell_id (an asset outside the clipped grid) stays NULL.
                old_cell_id = values[index["cell_id"]]
                values[index["cell_id"]] = (
                    cell_id_remap.get(old_cell_id) if old_cell_id is not None else None)
            values[index["id"]] = None  # let the target assign its own ids

            if dry_run:
                new_id = source_row_id
            else:
                cursor = dst.execute(insert_sql, values)
                new_id = cursor.lastrowid
            if table == "twin_cell":
                cell_id_remap[source_row_id] = new_id

        counts[table] = len(source_rows)
        print("%-20s %d rows" % (table, len(source_rows)))

    if dry_run:
        dst.rollback()
        print("dry run -- nothing written")
    else:
        dst.commit()
        print("done")

    src.close()
    dst.close()
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=DEFAULT_SOURCE,
                        help="SQLite database holding the seeded grid")
    parser.add_argument("--target", default=DEFAULT_TARGET,
                        help="this app's SQLite database")
    parser.add_argument("--city", action="append", dest="cities",
                        help="limit to one city slug (repeatable)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    import_grid(args.source, args.target, cities=args.cities, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
