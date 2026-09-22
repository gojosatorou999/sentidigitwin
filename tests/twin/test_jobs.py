"""APScheduler wiring tests (Phase 3/section 5.4).

Uses a tiny fake scheduler rather than a real APScheduler instance, so these
tests assert what jobs.py registers without depending on APScheduler's own
threading. Job *bodies* are exercised through their own call sites
(test_state_api.py monkeypatches engine.compute_state; grid/scoring have
their own direct tests) -- this file is about registration and the
never-die-permanently exception handling, not re-testing compute_state.
"""

import pytest

from twin import jobs as twin_jobs
from twin import models as m


class _FakeScheduler:
    def __init__(self):
        self.jobs = {}

    def add_job(self, func, trigger, id, replace_existing=True, **kwargs):
        self.jobs[id] = {"func": func, "trigger": trigger, "kwargs": kwargs}


class TestRegisterJobs:
    def test_registers_every_documented_job(self, app, db):
        scheduler = _FakeScheduler()
        twin_jobs.register_jobs(app, db, scheduler)

        assert set(scheduler.jobs) == {
            "twin_compute_state", "twin_ingest_radar_index", "twin_refresh_infrastructure",
            "twin_ingest_alerts", "twin_ingest_stations", "twin_ingest_transit",
            "twin_agent_triage",
        }

    def test_alert_and_agent_jobs_respect_their_feature_flags(self, app, db, monkeypatch):
        """TWIN_ALERTS_ENABLED=0 / TWIN_AGENT_ENABLED=0 must be fully supported
        steady states, not degraded ones (C5): a deployment that wants the twin
        without either gets a scheduler with neither job on it."""
        from twin import config

        monkeypatch.setattr(config, "ALERTS_ENABLED", False)
        monkeypatch.setattr(config, "AGENT_ENABLED", False)

        scheduler = _FakeScheduler()
        twin_jobs.register_jobs(app, db, scheduler)

        assert "twin_ingest_alerts" not in scheduler.jobs
        assert "twin_agent_triage" not in scheduler.jobs
        assert "twin_compute_state" in scheduler.jobs

    def test_compute_job_runs_at_the_configured_interval(self, app, db):
        from twin import config

        scheduler = _FakeScheduler()
        twin_jobs.register_jobs(app, db, scheduler)

        assert scheduler.jobs["twin_compute_state"]["trigger"] == "interval"
        assert scheduler.jobs["twin_compute_state"]["kwargs"]["minutes"] == config.COMPUTE_INTERVAL_MIN

    def test_infrastructure_job_is_weekly(self, app, db):
        scheduler = _FakeScheduler()
        twin_jobs.register_jobs(app, db, scheduler)

        assert scheduler.jobs["twin_refresh_infrastructure"]["kwargs"]["weeks"] == 1


class TestComputeJobResilience:
    def test_a_failing_city_does_not_stop_the_others(self, app, db, monkeypatch):
        """One city's compute_state blowing up must not prevent the other
        city from being computed on the same tick -- a job that dies
        permanently loses ALL future runs under APScheduler, which is worse
        than any single failure C1 is written to prevent."""
        calls = []

        def fake_compute_state(db_, city):
            calls.append(city.slug)
            if city.slug == "hyderabad":
                raise RuntimeError("simulated failure")
            return {"city": city.slug, "cells": 0, "changed_cells": []}

        monkeypatch.setattr(twin_jobs.engine, "compute_state", fake_compute_state)

        twin_jobs._run_compute(app, db)  # must not raise

        assert set(calls) == {"hyderabad", "bengaluru"}

    def test_publishes_only_when_cells_changed(self, app, db, monkeypatch):
        published = []
        monkeypatch.setattr(twin_jobs.stream, "publish", lambda *a, **kw: published.append((a, kw)))
        monkeypatch.setattr(
            twin_jobs.engine, "compute_state",
            lambda db_, city: {"city": city.slug, "cells": 1, "changed_cells": []})

        twin_jobs._run_compute(app, db)

        assert published == []

    def test_publishes_when_cells_did_change(self, app, db, monkeypatch):
        published = []
        monkeypatch.setattr(twin_jobs.stream, "publish", lambda *a, **kw: published.append((a, kw)))
        monkeypatch.setattr(
            twin_jobs.engine, "compute_state",
            lambda db_, city: {"city": city.slug, "cells": 1, "changed_cells": ["x"]})

        twin_jobs._run_compute(app, db)

        assert len(published) == 2  # one per city
        assert published[0][0][0] == "state_update"


class TestRadarAndInfrastructureJobResilience:
    def test_radar_job_never_raises_on_adapter_failure(self, app, db, monkeypatch):
        def boom(*a, **kw):
            raise ConnectionError("network down")
        monkeypatch.setattr(
            "twin.ingest.rainviewer.RainViewerAdapter.fetch_raw", boom)

        twin_jobs._run_radar_manifest(app, db)  # must not raise

    def test_infrastructure_job_continues_past_a_failing_city(self, app, db, monkeypatch):
        calls = []

        def fake_seed(db_, city):
            calls.append(city.slug)
            if city.slug == "hyderabad":
                raise RuntimeError("overpass exploded")
            return {"assets": 0, "water_features": 0, "drain_features": 0}

        monkeypatch.setattr(twin_jobs.grid, "seed_infrastructure_and_terrain", fake_seed)
        monkeypatch.setattr(twin_jobs.grid, "cache_terrain_scores", lambda db_, city: 0)

        twin_jobs._run_infrastructure_refresh(app, db)  # must not raise

        assert set(calls) == {"hyderabad", "bengaluru"}
