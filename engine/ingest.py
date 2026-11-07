"""Targeted incremental ingest — push specific files into the live index NOW.

Where `graph_builder.reindex` walks the entire universe (and reads every node's
content hash to decide what changed), `ingest_paths` touches ONLY the files it's
handed: extract → tag prong → hash-skip → upsert node + edges → embed the new
node. The heavy global passes (semantic-neighbor edges, PageRank centrality,
GC/dedup, the session-transcript harvest) stay on the nightly brain-reindex
schedule where they belong.

This is the write half of the "memory saved → searchable in seconds" loop: an
agent (or the brain-memory-watch daemon / a session-end hook) writes a
Memory/*.md file, and this lands it in brain.sqlite + FTS5 + the embeddings
table immediately, so every other agent can recall it on the next query instead
of waiting ~17h for the 02:30 full reindex.

A freshly upserted node is findable at once via FTS (text search) AND via its
own MiniLM vector (semantic search). Only its `semantically_similar_to` graph
edges and centrality score lag until the nightly pass — acceptable, and it keeps
the per-write cost to ~one model load + the changed rows.

    python3 -m engine.ingest <path> [<path>...] [--no-embed] [--quiet]
"""

from __future__ import annotations
import re
import sqlite3
import sys
import time
from pathlib import Path
from . import config as C
from . import db as DB
from . import walker
from .extractors import route
from .util import now_iso, parse_frontmatter, excluded_agent_for_path


# ─────────────────────────────────────────────────────────────────────────────
# Admission gate (Retrieval Honesty, Leg 4)
# ─────────────────────────────────────────────────────────────────────────────
# The index used to accept every file the walker could classify. That is how a
# 58,000-node graph ends up ~29% short-body rows: empty `_index.md` stubs, one-line
# `cycle_<timestamp>.md` research pings, unsubstituted scaffolding. None of it can
# ever be a retrieval answer, but all of it dilutes FTS/vector neighbourhoods and
# inflates every coverage denominator.
#
# A record earns indexing if ANY ONE of three tests passes. They are ORed, not
# ANDed, on purpose — each admits a legitimately different KIND of record, and the
# cost of a false reject (knowledge silently missing from the shared brain) is far
# worse than a false admit (one more mediocre row):
#
#   1. BODY      — ≥ ADMIT_MIN_BODY_CHARS of body. Prose long enough to answer with.
#   2. FRONTMATTER — a structured `type`/`status`/`owner` declaration. This is the
#      test that admits SHORT canonical records: a Decision note, and every Leg-3
#      distilled session record (measured: bodies down to 55 chars, all carrying
#      `type:` + `owner:`). Both classes are authored, addressable knowledge whose
#      value is not proportional to their length.
#   3. RARE TOKEN — the title/filename carries a token whose corpus document
#      frequency is ≤ ADMIT_RARE_DF_MAX (IDF ≥ ln(N/df)). A one-line note titled
#      with a Gate item alias or a unique identifier is exactly the record that a
#      keyword query will one day be the ONLY way to find. Checked last because it
#      is the only test that costs a DB round-trip.
#
# Rejects are never silently dropped: every one is recorded in the skip ledger
# (see `record_skip` / `--skips`) with the reason and the measured numbers, so a
# false reject is discoverable by sampling rather than by someone noticing an
# answer went missing months later.

ADMIT_MIN_BODY_CHARS = 200

# Document-frequency ceiling for "rare". df ≤ 60 against a ~58,000-node corpus is
# IDF ≥ ln(58000/60) ≈ 6.9 — comfortably above the ~4.0 band where ordinary domain
# vocabulary lives ("launchd" df≈1.4k, "brain" df≈9k), and permissive enough that a
# genuine alias/identifier (df 1-10) is never the thing that gets rejected. Tuned
# against a dry-run of the real walk (see leg-4-report.md): the interesting failure
# mode is a threshold so TIGHT that a hapax-legomenon title is called common, so
# this is deliberately set an order of magnitude above where real aliases sit.
ADMIT_RARE_DF_MAX = 60

