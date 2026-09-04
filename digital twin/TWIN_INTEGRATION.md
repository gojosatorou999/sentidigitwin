# Wiring the twin into Sentinel AI

The twin is a self-contained package. Integration is additive (C6): no existing
route, model, or template is modified. Everything below reflects what was
actually built and live-tested in this session (Phases 0–8), not just the
original plan.

## 1. Copy in

```
twin/                      -> sentinel-ai/twin/
scripts/                   -> sentinel-ai/scripts/
migrations/versions/20260903_01_twin_initial.py
                           -> sentinel-ai/migrations/versions/
data/twin/                 -> sentinel-ai/data/twin/       (boundaries ARE committed; cache/ is gitignored)
templates/digital_twin.html, templates/partials/twin_*.html
                           -> sentinel-ai/templates/
static/css/twin.css, static/js/digital-twin.js, twin-layers.js, twin-stream.js
                           -> sentinel-ai/static/
tests/twin/                -> sentinel-ai/tests/twin/
```

Append `requirements-twin.txt` to the host `requirements.txt`.

Do **not** copy `dev_app.py`, `migrations/env.py`, `migrations/alembic.ini`,
`migrations/script.py.mako`, or `pytest.ini` — the host repo already has its
own. `dev_app.py` exists only so this module could be built, migrated, run,
and tested standalone in an environment with no access to the real app.

## 2. Chain the migration

`twin_initial` ships with `down_revision = None` so it applies to a fresh tree.
In the host repo, point it at the current head:

```bash
flask db heads          # -> e.g. a1b2c3d4e5f6
```

Then in `migrations/versions/20260903_01_twin_initial.py`:

```python
down_revision = "a1b2c3d4e5f6"
```

The migration was applied and round-tripped (`upgrade` → `downgrade` →
`upgrade`) against SQLite this session, and its DDL was rendered offline
against PostgreSQL dialect and inspected (no PostGIS, `TIMESTAMP WITH TIME
ZONE`, native `JSON`) — **it was never run against a live PostgreSQL server**,
since none was available. Worth a real run before trusting it in prod.

## 3. Register the blueprint

One block in `app.py`, after `db` and the scheduler exist:

```python
from twin import create_twin_blueprint
from twin.ingest.internal_reports import register_approval_hook, register_report_model

create_twin_blueprint(
    app, db,
    scheduler=scheduler,                 # the existing APScheduler instance
    login_required=login_required,       # the app's own decorators
    role_required=role_required,
)

register_report_model(Report)  # the real Report model; see field mapping below

with app.app_context():
    from twin import stream as twin_stream
    register_approval_hook(
        db, on_approved=lambda report: twin_stream.publish("incident", city=None, report=report))
```

`login_required` / `role_required` are optional. Pass them and the twin uses
the app's auth; omit them and it falls back to its own Flask-Login
implementation in [twin/security.py](twin/security.py), which reads the role
from `current_user.role` (override the attribute with `TWIN_ROLE_ATTR`).

`register_report_model` defaults assume `Report.verification_status`,
`Report.latitude`/`longitude`, `Report.priority`, `Report.confidence_score`,
`Report.created_at`, `Report.hazard_type`, `Report.title`, `Report.image_url`.
Pass different attribute names as kwargs if the real schema differs — see
`twin/ingest/internal_reports.py` for the full signature.

Nothing else in `app.py` changes.

## 4. Wire the coordination action buttons (Phase 7)

The drill-down drawer's three action buttons (Declare Emergency, Dispatch
Volunteers, Broadcast Alert) POST to `window.TWIN_COORDINATION_ENDPOINTS[action]`
— a config object the host page must set, e.g. in the template that extends
`digital_twin.html` or a small inline script before it loads:

```html
<script>
  window.TWIN_COORDINATION_ENDPOINTS = {
    "declare-emergency": "/coordination/emergencies/new",
    "dispatch-volunteer": "/api/coordination/assign-volunteer",
    "broadcast-alert": "/send_global_alert",
  };
</script>
```

Without this, each button explains exactly what it would have POSTed
(`{latitude, longitude, source: "digital_twin", h3}`) rather than silently
doing nothing or pretending to succeed. **This is the one piece of Phase 7
this session could not finish**, because it requires the real
`EmergencyEvent`/`VolunteerAssignment`/alert-broadcast endpoints, which live
in the Sentinel AI codebase this session had no access to.

## 5. Mount the page

`/digital-twin` is already a route (registered by `create_twin_blueprint` via
a second, prefix-less blueprint). To also embed it as a tab inside
`analyst_dashboard.html`/`coordination_dashboard.html` per §8.1, wrap the pane
markup (`templates/partials/twin_map_pane.html` ×2 + the header controls) in
an `{% include %}` partial — the current `digital_twin.html` is a full page
shell; splitting the `<body>` content into its own partial for embedding is a
small follow-up, not done this session.

