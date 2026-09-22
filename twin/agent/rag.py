"""Document retrieval over SOPs, guidelines and past situation reports.

The agent's third job: give an analyst the procedure and the precedent that
apply to what was just flagged, with a citation they can open. A brief that
cites nothing is a brief nobody can check, and an unverifiable brief is worse
than none because it still carries authority.

Three deliberate choices:

* **Local embeddings, no key.** The corpus is small (SOP PDFs, past reports)
  and the queries are few, so a sentence-transformers model on disk beats
  paying per query -- and it keeps the agent's "works with no key" property
  intact.
* **FAISS, not Chroma.** Chroma publishes no wheel for this project's Python
  (3.14). FAISS does, and the index is a flat file.
* **Keyword fallback.** With neither library installed the search degrades to
  scoring passages by term overlap. Noticeably worse, and still better than a
  brief with no procedure attached at all.

Only *documents* live here. Live numbers belong to the deterministic scorer:
embedding "62 mm of rain" would let a flag cite a similar-sounding sentence
instead of the measurement, which is precisely the failure mode this module's
citations exist to prevent.
"""

import hashlib
import json
import logging
import os
import re

from .. import config

log = logging.getLogger("twin.agent.rag")

_INDEX = None
_INDEX_BUILT = False

#: Passage size, in characters. Long enough to carry a complete instruction,
#: short enough that a citation points at something an analyst can read at a
#: glance during an incident.
CHUNK_CHARS = 900
CHUNK_OVERLAP = 150


def corpus_files(directory=None):
    """Every readable document in the corpus directory."""
    directory = directory or config.RAG_CORPUS_DIR
    if not directory or not os.path.isdir(directory):
        return []

    found = []
    for root, _dirs, files in os.walk(directory):
        for name in sorted(files):
            if name.lower().endswith((".md", ".txt", ".json")):
                found.append(os.path.join(root, name))
    return found


def load_passages(directory=None):
    """Split the corpus into citable passages."""
    passages = []
    for path in corpus_files(directory):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError as exc:
            log.warning("rag: could not read %s (%s)", path, exc)
            continue

        if path.lower().endswith(".json"):
            text = _flatten_json(text)

        title = os.path.splitext(os.path.basename(path))[0].replace("_", " ")
        for index, chunk in enumerate(_chunk(text)):
            passages.append({
                "id": hashlib.sha1(("%s:%d" % (path, index)).encode("utf-8")).hexdigest()[:16],
                "text": chunk,
                "title": title,
                "source": os.path.relpath(path, directory or config.RAG_CORPUS_DIR),
                "url": None,
            })
    return passages


def _flatten_json(text):
    try:
        data = json.loads(text)
    except ValueError:
        return text
    return json.dumps(data, indent=1, ensure_ascii=False)


def _chunk(text):
    text = re.sub(r"\n{3,}", "\n\n", text or "").strip()
    if not text:
        return []
    chunks = []
    start = 0
    while start < len(text):
        end = min(len(text), start + CHUNK_CHARS)
        chunks.append(text[start:end].strip())
        if end == len(text):
            break
        start = end - CHUNK_OVERLAP
    return [chunk for chunk in chunks if chunk]


# --------------------------------------------------------------------------
# Index
# --------------------------------------------------------------------------

def _embedder():
    """The local embedding model, or None when it is not installed."""
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        return None
    try:
        return SentenceTransformer(config.RAG_EMBEDDING_MODEL)
    except Exception as exc:  # noqa: BLE001 - first run downloads the model
        log.warning("rag: embedding model unavailable (%s); using keyword search", exc)
        return None


def build_index(directory=None):
    """Embed the corpus into a FAISS index, or fall back to keyword search."""
    global _INDEX, _INDEX_BUILT
    _INDEX_BUILT = True

    passages = load_passages(directory)
    if not passages:
        _INDEX = {"mode": "empty", "passages": []}
        return _INDEX

    model = _embedder()
    if model is None:
        _INDEX = {"mode": "keyword", "passages": passages}
        return _INDEX

    try:
        import faiss
        import numpy as np

        vectors = model.encode([p["text"] for p in passages],
                               normalize_embeddings=True, show_progress_bar=False)
        vectors = np.asarray(vectors, dtype="float32")
        index = faiss.IndexFlatIP(vectors.shape[1])   # cosine, vectors are normalised
        index.add(vectors)
        _INDEX = {"mode": "faiss", "passages": passages, "index": index, "model": model}
    except Exception as exc:  # noqa: BLE001
        log.warning("rag: FAISS index unavailable (%s); using keyword search", exc)
        _INDEX = {"mode": "keyword", "passages": passages}
    return _INDEX


def index():
    global _INDEX_BUILT
    if not _INDEX_BUILT:
        build_index()
    return _INDEX or {"mode": "empty", "passages": []}


def reset():
    """Drop the cached index -- used by tests and after the corpus changes."""
    global _INDEX, _INDEX_BUILT
    _INDEX = None
    _INDEX_BUILT = False


def search(query, top_k=None):
    """Passages most relevant to `query`. Returns [] when there is no corpus.

    An empty corpus is the normal state of a fresh install: the twin ships no
    SOPs of its own, because the procedures that matter are the ones this
    city's authority actually publishes. Drop them in TWIN_RAG_CORPUS_DIR.
    """
    top_k = top_k or config.RAG_TOP_K
    store = index()
    passages = store.get("passages") or []
    if not passages or not (query or "").strip():
        return []

    if store["mode"] == "faiss":
        try:
            import numpy as np

            vector = store["model"].encode([query], normalize_embeddings=True)
            scores, positions = store["index"].search(
                np.asarray(vector, dtype="float32"), min(top_k, len(passages)))
            results = []
            for score, position in zip(scores[0], positions[0]):
                if position < 0:
                    continue
                passage = dict(passages[position])
                passage["score"] = float(score)
                results.append(passage)
            return results
        except Exception as exc:  # noqa: BLE001
            log.warning("rag: vector search failed (%s); using keyword search", exc)

    return _keyword_search(query, passages, top_k)


def _keyword_search(query, passages, top_k):
    """Term-overlap scoring: crude, dependency-free, and better than nothing."""
    terms = {t for t in re.findall(r"[a-z]{3,}", query.lower())}
    if not terms:
        return []

    scored = []
    for passage in passages:
        words = set(re.findall(r"[a-z]{3,}", passage["text"].lower()))
        overlap = len(terms & words)
        if overlap:
            scored.append((overlap / len(terms), passage))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [dict(passage, score=round(score, 3)) for score, passage in scored[:top_k]]


def describe():
    """Corpus status for the console's agent panel."""
    store = index()
    return {
        "mode": store.get("mode"),
        "passages": len(store.get("passages") or []),
        "corpus_dir": config.RAG_CORPUS_DIR,
        "note": ("No documents loaded. Put SOPs, NDMA/SDMA guidelines and past "
                 "situation reports in the corpus directory to have briefs cite "
                 "procedure." if not (store.get("passages")) else None),
    }