# Frontmatter keys that count as a STRUCTURED declaration. Read from the raw file,
# not from the extracted node: every extractor defaults `type`/`status` to something
# non-null, so the node dict cannot distinguish "author declared status: draft" from
# "extractor filled in status: active". Only the file itself knows.
ADMIT_FM_KEYS = ("type", "node_type", "entity_type", "status", "owner", "authored_by")

# Frontmatter is only read for files that already failed the body test, but a
# pathological multi-MB file would still be fully read. Frontmatter lives in the
# first bytes by definition, so cap the read.
_FM_PROBE_BYTES = 8192

# `\w` with re.UNICODE, not `[a-z0-9]`. An ASCII-only class yields NO candidate token for
# a CJK-, Cyrillic- or emoji-titled note, so such a note was refused on "no candidate
# token" regardless of how unique its title actually was — an ASCII bias in a privacy- and
# knowledge-critical gate, and one such row was already in the live skip ledger. `_` is
# excluded so `snake_case_title` still splits into its parts. FTS5 tokenizes with
# `unicode61`, so these tokens are the ones the frequency lookup can actually measure.
_ADMIT_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)

# Tokens too short to be discriminating regardless of their measured frequency — but ONLY
# for scripts where length means that. A 2-character CJK token routinely carries as much
# meaning as an 8-character Latin one, so the floor is applied to ASCII tokens alone.
_ADMIT_MIN_TOKEN_LEN = 4
_ADMIT_MIN_TOKEN_LEN_NON_ASCII = 1


def _canonical_source_path(path: Path) -> str:
    """Use the same stable path identity before and after extractor execution."""
    for root in (C.UNIVERSE_ROOT, C.DESKTOP_ROOT, C.WORK_ROOT):
        try:
            return str(path.relative_to(root))
        except ValueError:
            continue
    return str(path)


def _admit_tokens(node: dict, path: Path | None) -> list[str]:
    """Candidate rare-token strings: the node title plus the filename stem."""
    bits = [str(node.get("title") or "")]
    if path is not None:
        bits.append(path.stem)
    out, seen = [], set()
    for bit in bits:
        for tok in _ADMIT_TOKEN_RE.findall(bit.lower()):
            floor = (_ADMIT_MIN_TOKEN_LEN if tok.isascii()
                     else _ADMIT_MIN_TOKEN_LEN_NON_ASCII)
            if len(tok) >= floor and tok not in seen:
                seen.add(tok)
                out.append(tok)
    return out


def _has_structured_frontmatter(path: Path | None) -> bool:
    """True if the file on disk declares any of ADMIT_FM_KEYS in its frontmatter."""
    if path is None:
        return False
    try:
        with open(path, "rb") as fh:
            head = fh.read(_FM_PROBE_BYTES)
    except OSError:
        return False
    try:
        text = head.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — a decode failure means "no frontmatter", not a crash
        return False
    try:
        fm, _body = parse_frontmatter(text)
    except Exception:  # noqa: BLE001 — malformed YAML is "no frontmatter", never fatal
        return False
    if not isinstance(fm, dict):
        return False
    return any(fm.get(k) not in (None, "", [], {}) for k in ADMIT_FM_KEYS)


