"""Grid determinism and zone assignment (Phase 1/8 checkpoint).

No network calls: these tests build small synthetic polygons rather than
depending on scripts/fetch_boundaries.py having run.
"""

import json

import h3
import pytest
from shapely.geometry import Polygon

from twin import geo, grid


# A ~2.2km square around Hyderabad's centroid -- big enough for a handful of
# H3 res-8 cells (~460m edge), small enough to stay fast.
_SQUARE = Polygon([
    (78.470, 17.375), (78.500, 17.375), (78.500, 17.395), (78.470, 17.395),
])


class TestShapelyToH3Poly:
    def test_produces_a_stable_cell_set(self):
        h3poly = grid._shapely_polygon_to_h3poly(_SQUARE)
        cells_a = h3.polygon_to_cells(h3poly, 8)
        cells_b = h3.polygon_to_cells(h3poly, 8)
        assert cells_a == cells_b
        assert len(cells_a) > 0

    def test_cell_centroids_fall_within_a_buffered_polygon(self):
        """H3's polyfill can include cells whose CENTER is just outside the
        source ring depending on containment mode; assert centroids are at
        least near the polygon, not on the other side of the city."""
        from shapely.geometry import Point

        h3poly = grid._shapely_polygon_to_h3poly(_SQUARE)
        cells = h3.polygon_to_cells(h3poly, 8)
        buffered = _SQUARE.buffer(0.01)  # ~1km slack for edge cells
        for cell in cells:
            lat, lon = h3.cell_to_latlng(cell)
            assert buffered.contains(Point(lon, lat))

    def test_boundary_ring_is_valid_geojson_lon_lat_order(self):
        h3poly = grid._shapely_polygon_to_h3poly(_SQUARE)
        cell = next(iter(h3.polygon_to_cells(h3poly, 8)))
        ring = h3.cell_to_boundary(cell)
        coords = [[round(lng, 5), round(lat, 5)] for lat, lng in ring]
        # Hyderabad is at ~78E, 17N -- lon must be the larger-magnitude,
        # positive-in-this-hemisphere value in the FIRST slot (GeoJSON order).
        for lon, lat in coords:
            assert 78.0 < lon < 79.0
            assert 17.0 < lat < 18.0


class TestCellsForReport:
    def test_returns_home_cell_and_six_neighbours(self):
        """Confirmed live: h3.grid_disk() returns a list in h3-py 4.5.0, not
        a set as older docs/memory suggest -- `list - set` raised a
        TypeError the first time this ran against a real report."""
        from twin.ingest.internal_reports import cells_for_report

        home, neighbours = cells_for_report(17.385, 78.4867)
        assert isinstance(neighbours, set)
        assert home not in neighbours
        assert len(neighbours) == 6  # a full interior hex has 6 neighbours


class TestZoneIndex:
    def test_point_inside_polygon_resolves_to_its_zone(self):
        zone_a = Polygon([(78.0, 17.0), (78.2, 17.0), (78.2, 17.2), (78.0, 17.2)])
        zone_b = Polygon([(78.3, 17.0), (78.5, 17.0), (78.5, 17.2), (78.3, 17.2)])
        index = geo.ZoneIndex([(1, zone_a), (2, zone_b)])

        assert index.zone_for(17.1, 78.1) == 1
        assert index.zone_for(17.1, 78.4) == 2

    def test_point_outside_all_zones_is_none(self):
        zone_a = Polygon([(78.0, 17.0), (78.2, 17.0), (78.2, 17.2), (78.0, 17.2)])
        index = geo.ZoneIndex([(1, zone_a)])

        assert index.zone_for(20.0, 80.0) is None

    def test_empty_zone_list_never_raises(self):
        index = geo.ZoneIndex([])
        assert index.zone_for(17.1, 78.1) is None


class TestGenerateCellsForCity(object):
    def test_refuses_without_a_clip_polygon(self, db, tmp_path, monkeypatch):
        from twin import config, models as m

        monkeypatch.setattr(config, "BOUNDARY_DIR", str(tmp_path))
        city = db.session.query(m.TwinCity).filter_by(slug="hyderabad").one()

        with pytest.raises(RuntimeError, match="fetch_boundaries"):
            grid.generate_cells_for_city(db, city)

    def test_generates_and_is_idempotent(self, db, tmp_path, monkeypatch):
        from twin import config, models as m

        monkeypatch.setattr(config, "BOUNDARY_DIR", str(tmp_path))
        clip_path = tmp_path / "hyderabad_clip.geojson"
        clip_path.write_text(json.dumps({
            "type": "Feature",
            "properties": {},
            "geometry": {
                "type": "Polygon",
                "coordinates": [list(_SQUARE.exterior.coords)],
            },
        }), encoding="utf-8")

        city = db.session.query(m.TwinCity).filter_by(slug="hyderabad").one()

        first_count = grid.generate_cells_for_city(db, city)
        assert first_count > 0

        cells_after_first = {c.h3_index for c in
                             db.session.query(m.TwinCell).filter_by(city_id=city.id).all()}

        second_count = grid.generate_cells_for_city(db, city)  # no force -> skip
        assert second_count == first_count

        cells_after_second = {c.h3_index for c in
                              db.session.query(m.TwinCell).filter_by(city_id=city.id).all()}
        assert cells_after_first == cells_after_second

    def test_assigns_zone_id_from_committed_zone_geojson(self, db, tmp_path, monkeypatch):
        from twin import config, models as m

        monkeypatch.setattr(config, "BOUNDARY_DIR", str(tmp_path))
        (tmp_path / "hyderabad_clip.geojson").write_text(json.dumps({
            "type": "Feature", "properties": {},
            "geometry": {"type": "Polygon", "coordinates": [list(_SQUARE.exterior.coords)]},
        }), encoding="utf-8")

        city = db.session.query(m.TwinCity).filter_by(slug="hyderabad").one()
        zone = db.session.query(m.TwinZone).filter_by(city_id=city.id, slug="khairatabad").one()

        # A zone polygon covering the western half of the test square.
        half = Polygon([(78.470, 17.375), (78.485, 17.375), (78.485, 17.395), (78.470, 17.395)])
        (tmp_path / "hyderabad.geojson").write_text(json.dumps({
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "properties": {"slug": "khairatabad", "boundary_source": "osm"},
                "geometry": {"type": "Polygon", "coordinates": [list(half.exterior.coords)]},
            }],
        }), encoding="utf-8")

        grid.generate_cells_for_city(db, city)

        cells = db.session.query(m.TwinCell).filter_by(city_id=city.id).all()
        assigned = [c for c in cells if c.zone_id == zone.id]
        unassigned = [c for c in cells if c.zone_id is None]
        assert assigned, "at least one cell should fall inside the zone polygon"
        assert unassigned, "at least one cell should fall outside it (zone_id NULL, still belongs to the city)"
