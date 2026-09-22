"""Writing flags to the database — the one path both agents share.

The triage graph and the forecast graph produce different briefs from
different evidence, but they file them identically: same table, same
`pending` gate, same "a dismissed flag must not come back" rule, same cell
footprint. That was duplicated in both node modules, and the two copies had
already drifted -- one replaced a flag's cells by diffing them, the other by
deleting every row and re-adding, which churns primary keys and any audit
trail hanging off them for no reason.

One copy, so a change to the human gate is a change in one place.
"""

import logging
from datetime import timedelta

from .. import config
from .. import models as m

log = logging.getLogger("twin.agent.flagstore")

#: A flag in one of these states has already been decided by a person.
#: Re-raising it every ten minutes for the same storm is how an operator
#: learns to stop reading the queue.
SETTLED = ("rejected", "dispatched")


def upsert_flags(db, city, briefs):
    """File each brief as a pending flag. Returns ``{created, updated}``.

    ``brief`` keys: ``key``, ``hazard_type``, ``title``, ``headline_h3``,
    ``cells``, ``severity``, ``brief_md``, ``citations``, ``evidence``,
    ``agent_mode``, and optionally ``confidence``, ``peak_risk`` and
    ``anomaly``. The optional three are what the triage agent has and the
    forecast agent does not: a projection has no measured risk score yet, and
    saying so by omission is better than writing a zero that reads as "no
    risk" on the console.
    """
    created = updated = 0

    for brief in briefs or []:
        row = (db.session.query(m.TwinFlag)
               .filter_by(city_id=city.id, cluster_key=brief["key"])
               .order_by(m.TwinFlag.created_at.desc())
               .first())

        if row is not None and row.status in SETTLED:
            continue

        if row is None:
            row = m.TwinFlag(city_id=city.id, cluster_key=brief["key"],
                             status="pending")
            db.session.add(row)
            created += 1
        else:
            updated += 1

        cells = brief.get("cells") or []
        row.hazard_type = brief.get("hazard_type")
        row.title = brief.get("title")
        row.headline_h3 = brief.get("headline_h3")
        row.cell_count = len(cells)
        row.severity = brief.get("severity")
        row.brief_md = brief.get("brief_md")
        row.citations = brief.get("citations")
        row.evidence = brief.get("evidence")
        row.agent_mode = brief.get("agent_mode") or "rules"
        row.expires_at = m.utcnow() + ttl()

        # Only overwrite when the producing agent actually measured it.
        if brief.get("confidence") is not None:
            row.confidence = brief["confidence"]
        if brief.get("peak_risk") is not None:
            row.risk_score = brief["peak_risk"]
        if brief.get("anomaly") is not None:
            row.anomaly_sigma = brief["anomaly"]

        db.session.flush()
        replace_cells(db, row, cells)

    db.session.commit()
    return {"flags_created": created, "flags_updated": updated}


def ttl():
    return timedelta(hours=config.FLAG_TTL_HOURS)


def replace_cells(db, row, cells):
    """Make the flag's footprint exactly ``cells``, by difference.

    Diffed rather than delete-all-and-reinsert: a storm that shifts by one
    cell between passes would otherwise churn every row in its footprint,
    taking their ids with it.
    """
    wanted = set(cells)
    existing = {link.h3_index: link for link in
                db.session.query(m.TwinFlagCell).filter_by(flag_id=row.id).all()}

    for h3_index in wanted - set(existing):
        db.session.add(m.TwinFlagCell(flag_id=row.id, h3_index=h3_index))
    for h3_index, link in existing.items():
        if h3_index not in wanted:
            db.session.delete(link)
