"""SQLAlchemy models for the digital twin (section 6).

The host application owns the ``db`` object, so the models are defined lazily
by :func:`init_models` rather than at import time. This keeps the module free
of a circular ``from app import db`` and satisfies C6 (additive, not invasive):
nothing here touches the existing ``Report`` schema.

Call ``init_models(db)`` exactly once, before importing anything that
references the model classes. It is idempotent.
"""

from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, TypeDecorator

__all__ = [
    "init_models", "models_ready", "utcnow", "UTCDateTime",
    "TwinCity", "TwinZone", "TwinCell", "TwinCellState",
    "TwinCellHistory", "TwinInfrastructure", "TwinDataSnapshot",
]

# Populated by init_models().
db = None
TwinCity = None
TwinZone = None
TwinCell = None
TwinCellState = None
TwinCellHistory = None
TwinInfrastructure = None
TwinDataSnapshot = None

_READY = False


def utcnow():
    """Timezone-aware UTC now. The only clock the twin is allowed to read (C8)."""
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """A DateTime that is always timezone-aware UTC on both sides of the wire.

    SQLite discards tzinfo, so a plain ``DateTime(timezone=True)`` round-trips
    as naive there and as aware on PostgreSQL -- the exact cross-database
    divergence C2 forbids, and the exact silent 5.5 h error C8 is about.
    This normalises to UTC on write and re-attaches UTC on read, so
    ``hours_since_report`` means the same thing on both engines.
    """

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            # A naive datetime reaching the twin is a bug upstream, but
            # assuming UTC is strictly better than storing an offset we
            # cannot recover.
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def models_ready():
    return _READY


