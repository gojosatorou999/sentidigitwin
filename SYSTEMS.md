# Sentinel AI — the live systems

Everything in this file is a layer that was built on top of the base app that
`README.md` describes: the two AI agents, the weather and satellite maps, the
three camera layers, and the rules that keep them honest.

One sentence that explains most of the design decisions below: **an analyst
has to be able to say why, in numbers, in an enquiry.** So no model ever
computes a risk score, a wind vector or an arrival time. Models write the
paragraph that explains those numbers, and nothing else.

---

## 1. The two agents

Both are LangGraph DAGs. Both write to the same `twin_flag` table behind the
same `pending` gate. Neither can send anything to the public — an analyst
clicks, or nothing happens.

### Triage agent — *what is happening now*

`twin/agent/graph.py`

```
gather → extract → correlate → score → threshold
                                          |
                            nothing flagged → END
                                          |
                                   retrieve → draft_brief → persist
```

Reads what the twin already knows: official CAP alerts in force (NDMA
SACHET), cells the deterministic scorer has pushed near the threshold,
stalled transit fleets, station readings. Groups those into events, scores
them with `twin/scoring.py`, and files anything that clears the bar.

### Forecast agent — *what is coming, and how long have we got*

`twin/agent/forecast_graph.py`, physics in `twin/forecast.py`

```
sample → advect → detect → threshold
                              |
                nothing projected → END
                              |
                       retrieve → draft_brief → persist
```

| Node | What it does |
|---|---|
| `sample` | Hourly wind / cloud / rain / apparent-temperature on a ~5 km lattice over the city (`twin/ingest/windfield.py`, keyless Open-Meteo, one request per city) |
| `advect` | Carries every raining lattice point downwind hour by hour; reads heat threshold crossings in place |
| `detect` | Groups projections within 6 km into events, each with cells, an arrival hour and a confidence |
| `threshold` | Drops anything under the confidence floor, off the grid, or below every severity band |
| `retrieve` | RAG over `data/twin/corpus/` for that hazard's SOP |
| `draft_brief` | Deterministic brief from the numbers; the LLM only rewrites it |
| `persist` | Upserts into `twin_flag` as `pending` |

**Two mechanisms, kept separate because they fail differently.**

*Advection* carries a rain field along the wind vector. It is first-order
kinematics, not a weather model: the system is assumed carried without
growing, decaying or turning. Good for a couple of hours, degrading after —
hence `MAX_LEAD_HOURS = 12` and a confidence that decays with lead time and
with light wind, where the bearing is noise rather than signal.

*Heat* does not advect, it builds in place, so it is read straight off the
apparent-temperature series (which already folds in humidity and wind) and
flagged on **crossings** — "it passes 42 °C at 14:00" — not on every hot hour.

**Live output right now:** e.g. *"Rain arriving in ~13h — Bengaluru,
5.2 mm/h"*, carried by a 10 km/h wind from 267°, 6 independent lattice points
agreeing, confidence 0.94. That flag appears in the console's flag queue with
the dispatch buttons beside it.

### The conditional edge

Both graphs skip every downstream node when nothing clears the threshold, and
both LLM nodes sit downstream. A calm city costs **zero tokens**. An agent
that bills for silence gets switched off, and an agent that is switched off
cannot warn anyone.

### Without LangGraph, or without a model key

Both graphs run their nodes in plain sequence if `langgraph` is absent, and
both fall back to templated briefs built from the evidence if no LLM is
configured. The flag records which mode produced it (`agent_mode`).

### Three bugs worth remembering

**The wind convention.** `wind_direction_10m` is the direction wind blows
*from*. A 90° wind is an easterly and moves air *westward*. Using it directly
puts every projected storm on the opposite side of the city and still looks
completely plausible on a map. The conversion lives in one tested function
(`windfield.wind_vector`) with a test per cardinal direction.

**Hyderabad projected nothing.** Its clip polygon is GHMC (805 cells, ~600 km²)
inside a bbox several times that area, so projections landed just off the
modelled grid and were discarded. Now snapped to cells within the
projection's own ~1.5 km error bar, which is the accuracy the method actually
has.

**Duplicate flags.** `_cluster_key` decides whether a projection updates an
existing flag or creates a new one, and it was wrong twice the same way:
keyed on the arrival hour it minted a flag every pass (the hour counts down),
and keyed on `cells[0]` it minted one whenever the projection drifted enough
to reorder the sorted cell list — observed live, a re-run created 7 new flags
instead of updating 7. Now bucketed on a coarse (resolution 5, district-sized)
cell containing the cluster centre. A second pass over the same weather
reports `flags_updated`, pinned by three tests.

---

## 2. The maps

