"""The triage agent: clustering, the deterministic gate, flags, and dispatch.

The three properties worth breaking a build over:

1. **A quiet city costs nothing.** Most polls find nothing, and the LLM nodes
   sit after the threshold gate, so a calm cycle must make zero model calls.
   An agent that bills for silence gets switched off, and a switched-off agent
   warns nobody.
2. **The LLM never sets a score.** Risk comes from twin/scoring.py. A model
   may rename a flag; it may not renumber one.
3. **Nothing reaches the public unaided.** Flags land as `pending`. The only
   path to a phone is an analyst's POST, behind a preview and a cooldown.
"""

from datetime import timedelta

import h3
import pytest

from twin import agent, config, dispatch
from twin import models as m
from twin.agent import llm, nodes


@pytest.fixture()
def bengaluru(db):
    return db.session.query(m.TwinCity).filter_by(slug="bengaluru").one()


@pytest.fixture(autouse=True)
def _reset_llm():
    """Every test starts in deterministic mode unless it says otherwise."""
    llm.reset()
    yield
    llm.reset()


@pytest.fixture()
def hot_cells(db, bengaluru):
    """Three adjacent cells scored well above the flag threshold."""
    created = []
    centre = h3.latlng_to_cell(12.97, 77.59, config.H3_RESOLUTION)
    # grid_disk includes the origin cell, so the neighbours have to exclude it
    # or the fixture inserts the same h3_index twice.
    neighbours = [cell for cell in h3.grid_disk(centre, 1) if cell != centre][:2]

    for index, h3_index in enumerate([centre] + neighbours):
        lat, lon = h3.cell_to_latlng(h3_index)
        cell = m.TwinCell(h3_index=h3_index, city_id=bengaluru.id,
                          center_latitude=lat, center_longitude=lon)
        db.session.add(cell)
        db.session.flush()
        db.session.add(m.TwinCellState(
            cell_id=cell.id, horizon_hours=0,
            risk_score=config.FLAG_THRESHOLD + 15 - index,
            status="warning", hydro_score=80.0,
            raw_inputs={
                "rain_now_mm_1h": 41.0, "rain_forecast_mm": 62.0,
                "dist_to_water_m": 220.0, "anomaly_score": 74.0,
                "rain_exceedance": {"level": "p95", "threshold": 30.0,
                                    "value": 62.0, "ratio": 2.07,
                                    "sample_count": 4380,
                                    "source": "open_meteo_archive"},
            }))
        created.append(h3_index)
    db.session.commit()
    return created


@pytest.fixture()
def official_alert(db, bengaluru, hot_cells):
    alert = m.TwinExternalAlert(
        source="sachet", source_uid="test-1", cap_identifier="IN-TEST-1",
        city_id=bengaluru.id, sender="IMD Bengaluru",
        event="Heavy rainfall warning", category="Met",
        severity="Severe", certainty="Observed", urgency="Immediate",
        headline="Heavy rain over Bengaluru Urban",
        instruction="Avoid low-lying areas.",
        geometry_kind="polygon", raw_url="https://sachet.example/1",
        effective_at=m.utcnow() - timedelta(minutes=20),
        expires_at=m.utcnow() + timedelta(hours=3),
        fetched_at=m.utcnow(), raw={})
    db.session.add(alert)
    db.session.flush()
    for h3_index in hot_cells:
        db.session.add(m.TwinAlertCell(alert_id=alert.id, city_id=bengaluru.id,
                                       h3_index=h3_index))
    db.session.commit()
    return alert


class TestQuietCity:
    def test_a_calm_city_raises_nothing(self, db, bengaluru):
        result = agent.run_triage(db, bengaluru)
        assert result["flagged"] == 0
        assert db.session.query(m.TwinFlag).count() == 0

    def test_a_quiet_poll_makes_no_model_calls(self, db, bengaluru, monkeypatch):
        calls = []
        monkeypatch.setattr(llm, "available", lambda: True)
        monkeypatch.setattr(llm, "complete", lambda *a, **k: calls.append(a) or "x")
        monkeypatch.setattr(llm, "complete_json", lambda *a, **k: calls.append(a) or None)

        agent.run_triage(db, bengaluru)
        assert calls == []


