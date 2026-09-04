"""Standalone development host for the twin module.

The real host is Sentinel AI's ``app.py``. This file stands in for it so the
twin can be migrated, run, and tested in isolation -- and so the Phase 0
checkpoint is verifiable before the module is dropped into that repo.

It deliberately mimics only the three things the twin needs from the host:
a Flask app, a Flask-SQLAlchemy ``db``, and Flask-Login with a user that has
a ``role`` attribute.

    flask --app dev_app db upgrade
    flask --app dev_app run

Integrating into the real Sentinel AI app is the last twenty lines of this
file, and nothing else.
"""

import os
from datetime import datetime, timezone

# Must happen before `twin.config` (or anything importing it) loads, since
# it reads TOMTOM_API_KEY etc. from the environment at import time. `flask
# run` auto-loads .env via python-dotenv; a direct `python dev_app.py` does
# not, so this is explicit rather than relying on which entry point is used.
from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from flask import Flask, jsonify, redirect, request, session, url_for
from flask_login import (LoginManager, UserMixin, current_user, login_user,
                         logout_user)
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
login_manager = LoginManager()
migrate = Migrate()


class Report(db.Model):
    """Stand-in for Sentinel AI's real `Report` model (README section 4.1:
    approved reports where `verification_status='approved'`). Only the
    columns twin.ingest.internal_reports.register_report_model's defaults
    expect -- this is intentionally minimal, not a Report clone.
    """

    __tablename__ = "report"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255))
    hazard_type = db.Column(db.String(64))
    priority = db.Column(db.String(16), default="medium")
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    verification_status = db.Column(db.String(16), default="pending")
    confidence_score = db.Column(db.Float)
    image_url = db.Column(db.String(512))
    created_at = db.Column(db.DateTime(timezone=True),
                           default=lambda: datetime.now(timezone.utc))


class DevUser(UserMixin):
    """Stand-in for the host app's User model."""

    _ROLES = {
        "official": "official",
        "analyst": "analyst",
        "citizen": "citizen",
        "volunteer": "volunteer",
    }

    def __init__(self, role):
        self.id = role
        self.role = role
        self.username = "dev-%s" % role


@login_manager.user_loader
def load_user(user_id):
    if user_id in DevUser._ROLES:
        return DevUser(user_id)
    return None


def create_app(database_uri=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.getenv("SECRET_KEY", "dev-only-not-a-secret"),
        SQLALCHEMY_DATABASE_URI=(
            database_uri
            or os.getenv("DATABASE_URL")
            or "sqlite:///" + os.path.join(app.instance_path, "sentinel_dev.db")
        ),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        # SQLite's default busy behaviour is to fail a write instantly if
        # another connection holds the lock, instead of waiting -- which a
        # multi-request Flask dev server (or a seed script running
        # alongside it) hits routinely. 15s matches the intent of C1: a
        # transient lock should not surface as a request failure. This has
        # no effect on PostgreSQL (C2: connect_args is dialect-specific, and
        # the 'timeout' kwarg is silently ignored by psycopg2's connect()).
        SQLALCHEMY_ENGINE_OPTIONS=(
            {"connect_args": {"timeout": 15}}
            if (database_uri or os.getenv("DATABASE_URL", "")).startswith("sqlite")
            or not (database_uri or os.getenv("DATABASE_URL"))
            else {}
        ),
    )
    os.makedirs(app.instance_path, exist_ok=True)

    db.init_app(app)
    login_manager.init_app(app)
    migrate.init_app(app, db, directory="migrations")

    # ---- dev-only auth shortcuts -----------------------------------------
    @app.get("/login")
    def login():
        role = request.args.get("role", "official")
        if role not in DevUser._ROLES:
            return jsonify({"error": "unknown role", "roles": list(DevUser._ROLES)}), 400
        login_user(DevUser(role))
        return jsonify({"logged_in_as": role})

    @app.get("/logout")
    def logout():
        logout_user()
        return jsonify({"logged_out": True})

    @app.get("/whoami")
    def whoami():
        if not current_user.is_authenticated:
            return jsonify({"authenticated": False})
        return jsonify({"authenticated": True, "role": current_user.role})

    @app.get("/")
    def index():
        return jsonify({
            "app": "sentinel-dev-host",
            "twin": app.extensions.get("twin", {}).get("version"),
            "hint": "GET /login?role=official then GET /api/twin/cities",
        })

    # ---- dev-only Report submit/approve, to exercise the incident layer --
    @app.post("/reports")
    def submit_report():
        body = request.get_json(force=True)
        report = Report(
            title=body.get("title", "Untitled"), hazard_type=body.get("hazard_type", "flood"),
            priority=body.get("priority", "medium"), latitude=body["latitude"],
            longitude=body["longitude"], confidence_score=body.get("confidence_score"),
            verification_status="pending",
        )
        db.session.add(report)
        db.session.commit()
        return jsonify({"id": report.id}), 201

    @app.post("/reports/<int:report_id>/approve")
    def approve_report(report_id):
        report = db.session.get(Report, report_id)
        if report is None:
            return jsonify({"error": "not found"}), 404
        report.verification_status = "approved"
        db.session.commit()  # fires the twin's after_update SSE hook
        return jsonify({"id": report.id, "status": "approved"})

    # ---- the only lines that matter for the real app.py ------------------
    from twin import create_twin_blueprint
    from twin.ingest.internal_reports import register_approval_hook, register_report_model

    scheduler = None
    if os.getenv("TWIN_DEV_SCHEDULER", "0") == "1":
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler(daemon=True)

    create_twin_blueprint(app, db, scheduler=scheduler)
    register_report_model(Report)  # default attribute names match Report above

    with app.app_context():
        # dev-only convenience: Report is not part of the twin's own
        # migration (twin_initial) -- it stands in for a table the real
        # host app already owns, so it is created directly rather than
        # via Alembic, exactly like TWIN_INTEGRATION.md excludes dev_app.py
        # itself from the host copy.
        Report.__table__.create(db.engine, checkfirst=True)

        from twin import stream as twin_stream
        register_approval_hook(
            db, on_approved=lambda report_dict: twin_stream.publish(
                "incident", city=None, report=report_dict))

    if scheduler is not None:
        scheduler.start()
    # ----------------------------------------------------------------------

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True, port=int(os.getenv("PORT", "5000")), threaded=True)