def init_models(db_):
    """Define the twin models against the host app's Flask-SQLAlchemy ``db``.

    Returns a dict of the model classes. Safe to call more than once.
    """
    global _READY, db
    global TwinCity, TwinZone, TwinCell, TwinCellState
    global TwinCellHistory, TwinInfrastructure, TwinDataSnapshot

    if _READY:
        return _registry()

    db = db_
    Model = db_.Model
    Column = db_.Column
    Integer = db_.Integer
    String = db_.String
    Float = db_.Float
    Text = db_.Text
    Boolean = db_.Boolean
    ForeignKey = db_.ForeignKey
    Index = db_.Index
    UniqueConstraint = db_.UniqueConstraint

    class _TwinCity(Model):
        __tablename__ = "twin_city"

        id = Column(Integer, primary_key=True)
        slug = Column(String(64), unique=True, nullable=False, index=True)
        display_name = Column(String(128), nullable=False)
        state = Column(String(128))
        country = Column(String(128), default="India")

        center_latitude = Column(Float, nullable=False)
        center_longitude = Column(Float, nullable=False)
        bbox_min_lon = Column(Float, nullable=False)
        bbox_min_lat = Column(Float, nullable=False)
        bbox_max_lon = Column(Float, nullable=False)
        bbox_max_lat = Column(Float, nullable=False)

        default_zoom = Column(Float, default=10.2)
        default_pitch = Column(Float, default=55.0)
        default_bearing = Column(Float, default=-12.5)

        # "GHMC-6" | "BBMP-8" -- a re-seed to new corporation boundaries is a
        # data migration, not a code change (section 2.1).
        zone_scheme = Column(String(32))
        h3_resolution = Column(Integer, default=8, nullable=False)

        # GloFAS return-period reference for the hydro discharge sub-score.
        # There is no API that serves this; it is derived once at seed time
        # from the flood archive and stored here (section 5.1).
        basin_name = Column(String(64))
        basin_outlet_lat = Column(Float)
        basin_outlet_lon = Column(Float)
        discharge_2yr_return = Column(Float)

        is_active = Column(Boolean, default=True, nullable=False)
        created_at = Column(UTCDateTime, default=utcnow, nullable=False)
        updated_at = Column(UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False)

        def __repr__(self):
            return "<TwinCity %s>" % self.slug

    class _TwinZone(Model):
        __tablename__ = "twin_zone"
        __table_args__ = (
            UniqueConstraint("city_id", "slug", name="uq_twin_zone_city_slug"),
        )

        id = Column(Integer, primary_key=True)
        city_id = Column(Integer, ForeignKey("twin_city.id"), nullable=False, index=True)
        slug = Column(String(64), nullable=False)
        display_name = Column(String(128), nullable=False)

        zone_type = Column(String(16), default="zone", nullable=False)  # zone|circle|ward
        parent_zone_id = Column(Integer, ForeignKey("twin_zone.id"), nullable=True)

        center_latitude = Column(Float)
        center_longitude = Column(Float)
        boundary_geojson = Column(Text)
        boundary_source = Column(String(24))  # osm|datameet|approximate
        population_estimate = Column(Integer)

        created_at = Column(UTCDateTime, default=utcnow, nullable=False)

        def __repr__(self):
            return "<TwinZone %s/%s>" % (self.city_id, self.slug)

    class _TwinCell(Model):
        __tablename__ = "twin_cell"

        id = Column(Integer, primary_key=True)
        h3_index = Column(String(20), unique=True, nullable=False, index=True)
        city_id = Column(Integer, ForeignKey("twin_city.id"), nullable=False, index=True)
        zone_id = Column(Integer, ForeignKey("twin_zone.id"), nullable=True, index=True)

        center_latitude = Column(Float, nullable=False)
        center_longitude = Column(Float, nullable=False)
        boundary_geojson = Column(Text)   # 7-point hexagon ring, (lng, lat), 5 dp
        area_sqkm = Column(Float)

        elevation_m = Column(Float)
        elevation_source = Column(String(24))   # open_meteo|opentopodata|unknown
        dist_to_water_m = Column(Float)
        drain_length_m = Column(Float, default=0.0, nullable=False)

        # Horizon-invariant, cached at seed / weekly refresh (section 5.3).
        infra_criticality_cached = Column(Float, default=0.0, nullable=False)
        terrain_score_cached = Column(Float)

        created_at = Column(UTCDateTime, default=utcnow, nullable=False)
        updated_at = Column(UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False)

        def __repr__(self):
            return "<TwinCell %s>" % self.h3_index

    class _TwinCellState(Model):
        __tablename__ = "twin_cell_state"
        __table_args__ = (
            UniqueConstraint("cell_id", "horizon_hours", name="uq_twin_state_cell_horizon"),
            Index("ix_twin_state_cell_horizon", "cell_id", "horizon_hours"),
        )

        id = Column(Integer, primary_key=True)
        cell_id = Column(Integer, ForeignKey("twin_cell.id"), nullable=False, index=True)
        horizon_hours = Column(Integer, nullable=False, index=True)  # 0|3|6|24

        risk_score = Column(Float, nullable=False, default=0.0)
        status = Column(String(16), nullable=False, default="normal")

        # The two halves of the composite (section 5.1).
        hazard_score = Column(Float)
        vulnerability_multiplier = Column(Float)

        # The five named sub-scores. None means UNMEASURED; 0.0 means measured
        # as nothing. Never conflate them -- see scoring.py.
        hydro_score = Column(Float)
        incident_score = Column(Float)
        terrain_score = Column(Float)
        infra_score = Column(Float)
        env_score = Column(Float)

        raw_inputs = Column(JSON)        # every raw number that fed the formula (C3)
        degraded_inputs = Column(JSON)   # e.g. ["flood", "aqi"]

        incident_count = Column(Integer, default=0, nullable=False)
        top_incident_report_id = Column(Integer, nullable=True)

        computed_at = Column(UTCDateTime, default=utcnow, nullable=False, index=True)

        def __repr__(self):
            return "<TwinCellState cell=%s h=%s risk=%.1f>" % (
                self.cell_id, self.horizon_hours, self.risk_score or 0.0)

    class _TwinCellHistory(Model):
        __tablename__ = "twin_cell_history"
        __table_args__ = (
            Index("ix_twin_history_cell_time", "cell_id", "computed_at"),
        )

        id = Column(Integer, primary_key=True)
        cell_id = Column(Integer, ForeignKey("twin_cell.id"), nullable=False, index=True)
        horizon_hours = Column(Integer, nullable=False)
        risk_score = Column(Float, nullable=False)
        status = Column(String(16), nullable=False)
        computed_at = Column(UTCDateTime, default=utcnow, nullable=False, index=True)

    class _TwinInfrastructure(Model):
        __tablename__ = "twin_infrastructure"
        __table_args__ = (
            Index("ix_twin_infra_city_type", "city_id", "asset_type"),
        )

        id = Column(Integer, primary_key=True)
        city_id = Column(Integer, ForeignKey("twin_city.id"), nullable=False, index=True)
        cell_id = Column(Integer, ForeignKey("twin_cell.id"), nullable=True, index=True)
        osm_id = Column(String(48), index=True)

        asset_type = Column(String(32), nullable=False)
        name = Column(String(255))
        criticality = Column(Float, default=0.0, nullable=False)

        latitude = Column(Float, nullable=False)
        longitude = Column(Float, nullable=False)
        tags = Column(JSON)

        source = Column(String(24), default="overpass")
        fetched_at = Column(UTCDateTime, default=utcnow, nullable=False)

        def __repr__(self):
            return "<TwinInfrastructure %s %s>" % (self.asset_type, self.name)

    class _TwinDataSnapshot(Model):
        """Audit row for every ingestion run (C7) and the source of the UI health pill."""

        __tablename__ = "twin_data_snapshot"
        __table_args__ = (
            Index("ix_twin_snapshot_source_time", "source_key", "started_at"),
        )

        id = Column(Integer, primary_key=True)
        source_key = Column(String(64), nullable=False, index=True)
        city_id = Column(Integer, ForeignKey("twin_city.id"), nullable=True, index=True)

        status = Column(String(16), nullable=False)  # ok|degraded|failed
        latency_ms = Column(Integer)
        records_ingested = Column(Integer, default=0)
        error_message = Column(Text)
        payload_digest = Column(String(64))

        started_at = Column(UTCDateTime, default=utcnow, nullable=False, index=True)
        finished_at = Column(UTCDateTime)

        def __repr__(self):
            return "<TwinDataSnapshot %s %s>" % (self.source_key, self.status)

    TwinCity = _TwinCity
    TwinZone = _TwinZone
    TwinCell = _TwinCell
    TwinCellState = _TwinCellState
    TwinCellHistory = _TwinCellHistory
    TwinInfrastructure = _TwinInfrastructure
    TwinDataSnapshot = _TwinDataSnapshot

    _READY = True

    # Re-export onto the module globals so `from twin.models import TwinCity`
    # works after initialisation.
    globals().update(_registry())
    return _registry()


def _registry():
    return {
        "TwinCity": TwinCity,
        "TwinZone": TwinZone,
        "TwinCell": TwinCell,
        "TwinCellState": TwinCellState,
        "TwinCellHistory": TwinCellHistory,
        "TwinInfrastructure": TwinInfrastructure,
        "TwinDataSnapshot": TwinDataSnapshot,
    }
