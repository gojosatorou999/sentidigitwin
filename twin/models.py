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
    "TwinExternalAlert", "TwinAlertCell", "TwinObservation",
    "TwinBaseline", "TwinFlag", "TwinFlagCell", "TwinDispatch",
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
TwinExternalAlert = None
TwinAlertCell = None
TwinObservation = None
TwinBaseline = None
TwinFlag = None
TwinFlagCell = None
TwinDispatch = None

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
    global TwinExternalAlert, TwinAlertCell, TwinObservation
    global TwinBaseline, TwinFlag, TwinFlagCell, TwinDispatch

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

    class _TwinExternalAlert(Model):
        """One official alert from an external authority (NDMA SACHET, GDACS, USGS).

        Kept structurally separate from the host's citizen ``Report`` table
        because the two carry different authority: an IMD thunderstorm warning
        and an anonymous submission may both raise the same sub-score, but an
        operator deciding whether to evacuate a ward must always be able to see
        which one they are looking at.

        ``UNIQUE(source, source_uid)`` is what makes re-polling idempotent --
        SACHET's ``<guid>`` is stable across polls, so a feed re-read updates
        rows instead of duplicating them.
        """

        __tablename__ = "twin_external_alert"
        __table_args__ = (
            UniqueConstraint("source", "source_uid", name="uq_twin_alert_source_uid"),
            Index("ix_twin_alert_city_expires", "city_id", "expires_at"),
            Index("ix_twin_alert_effective", "effective_at"),
        )

        id = Column(Integer, primary_key=True)
        source = Column(String(24), nullable=False, index=True)   # sachet|gdacs|usgs
        source_uid = Column(String(128), nullable=False)
        cap_identifier = Column(String(128), index=True)

        city_id = Column(Integer, ForeignKey("twin_city.id"), nullable=True, index=True)

        sender = Column(String(255))
        event = Column(String(255))
        category = Column(String(64))          # Met|Geo|Safety|Health|...
        severity = Column(String(24))          # Extreme|Severe|Moderate|Minor|Unknown
        certainty = Column(String(24))         # Observed|Likely|Possible|Unlikely
        urgency = Column(String(24))           # Immediate|Expected|Future|Past
        msg_type = Column(String(24))          # Alert|Update|Cancel|Ack|Error

        headline = Column(Text)
        description = Column(Text)
        instruction = Column(Text)
        area_desc = Column(Text)

        # polygon: a real CAP polygon was fetched and stored.
        # district: only an LGD/district code or areaDesc was given -- the alert
        #           is real but its footprint is not, so it must never be drawn
        #           as if it were a surveyed boundary.
        # point:    a coordinate + radius (GDACS/USGS).
        geometry_kind = Column(String(16))
        geometry_geojson = Column(Text)

        effective_at = Column(UTCDateTime, index=True)
        onset_at = Column(UTCDateTime)
        expires_at = Column(UTCDateTime, index=True)
        sent_at = Column(UTCDateTime)

        # A cap:msgType=Update supersedes the alert it references rather than
        # adding to it. Without this, an updated warning is counted twice.
        references_uid = Column(String(255))
        superseded_at = Column(UTCDateTime)

        raw_url = Column(Text)
        raw = Column(JSON)
        fetched_at = Column(UTCDateTime, default=utcnow, nullable=False)

        def __repr__(self):
            return "<TwinExternalAlert %s %s>" % (self.source, self.event)

    class _TwinAlertCell(Model):
        """Which H3 cells an alert's polygon actually covers.

        Only written for ``geometry_kind='polygon'``. A district-scoped alert
        deliberately produces no rows here: inflating one to every cell in the
        city would light the whole map red on a routine IMD advisory and train
        operators to ignore the colour.
        """

        __tablename__ = "twin_alert_cell"
        __table_args__ = (
            UniqueConstraint("alert_id", "h3_index", name="uq_twin_alert_cell"),
            Index("ix_twin_alert_cell_h3", "h3_index"),
        )

        id = Column(Integer, primary_key=True)
        alert_id = Column(Integer, ForeignKey("twin_external_alert.id"),
                          nullable=False, index=True)
        city_id = Column(Integer, ForeignKey("twin_city.id"), nullable=True, index=True)
        h3_index = Column(String(20), nullable=False)

    class _TwinObservation(Model):
        """The latest reading from one live point source (station or vehicle).

        Latest-only by design: ``UNIQUE(source_key, station_uid)`` means a poll
        updates in place. A live map layer wants "where is every bus now",
        not "every position every bus has ever had" -- at 5,000 vehicles on a
        30 s cadence the history variant writes 14 M rows a day and the layer
        query slows to a crawl. Long-run history for anomaly baselines lives in
        ``twin_baseline`` instead, computed from archives rather than polling.
        """

        __tablename__ = "twin_observation"
        __table_args__ = (
            UniqueConstraint("source_key", "station_uid", name="uq_twin_obs_source_station"),
            Index("ix_twin_obs_city_kind", "city_id", "kind"),
            Index("ix_twin_obs_observed", "observed_at"),
        )

        id = Column(Integer, primary_key=True)
        source_key = Column(String(64), nullable=False, index=True)
        station_uid = Column(String(128), nullable=False)
        city_id = Column(Integer, ForeignKey("twin_city.id"), nullable=True, index=True)

        # air_quality|water_level|transit_vehicle|traffic|weather_station
        kind = Column(String(32), nullable=False)
        name = Column(String(255))
        operator = Column(String(128))

        latitude = Column(Float)
        longitude = Column(Float)
        h3_index = Column(String(20), index=True)

        # The headline number for the layer's colour ramp (AQI, metres, km/h).
        value = Column(Float)
        unit = Column(String(24))
        status = Column(String(24))        # ok|stale|offline|delayed
        metrics = Column(JSON)             # every other measured field
        raw = Column(JSON)

        observed_at = Column(UTCDateTime)  # when the *source* measured it
        fetched_at = Column(UTCDateTime, default=utcnow, nullable=False)

        def __repr__(self):
            return "<TwinObservation %s %s=%s>" % (self.kind, self.station_uid, self.value)

    class _TwinBaseline(Model):
        """What "normal" looks like for one cell, one metric, one month.

        This is the memory the anomaly detector needs and the twin did not have:
        ``twin_cell_history`` starts empty, so "62 mm of rain" cannot be judged
        abnormal until something says what September usually brings *here*.
        Populated from open archives (scripts/backfill_baselines.py), not from
        live polling, so it is useful on day one rather than after a monsoon.
        """

        __tablename__ = "twin_baseline"
        __table_args__ = (
            UniqueConstraint("cell_id", "metric", "month", name="uq_twin_baseline_key"),
            Index("ix_twin_baseline_metric", "metric", "month"),
        )

        id = Column(Integer, primary_key=True)
        cell_id = Column(Integer, ForeignKey("twin_cell.id"), nullable=True, index=True)
        city_id = Column(Integer, ForeignKey("twin_city.id"), nullable=True, index=True)

        metric = Column(String(48), nullable=False)   # rain_1h|rain_3h|rain_24h|aqi|temp_max
        month = Column(Integer, nullable=False)       # 1-12; 0 = all-year

        mean = Column(Float)
        stddev = Column(Float)
        p50 = Column(Float)
        p90 = Column(Float)
        p95 = Column(Float)
        p99 = Column(Float)
        maximum = Column(Float)
        sample_count = Column(Integer, default=0, nullable=False)

        source = Column(String(48))                   # open_meteo_archive|cpcb|...
        window_start = Column(UTCDateTime)
        window_end = Column(UTCDateTime)
        computed_at = Column(UTCDateTime, default=utcnow, nullable=False)

        def __repr__(self):
            return "<TwinBaseline %s m%s p95=%s>" % (self.metric, self.month, self.p95)

    class _TwinFlag(Model):
        """One candidate event the agent raised for an analyst to act on.

        The agent writes these; it never writes to the map. ``status`` is the
        human gate: nothing reaches the public alerting path until an analyst
        moves a flag out of ``pending``, which mirrors the host app's existing
        ``verification_status == 'approved'`` rule for citizen reports.
        """

        __tablename__ = "twin_flag"
        __table_args__ = (
            Index("ix_twin_flag_city_status", "city_id", "status"),
            Index("ix_twin_flag_created", "created_at"),
        )

        id = Column(Integer, primary_key=True)
        cluster_key = Column(String(128), nullable=False, index=True)
        city_id = Column(Integer, ForeignKey("twin_city.id"), nullable=False, index=True)

        hazard_type = Column(String(48))       # flood|rain|heat|air_quality|quake|other
        title = Column(String(255))
        headline_h3 = Column(String(20))       # representative cell, for the map pin
        cell_count = Column(Integer, default=0, nullable=False)

        risk_score = Column(Float)             # from twin/scoring.py -- never from an LLM
        severity = Column(String(16))          # normal|watch|warning|critical
        anomaly_sigma = Column(Float)          # how far from this area's own normal
        confidence = Column(Float)

        brief_md = Column(Text)
        citations = Column(JSON)               # [{source, title, url, fetched_at}, ...]
        evidence = Column(JSON)                # the raw numbers that justified it (C3)

        # rules  -- deterministic path, no model key configured
        # llm    -- an LLM wrote the extraction/brief
        agent_mode = Column(String(16), default="rules", nullable=False)

        status = Column(String(16), default="pending", nullable=False, index=True)
        reviewed_by = Column(Integer, nullable=True)
        reviewed_at = Column(UTCDateTime)
        review_note = Column(Text)

        created_at = Column(UTCDateTime, default=utcnow, nullable=False)
        updated_at = Column(UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False)
        expires_at = Column(UTCDateTime)

        def __repr__(self):
            return "<TwinFlag %s %s %s>" % (self.hazard_type, self.severity, self.status)

    class _TwinFlagCell(Model):
        """The cells one flag covers -- the alert's actual footprint."""

        __tablename__ = "twin_flag_cell"
        __table_args__ = (
            UniqueConstraint("flag_id", "h3_index", name="uq_twin_flag_cell"),
        )

        id = Column(Integer, primary_key=True)
        flag_id = Column(Integer, ForeignKey("twin_flag.id"), nullable=False, index=True)
        h3_index = Column(String(20), nullable=False, index=True)
        risk_score = Column(Float)

    class _TwinDispatch(Model):
        """Audit row for an alert an analyst actually sent to the public.

        Separate from the flag because one flag may be dispatched more than
        once (an escalation, a wider radius) and because "who pressed send, at
        what time, to how many people, with what words" is the record that has
        to survive an enquiry.
        """

        __tablename__ = "twin_dispatch"
        __table_args__ = (
            Index("ix_twin_dispatch_city_time", "city_id", "sent_at"),
        )

        id = Column(Integer, primary_key=True)
        flag_id = Column(Integer, ForeignKey("twin_flag.id"), nullable=True, index=True)
        city_id = Column(Integer, ForeignKey("twin_city.id"), nullable=False, index=True)

        sent_by = Column(Integer, nullable=True)       # host User.id
        sent_by_username = Column(String(128))
        channel = Column(String(32), default="in_app+whatsapp", nullable=False)

        message = Column(Text)
        radius_km = Column(Float)
        cells = Column(JSON)                           # h3 indexes the alert covered

        recipients = Column(Integer, default=0, nullable=False)
        whatsapp_sent = Column(Integer, default=0, nullable=False)
        whatsapp_failed = Column(Integer, default=0, nullable=False)

        sent_at = Column(UTCDateTime, default=utcnow, nullable=False, index=True)

        def __repr__(self):
            return "<TwinDispatch flag=%s to=%s>" % (self.flag_id, self.recipients)

    TwinCity = _TwinCity
    TwinZone = _TwinZone
    TwinCell = _TwinCell
    TwinCellState = _TwinCellState
    TwinCellHistory = _TwinCellHistory
    TwinInfrastructure = _TwinInfrastructure
    TwinDataSnapshot = _TwinDataSnapshot
    TwinExternalAlert = _TwinExternalAlert
    TwinAlertCell = _TwinAlertCell
    TwinObservation = _TwinObservation
    TwinBaseline = _TwinBaseline
    TwinFlag = _TwinFlag
    TwinFlagCell = _TwinFlagCell
    TwinDispatch = _TwinDispatch

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
        "TwinExternalAlert": TwinExternalAlert,
        "TwinAlertCell": TwinAlertCell,
        "TwinObservation": TwinObservation,
        "TwinBaseline": TwinBaseline,
        "TwinFlag": TwinFlag,
        "TwinFlagCell": TwinFlagCell,
        "TwinDispatch": TwinDispatch,
    }
