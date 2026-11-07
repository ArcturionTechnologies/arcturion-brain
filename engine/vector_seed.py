"""Query-time vector search over the `embeddings` table (Leg 2, retrieval-honesty 2026-08-04).

The Brain has embedded every node since Phase 5, but until now those vectors were read only
at INDEX time (`embedder.derive_semantic_edges`). The query engine never touched them, so a
question phrased in words that don't lexically match its answer returned nothing even when
the right node was one hop away — Leg 1 measured that as `paraphrase` recall@5 0.333 with
top1 0.000.

This module is the read side: embed the query, cosine-rank it against every node vector, and
hand the result to `brain._seed_nodes` (as an additional SEED LIST for rank fusion) and to
the final scorer (as a per-node similarity lookup). Vectors choose starting nodes; the graph
still does the walking. `semantically_similar_to` stays out of BFS — similarity belongs at
seed selection, never traversal.

Everything here is best-effort. `engine.ingest` already treats embedding as optional so FTS
keeps working when the model is unavailable; the query path must degrade the same way because
it runs behind a live HTTP service. Every entry point returns None/[] rather than raising, and
`brain` falls back to lexical-only seeds.

Cost (measured 2026-08-04 on the live 55,551 x 384 float32 graph, this machine):
  - `import sentence_transformers` ~3.2 s + model load ~2.0 s, ONCE per process (lazy). The
    HTTP server's `_warmup()` thread pays this before the first real request.
  - matrix load from SQLite ~30 ms, cached with a TTL
  - warm query: encode 30-110 ms + brute-force cosine 1-2 ms
Brute force is entirely adequate at this size — no ANN index, no new dependency.

Provenance note: the encoder/matrix/caching design here is carried over from the unmerged
branch `claude/frosty-almeida-9e5571` ("read the embeddings at query time"), which Leg 2
supersedes. What changed: the ranking half of that branch (a standalone similarity weight)
was replaced by reciprocal-rank fusion in `engine.rank_signals`, and the seed path is now one
ranked list among several rather than a blind append.
"""

from __future__ import annotations

import os

# MiniLM pulls in torch's OpenMP runtime, which aborts the process ("OMP: Error #15") when a
# second copy is already linked in — as it is under the HTTP server. Set before torch loads.
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import sqlite3
import sys
import threading
import time

from . import config as C

# Guards the lazy model/matrix loads. The HTTP server is a ThreadingHTTPServer, so two
# concurrent first-queries would otherwise each pay the multi-second model load.
_LOCK = threading.Lock()

_MODEL = None
_MODEL_FAILED = False  # sticky: don't retry a multi-second import on every query

# (ids, matrix, id->row index, loaded_at). Reloaded on TTL expiry so nodes the watcher embeds
# after process start become reachable without a restart.
_MATRIX: tuple[list[str], object, dict[str, int], float] | None = None

_WARNED: set[str] = set()


def _warn_once(key: str, msg: str) -> None:
    if key not in _WARNED:
        _WARNED.add(key)
        print(f"[brain] vector-seed degraded: {msg}", file=sys.stderr)


def _get_model():
    """Load the query encoder once per process. Returns None (and stays None) if
    sentence-transformers or the model weights are unavailable — the caller then falls back
    to lexical-only seeding."""
    global _MODEL, _MODEL_FAILED
    if _MODEL is not None or _MODEL_FAILED:
        return _MODEL
    try:
        if not C.EMBED_MODEL or not os.path.isabs(C.EMBED_MODEL) or not os.path.isdir(C.EMBED_MODEL):
            raise ValueError('explicit local model directory required')
        from sentence_transformers import SentenceTransformer
        # ONE thread. The work is a single ~15-token sentence; torch's default (one thread per
        # core) spins up the whole machine for ~8 ms of matmul, and this runs inside a
        # ThreadingHTTPServer that already serves requests concurrently — so the parallelism
        # buys nothing and costs contention. It also respects the Founder's standing
        # heat/thermal canon: a per-turn RAG lookup must not sound like a build.
        try:
            import torch
            torch.set_num_threads(1)
        except Exception:  # noqa: BLE001 — a thread-count hint is never worth failing over
            pass
        model = SentenceTransformer(C.EMBED_MODEL, local_files_only=True, trust_remote_code=False)
        ok, why = _selfcheck(model)
        if not ok:
            _MODEL_FAILED = True
            _warn_once("selfcheck",
                       f"query encoder FAILED its numeric self-check ({why}) — vector seeding "
                       f"is DISABLED for this process; retrieval has degraded to the lexical "
                       f"channels. This is the KMP_DUPLICATE_LIB_OK risk realised.")
            return None
        _MODEL = model
    except Exception as exc:  # noqa: BLE001 — any failure means "no vectors today"
        _MODEL_FAILED = True
        _warn_once("model", f"could not load {C.EMBED_MODEL} ({exc.__class__.__name__}: {exc})")
    return _MODEL


