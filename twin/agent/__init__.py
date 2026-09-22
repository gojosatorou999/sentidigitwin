"""The twin's triage agent: LangChain/LangGraph over the live city picture.

One entry point, ``run_triage(db, city)``, called by the scheduler
(``twin/jobs.py``) and by ``POST /api/twin/agent/run``. It reads what the twin
already knows -- official alerts in force, cells the deterministic scorer has
pushed near the threshold, stalled transit, station readings -- groups that
into events, scores them with ``twin/scoring.py``, and writes any that clear
the bar into ``twin_flag`` for an analyst to act on.

Three properties hold whatever is installed or configured:

1. **The LLM never computes risk.** Scores come from the same pure functions
   the map is drawn from, so "why was this flagged" always has a numeric
   answer an official can repeat in an enquiry.
2. **No key, no problem.** Without a model the agent still flags, using the
   CAP documents' own structured fields and templated briefs. The flag records
   which mode produced it.
3. **No flag reaches the public by itself.** Everything lands as ``pending``.
   The dispatch path (``twin/dispatch.py``) is only reachable through an
   analyst's click.
"""

from . import forecast_graph, forecast_nodes, graph, llm, nodes, rag

__all__ = ["run_triage", "run_forecast", "graph", "forecast_graph", "llm",
           "nodes", "forecast_nodes", "rag", "describe"]


def run_triage(db, city):
    """One triage pass for one city. Never raises."""
    return graph.run(db, city)


def run_forecast(db, city):
    """One forecast pass for one city -- what is coming, not what is here.

    Same flag table, same pending gate, same console queue. The two agents
    are separate graphs because they read different inputs on different
    cadences; see twin/agent/forecast_graph.py.
    """
    return forecast_graph.run(db, city)


def describe():
    """Agent status for the console's health panel."""
    return {
        "llm": llm.describe(),
        "rag": rag.describe(),
        "engine": "langgraph" if graph.langgraph_available() else "sequential",
        "forecast": {
            "engine": ("langgraph" if forecast_graph.langgraph_available()
                       else "sequential"),
            "max_lead_hours": forecast_nodes.fx.MAX_LEAD_HOURS,
            "note": ("Projects rain arrivals along the wind vector and reads "
                     "heat crossings in place. Every number comes from "
                     "twin/forecast.py; the model only writes the brief."),
        },
    }
