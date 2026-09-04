# 🌐 Sentinel Digital Twin (Hyderabad + Bengaluru)

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Flask](https://img.shields.io/badge/framework-Flask-lightgrey.svg)](https://flask.palletsprojects.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

A high-performance, real-time urban **Digital Twin** for disaster response, hazard risk scoring, and multi-agent AI incident coordination across **Hyderabad** and **Bengaluru**.

The twin decomposes urban spaces into **H3 hexagonal cell grids** (resolution 8) with continuously calculated state vectors—integrating live weather forecasts, flood discharge, air quality, elevation models, critical infrastructure density, and live verified incident reports.

---

## 🚀 Features

- **Dual-City H3 Hexagonal Grid**: Over 1,700 H3 cells mapped dynamically across Hyderabad (GHMC) and Bengaluru (BBMP).
- **Composite Risk Engine**: Dynamic scoring ($0 - 100$) combining **Hydro Pressure**, **Incident Severity**, **Terrain Vulnerability**, **Infrastructure Density**, and **Environmental Stress**.
- **Predictive Horizon Scrubber**: Supports 4 forecast time horizons (`Now`, `T+3h`, `T+6h`, `T+24h`).
- **3D Visualization**: Renders 3D extruded cell risk heights over MapLibre GL vector basemaps with satellite and weather radar (RainViewer / NASA GIBS) overlays.
- **Real-time Server-Sent Events (SSE)**: Live streaming of state updates and incident approval notifications to dashboard clients.
- **Resilient Multi-Tier Ingestion**: Fault-tolerant API adapters with automatic caching, retries, and neutral-value fallbacks (Open-Meteo, OpenTopography, Overpass, RainViewer).

---

## 🛠️ System Architecture

```
                                    ┌───────────────────────┐
                                    │ Open-Meteo / Overpass │
                                    │ USGS / NASA GIBS      │
                                    └───────────┬───────────┘
                                                │ (Ingest Adapters)
                                                ▼
┌───────────────────────┐           ┌───────────────────────┐
│ Sentinel AI Incidents ├──────────►│  State Engine         │
└───────────────────────┘           │  - Hydro              │
                                    │  - Incidents          │
┌───────────────────────┐           │  - Terrain            │
│  H3 Hexagonal Grid    ├──────────►│  - Infra & Env        │
│  (Resolution 8)       │           └───────────┬───────────┘
└───────────────────────┘                       │
                                                ▼
                                    ┌───────────────────────┐
                                    │  Flask REST API &     │
                                    │  SSE Event Stream     │
                                    └───────────┬───────────┘
                                                │
                                                ▼
                                    ┌───────────────────────┐
                                    │ Dual-Pane MapLibre GL │
                                    │ Web UI Dashboard      │
                                    └───────────────────────┘
```

---

## 📋 Quick Start

### 1. Prerequisites
- **Python 3.10+**
- **pip** package manager

### 2. Installation

Clone the repository and install the dependencies:

```bash
git clone https://github.com/gojosatorou999/digital-twin.git
cd digital-twin
pip install -r requirements-twin.txt
```

### 3. Database Setup

Copy the example environment file and run database migrations:

```bash
cp .env.example .env
flask --app dev_app db upgrade
```

### 4. Running the Development Host

Launch the standalone Flask development server:

```bash
TWIN_DEV_SCHEDULER=1 flask --app dev_app run
```

Access the application in your browser:
- **Dev Login**: `http://localhost:5000/login?role=official`
- **Digital Twin Dashboard**: `http://localhost:5000/digital-twin`
- **API Health Check**: `http://localhost:5000/api/twin/health`

---

## 🧪 Testing

Run unit and integration tests using `pytest`:

```bash
pytest
```

---

## 📁 Repository Structure

```
digital-twin/
├── twin/                       # Core Digital Twin Python Package
│   ├── ingest/                 # API adapters (Open-Meteo, Overpass, NASA GIBS, etc.)
│   ├── engine.py               # State computation orchestrator
│   ├── scoring.py              # Composite risk score & sub-score logic
│   ├── grid.py                 # H3 spatial indexing & polygon math
│   ├── models.py               # SQLAlchemy database models
│   ├── routes.py               # Flask REST API endpoints
│   ├── stream.py               # Server-Sent Events (SSE) broker
│   └── config.py               # City metadata & scoring weights
├── static/                     # Frontend Assets
│   ├── css/twin.css            # Custom UI styling & themes
│   └── js/                     # MapLibre GL controllers & SSE streaming
├── templates/                  # Jinja2 HTML Templates
│   ├── digital_twin.html       # Standalone dual-city dashboard shell
│   └── partials/               # Reusable map pane & drill-down drawers
├── scripts/                    # Seeding & Boundary Sourcing Tools
│   ├── fetch_boundaries.py     # Overpass boundary fetcher
│   └── seed_twin.py            # Cell grid & elevation seeder
├── tests/twin/                 # Comprehensive Test Suite
├── dev_app.py                  # Standalone Flask Development Host
├── DIGITAL_TWIN_README.md      # In-depth Architecture & Implementation Plan
└── TWIN_INTEGRATION.md         # Guide for embedding twin into host application
```

---

## 🌐 Key API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/twin/cities` | List configured twin cities and zones |
| `GET` | `/api/twin/<city>/state` | Fetch spatial H3 grid risk state GeoJSON |
| `GET` | `/api/twin/<city>/cell/<h3_index>` | Detailed cell drill-down data & explainable sub-scores |
| `GET` | `/api/twin/<city>/incidents` | GeoJSON stream of verified incident reports |
| `GET` | `/api/twin/health` | Health status of data sources & ingestion pipelines |
| `GET` | `/api/twin/stream` | Real-time SSE event stream |

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).
