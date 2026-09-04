"""Report-approval -> SSE hook (Phase 7 checkpoint):
"approve a report in one browser tab -> the hex lights up ... within 5s in
another tab." This tests the plumbing (the SQLAlchemy after_update listener
firing exactly on the pending->approved transition) without a browser.
"""

import pytest

from dev_app import Report
from twin.ingest import internal_reports


# No teardown needed: register_approval_hook() reconfigures which callback
# the one installed SQLAlchemy listener dispatches to (see
# twin/ingest/internal_reports.py), so each test's own call simply replaces
# the previous test's callback rather than leaking a stacked listener.


class TestApprovalHook:
    def test_fires_on_pending_to_approved_transition(self, app, db):
        received = []
        internal_reports.register_approval_hook(db, on_approved=received.append)

        with app.app_context():
            report = Report(title="Flooded underpass", hazard_type="flood",
                            priority="high", latitude=17.4, longitude=78.5,
                            verification_status="pending", confidence_score=0.9)
            db.session.add(report)
            db.session.commit()

            report.verification_status = "approved"
            db.session.commit()

        assert len(received) == 1
        assert received[0]["hazard_type"] == "flood"
        assert received[0]["priority"] == "high"

    def test_does_not_fire_on_unrelated_field_updates(self, app, db):
        received = []
        internal_reports.register_approval_hook(db, on_approved=received.append)

        with app.app_context():
            report = Report(title="X", hazard_type="fire", priority="low",
                            latitude=1.0, longitude=1.0, verification_status="approved")
            db.session.add(report)
            db.session.commit()
            received.clear()  # the insert-then-commit above is not an update

            report.title = "Renamed"
            db.session.commit()

        assert received == []

    def test_redundant_resave_of_the_same_value_may_refire_harmlessly(self, app, db):
        """SQLAlchemy's attribute history only retains the prior value when
        it differs from the new one (confirmed empirically: history.deleted
        is empty for a same-value reassignment even at flush time), so a
        genuine no-op re-save of "approved" -> "approved" cannot be
        perfectly suppressed. That is an acceptable failure direction for
        an incident-detection hook: an extra notification is harmless,
        unlike a suppressed real approval would be."""
        received = []
        internal_reports.register_approval_hook(db, on_approved=received.append)

        with app.app_context():
            report = Report(title="X", hazard_type="fire", priority="low",
                            latitude=1.0, longitude=1.0, verification_status="approved")
            db.session.add(report)
            db.session.commit()
            received.clear()

            report.verification_status = "approved"  # no-op reassignment
            db.session.commit()

        assert len(received) <= 1  # documented over-fire, never a crash or a missed transition

    def test_does_not_fire_on_rejection(self, app, db):
        received = []
        internal_reports.register_approval_hook(db, on_approved=received.append)

        with app.app_context():
            report = Report(title="X", hazard_type="fire", priority="low",
                            latitude=1.0, longitude=1.0, verification_status="pending")
            db.session.add(report)
            db.session.commit()

            report.verification_status = "rejected"
            db.session.commit()

        assert received == []

    def test_a_broken_listener_does_not_break_the_save(self, app, db):
        def boom(_report_dict):
            raise RuntimeError("listener bug")

        internal_reports.register_approval_hook(db, on_approved=boom)

        with app.app_context():
            report = Report(title="X", hazard_type="fire", priority="low",
                            latitude=1.0, longitude=1.0, verification_status="pending")
            db.session.add(report)
            db.session.commit()

            report.verification_status = "approved"
            db.session.commit()  # must not raise despite the listener blowing up

            assert db.session.get(Report, report.id).verification_status == "approved"

    def test_publishes_to_the_sse_stream_end_to_end(self, app, db):
        from twin import stream as twin_stream

        twin_stream._subscribers.clear()
        sub_id = twin_stream.subscribe(city=None)
        internal_reports.register_approval_hook(
            db, on_approved=lambda r: twin_stream.publish("incident", city=None, report=r))

        with app.app_context():
            report = Report(title="X", hazard_type="flood", priority="critical",
                            latitude=1.0, longitude=1.0, verification_status="pending")
            db.session.add(report)
            db.session.commit()
            report.verification_status = "approved"
            db.session.commit()

        _, _, q = twin_stream._subscribers[sub_id]
        event = q.get_nowait()
        assert event["type"] == "incident"
        assert event["report"]["hazard_type"] == "flood"