class TestFlagging:
    def test_hot_cells_plus_an_alert_produce_one_flag(self, db, bengaluru, hot_cells,
                                                      official_alert, monkeypatch):
        # Pin the deterministic path. `agent_mode` is derived from whether a
        # key happens to be configured, so without this the assertion tests
        # the developer's .env rather than the flagging logic -- and fails on
        # any machine that has an OPENAI_API_KEY set.
        monkeypatch.setattr(llm, "available", lambda: False)

        result = agent.run_triage(db, bengaluru)
        assert result["flags_created"] == 1

        flag = db.session.query(m.TwinFlag).one()
        assert flag.status == "pending"
        assert flag.severity in ("warning", "critical")
        assert flag.cell_count == len(hot_cells)
        assert flag.agent_mode == "rules"

    def test_the_flag_cites_the_official_alert(self, db, bengaluru, hot_cells,
                                               official_alert):
        agent.run_triage(db, bengaluru)
        flag = db.session.query(m.TwinFlag).one()

        sources = [citation["source"] for citation in flag.citations]
        assert "IMD Bengaluru" in sources
        assert any(c["url"] == "https://sachet.example/1" for c in flag.citations)

    def test_the_brief_quotes_checkable_numbers(self, db, bengaluru, hot_cells,
                                                official_alert):
        agent.run_triage(db, bengaluru)
        flag = db.session.query(m.TwinFlag).one()
        # An operator must be able to re-derive the claim, not just trust it.
        assert "p95" in flag.brief_md
        assert "62.0 mm" in flag.brief_md

    def test_the_score_comes_from_the_twin_not_the_agent(self, db, bengaluru,
                                                        hot_cells, official_alert):
        agent.run_triage(db, bengaluru)
        flag = db.session.query(m.TwinFlag).one()

        peak = max(row.risk_score for row in db.session.query(m.TwinCellState).all())
        assert flag.risk_score == pytest.approx(peak, abs=0.1)

    def test_rerunning_updates_rather_than_duplicating(self, db, bengaluru,
                                                       hot_cells, official_alert):
        agent.run_triage(db, bengaluru)
        second = agent.run_triage(db, bengaluru)

        assert second["flags_created"] == 0
        assert second["flags_updated"] == 1
        assert db.session.query(m.TwinFlag).count() == 1

    def test_a_dismissed_flag_is_not_resurrected(self, db, bengaluru, hot_cells,
                                                 official_alert):
        agent.run_triage(db, bengaluru)
        flag = db.session.query(m.TwinFlag).one()
        flag.status = "rejected"
        db.session.commit()

        agent.run_triage(db, bengaluru)
        # Re-raising what an analyst just dismissed, every ten minutes, is how
        # an operator learns to ignore the queue entirely.
        assert db.session.query(m.TwinFlag).count() == 1
        assert db.session.query(m.TwinFlag).one().status == "rejected"

    def test_an_extreme_alert_is_flagged_even_with_calm_cells(self, db, bengaluru):
        db.session.add(m.TwinExternalAlert(
            source="sachet", source_uid="extreme-1", city_id=bengaluru.id,
            sender="IMD", event="Red warning: extremely heavy rainfall",
            severity="Extreme", certainty="Likely", urgency="Immediate",
            geometry_kind="district", area_desc="23 districts of Karnataka",
            expires_at=m.utcnow() + timedelta(hours=6),
            fetched_at=m.utcnow(), raw={}))
        db.session.commit()

        agent.run_triage(db, bengaluru)
        flag = db.session.query(m.TwinFlag).one()
        # The twin models rain and terrain; it does not model everything IMD
        # warns about, so an Extreme warning must never be silenced by calm
        # hexagons.
        assert flag.severity == "critical"
        assert flag.cell_count == 0