# Fixed probes for the numeric self-check. Never user input.
_PROBE_A = "the thermal gate throttles local model inference"
_PROBE_B = "a scheduled job failed to start because its interpreter was missing"
# Cosine between the two probes under all-MiniLM-L6-v2, measured on a healthy process
# (2026-08-04). The tolerance is deliberately wide: this guards against CORRUPTION — NaN,
# denormals, values collapsing toward a constant — not against the third-decimal drift a
# library upgrade would produce. A wrong-but-plausible cosine is the thing it must catch, and
# corruption does not land within +/- 0.05 of the right answer.
_PROBE_COS = -0.0727
_PROBE_TOL = 0.05


def _selfcheck(model) -> tuple[bool, str]:
    """Is this encoder producing real numbers?

    `KMP_DUPLICATE_LIB_OK=TRUE` at the top of this module suppresses OpenMP's
    duplicate-runtime abort. That abort is not decorative — OpenMP's own message says the
    workaround "may cause crashes or SILENTLY PRODUCE INCORRECT RESULTS". A crash is
    survivable here: the model fails to load, `_warn_once` fires, and retrieval degrades to
    the lexical channels. Silently wrong numbers are not survivable: every cosine in the
    ranking would be garbage, the relevance floor would gate on garbage, and the golden gate
    would publish a number nobody could interpret — the exact class of unfalsifiable
    measurement this whole programme exists to end.

    The env var cannot simply be dropped (torch and the process's other OpenMP consumer each
    link a runtime), so the risk is converted from silent to detected.

    Three assertions, ~10 ms once per process:
      1. finite and unit-norm — `normalize_embeddings=True` must yield ||v|| == 1
      2. self-cosine == 1 — a vector must match itself
      3. a PINNED cross-cosine between two fixed probes — the one that catches vectors which
         are individually well-formed but numerically wrong, which (1) and (2) cannot see

    Returns (ok, reason) and never raises: a self-check that crashes takes down the thing it
    was meant to protect."""
    try:
        import numpy as np
        v = model.encode([_PROBE_A, _PROBE_B], convert_to_numpy=True,
                         normalize_embeddings=True)
        if tuple(v.shape) != (2, C.EMBED_DIM):
            return False, f"unexpected shape {tuple(v.shape)}, wanted (2, {C.EMBED_DIM})"
        if not np.all(np.isfinite(v)):
            return False, "encoder produced non-finite values (NaN/inf)"
        norms = np.linalg.norm(v, axis=1)
        if not np.allclose(norms, 1.0, atol=1e-3):
            return False, f"vectors are not unit-norm: {norms}"
        self_cos = float(v[0] @ v[0])
        if abs(self_cos - 1.0) > 1e-3:
            return False, f"self-cosine {self_cos:.6f} != 1.0"
        cross = float(v[0] @ v[1])
        if abs(cross - _PROBE_COS) > _PROBE_TOL:
            return False, (f"pinned probe cosine {cross:.4f} outside {_PROBE_COS} +/- "
                           f"{_PROBE_TOL} — the encoder loads but its numerics are wrong")
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"{exc.__class__.__name__}: {exc}"


