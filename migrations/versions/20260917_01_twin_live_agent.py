"""twin_live_agent -- external alerts, live observations, baselines, agent flags

Adds the seven tables the live-data and agent phases need:

* ``twin_external_alert`` / ``twin_alert_cell`` -- official CAP alerts (NDMA
  SACHET) and global event feeds, plus which H3 cells each covers.
* ``twin_observation`` -- the latest reading from each live point source
  (pollution stations, transit vehicles, water sensors).
* ``twin_baseline`` -- what "normal" is for a cell in a given month, so
  "abnormal" can be claimed with a number behind it.
* ``twin_flag`` / ``twin_flag_cell`` -- what the agent raises for an analyst.
* ``twin_dispatch`` -- the audit record of an alert actually sent to people.

Hand-written, cross-database (C2): no PostGIS, no dialect-specific types.
``down_revision`` chains onto ``twin_initial``, which is itself a root -- see
that file's docstring for why this repo's migration history has many heads and
why the application provisions its schema with ``db.create_all()``.
"""

import sqlalchemy as sa
from alembic import op

revision = "twin_live_agent"
down_revision = "twin_initial"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "twin_external_alert",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(24), nullable=False),
        sa.Column("source_uid", sa.String(128), nullable=False),
        sa.Column("cap_identifier", sa.String(128)),
        sa.Column("city_id", sa.Integer(), sa.ForeignKey("twin_city.id")),
        sa.Column("sender", sa.String(255)),
        sa.Column("event", sa.String(255)),
        sa.Column("category", sa.String(64)),
        sa.Column("severity", sa.String(24)),
        sa.Column("certainty", sa.String(24)),
        sa.Column("urgency", sa.String(24)),
        sa.Column("msg_type", sa.String(24)),
        sa.Column("headline", sa.Text()),
        sa.Column("description", sa.Text()),
        sa.Column("instruction", sa.Text()),
        sa.Column("area_desc", sa.Text()),
        sa.Column("geometry_kind", sa.String(16)),
        sa.Column("geometry_geojson", sa.Text()),
        sa.Column("effective_at", sa.DateTime(timezone=True)),
        sa.Column("onset_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("references_uid", sa.String(255)),
        sa.Column("superseded_at", sa.DateTime(timezone=True)),
        sa.Column("raw_url", sa.Text()),
        sa.Column("raw", sa.JSON()),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source", "source_uid", name="uq_twin_alert_source_uid"),
    )
    op.create_index("ix_twin_external_alert_source", "twin_external_alert", ["source"])
    op.create_index("ix_twin_external_alert_city_id", "twin_external_alert", ["city_id"])
    op.create_index("ix_twin_alert_city_expires", "twin_external_alert",
                    ["city_id", "expires_at"])
    op.create_index("ix_twin_alert_effective", "twin_external_alert", ["effective_at"])

    op.create_table(
        "twin_alert_cell",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("alert_id", sa.Integer(), sa.ForeignKey("twin_external_alert.id"),
                  nullable=False),
        sa.Column("city_id", sa.Integer(), sa.ForeignKey("twin_city.id")),
        sa.Column("h3_index", sa.String(20), nullable=False),
        sa.UniqueConstraint("alert_id", "h3_index", name="uq_twin_alert_cell"),
    )
    op.create_index("ix_twin_alert_cell_h3", "twin_alert_cell", ["h3_index"])
    op.create_index("ix_twin_alert_cell_alert_id", "twin_alert_cell", ["alert_id"])

    op.create_table(
        "twin_observation",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_key", sa.String(64), nullable=False),
        sa.Column("station_uid", sa.String(128), nullable=False),
        sa.Column("city_id", sa.Integer(), sa.ForeignKey("twin_city.id")),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("name", sa.String(255)),
        sa.Column("operator", sa.String(128)),
        sa.Column("latitude", sa.Float()),
        sa.Column("longitude", sa.Float()),
        sa.Column("h3_index", sa.String(20)),
        sa.Column("value", sa.Float()),
        sa.Column("unit", sa.String(24)),
        sa.Column("status", sa.String(24)),
        sa.Column("metrics", sa.JSON()),
        sa.Column("raw", sa.JSON()),
        sa.Column("observed_at", sa.DateTime(timezone=True)),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_key", "station_uid", name="uq_twin_obs_source_station"),
    )
    op.create_index("ix_twin_obs_city_kind", "twin_observation", ["city_id", "kind"])
    op.create_index("ix_twin_obs_observed", "twin_observation", ["observed_at"])
    op.create_index("ix_twin_observation_h3_index", "twin_observation", ["h3_index"])

    op.create_table(
        "twin_baseline",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cell_id", sa.Integer(), sa.ForeignKey("twin_cell.id")),
        sa.Column("city_id", sa.Integer(), sa.ForeignKey("twin_city.id")),
        sa.Column("metric", sa.String(48), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),
        sa.Column("mean", sa.Float()),
        sa.Column("stddev", sa.Float()),
        sa.Column("p50", sa.Float()),
        sa.Column("p90", sa.Float()),
        sa.Column("p95", sa.Float()),
        sa.Column("p99", sa.Float()),
        sa.Column("maximum", sa.Float()),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source", sa.String(48)),
        sa.Column("window_start", sa.DateTime(timezone=True)),
        sa.Column("window_end", sa.DateTime(timezone=True)),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("cell_id", "metric", "month", name="uq_twin_baseline_key"),
    )
    op.create_index("ix_twin_baseline_metric", "twin_baseline", ["metric", "month"])

    op.create_table(
        "twin_flag",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cluster_key", sa.String(128), nullable=False),
        sa.Column("city_id", sa.Integer(), sa.ForeignKey("twin_city.id"), nullable=False),
        sa.Column("hazard_type", sa.String(48)),
        sa.Column("title", sa.String(255)),
        sa.Column("headline_h3", sa.String(20)),
        sa.Column("cell_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("risk_score", sa.Float()),
        sa.Column("severity", sa.String(16)),
        sa.Column("anomaly_sigma", sa.Float()),
        sa.Column("confidence", sa.Float()),
        sa.Column("brief_md", sa.Text()),
        sa.Column("citations", sa.JSON()),
        sa.Column("evidence", sa.JSON()),
        sa.Column("agent_mode", sa.String(16), nullable=False, server_default="rules"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("reviewed_by", sa.Integer()),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("review_note", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_twin_flag_city_status", "twin_flag", ["city_id", "status"])
    op.create_index("ix_twin_flag_created", "twin_flag", ["created_at"])
    op.create_index("ix_twin_flag_cluster_key", "twin_flag", ["cluster_key"])

    op.create_table(
        "twin_flag_cell",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("flag_id", sa.Integer(), sa.ForeignKey("twin_flag.id"), nullable=False),
        sa.Column("h3_index", sa.String(20), nullable=False),
        sa.Column("risk_score", sa.Float()),
        sa.UniqueConstraint("flag_id", "h3_index", name="uq_twin_flag_cell"),
    )
    op.create_index("ix_twin_flag_cell_h3_index", "twin_flag_cell", ["h3_index"])

    op.create_table(
        "twin_dispatch",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("flag_id", sa.Integer(), sa.ForeignKey("twin_flag.id")),
        sa.Column("city_id", sa.Integer(), sa.ForeignKey("twin_city.id"), nullable=False),
        sa.Column("sent_by", sa.Integer()),
        sa.Column("sent_by_username", sa.String(128)),
        sa.Column("channel", sa.String(32), nullable=False,
                  server_default="in_app+whatsapp"),
        sa.Column("message", sa.Text()),
        sa.Column("radius_km", sa.Float()),
        sa.Column("cells", sa.JSON()),
        sa.Column("recipients", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("whatsapp_sent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("whatsapp_failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_twin_dispatch_city_time", "twin_dispatch", ["city_id", "sent_at"])


def downgrade():
    op.drop_table("twin_dispatch")
    op.drop_table("twin_flag_cell")
    op.drop_table("twin_flag")
    op.drop_table("twin_baseline")
    op.drop_table("twin_observation")
    op.drop_table("twin_alert_cell")
    op.drop_table("twin_external_alert")