class TestLlmMode:
    def test_the_model_may_rename_a_flag_but_not_rescore_it(self, db, bengaluru,
                                                           hot_cells, official_alert,
                                                           monkeypatch):
        monkeypatch.setattr(llm, "available", lambda: True)
        monkeypatch.setattr(llm, "mode", lambda: "llm")
        monkeypatch.setattr(llm, "complete", lambda system, user, fallback=None:
                            "Heavy rain is flooding low-lying roads. Risk score 3.")
        monkeypatch.setattr(llm, "complete_json", lambda system, user, fallback=None: None)

        agent.run_triage(db, bengaluru)
        flag = db.session.query(m.TwinFlag).one()

        peak = max(row.risk_score for row in db.session.query(m.TwinCellState).all())
        assert flag.risk_score == pytest.approx(peak, abs=0.1)
        assert flag.agent_mode == "llm"

    def test_malformed_model_json_falls_back_silently(self, db, bengaluru,
                                                      hot_cells, official_alert,
                                                      monkeypatch):
        monkeypatch.setattr(llm, "available", lambda: True)
        monkeypatch.setattr(llm, "complete_json",
                            lambda system, user, fallback=None: fallback)
        monkeypatch.setattr(llm, "complete", lambda system, user, fallback=None: fallback)

        result = agent.run_triage(db, bengaluru)
        assert result["flags_created"] == 1
        assert db.session.query(m.TwinFlag).one().brief_md


class TestJsonParsing:
    def test_fenced_json_is_recovered(self):
        parsed = llm._parse_json_loosely('```json\n{"items": [1, 2]}\n```')
        assert parsed == {"items": [1, 2]}

    def test_prose_around_json_is_stripped(self):
        parsed = llm._parse_json_loosely('Sure! Here it is: {"a": 1} Hope that helps.')
        assert parsed == {"a": 1}

    def test_a_trailing_comma_is_repaired(self):
        assert llm._parse_json_loosely('{"a": 1,}') == {"a": 1}

    def test_unrecoverable_output_returns_none(self):
        assert llm._parse_json_loosely("I cannot help with that.") is None