### Windy embeds — analyst dashboard

Three keyless iframes: **live temperature**, **live satellite** (cloud), and
**fire danger** (FIRMS). They are what an analyst *looks at* — fast, global,
familiar.

They are deliberately **not** what the agent computes on. An iframe cannot be
audited, sampled or cited: you cannot ask it what the wind was at a point, and
you cannot put its answer in a brief. So the forecast agent reads Open-Meteo's
hourly series for the same quantities and does its own arithmetic. The two are
complementary — Windy for the human eye, Open-Meteo for the numbers that have
to survive a question.

### The twin's own map

MapLibre, H3 cells at resolution 8, coloured by risk. Basemap selector
includes NASA GIBS satellite imagery by date. Horizon buttons (now / +3h /
+6h / +24h) re-score the city; the KPI row shows the signed change against
"now", because on a calm day the numbers move without any cell changing
status band, and the buttons would otherwise look inert.

---

## 3. Cameras — three distinct layers

They answer three different questions and are never mixed.

| Layer | Question | Source |
|---|---|---|
| `twin/ingest/cctv.py` | *Is there a camera here, and whose?* | OpenStreetMap — positions only |
| `twin/cameras.py` | *What does it see right now?* | The operator's own hand-listed feeds |
| `twin/ingest/cctv_live.py` | *Same, at the scale of a road authority* | Ten public keyless catalogs |

### The ten authorities

Probed live; all ten responding, **8,879 cameras** reachable.

| Key | Authority | Cameras |
|---|---|---|
| `fintraffic` | Fintraffic (Finland) | 2,255 |
| `caltrans` | Caltrans | 1,559 |
| `drivebc` | DriveBC | 1,049 |
| `hongkong` | Transport Department, HKSAR | 1,013 |
| `ontario` | Ontario 511 | 945 |
| `austin` | Austin Transportation | 817 |
| `tfl` | Transport for London | 800 |
| `calgary` | City of Calgary | 217 |
| `nsw` | Transport for NSW | 216 |
| `singapore` | Land Transport Authority | 8–90 |

### Three rules, enforced in code

1. **Keyless.** Every catalog is public. Works on a fresh clone with an empty
   `.env`. No key means no key to leak.
2. **Coverage-gated.** A provider declares the bbox it serves and is fetched
   *only* when that meets the bbox being asked about. A foreign city's cameras
   never stand in for a local one. `providers_for(None)` returns `()` —
   an unscoped request fetches nothing.
3. **Never proxied.** Frame URLs are *built* from an official origin plus a
   validated id, never copied from an upstream string, and the server never
   fetches a frame. The browser loads it directly from the publishing
   authority, so the twin never becomes an access route into a camera network.

Hong Kong is the only non-JSON catalog (UTF-16 TSV). It publishes a `url`
column that is deliberately **ignored** — the frame is rebuilt from a
validated key. Singapore is a documented exception to rule 3: it republishes
each frame under a fresh UUID, so the URL cannot be derived; it is
origin-pinned instead, which is weaker and is labelled as such rather than
quietly levelled in.

### Why India is empty, and what the panel shows instead

No Indian authority publishes a live camera catalog. Checked on 2026-09-21:
Bengaluru Traffic Police (a viewer, not an API), GHMC, TS Police,
data.gov.in, OpenCity (positions only, crowdsourced from OSM — already
ingested as the OSINT layer).

Since both modelled cities are Indian, the panel was empty on *every* screen
the project has, which makes a working layer indistinguishable from a broken
one. So when no authority covers the city **and** the operator has no feed of
their own, the panel shows a small live sample from Hong Kong under an amber
**"Not local"** banner. It is fenced as a display device:

- never beside local cameras — an operator feed suppresses it entirely;
- never on an unscoped request;
- every stream flagged and labelled, in the list *and* in the player;
- **positionless** — coordinates are nulled server-side, so it cannot be
  drawn as a pin on this city's map even by a consumer that ignores the flag;
- never in scoring — a test asserts `live_streams` has exactly one caller.

`TWIN_CCTV_REFERENCE_ENABLED=0` restores the strictly-empty panel.

The **Camera coverage** panel (video icon in the twin toolbar,
`GET /api/twin/cctv/coverage`) probes every authority on demand and reports
which are up, how many cameras each has, and which modelled cities they serve.
It is not on any refresh path — it calls every authority in the registry,
which is what the coverage gate exists to avoid doing routinely.

### Street imagery

Mapillary, via a hand-rolled Mapbox Vector Tile reader
(`twin/ingest/mapillary_tiles.py`, no new dependency). The documented Graph
bbox endpoint returns zero rows with HTTP 200 over Bengaluru, Amsterdam *and*
Helsinki, so discovery goes through tiles instead: one Bengaluru tile decodes
to 15,412 photos, nearest 23 m away, with bearings. Cameras whose bearing was
inferred rather than published are marked as guesses.

