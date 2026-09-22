"""One OpenAI client, and a working path with no key at all.

The model is OpenAI's cheapest, gpt-5-nano, at minimal reasoning effort::

    OPENAI_API_KEY=sk-...
    TWIN_LLM_MODEL=gpt-5-nano

**With no key configured the agent still runs.** That is not a courtesy; it is
the property that keeps this deployable. Extraction falls back to the CAP
document's own structured fields (which are already machine-readable),
correlation falls back to spatial clustering, and briefs fall back to
templates built from the same numbers an LLM would have been handed. The
console shows which mode produced each flag, so nobody mistakes one for the
other.

Two defensive habits, both earned:

* **Never trust the JSON.** Hosted models differ wildly in how well they
  honour a schema -- some emit fenced code blocks, some prepend prose, some
  emit trailing commas. Every structured call is parsed leniently and falls
  back to the deterministic path rather than raising.
* **Never let the model produce a number that matters.** Coordinates, risk
  scores and severities come from ``twin/scoring.py`` and the feeds. The model
  writes prose and groups items; it does not do arithmetic, and it is never
  asked to.
"""

import json
import logging
import re

from .. import config

log = logging.getLogger("twin.agent.llm")

#: Cached client, so a triage run does not rebuild it per node.
_CLIENT = None
_CLIENT_BUILT = False


def available():
    """Is an LLM configured *and* importable?"""
    return client() is not None


def mode():
    """'llm' or 'rules' -- recorded on every flag so the UI can say which."""
    return "llm" if available() else "rules"


def client():
    """A langchain-openai ChatOpenAI for the configured OpenAI model.

    Returns None when no key is configured or the dependency is missing --
    both are ordinary, supported states, not errors.
    """
    global _CLIENT, _CLIENT_BUILT
    if _CLIENT_BUILT:
        return _CLIENT

    _CLIENT_BUILT = True
    if not config.LLM_API_KEY:
        log.info("twin agent: no OPENAI_API_KEY set; running in deterministic mode")
        return None

    try:
        from langchain_openai import ChatOpenAI
    except ImportError:
        log.warning("twin agent: langchain-openai is not installed; "
                    "running in deterministic mode")
        return None

    kwargs = {
        "model": config.LLM_MODEL,
        "api_key": config.LLM_API_KEY,
        "max_tokens": config.LLM_MAX_TOKENS,
        "timeout": config.LLM_TIMEOUT_S,
    }
    if config.LLM_REASONING_EFFORT:
        kwargs["reasoning_effort"] = config.LLM_REASONING_EFFORT

    try:
        _CLIENT = ChatOpenAI(**kwargs)
    except Exception as exc:  # noqa: BLE001 - bad config must not break the twin
        log.warning("twin agent: could not build the LLM client (%s); "
                    "running in deterministic mode", exc)
        _CLIENT = None
    return _CLIENT


def reset():
    """Drop the cached client. Used by tests and after a config change."""
    global _CLIENT, _CLIENT_BUILT
    _CLIENT = None
    _CLIENT_BUILT = False


def complete(system, user, fallback=None):
    """One chat completion, or `fallback` if anything at all goes wrong.

    Deliberately swallows every exception: the agent runs unattended on a
    scheduler, and a provider outage, a rate limit or an expired key must cost
    the flag its prose, never the flag itself.
    """
    model = client()
    if model is None:
        return fallback

    try:
        response = model.invoke([("system", system), ("human", user)])
        text = getattr(response, "content", None)
        if isinstance(text, list):
            # Some providers return content parts rather than a plain string.
            text = "".join(part.get("text", "") for part in text
                           if isinstance(part, dict))
        return (text or "").strip() or fallback
    except Exception as exc:  # noqa: BLE001
        log.warning("twin agent: LLM call failed (%s); using deterministic output", exc)
        return fallback


def complete_json(system, user, fallback=None):
    """A chat completion parsed as JSON, or `fallback`.

    The prompt asks for bare JSON, but asking is not getting: models fence it
    in ``` blocks, prefix it with "Here is the JSON:", and emit trailing
    commas. This strips what it can and gives up quietly rather than raising.
    """
    raw = complete(system, user + "\n\nReply with JSON only. No prose, no code fences.",
                   fallback=None)
    if not raw:
        return fallback

    parsed = _parse_json_loosely(raw)
    if parsed is None:
        log.warning("twin agent: model did not return usable JSON; "
                    "falling back to the deterministic path")
        return fallback
    return parsed


def _parse_json_loosely(raw):
    """Best-effort JSON out of whatever a model actually said."""
    text = raw.strip()

    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()

    try:
        return json.loads(text)
    except ValueError:
        pass

    # Fall back to the outermost {...} or [...] in the response.
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            candidate = text[start:end + 1]
            # A trailing comma before a closer is the single most common
            # malformation and is trivially repairable.
            candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
            try:
                return json.loads(candidate)
            except ValueError:
                continue
    return None


def describe():
    """What the console's health panel says about the agent's model."""
    return {
        "configured": bool(config.LLM_API_KEY),
        "available": available(),
        "mode": mode(),
        "model": config.LLM_MODEL if config.LLM_API_KEY else None,
        "note": ("Deterministic mode: alerts are read from their structured CAP "
                 "fields, events are grouped spatially, and briefs are built from "
                 "templates. Set OPENAI_API_KEY to add language understanding."
                 if not available() else
                 "An LLM writes the extraction, grouping and briefs. Risk scores "
                 "remain deterministic and are never produced by the model."),
    }