def _get_matrix(conn: sqlite3.Connection):
    """(ids, normalized float32 matrix, id->row) for every embedded node, cached with a TTL.

    Vectors are written normalized (`embedder.embed_all` passes normalize_embeddings=True), so
    cosine similarity is a plain dot product. Rows whose
    dim doesn't match `C.EMBED_DIM` are skipped rather than reshaped into garbage — a model
    change would otherwise silently corrupt every ranking."""
    global _MATRIX
    from .capacity_admission import is_scoped
    scoped = is_scoped(conn)
    now = time.time()
    if _MATRIX is not None and (now - _MATRIX[3]) < C.VECTOR_SEED_CACHE_TTL:
        ids, mat, index = _MATRIX[:3]
        if scoped:
            # Scope the already-warm global cache BEFORE any cosine calculation.
            admitted = {r[0] for r in conn.execute("SELECT id FROM nodes")}
            positions = [i for i,nid in enumerate(ids) if nid in admitted]
            ids = [ids[i] for i in positions]
            mat = mat[positions]
            index = {nid:i for i,nid in enumerate(ids)}
        return ids, mat, index
    import numpy as np
    rows = conn.execute(
        "SELECT node_id, vector FROM embeddings WHERE dim = ?", (C.EMBED_DIM,)
    ).fetchall()
    if not rows:
        empty = ([], np.empty((0, C.EMBED_DIM), dtype=np.float32), {})
        if not scoped:
            _MATRIX = (*empty, now)
        return empty
    ids = [r[0] for r in rows]
    buf = b"".join(r[1] for r in rows)
    expected = len(ids) * C.EMBED_DIM * 4
    if len(buf) != expected:
        # A short/long blob would misalign every subsequent row. Fall back to per-row parsing
        # so one bad vector costs one node, not the whole ranking.
        mat = np.zeros((len(ids), C.EMBED_DIM), dtype=np.float32)
        keep: list[str] = []
        k = 0
        for nid, blob in rows:
            v = np.frombuffer(blob, dtype=np.float32)
            if v.shape[0] != C.EMBED_DIM:
                continue
            mat[k] = v
            keep.append(nid)
            k += 1
        ids, mat = keep, mat[:k]
    else:
        mat = np.frombuffer(buf, dtype=np.float32).reshape(len(ids), C.EMBED_DIM)
    index = {nid: i for i, nid in enumerate(ids)}
    # A cold scoped load must NEVER poison the process-wide cache for another capacity.
    if not scoped:
        _MATRIX = (ids, mat, index, now)
    return ids, mat, index


class QuerySimilarity:
    """One query's cosine against EVERY embedded node — both as a ranked top-K (the seed
    list that feeds rank fusion) and as an O(1) per-node lookup (so a node the graph walk
    discovered can still be credited for semantic relevance)."""

    __slots__ = ("_sims", "_index", "_ids")

    def __init__(self, sims, index: dict[str, int], ids: list[str]):
        self._sims = sims
        self._index = index
        self._ids = ids

    def get(self, node_id: str, default: float = 0.0) -> float:
        """Cosine for one node; `default` (0.0) if it has no embedding.

        The `default` parameter exists so the call site can read like every other lookup in
        the scorer (`fusion.get(id, 0.0)`), and so this class is substitutable for a plain
        dict. Review round 4 flagged `sims.get(n["id"])` as a possible None-then-TypeError.
        It was not — this method has always defaulted internally — but both facts that made
        it safe were invisible at the call site, and `sims.get(id, 0.0)` would ITSELF have
        raised `TypeError: takes 2 positional arguments but 3 were given`. A contract that is
        only correct if you read the implementation is a contract waiting to break, so it is
        now explicit at both ends.

        Note this is the COMMON path, not an edge case: the graph walk reaches thousands of
        nodes the vector channel never ranked, and every one of them lands here."""
        i = self._index.get(node_id)
        return float(self._sims[i]) if i is not None else default

    def best(self) -> float:
        """The single highest cosine in the corpus for this query. This is the corpus-wide
        answer to 'is there anything here that even resembles the question', and it is the
        semantic half of the relevance floor."""
        return float(self._sims.max()) if len(self._sims) else 0.0

    def top(self, k: int, floor: float | None = None) -> list[tuple[str, float]]:
        """Top-`k` (node_id, cosine) at or above `floor`, descending."""
        import numpy as np
        n = len(self._sims)
        if k <= 0 or n == 0:
            return []
        floor = C.VECTOR_SEED_MIN_SIM if floor is None else floor
        k = min(k, n)
        # argpartition is O(n) vs a full O(n log n) sort of 55K scores; only the top-k slice
        # is then sorted.
        part = np.argpartition(-self._sims, k - 1)[:k] if k < n else np.arange(n)
        part = part[np.argsort(-self._sims[part])]
        return [(self._ids[i], float(self._sims[i])) for i in part if self._sims[i] >= floor]


_PREWARM_STARTED = False

# Whether a query may return WITHOUT the vector channel because the encoder is still loading.
#
# OFF by default, and that default is load-bearing. The non-blocking path was added to fix a
# 6,497 ms cold p95 on the HTTP service, and making it the global default immediately broke
# something worse than latency: `python3 -m engine.retrieval_eval` is a fresh process, so its
# first queries ran with NO vector channel and the golden gate scored a ranker that was not
# the shipped one — positive recall@5 read 0.368 instead of 0.474, and varied run to run
# depending on how far the background load had got. A measurement harness must never race the
# thing it measures. So: batch and CLI callers BLOCK once (~5 s) and get deterministic
# results; only a declared latency-critical server opts into degrading.
_NONBLOCKING = False


