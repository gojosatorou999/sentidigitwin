"""The triage graph: LangGraph when it is installed, a plain sequence when not.

The pipeline is a DAG with one conditional edge::

    gather -> extract -> correlate -> score -> threshold
                                                  |
                                    nothing flagged -> END
                                                  |
                                           retrieve -> draft_brief -> persist

That conditional edge is the important one. Most polls of a calm city find
nothing, and both LLM nodes (``draft_brief``, and the optional model calls
inside ``extract``/``correlate``) sit *after* it -- so a quiet cycle costs zero
tokens. An agent that bills for silence gets switched off, and an agent that is
switched off cannot warn anyone.

Why two execution paths: the twin's standing constraint is that it boots and
runs with no optional dependency installed (C5). langgraph is optional, so the
same node functions are run in sequence when it is absent. The nodes take a
state dict and return the keys they changed -- LangGraph's own contract -- so
neither path needs code the other does not.

The human gate is deliberately **not** a ``langgraph.interrupt``. A flag waits
in ``twin_flag.status='pending'`` in the database, because an analyst may take
hours, may never come, and the server restarts nightly -- a paused in-memory
graph run is the wrong shape for a queue that has to survive all three.
"""

import logging

from . import nodes

log = logging.getLogger("twin.agent.graph")

#: Node order for the fallback path, and the documentation of the DAG.
SEQUENCE = (
    ("gather", nodes.gather),
    ("extract", nodes.extract),
    ("correlate", nodes.correlate),
    ("score", nodes.score),
    ("threshold", nodes.threshold),
    ("retrieve", nodes.retrieve),
    ("draft_brief", nodes.draft_brief),
    ("persist", nodes.persist),
)

#: Everything from here on only runs when something cleared the threshold.
_AFTER_THRESHOLD = {"retrieve", "draft_brief", "persist"}


def langgraph_available():
    try:
        import langgraph.graph  # noqa: F401
        return True
    except Exception:  # noqa: BLE001 - ImportError, or a broken install
        return False


def build_graph():
    """A compiled LangGraph StateGraph, or None if langgraph is not installed."""
    if not langgraph_available():
        return None

    from typing import Annotated, Any, TypedDict
    import operator

    from langgraph.graph import END, START, StateGraph

    class TriageState(TypedDict, total=False):
        # Handles, not data: the nodes query the database directly rather than
        # dragging a city's worth of rows through the graph state.
        db: Any
        city: Any
        signals: list
        alerts: list
        hot_cells: list
        disruption: dict
        extracted: list
        clusters: list
        scored: list
        flagged: list
        context: dict
        briefs: list
        flags_created: int
        flags_updated: int
        errors: Annotated[list, operator.add]

    graph = StateGraph(TriageState)
    for name, function in SEQUENCE:
        graph.add_node(name, _guarded(name, function))

    graph.add_edge(START, "gather")
    graph.add_edge("gather", "extract")
    graph.add_edge("extract", "correlate")
    graph.add_edge("correlate", "score")
    graph.add_edge("score", "threshold")

    # The token-saving edge: skip everything downstream when nothing qualified.
    graph.add_conditional_edges(
        "threshold",
        lambda state: "retrieve" if state.get("flagged") else END,
        {"retrieve": "retrieve", END: END},
    )
    graph.add_edge("retrieve", "draft_brief")
    graph.add_edge("draft_brief", "persist")
    graph.add_edge("persist", END)

    return graph.compile()


def _guarded(name, function):
    """Wrap a node so one failure degrades the run instead of killing it.

    The agent runs unattended on a scheduler. A node that raises must not take
    the whole pass down -- losing the brief on one cluster is recoverable,
    losing every flag in the city because a single alert had a null field is
    not.
    """
    def run(state):
        try:
            return function(state)
        except Exception as exc:  # noqa: BLE001
            log.exception("twin agent node %s failed", name)
            return {"errors": ["%s: %s" % (name, exc)]}
    return run


def run(db, city):
    """Run one triage pass for one city. Never raises.

    Returns a summary dict: what was gathered, what was flagged, which engine
    ran it, and any node errors.
    """
    state = {"db": db, "city": city, "errors": []}

    graph = build_graph()
    if graph is not None:
        try:
            final = graph.invoke(state)
            return _summarise(final, engine="langgraph")
        except Exception:  # noqa: BLE001
            log.exception("langgraph run failed for %s; falling back to sequential",
                          city.slug)

    return _summarise(_run_sequential(state), engine="sequential")


def _run_sequential(state):
    """The same nodes, in order, with the same short-circuit."""
    for name, function in SEQUENCE:
        if name in _AFTER_THRESHOLD and not state.get("flagged"):
            continue
        try:
            update = function(state) or {}
        except Exception as exc:  # noqa: BLE001
            log.exception("twin agent node %s failed", name)
            state.setdefault("errors", []).append("%s: %s" % (name, exc))
            continue
        state.update(update)
    return state


def _summarise(state, engine):
    from . import llm

    return {
        "engine": engine,
        "mode": llm.mode(),
        "signals": len(state.get("signals") or []),
        "clusters": len(state.get("clusters") or []),
        "flagged": len(state.get("flagged") or []),
        "flags_created": state.get("flags_created", 0),
        "flags_updated": state.get("flags_updated", 0),
        "errors": state.get("errors") or [],
    }