## 6. Verify

```bash
flask db upgrade
curl -b cookies.txt localhost:5000/api/twin/cities
curl -b cookies.txt localhost:5000/api/twin/health
```

Then seed for real:

```bash
python -m scripts.fetch_boundaries --city all       # ~1-2 min, hits Overpass
python -m scripts.seed_twin --city all              # grid + elevation; 5-20 min depending on Open-Meteo rate limits
```

## Notes and real gotchas found while building this

- **SQLite lock contention is real, not theoretical.** Running the seed
  script and the dev server against the same SQLite file at the same time
  produced repeated `database is locked` errors during this session, even
  with `connect_args={"timeout": 15}` set. `dev_app.py` sets that timeout as
  a floor, but **don't run seeding concurrently with anything else writing
  to the same SQLite file** — run it once, let it finish, then start the
  server. PostgreSQL's MVCC makes this a non-issue in prod.
- **A resulting real bug, now fixed:** `IngestAdapter.run()` used to let a
  *failure to persist the audit snapshot* discard an already-successful
  fetch's data. Confirmed live: a real 4,233-record Overpass fetch was
  thrown away because the snapshot INSERT hit a transient lock. Fixed —
  audit-write failures now log a warning and return the data anyway. See the
  regression test in `tests/twin/test_ingest_fallbacks.py`.
- **h3-py v4 API surprises, confirmed against 4.5.0 specifically:**
  `h3.grid_disk()` returns a `list`, not a `set` — `list - set` raises
  `TypeError`. `cells_for_report()` in `internal_reports.py` wraps it in
  `set(...)`; check any other h3 call sites you add against the real
  installed version, not older docs/memory.
- **MapLibre GL rejects `fill-extrusion-opacity` as a data-driven
  expression** (confirmed live: `addLayer` throws "data expressions not
  supported" and the whole layer silently fails to add). The fix already
  applied: opacity per status band is baked into an `rgba()` colour instead.
  If you add more data-driven paint properties, check MapLibre's own docs for
  which ones actually support expressions before assuming.
- **NASA GIBS's true-colour layers cap at zoom 9** (native ~500m/px
  resolution) — a raster source with no `maxzoom` requests tiles past that
  and gets a 400 for every one. `GET /api/twin/gibs` returns `max_zoom`;
  the frontend must pass it to `TwinMap.setRasterUrl`.
- **OpenFreeMap Liberty's vector source is named `openmaptiles`**, and its
  `building` source-layer only has data from zoom 13 — confirmed by fetching
  the live style.json, not guessed. The 3D buildings layer (`buildings-3d`)
  is set to `minzoom: 13` to match; don't expect it to render at the twin's
  default city-wide zoom (10.2).
- **Scheduler.** `create_twin_blueprint` calls `twin.jobs.register_jobs` only
  when a scheduler is passed and `TWIN_SCHEDULER_ENABLED=1`, so passing the
  real scheduler now is harmless even before Phase 3 code exists in an older
  checkout.
- **Seeding metadata.** City and zone metadata (not the H3 grid) is upserted
  at registration. If the twin tables don't exist yet, this is skipped with a
  warning rather than raising — otherwise the very migration that creates
  them could never run.
- **SSE.** `/api/twin/stream` pins one worker per connection, two per open
  dashboard. Run gunicorn with `-k gevent` or `-k eventlet`, or accept the
  client's 60s poll fallback under a sync worker class.
- **flask-compress.** If the host adds it, exclude `text/event-stream` or the
  stream buffers and never flushes.
- **`/timeline` is wired but unpopulated.** `TwinCellHistory` has no writer
  job yet — add an hourly rollup (copy the current horizon=0
  `TwinCellState` rows into `TwinCellHistory`) if the sparkline needs to show
  real data. The route itself works correctly against whatever rows exist.

## Standalone development

```bash
python -m pip install -r requirements-twin.txt
flask --app dev_app db upgrade
TWIN_DEV_SCHEDULER=1 flask --app dev_app run   # or omit the env var to skip the scheduler
# then: GET /login?role=official  ->  GET /digital-twin
pytest
```

`dev_app.py` includes a minimal `Report` model and `/reports`,
`/reports/<id>/approve` dev-only endpoints so the incident layer and the
approval→SSE hook can be exercised without the real Sentinel AI app.

`TWIN_DEV_OPEN_AUTH=1` bypasses both auth checks, and is itself ignored unless
the app is in debug or testing mode.