---

## 4. RAG

`twin/agent/rag.py`. Local embeddings, no key. Both agents retrieve from
`data/twin/corpus/` and cite what they used.

The folder is currently **empty**, so briefs carry no citations. Drop `.md`,
`.txt` or `.json` files in — SOPs, escalation matrices, past situation
reports, shelter registers — and restart. The folder's own `README` is
deliberately extensionless so it is never itself ingested and quoted back
inside an incident brief.

Retrieval is keyword-scored rather than purely embedded for numeric queries:
embedding "62 mm of rain" would let a flag cite a similar-*sounding* sentence
instead of the measurement, which is the exact failure citations exist to
prevent.

---

## 5. Security

Fixed in this pass, each verified against the running app:

| Issue | Was | Now |
|---|---|---|
| **Debug RCE** | `app.run(debug=True, host='0.0.0.0')` — the Werkzeug console, which executes arbitrary Python from the browser, offered to the whole network | Both opt-in, default off, default bind loopback; refuses to start if debug is on and the bind is not loopback |
| **Open redirect** | `redirect(request.args.get('next'))` unvalidated | `_safe_next()` accepts only root-relative paths; rejects absolute, scheme-relative (`//evil.example`) and scheme-carrying URLs |
| **Session forgery** | `SECRET_KEY` fell back to the hardcoded `'dev-key-for-demo-only'` | Random per process, with a warning. Sessions not surviving a restart is a visible nuisance; a known key is an invisible one |
| **CSRF** | Only FlaskForm routes were covered. `/verify_report` and `/reject_report` read `request.form` directly — an official opening a crafted page would approve a fabricated report in their own name | `CSRFProtect` site-wide; the token is attached to every same-origin mutating request by a `fetch`/XHR wrapper in `base.html`, so no call site changed |
| **Forged webhook** | `/webhook/whatsapp` accepted any POST — spoof `From` and drive volunteer registration, alerts, another person's data | Twilio signature required, fails closed when unconfigured, CSRF-exempt because Twilio cannot carry our token |

The CSRF wrapper never attaches the token to a cross-origin request — that
would leak it to whoever is being called, which is worse than not having it.

**Still open:** `scripts/import_twin_grid.py` interpolates table names into
SQL. It is a local one-shot migration script with no external input, so it is
noted rather than rewritten.

---

## 6. Configuration

Keys live in `.env` only, which is git-ignored. `.env.example` carries the
names and explanations, never values.

| Working | Notes |
|---|---|
| `OPENAI_API_KEY` | Both agents in LLM mode on `gpt-5-nano` |
| `MAPILLARY_TOKEN` | Regenerated with Graph read scope; thumbnails verified |
| `TOMTOM_API_KEY` | Live traffic flow |
| `OPENAQ_API_KEY` | 26 monitoring locations near Bengaluru |
| `AQICN_TOKEN` | City feeds work; `/map/bounds/` returns nothing for either city. Do **not** use `/feed/geo:` as a fallback — it returned a *Delhi* station for a Bengaluru query |
| — | NDMA SACHET, GDACS, USGS, Open-Meteo and all ten camera providers need no key |

**Not obtained:** `DATA_GOV_IN_KEY` — without it there is effectively no CPCB
station data. Twilio is unset, so approved flags reach people in-app but not
by WhatsApp or SMS.

| Switch | Default | Effect |
|---|---|---|
| `TWIN_FORECAST_ENABLED` | `1` | `0` leaves only the triage agent |
| `TWIN_CCTV_REFERENCE_ENABLED` | `1` | `0` restores the strictly-empty camera panel |
| `TWIN_CCTV_REFERENCE_PROVIDER` | `hongkong` | Which authority stands in |
| `TWIN_CCTV_LIVE_ENABLED` | `1` | `0` leaves only the operator's own feed file |
| `FLASK_DEBUG` | `0` | `1` enables the debugger; loopback only |

---

## 7. Tests

**544 passing.** `python -m pytest`

On Windows, run with `--capture=tee-sys`: pytest's default capture breaks the
Node subprocess in `test_layers_js.py` with `WinError 50`, which presents as
eight real failures and is not.

The suite is hermetic. One thing worth knowing: the reference camera feed is
disabled in `tests/twin/conftest.py`, because every test city is one with no
local coverage, so leaving it on turned parts of the suite into live network
calls against Hong Kong's Transport Department — which it did, for two tests,
before that line existed. The tests that exercise it stub the provider.