def set_nonblocking(value: bool = True) -> None:
    """Declare this process latency-critical: queries degrade to lexical-only rather than
    wait for the encoder. Called by `http_server.main()`; nothing else should call it."""
    global _NONBLOCKING
    _NONBLOCKING = bool(value)


def prewarm() -> None:
    """Load the encoder synchronously. Meant for a background thread."""
    global _PREWARM_STARTED
    with _LOCK:
        _PREWARM_STARTED = True
        _get_model()


def _start_prewarm() -> None:
    """Kick the multi-second model load onto a background thread, exactly once."""
    global _PREWARM_STARTED
    if _PREWARM_STARTED or _MODEL_FAILED:
        return
    _PREWARM_STARTED = True
    threading.Thread(target=prewarm, name="brain-vector-prewarm", daemon=True).start()


def similarity(conn: sqlite3.Connection, topic: str) -> QuerySimilarity | None:
    """Cosine of `topic` against every embedded node, or None if vectors are unusable (no
    embeddings, model unavailable, numpy missing, empty topic, or the encoder is still
    loading).

    NEVER BLOCKS ON THE MODEL LOAD. `import sentence_transformers` + MiniLM costs ~5 s once
    per process, and the first measurement of this leg showed exactly why that must not sit in
    the request path: right after a service restart, cold p95 was 6,497 ms — every query
    arriving during the load queued behind it, against a RAG hook that times out at 1,500 ms.
    So a query that finds the encoder not ready starts the load on a background thread and
    returns None for ITSELF, degrading to lexical-only for a few seconds instead of stalling
    every agent in the fleet.

    Never raises, either — the query path runs behind a live HTTP service and must degrade
    exactly the way `engine.ingest` degrades when embedding fails."""
    topic = (topic or "").strip()
    if not topic:
        return None
    try:
        # A thread holding the lock is loading the model or the matrix. In a declared
        # latency-critical process, waiting on it IS the tail this guard exists to remove;
        # everywhere else, waiting is simply correct.
        if not _LOCK.acquire(blocking=not _NONBLOCKING):
            _start_prewarm()
            return None
        try:
            if _NONBLOCKING and _MODEL is None:
                _start_prewarm()
                return None
            model = _get_model()
            if model is None:
                return None
            ids, mat, index = _get_matrix(conn)
        finally:
            _LOCK.release()
        if not ids:
            return None
        q = model.encode([topic], convert_to_numpy=True, normalize_embeddings=True)[0]
        return QuerySimilarity(mat @ q.astype("float32"), index, ids)
    except Exception as exc:  # noqa: BLE001 — best-effort by contract
        _warn_once("search", f"{exc.__class__.__name__}: {exc}")
        return None


def search(conn: sqlite3.Connection, topic: str, top_k: int) -> list[tuple[str, float]]:
    """Top-`top_k` node ids by cosine similarity to `topic`. [] when unusable."""
    qs = similarity(conn, topic)
    return qs.top(top_k) if qs is not None else []


def reset_cache() -> None:
    """Drop the cached matrix (and the sticky model-failure flag). Tests point `C.BRAIN_DB`
    at a temp fixture between cases, so the cache must not survive."""
    global _MATRIX, _MODEL_FAILED, _PREWARM_STARTED
    with _LOCK:
        _MATRIX = None
        _MODEL_FAILED = False
        _PREWARM_STARTED = _MODEL is not None
    _WARNED.clear()


def available(conn: sqlite3.Connection | None = None) -> dict:
    """Diagnostics for /status and the A/B harness — is hybrid seeding actually live?

    Without this, "hybrid retrieval is on" and "the model silently failed to load and every
    query quietly fell back to lexical seeding" look identical from outside the process."""
    from . import db as DB
    own = conn is None
    conn = conn or DB.connect()
    try:
        n = conn.execute("SELECT COUNT(*) FROM embeddings WHERE dim = ?",
                         (C.EMBED_DIM,)).fetchone()[0]
    except sqlite3.Error:
        n = 0
    finally:
        if own:
            conn.close()
    with _LOCK:
        model_loaded = _MODEL is not None
    return {
        "enabled": bool(C.VECTOR_SEED_ENABLED),
        "embeddings": n,
        "model": C.EMBED_MODEL,
        "model_loaded": model_loaded,
        "model_failed": _MODEL_FAILED,
        "top_k": C.VECTOR_SEED_TOP_K,
        "min_sim": C.VECTOR_SEED_MIN_SIM,
    }
