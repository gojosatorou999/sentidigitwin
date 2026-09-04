"""Kill-switch tests (Phase 2 checkpoint): block every adapter's network path
and confirm it degrades to a TwinDataSnapshot row, never an exception (C1).
"""

import pytest

from twin import models as m
from twin.ingest.base import IngestAdapter


class _FlakyAdapter(IngestAdapter):
    """An adapter whose network call can be toggled to fail on demand."""

    source_key = "flaky_test_source"
    cache_ttl_s = 900

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.should_fail = False
        self.calls = 0

    def fetch_raw(self, value=None, timeout_s=None, **_):
        self.calls += 1
        if self.should_fail:
            raise ConnectionError("simulated network kill")
        return {"value": value}

    def neutral_value(self, value=None, **_):
        return {"value": "neutral"}


class TestIngestAdapterDegradation:
    def test_success_writes_ok_snapshot(self, db):
        adapter = _FlakyAdapter()
        data, snapshot = adapter.run(db, city_id=None, value=1)

        assert data == {"value": 1}
        assert snapshot.status == "ok"
        assert snapshot.error_message is None
        assert snapshot.latency_ms is not None

    def test_fresh_cache_short_circuits_the_network_entirely(self, db):
        """A cache entry still within its TTL is not a fallback -- it must
        skip the network call outright (this is what re-hitting Open-Meteo
        on every 5-minute compute tick used to do, and is what produced a
        real 429 while this module was built)."""
        adapter = _FlakyAdapter()
        adapter.run(db, city_id=None, value=42)  # warms the cache
        assert adapter.calls == 1

        data, snapshot = adapter.run(db, city_id=None, value=42)  # should hit cache
        assert data == {"value": 42}
        assert snapshot.status == "ok"
        assert snapshot.latency_ms == 0
        assert adapter.calls == 1  # network was never touched a second time

    def test_network_killed_with_stale_cache_degrades_not_fails(self, db, monkeypatch):
        adapter = _FlakyAdapter()
        adapter.run(db, city_id=None, value=42)  # warms the cache

        # Age the cache past its TTL without sleeping: patch utcnow() forward.
        from datetime import timedelta

        from twin import models as m
        real_utcnow = m.utcnow
        monkeypatch.setattr(m, "utcnow", lambda: real_utcnow() + timedelta(seconds=adapter.cache_ttl_s + 1))

        adapter.should_fail = True
        data, snapshot = adapter.run(db, city_id=None, value=42)

        assert data == {"value": 42}          # served from the now-stale cache
        assert snapshot.status == "degraded"
        assert snapshot.error_message is not None

    def test_network_killed_with_cold_cache_falls_to_neutral(self, db):
        adapter = _FlakyAdapter()
        adapter.should_fail = True

        data, snapshot = adapter.run(db, city_id=None, value="never-cached")

        assert data == {"value": "neutral"}
        assert snapshot.status == "failed"
        assert snapshot.error_message is not None

    def test_never_raises_regardless_of_failure_mode(self, db):
        """C1: a dead API produces a `stale` flag, never a 500."""
        adapter = _FlakyAdapter()
        adapter.should_fail = True
        try:
            adapter.run(db, city_id=None, value="anything")
        except Exception as exc:  # noqa: BLE001
            pytest.fail("IngestAdapter.run() raised: %r" % exc)

    def test_writes_one_snapshot_row_per_run(self, db):
        adapter = _FlakyAdapter()
        before = db.session.query(m.TwinDataSnapshot).count()
        adapter.run(db, city_id=None, value=1)
        adapter.run(db, city_id=None, value=2)
        after = db.session.query(m.TwinDataSnapshot).count()
        assert after - before == 2

    def test_audit_write_failure_does_not_discard_good_data(self, db, monkeypatch):
        """Confirmed live: a real, successful fetch was thrown away because
        the TwinDataSnapshot audit INSERT hit a transient SQLite lock.
        Auditing must never be able to destroy good data."""
        adapter = _FlakyAdapter()

        real_commit = db.session.commit
        calls = {"n": 0}

        def flaky_commit():
            calls["n"] += 1
            if calls["n"] == 1:
                raise Exception("simulated lock on the snapshot insert")
            return real_commit()

        monkeypatch.setattr(db.session, "commit", flaky_commit)

        data, snapshot = adapter.run(db, city_id=None, value="good-data")

        assert data == {"value": "good-data"}  # NOT discarded by the audit failure
        assert snapshot is not None
        assert snapshot.status == "ok"  # true fetch outcome, not misreported as failed

    def test_retries_before_giving_up(self, db, monkeypatch):
        adapter = _FlakyAdapter()
        adapter.max_retries = 2
        adapter.retry_backoff_s = 0  # keep the test fast

        attempts = {"n": 0}
        real_fetch = adapter.fetch_raw

        def counting_fetch(*args, **kwargs):
            attempts["n"] += 1
            raise ConnectionError("always fails")

        adapter.fetch_raw = counting_fetch
        adapter.run(db, city_id=None, value=1)

        assert attempts["n"] == adapter.max_retries + 1


class TestOpenMeteoNeutralFallbacks:
    """Section 4.1: forecast -> neutral rain=0; air quality -> neutral AQI=50."""

    def test_forecast_neutral_is_zero_rain(self):
        from twin.ingest.open_meteo import OpenMeteoForecastAdapter

        adapter = OpenMeteoForecastAdapter()
        neutral = adapter.neutral_value(points=[(17.3, 78.4), (12.9, 77.5)])

        assert len(neutral) == 2
        for entry in neutral:
            assert entry["rain_now_mm_1h"] == 0.0
            assert entry["apparent_temp_c"] == 30.0  # heat_s(30) == 0

    def test_airquality_neutral_is_aqi_50(self):
        from twin import config
        from twin.ingest.open_meteo import OpenMeteoAirQualityAdapter

        adapter = OpenMeteoAirQualityAdapter()
        neutral = adapter.neutral_value(points=[(17.3, 78.4)])

        assert neutral[0]["us_aqi"] == config.NEUTRAL_AQI

    def test_flood_has_no_neutral_value_by_design(self):
        """No API serves a sane 'neutral' discharge; None flows through to
        scoring.hydro_score, which drops+renormalises rather than guessing."""
        from twin.ingest.open_meteo import OpenMeteoFloodAdapter

        adapter = OpenMeteoFloodAdapter()
        assert adapter.neutral_value(lat=17.3, lon=78.4) is None


class TestInternalReportsDegradation:
    def test_unregistered_model_degrades_to_empty_not_a_crash(self, db, app):
        from twin.ingest import internal_reports

        internal_reports._registration.clear()  # simulate host never wiring it up
        adapter = internal_reports.InternalReportsAdapter()

        data, snapshot = adapter.run(db, city_id=None)

        assert data is None  # base neutral_value default
        assert snapshot.status == "failed"
        assert snapshot.error_message is not None
