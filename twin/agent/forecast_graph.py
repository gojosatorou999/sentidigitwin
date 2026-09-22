"""The forecast DAG, on LangGraph when available and in sequence when not.

Deliberately a sibling of ``graph.py`` rather than a branch inside it. The two
graphs answer different questions from different inputs on different cadences
-- triage reads what is already true, forecast reads what is projected -- and
folding them together would put a conditional in every node asking which mode
it was in. They share what is genuinely shared: the flag table, the pending
gate, the RAG index and the LLM client.
"""

import logging

from . import forecast_nodes as fn

log = logging.getLogger("twin.agent.forecast_graph")

#: Node order for the fallback path, and the documentation of the DAG.
SEQUENCE = (
    ("sample", fn.sample),
    ("advect", fn.advect),
    ("detect", fn.detect),
    ("threshold", fn.threshold),
    ("retrieve", fn.retrieve),
    ("draft_brief", fn.draft_brief),
    ("persist", fn.persist),
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

    class ForecastState(TypedDict, total=False):
        db: Any
        city: Any
        points: list
        field_status: str
        lattice_size: int
        arrivals: list
        heat: list
        candidates: list
        flagged: list
        flag_count: int
        context: dict
        briefs: list
        flags_created: int
        flags_updated: int
        errors: Annotated[list, operator.add]

    graph = StateGraph(ForecastState)
    for name, function in SEQUENCE:
        graph.add_node(name, _guarded(name, function))

    graph.add_edge(START, "sample")
    graph.add_edge("sample", "advect")
    graph.add_edge("advect", "detect")
    graph.add_edge("detect", "threshold")

    # The token-saving edge: a calm forecast never reaches an LLM.
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
    """One failing node degrades the pass instead of killing it."""
    def run(state):
        try:
            return function(state)
        except Exception as exc:  # noqa: BLE001
            log.exception("forecast node %s failed", name)
            return {"errors": ["%s: %s" % (name, exc)]}
    return run


def run(db, city):
    """One forecast pass for one city. Never raises."""
    state = {"db": db, "city": city, "errors": []}

    graph = build_graph()
    if graph is not None:
        try:
            return _summarise(graph.invoke(state), engine="langgraph")
        except Exception as exc:  # noqa: BLE001 - fall back rather than lose the pass
            log.exception("forecast graph invoke failed; running in sequence")
            state["errors"].append("langgraph: %s" % exc)

    for name, function in SEQUENCE:
        if name in _AFTER_THRESHOLD and not state.get("flagged"):
            continue
        state.update(_guarded(name, function)(state) or {})
    return _summarise(state, engine="sequence")


def _summarise(state, engine):
    return {
        "engine": engine,
        "lattice_size": state.get("lattice_size") or 0,
        "field_status": state.get("field_status") or "unknown",
        "arrivals": len(state.get("arrivals") or []),
        "heat_crossings": len(state.get("heat") or []),
        "candidates": len(state.get("candidates") or []),
        "flagged": len(state.get("flagged") or []),
        "flags_created": state.get("flags_created") or 0,
        "flags_updated": state.get("flags_updated") or 0,
        "errors": state.get("errors") or [],
    }