class TestDispatch:
    @pytest.fixture()
    def flag(self, db, bengaluru, hot_cells, official_alert):
        agent.run_triage(db, bengaluru)
        return db.session.query(m.TwinFlag).one()

    @pytest.fixture()
    def channel(self):
        sent = {"notified": [], "whatsapp": []}

        dispatch.register_alert_channel(
            recipients_near=lambda lat, lon, radius_km: [
                {"id": 1, "username": "asha", "distance_km": 0.4,
                 "whatsapp_number": "+910000000001"},
                {"id": 2, "username": "ravi", "distance_km": 1.9,
                 "whatsapp_number": None},
            ],
            notify=lambda user_id, message: sent["notified"].append((user_id, message)),
            send_whatsapp=lambda number, body: sent["whatsapp"].append((number, body)),
        )
        yield sent
        dispatch._channel.clear()

    def test_preview_counts_before_anything_is_sent(self, db, flag, channel):
        preview = dispatch.preview(db, flag)
        assert preview["recipients"] == 2
        assert preview["whatsapp_reachable"] == 1
        assert channel["notified"] == []   # nothing sent by a preview

    def test_the_message_names_the_authority_and_what_to_do(self, db, flag, channel):
        message = dispatch.build_message(flag)
        assert "IMD Bengaluru" in message
        assert "Avoid low-lying areas." in message

    def test_sending_records_an_audit_row(self, db, flag, channel):
        result = dispatch.send(db, flag, sent_by=7, sent_by_username="analyst")
        assert result["success"] is True
        assert result["recipients"] == 2
        assert len(channel["whatsapp"]) == 1

        record = db.session.query(m.TwinDispatch).one()
        assert record.sent_by_username == "analyst"
        assert record.recipients == 2
        assert record.cells

    def test_a_dispatched_flag_leaves_the_queue(self, db, flag, channel):
        dispatch.send(db, flag, sent_by=7, sent_by_username="analyst")
        assert db.session.query(m.TwinFlag).one().status == "dispatched"

    def test_the_cooldown_blocks_an_immediate_resend(self, db, flag, channel):
        dispatch.send(db, flag, sent_by=7, sent_by_username="analyst")
        again = dispatch.send(db, flag, sent_by=7, sent_by_username="analyst")

        assert again["success"] is False
        assert again["cooldown_active"] is True
        assert len(channel["whatsapp"]) == 1

    def test_force_overrides_the_cooldown(self, db, flag, channel):
        dispatch.send(db, flag, sent_by=7, sent_by_username="analyst")
        again = dispatch.send(db, flag, sent_by=7, sent_by_username="analyst", force=True)
        assert again["success"] is True

    def test_one_failing_recipient_does_not_abort_the_rest(self, db, flag):
        def explode(number, body):
            raise RuntimeError("carrier rejected the message")

        dispatch.register_alert_channel(
            recipients_near=lambda lat, lon, radius: [
                {"id": 1, "username": "a", "distance_km": 1.0,
                 "whatsapp_number": "+911"},
                {"id": 2, "username": "b", "distance_km": 1.0,
                 "whatsapp_number": "+912"},
            ],
            notify=lambda user_id, message: None,
            send_whatsapp=explode)
        try:
            result = dispatch.send(db, flag, sent_by=1, sent_by_username="x")
            assert result["success"] is True
            assert result["whatsapp_failed"] == 2
            assert result["recipients"] == 2
        finally:
            dispatch._channel.clear()

    def test_dispatch_is_unavailable_rather_than_half_working(self, db, flag):
        dispatch._channel.clear()
        assert dispatch.preview(db, flag)["available"] is False
        assert dispatch.send(db, flag)["success"] is False


class TestFlagRoutes:
    @pytest.fixture()
    def flag(self, db, bengaluru, hot_cells, official_alert):
        agent.run_triage(db, bengaluru)
        return db.session.query(m.TwinFlag).one()

    def test_flags_are_listed_for_an_analyst(self, analyst, flag):
        body = analyst.get("/api/twin/bengaluru/flags").get_json()
        assert body["pending"] == 1
        assert body["flags"][0]["title"]

    def test_geometry_is_optional(self, analyst, flag):
        body = analyst.get("/api/twin/bengaluru/flags?geometry=true").get_json()
        assert body["geojson"]["features"]

    def test_an_anonymous_caller_gets_nothing(self, anon):
        assert anon.get("/api/twin/bengaluru/flags").status_code in (401, 403)

    def test_review_moves_a_flag_out_of_pending(self, analyst, db, flag):
        response = analyst.post("/api/twin/flags/%d/review" % flag.id,
                                json={"decision": "reject", "note": "known roadworks"})
        assert response.status_code == 200
        assert db.session.query(m.TwinFlag).get(flag.id).status == "rejected"

    def test_review_rejects_an_unknown_decision(self, analyst, flag):
        response = analyst.post("/api/twin/flags/%d/review" % flag.id,
                                json={"decision": "maybe"})
        assert response.status_code == 400

    def test_dispatch_requires_an_official_not_just_an_analyst(self, analyst, flag):
        # Reviewing is an analyst's job; reaching the public is not.
        response = analyst.post("/api/twin/flags/%d/dispatch" % flag.id, json={})
        assert response.status_code in (401, 403)

    def test_agent_status_reports_the_mode_honestly(self, analyst):
        body = analyst.get("/api/twin/agent/status").get_json()
        assert body["llm"]["mode"] in ("rules", "llm")
        assert "dispatch" in body
