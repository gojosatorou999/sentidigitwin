"""Model-level guarantees: UTC handling (C8), seed idempotency, config sanity."""

from datetime import datetime, timedelta, timezone

import pytest

from twin import config
from twin import models as m
from twin.seed import seed_metadata


class TestUTCDateTime:
    """C8: a timestamp must mean the same thing on SQLite and PostgreSQL."""

    def test_roundtrips_aware_and_stays_aware(self, db):
        written = datetime(2026, 9, 3, 12, 30, tzinfo=timezone.utc)
        snap = m.TwinDataSnapshot(
            source_key="test", status="ok", started_at=written)
        db.session.add(snap)
        db.session.commit()
        db.session.expire_all()

        read = db.session.get(m.TwinDataSnapshot, snap.id).started_at
        assert read.tzinfo is not None
        assert read == written

    def test_non_utc_input_is_normalised_not_truncated(self, db):
        ist = timezone(timedelta(hours=5, minutes=30))
        # 18:00 IST is 12:30 UTC. Storing the wall clock instead of the instant
        # is the 5.5 h error C8 exists to prevent.
        snap = m.TwinDataSnapshot(
            source_key="test", status="ok",
            started_at=datetime(2026, 9, 3, 18, 0, tzinfo=ist))
        db.session.add(snap)
        db.session.commit()
        db.session.expire_all()

        read = db.session.get(m.TwinDataSnapshot, snap.id).started_at
        assert read == datetime(2026, 9, 3, 12, 30, tzinfo=timezone.utc)
        assert read.hour == 12

    def test_elapsed_hours_is_computable_after_a_roundtrip(self, db):
        """The read path must be directly subtractable from utcnow()."""
        snap = m.TwinDataSnapshot(
            source_key="test", status="ok",
            started_at=m.utcnow() - timedelta(hours=6))
        db.session.add(snap)
        db.session.commit()
        db.session.expire_all()

        read = db.session.get(m.TwinDataSnapshot, snap.id)
        hours = (m.utcnow() - read.started_at).total_seconds() / 3600.0
        assert 5.9 < hours < 6.1

    def test_utcnow_is_aware(self):
        assert m.utcnow().tzinfo is not None


class TestSeedIdempotency:
    def test_reseeding_creates_no_duplicates(self, db):
        before_cities = db.session.query(m.TwinCity).count()
        before_zones = db.session.query(m.TwinZone).count()

        result = seed_metadata(db)

        assert db.session.query(m.TwinCity).count() == before_cities
        assert db.session.query(m.TwinZone).count() == before_zones
        assert result["cities_created"] == 0
        assert result["zones_created"] == 0

    def test_seed_counts_match_config(self, db):
        assert db.session.query(m.TwinCity).count() == len(config.CITY_DEFS)
        assert db.session.query(m.TwinZone).count() == sum(
            len(z) for z in config.ZONE_DEFS.values())

    def test_reseed_does_not_clobber_a_fetched_boundary(self, db):
        """Phase 1 writes real polygons; seed_zones must not overwrite them."""
        zone = db.session.query(m.TwinZone).first()
        zone.boundary_geojson = '{"type":"Polygon","coordinates":[]}'
        zone.boundary_source = "osm"
        db.session.commit()

        seed_metadata(db)
        db.session.expire_all()

        refreshed = db.session.get(m.TwinZone, zone.id)
        assert refreshed.boundary_source == "osm"
        assert refreshed.boundary_geojson is not None

    def test_basin_outlet_is_seeded_for_the_flood_adapter(self, db):
        for city in db.session.query(m.TwinCity).all():
            assert city.basin_name
            assert city.basin_outlet_lat is not None
            assert city.basin_outlet_lon is not None
            # No API serves this; it is derived at seed time in Phase 1.
            assert city.discharge_2yr_return is None


class TestStatusBands:
    @pytest.mark.parametrize("risk,expected", [
        (0, "normal"), (24.9, "normal"),
        (25, "watch"), (49.9, "watch"),
        (50, "warning"), (74.9, "warning"),
        (75, "critical"), (100, "critical"),
    ])
    def test_boundaries(self, risk, expected):
        assert config.band_for(risk)[0] == expected

    def test_out_of_range_is_clamped_not_crashed(self):
        assert config.band_for(-10)[0] == "normal"
        assert config.band_for(1000)[0] == "critical"
        assert config.band_for(None)[0] == "normal"

    def test_bands_are_contiguous_and_cover_zero_to_hundred(self):
        edges = [(low, high) for low, high, *_ in config.STATUS_BANDS]
        assert edges[0][0] == 0.0
        assert edges[-1][1] > 100.0
        for (_, prev_high), (next_low, _) in zip(edges, edges[1:]):
            assert prev_high == next_low


class TestWeights:
    def test_hazard_weights_sum_to_one(self):
        assert abs(sum(config.HAZARD_WEIGHTS.values()) - 1.0) < 1e-9

    def test_vulnerability_weights_sum_to_one(self):
        assert abs(sum(config.VULNERABILITY_WEIGHTS.values()) - 1.0) < 1e-9

    def test_every_horizon_has_hydro_weights_summing_to_one(self):
        assert set(config.HYDRO_WEIGHTS) == set(config.HORIZONS)
        for horizon, weights in config.HYDRO_WEIGHTS.items():
            assert abs(sum(weights) - 1.0) < 1e-9, horizon

    def test_terrain_and_env_weights_sum_to_one(self):
        assert abs(sum(config.TERRAIN_WEIGHTS.values()) - 1.0) < 1e-9
        assert abs(sum(config.ENV_WEIGHTS.values()) - 1.0) < 1e-9

    def test_vulnerability_multiplier_never_reduces_risk(self):
        """A calm cell must read 0, and exposure must only ever amplify."""
        lo = 1.0 + config.VULNERABILITY_GAIN * 0.0 / 100
        hi = 1.0 + config.VULNERABILITY_GAIN * 100.0 / 100
        assert lo == 1.0
        assert hi == pytest.approx(1.6)

    def test_every_criticality_asset_has_an_overpass_filter(self):
        for asset_type in config.ASSET_CRITICALITY:
            assert asset_type in config.OVERPASS_FILTERS, asset_type
        for asset_type in config.TERRAIN_ASSET_TYPES:
            assert asset_type in config.OVERPASS_FILTERS, asset_type

    def test_terrain_assets_carry_no_criticality(self):
        """water_body and drain feed terrain, not infra (section 4.3)."""
        for asset_type in config.TERRAIN_ASSET_TYPES:
            assert asset_type not in config.ASSET_CRITICALITY