def admit_node(conn, node: dict, path=None) -> tuple[bool, str, dict]:
    """Decide whether `node` earns a row in the index.

    Returns `(admit, reason, facts)`. `reason` names the test that ADMITTED it, or
    the compound reason it was refused; `facts` carries the measured numbers so the
    skip ledger records evidence rather than an opinion.

    FAIL-OPEN by construction. Every failure mode inside this function (unreadable
    file, missing FTS table, DB error) resolves to "admit". A gate that silently
    drops knowledge when its own machinery breaks is strictly worse than no gate:
    the junk it would have caught is visible in the index, but the good record it
    wrongly ate is invisible everywhere.
    """
    if not node.get("path"):
        # Synthetic/harvested rows (sessions, agents, capabilities) have no file
        # behind them and are not this gate's business.
        return True, "not-file-backed", {}

    p = Path(path) if path is not None else None
    facts = {}

    # Defense-in-depth (Retrieval Honesty, Leg 7 / 7.1). The walker is the primary
    # enforcement point for `config.EXCLUDED_AGENTS` (root selection excludes those
    # trees entirely; `walker._excluded` re-checks every path handed to it, single-file
    # branch included). This is a SECOND, independent check on the admission gate every
    # producer path funnels through before a node earns a row — so a future producer
    # that calls `admit_node` directly, bypassing the walker, still cannot seat an
    # excluded agent's file. Checked on both the walk-relative `path` argument AND the
    # node's own recorded `path` field, because callers have been inconsistent about
    # which one is absolute.
    for candidate in (p, node.get("path")):
        if candidate is None:
            continue
        agent = excluded_agent_for_path(candidate)
        if agent:
            return False, f"excluded-agent:{agent}", {}

    body_chars = len(node.get("body_excerpt") or "")
    facts["body_chars"] = body_chars

    if body_chars >= ADMIT_MIN_BODY_CHARS:
        return True, "body", facts

    if _has_structured_frontmatter(p):
        return True, "frontmatter", facts

    min_df = -1
    tokens = _admit_tokens(node, p)
    try:
        from .rank_signals import document_frequency
        for tok in tokens:
            df = document_frequency(conn, tok)
            if df < 0:
                # Frequency unavailable (no FTS5, unusable token). Unknown is not
                # "common" — if we cannot measure rarity at all, admit and say so.
                return True, "df-unavailable", facts
            if min_df < 0 or df < min_df:
                min_df = df
            # `df <= MAX`, NOT `0 < df <= MAX`. df == 0 means the token appears in NO
            # indexed document — the rarest a token can possibly be, and the single most
            # important case this test exists for: a brand-new short note titled with a
            # NOVEL alias. The `0 <` guard rejected exactly that, permanently and
            # self-reinforcingly (refused → never indexed → still df 0 → refused again),
            # and on a cold or empty corpus it refused every short frontmatter-less
            # record there was. A rarity gate that rejects the rarest possible token is
            # backwards. df < 0 is the genuinely-unmeasurable case and is handled above.
            if df <= ADMIT_RARE_DF_MAX:
                facts["min_df"] = df
                return True, "rare-token", {**facts, "token": tok}
    except Exception as exc:  # noqa: BLE001 — see FAIL-OPEN above
        return True, f"gate-error:{type(exc).__name__}", facts

    facts["min_df"] = min_df
    # -1 here means the title/stem yielded no candidate token at all (nothing ≥
    # _ADMIT_MIN_TOKEN_LEN alphanumeric characters) — a DIFFERENT fact from "we asked
    # and could not measure", which returns "df-unavailable" and ADMITS above. Spelled
    # out so a ledger row is readable without reading this function.
    rarity = ("no candidate token in title/filename" if not tokens
              else f"rarest title token df={min_df} > {ADMIT_RARE_DF_MAX}")
    return False, (f"no-signal (body={body_chars} < {ADMIT_MIN_BODY_CHARS} chars; "
                   f"no structured frontmatter ({'/'.join(ADMIT_FM_KEYS[:3])}…); "
                   f"{rarity})"), facts


# ── Skip ledger ──────────────────────────────────────────────────────────────
# Durable, inspectable, sampleable. A table in brain.sqlite rather than a log file:
# a log grows without bound across nightly runs and answers "what was refused?" only
# by grep, whereas one row per PATH (upserted, with a hit counter) stays bounded,
# survives restarts, and is sampleable for false rejections with one ORDER BY RANDOM.
#
# Created lazily here rather than in db.init_db so the gate owns its own storage and
# an older DB opened by an older engine is unaffected.

_SKIP_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS admission_skips (
    path        TEXT PRIMARY KEY,
    title       TEXT,
    reason      TEXT,
    body_chars  INTEGER,
    min_df      INTEGER,
    first_seen  TEXT,
    last_seen   TEXT,
    times       INTEGER NOT NULL DEFAULT 1
)
"""


def ensure_skip_ledger(conn) -> bool:
    try:
        conn.execute(_SKIP_LEDGER_DDL)
        return True
    except sqlite3.Error as exc:
        print(f"[brain-ingest] skip ledger unavailable: {exc}", file=sys.stderr)
        return False


def record_skip(conn, node: dict, reason: str, facts: dict) -> None:
    """Append/refresh one skip-ledger row. Never raises — a ledger write failure must
    not abort a reindex, but it IS reported, because a silent ledger is the exact
    failure this gate exists to avoid."""
    ts = now_iso()
    try:
        conn.execute(
            "INSERT INTO admission_skips "
            "(path, title, reason, body_chars, min_df, first_seen, last_seen, times) "
            "VALUES (?,?,?,?,?,?,?,1) "
            "ON CONFLICT(path) DO UPDATE SET "
            "  title=excluded.title, reason=excluded.reason, "
            "  body_chars=excluded.body_chars, min_df=excluded.min_df, "
            "  last_seen=excluded.last_seen, times=admission_skips.times+1",
            (node.get("path"), node.get("title"), reason,
             facts.get("body_chars"), facts.get("min_df"), ts, ts),
        )
    except sqlite3.Error as exc:
        print(f"[brain-ingest] skip ledger write failed for "
              f"{node.get('path')}: {exc}", file=sys.stderr)


def sample_skips(limit: int = 20, db_path=None) -> list[dict]:
    """Random sample of the skip ledger — the verification surface for false rejects."""
    conn = DB.connect(db_path) if db_path else DB.connect()
    try:
        rows = conn.execute(
            "SELECT path, title, reason, body_chars, min_df, times, last_seen "
            "FROM admission_skips ORDER BY RANDOM() LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


import contextvars as _contextvars
_OPENED: "_contextvars.ContextVar" = _contextvars.ContextVar("ingest_opened_conns", default=None)


def ingest_paths(paths, embed: bool = True, verbose: bool = False) -> dict:
    """Ingest; on ANY exception, roll back and close every write connection it opened.

    Without this, a failure between `DB.connect()` and the final commit (a lock during
    `init_db`, a raising upsert) left an open write transaction on a connection that only
    garbage collection would close. In the long-lived memory watcher that held the WAL write
    lock indefinitely and stalled every other writer (observed live 2026-11-07)."""
    opened: list = []
    token = _OPENED.set(opened)
    try:
        return _ingest_paths(paths, embed=embed, verbose=verbose)
    finally:
        _OPENED.reset(token)
        for c in opened:
            try:
                if c.in_transaction:
                    c.rollback()
            except Exception:  # noqa: BLE001 - already closed
                pass
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass


def _tracked_connect():
    """DB.connect() registered with the current ingest_paths call (per thread/context)."""
    c = DB.connect()
    lst = _OPENED.get()
    if lst is not None:
        lst.append(c)
    return c


def _ingest_paths(paths, embed: bool = True, verbose: bool = False) -> dict:
    """Ingest the given files/dirs into brain.sqlite immediately.

    Mirrors the per-file half of graph_builder.reindex: same extractor router,
    same prong tag, same content-hash skip, same idempotent upserts — just scoped
    to `paths` and without the universe-wide derivation/harvest passes.
    """
    from .telemetry import extraction_current
    t0 = time.time()
    paths = list(paths)  # The watcher and the desk sync both need the original set.
    walk_items = list(walker.walk_paths([Path(p) for p in paths]))

    conn = _tracked_connect()
    DB.init_db(conn)
    have_ledger = ensure_skip_ledger(conn)

    upserted = skipped_hash = edges_inserted = failures = refused = 0
    upserted_ids: list[str] = []

    # VEGA desk sync (opt-in). The slow part — walking and hashing the VEGA
    # workspace, up to ~90 s — runs HERE, before this ingest writes anything, so
    # it never holds the Brain write lock. Only the short apply step runs inside
    # the transaction below; it re-checks the plan's DB inputs under the lock and
    # re-plans if another writer moved them. The original paths are passed so
    # deleted sources are reconciled even though the walker cannot yield them.
    from . import vega_index
    vega_plans = None
    if vega_index.enabled():
        if conn.in_transaction:
            conn.commit()  # schema setup only; the scan must not sit inside a write
        try:
            from .vega_corpus_index import prepare_corpus
            vega_plans = (vega_index._prepare_sync(conn, paths=paths),
                          prepare_corpus(conn, paths=paths))
        except Exception as exc:
            failures += 1
            print(f"[brain-ingest] VEGA desk sync failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        finally:
            if conn.in_transaction:
                conn.rollback()  # the scan is read-only; never carry a snapshot into the loop
    for p, family, node_type in walk_items:
        source_path = _canonical_source_path(p)
        source_key = f"file:{source_path}"
        try:
            node, edges = route(p, family, node_type)
        except Exception as e:  # noqa: BLE001 — never let one bad file abort the batch
            failures += 1
            DB.record_source_watermark(
                conn, source_key=source_key, source_kind="file",
                source_path=source_path, observed_at=now_iso(), success=False,
                error=f"{type(e).__name__}: {e}")
            if verbose:
                print(f"[brain-ingest] extract failed for {p}: "
                      f"{type(e).__name__}: {e}", file=sys.stderr)
            continue
        if node is None:
            failures += 1
            DB.record_source_watermark(
                conn, source_key=source_key, source_kind="file",
                source_path=source_path, observed_at=now_iso(), success=False,
                error="extractor-returned-no-node")
            continue
        extract_error = (node.get("metadata") or {}).get("extract_error")
        if extract_error:
            failures += 1
            DB.upsert_node(conn, node)
            DB.record_source_watermark(
                conn, source_key=source_key, source_kind="file",
                source_path=source_path, observed_at=now_iso(), success=False,
                content_hash=node.get("content_hash"), node_id=node.get("id"),
                error=str(extract_error))
            continue
        # Four-Prong Canon — tag every node with its prong (mirrors graph_builder).
        node["prong"] = C.prong_for_path(p)

        # Admission gate (Leg 4). Placed here, in the shared per-file body, so the
        # live watcher / session-end hooks (which call ingest_paths) and the nightly
        # walker (graph_builder._consume, which calls admit_node the same way) get
        # the SAME answer. A gate wired into one caller is a gate the other caller
        # routes around.
        ok, reason, facts = admit_node(conn, node, p)
        if not ok:
            refused += 1
            if have_ledger:
                record_skip(conn, node, reason, facts)
            if verbose:
                print(f"[brain-ingest] refused {node.get('path')}: {reason}",
                      file=sys.stderr)
            DB.record_source_watermark(
                conn, source_key=source_key, source_kind="file",
                source_path=source_path, observed_at=now_iso(),
                success=False, content_hash=node.get("content_hash"), node_id=node.get("id"),
                error=f"admission-refused:{reason}")
            continue

        rel = node.get("path")
        try:
            mtime_ns = p.stat().st_mtime_ns
        except OSError:
            mtime_ns = None
        if rel:
            row = conn.execute(
                "SELECT content_hash, metadata FROM nodes WHERE path = ?", (rel,)
            ).fetchone()
            if row and row["content_hash"] == node.get("content_hash") and extraction_current(node, row["metadata"]):
                DB.record_source_watermark(
                    conn, source_key=source_key, source_kind="file",
                    source_path=source_path, observed_at=now_iso(), success=True,
                    content_hash=node.get("content_hash"), source_mtime_ns=mtime_ns,
                    node_id=node.get("id"))
                skipped_hash += 1
                continue

        DB.upsert_node(conn, node)
        upserted += 1
        upserted_ids.append(node["id"])
        for e in edges:
            if DB.insert_edge(conn, e):
                edges_inserted += 1
        # Mark success only after the node and admitted edges participate in this
        # transaction. If an upsert raises, no fresh success can be committed alone.
        DB.record_source_watermark(
            conn, source_key=source_key, source_kind="file",
            source_path=source_path, observed_at=now_iso(), success=True,
            content_hash=node.get("content_hash"), source_mtime_ns=mtime_ns,
            node_id=node.get("id"))

    if any(str(p).endswith(".py") for p, _, _ in walk_items) and upserted_ids:
        from .code_references import refresh as refresh_code_references
        refresh_code_references(conn)

    # Drop stale embeddings for upserted nodes so embed_all re-creates them. A node
    # that changed but kept its id still has its OLD vector; embed_all only fills
    # missing ones, so without this the semantic vector would lag the fresh FTS text.
    for nid in upserted_ids:
        conn.execute("DELETE FROM embeddings WHERE node_id = ?", (nid,))

    # Apply the pre-scanned VEGA plans inside this transaction (DB work only).
    # A savepoint keeps a failed apply from discarding the upserts above.
    if vega_plans is not None:
        from .vega_corpus_index import apply_prepared_corpus
        options = {"paths": paths}
        conn.execute("SAVEPOINT vega_desk_ingest")
        try:
            desk_result = vega_index._apply_prepared_sync(conn, vega_plans[0], options)
            corpus_result = apply_prepared_corpus(conn, vega_plans[1], options)
            if desk_result.get("unchanged") and corpus_result.get("unchanged"):
                conn.execute("ROLLBACK TO vega_desk_ingest")  # drop no-op DDL/reads
            conn.execute("RELEASE vega_desk_ingest")
        except Exception as exc:
            conn.execute("ROLLBACK TO vega_desk_ingest")
            conn.execute("RELEASE vega_desk_ingest")
            failures += 1
            print(f"[brain-ingest] VEGA desk sync failed: {type(exc).__name__}: {exc}", file=sys.stderr)

    conn.commit()
    conn.close()  # release the write connection before the embedder opens its own

    # Embed only the just-upserted nodes. embed_all() is already incremental —
    # it embeds only nodes that lack an embedding — so this picks up our new rows
    # (and any prior backlog) and skips everything else.
    embedded = 0
    if embed and upserted:
        try:
            from .embedder import embed_all
            embedded = embed_all(verbose=verbose).get("embedded", 0)
        except Exception as e:  # noqa: BLE001 — embedding is best-effort; FTS still works
            if verbose:
                print(f"[brain-ingest] embed skipped: "
                      f"{type(e).__name__}: {e}", file=sys.stderr)

    stats = {
        "files": len(walk_items),
        "upserted": upserted,
        "skipped_hash": skipped_hash,
        "refused": refused,
        "edges": edges_inserted,
        "embedded": embedded,
        "failures": failures,
        "elapsed_sec": round(time.time() - t0, 2),
        "finished_at": now_iso(),
    }
    if verbose:
        print(f"[brain-ingest] {stats}")
    return stats


def main():
    import argparse
    ap = argparse.ArgumentParser(
        description="Targeted incremental ingest into the Brain index."
    )
    ap.add_argument("paths", nargs="*", help="files (or dirs) to ingest now")
    ap.add_argument("--no-embed", action="store_true", help="skip the embedding pass")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--skips", type=int, nargs="?", const=20, metavar="N",
                    help="print a random sample of N skip-ledger rows and exit "
                         "(the false-rejection inspection surface)")
    args = ap.parse_args()

    if args.skips is not None:
        rows = sample_skips(args.skips)
        conn = _tracked_connect()
        try:
            total = conn.execute("SELECT COUNT(*) FROM admission_skips").fetchone()[0]
        except sqlite3.Error:
            total = 0
        finally:
            conn.close()
        print(f"[skip-ledger] {total} distinct paths refused; random sample of {len(rows)}:")
        for r in rows:
            print(f"  body={r['body_chars']:>5} min_df={r['min_df']:>6} "
                  f"×{r['times']}  {r['path']}")
            print(f"      reason: {r['reason']}")
        return

    if not args.paths:
        ap.error("at least one path is required (or use --skips)")
    stats = ingest_paths(args.paths, embed=not args.no_embed, verbose=not args.quiet)
    # Machine-readable last line for callers/logs (key=value, space-separated).
    print(" ".join(f"{k}={v}" for k, v in stats.items()))


if __name__ == "__main__":
    main()
