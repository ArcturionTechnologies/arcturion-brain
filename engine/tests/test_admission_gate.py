"""Hermetic test for the Leg-4 admission gate + skip ledger (no pytest).

The gate (`engine.ingest.admit_node`) decides whether a walked file earns a row in
the index. It is ORed over three tests, and the ONLY interesting failure mode is a
false rejection — knowledge that silently never enters the shared brain. So most of
what is asserted here is what must still be ADMITTED:

  * a SHORT canonical Decision record (body well under the 200-char bar, saved by its
    `type:`/`owner:` frontmatter),
  * a short synthetic distilled session record,
  * a one-line note whose title carries a rare alias token,
  * anything at all when the gate's own machinery is broken (FAIL-OPEN).

Run:  python3 -m engine.tests.test_admission_gate
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

from .. import ingest as IN


def _node(path: Path, title: str, body: str) -> dict:
    return {"id": "n_" + title, "title": title, "path": str(path),
            "body_excerpt": body, "type": "Source", "status": "active"}


def _write(d: Path, name: str, text: str) -> Path:
    p = d / name
    p.write_text(text, encoding="utf-8")
    return p


class _NoFTSConn:
    """A connection whose FTS table does not exist — document_frequency returns -1."""

    def execute(self, *_a, **_k):
        raise sqlite3.OperationalError("no such table: fts_nodes")


class _RaisingConn:
    def execute(self, *_a, **_k):
        raise RuntimeError("gate machinery is broken")


def run() -> int:  # noqa: C901 — a checklist, deliberately flat
    failures: list[str] = []

    def check(name: str, got, want):
        if got != want:
            failures.append(f"  {name}: got {got!r}, want {want!r}")

    tmp = Path(tempfile.mkdtemp(prefix="brain-admit-"))
    db = tmp / "t.sqlite"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE VIRTUAL TABLE fts_nodes USING fts5(node_id, title, body)")
    # A corpus where 'launchd' is common and 'zorptastic' is a hapax legomenon.
    for i in range(200):
        conn.execute("INSERT INTO fts_nodes VALUES (?,?,?)",
                     (f"c{i}", "launchd note", "launchd plist scheduling"))
    conn.execute("INSERT INTO fts_nodes VALUES (?,?,?)",
                 ("rare", "zorptastic", "zorptastic alias"))
    conn.commit()

    # ── 1. BODY: ≥ ADMIT_MIN_BODY_CHARS admits on its own ───────────────────
    p = _write(tmp, "long.md", "x")
    ok, why, _ = IN.admit_node(conn, _node(p, "long", "y" * IN.ADMIT_MIN_BODY_CHARS), p)
    check("long body admitted", (ok, why), (True, "body"))

    ok, why, _ = IN.admit_node(
        conn, _node(p, "long", "y" * (IN.ADMIT_MIN_BODY_CHARS - 1)), p)
    check("one char under the bar does NOT admit on body", why != "body", True)

    # ── 2. FRONTMATTER: a SHORT canonical Decision record must survive ───────
    # This is the false-rejection case that matters most. 60 chars of body.
    dec = _write(tmp, "launchd-absolute-paths.md",
                 "---\ntype: decision\nowner: ALTAIR\nstatus: active\n---\n"
                 "Always use absolute paths in launchd plists.\n")
    ok, why, _ = IN.admit_node(conn, _node(dec, "launchd absolute paths",
                                           "Always use absolute paths in launchd plists."),
                               dec)
    check("short canonical Decision admitted", (ok, why), (True, "frontmatter"))

    # ── 3. FRONTMATTER: a short synthetic session record.
    sess = _write(tmp, "example-session.md",
                  '---\ntitle: "Example synthetic session"\n'
                  "type: session-log\nowner: ALTAIR\nsurface: claude\n"
                  "distill_quality: mechanical\n---\n\n## User Requests\n1. say hi\n")
    ok, why, _ = IN.admit_node(conn, _node(sess, "Session 2026-07-24 claude say hi",
                                           "## User Requests\n1. say hi\n"), sess)
    check("distilled session record admitted", (ok, why), (True, "frontmatter"))

    # Frontmatter that carries only decoration (title/tags/dates) is NOT structured —
    # that is the auto-generated hub-stub class the gate is meant to catch.
    hub = _write(tmp, "hub.md",
                 '---\ntitle: "Runbooks"\ntags: [index]\ncreated: 2026-06-10\n---\n'
                 "# Runbooks\n> see home\n")
    ok, _why, _ = IN.admit_node(conn, _node(hub, "launchd", "# Runbooks\n> see home\n"), hub)
    check("decoration-only frontmatter does not admit", ok, False)

    # ── 4. RARE TOKEN: a one-line note titled with an alias ──────────────────
    alias = _write(tmp, "zorptastic.md", "one line\n")
    ok, why, _ = IN.admit_node(conn, _node(alias, "zorptastic", "one line\n"), alias)
    check("rare-token title admitted", (ok, why), (True, "rare-token"))

    # ── 4b. df == 0 — THE RAREST A TOKEN CAN BE — must admit ────────────────
    # The boundary was `0 < df <= MAX`, which rejected exactly the case this test exists
    # for: a brand-new short note titled with a NOVEL alias, whose tokens appear in no
    # indexed document yet. The refusal was self-reinforcing (refused → never indexed →
    # still df 0 → refused again) and invisible to a dry run over an ALREADY-indexed
    # corpus, which is why the live skip ledger's minimum min_df is 62 and not 0.
    from ..rank_signals import document_frequency
    novel = _write(tmp, "quibbleflorp-vantablack.md", "one line\n")
    check("precondition: the novel token really is df=0",
          document_frequency(conn, "quibbleflorp"), 0)
    ok, why, facts = IN.admit_node(
        conn, _node(novel, "quibbleflorp vantablack", "one line\n"), novel)
    check("df=0 novel-alias note admitted", (ok, why), (True, "rare-token"))
    check("df=0 recorded as 0, not as unknown", facts.get("min_df"), 0)

    # The degenerate corpus: an EMPTY index. Every token is df=0 there, so the old
    # boundary refused every short frontmatter-less record a fresh brain ever saw.
    cold = sqlite3.connect(":memory:")
    cold.row_factory = sqlite3.Row
    cold.execute("CREATE VIRTUAL TABLE fts_nodes USING fts5(node_id, title, body)")
    ok, why, _ = IN.admit_node(cold, _node(novel, "brandnew note", "hi"), novel)
    check("cold/empty corpus admits a short note", (ok, why), (True, "rare-token"))
    cold.close()

    # ── 4c. Non-Latin titles must be tokenizable ────────────────────────────
    # `[a-z0-9]+` yielded NO candidate token for a CJK/Cyrillic/emoji title, so such a
    # note was refused for "no candidate token" however unique it actually was — an
    # ASCII bias in a gate that decides what knowledge exists. One such row was already
    # in the live skip ledger.
    cjk = _write(tmp, "議事録.md", "短い\n")
    check("CJK title yields candidate tokens",
          IN._admit_tokens({"title": "議事録"}, cjk) != [], True)
    # It is ADMITTED — which is the fix — but via `df-unavailable`, not `rare-token`, and
    # the distinction is worth asserting rather than glossing. `rank_signals`'
    # `document_frequency` sanitizes to `[a-z0-9]+` runs before it queries FTS, so a CJK
    # token reduces to nothing and the frequency is genuinely unmeasurable; the gate's
    # fail-open branch then admits. Widening that sanitizer is Leg 2's ranking path (it
    # also feeds the exact-token pin) and is deliberately NOT touched here. Net effect:
    # non-Latin-titled short records are admitted unconditionally, which is the safe
    # direction, and the reason string says so honestly instead of claiming a measurement.
    ok, why, _ = IN.admit_node(conn, _node(cjk, "議事録", "x\n"), cjk)
    check("CJK-titled short note is ADMITTED (was refused before)", ok, True)
    check("...and says why honestly", why, "df-unavailable")
    check("a purely-emoji title still yields nothing (correctly — no word characters)",
          IN._admit_tokens({"title": "\U0001F3E0 ⭐"}, None), [])

    # ...and the same note titled with a COMMON token is not rescued by rarity.
    common = _write(tmp, "launchd.md", "one line\n")
    ok, why, facts = IN.admit_node(conn, _node(common, "launchd", "one line\n"), common)
    check("common-token title refused", ok, False)
    check("refusal records the measured df", facts.get("min_df"), 200)
    check("refusal reason names all three tests",
          all(s in why for s in ("body=", "frontmatter", "df=")), True)

    # ── 5. FAIL-OPEN — a broken gate must never eat a record ─────────────────
    # Distinct, never-before-seen title tokens on purpose: rank_signals memoizes
    # document frequency by (db path, token), so reusing "launchd" here would answer
    # from cache and never reach the deliberately-broken connection.
    ok, why, _ = IN.admit_node(_NoFTSConn(),
                               _node(common, "snorkumbulate alpha", "x"), common)
    check("no FTS → admit, not reject", (ok, why), (True, "df-unavailable"))
    ok, why, _ = IN.admit_node(_RaisingConn(),
                               _node(common, "flimbertwidge bravo", "x"), common)
    check("gate exception → admit", (ok, why.startswith("gate-error:")), (True, True))

    # A record with no file behind it (harvested Session/Agent/Capability rows) is
    # never this gate's business.
    ok, why, _ = IN.admit_node(conn, {"title": "agent altair", "path": None}, None)
    check("non-file-backed admitted", (ok, why), (True, "not-file-backed"))

    # An unreadable file cannot be probed for frontmatter — that must not reject it
    # outright, only fall through to the rarity test.
    ok, _why, _ = IN.admit_node(conn, _node(tmp / "gone.md", "zorptastic", ""),
                                tmp / "gone.md")
    check("missing file still reachable by rare-token", ok, True)

    # ── 6. SKIP LEDGER — durable, upserting, sampleable ──────────────────────
    check("ensure_skip_ledger", IN.ensure_skip_ledger(conn), True)
    n = _node(common, "launchd", "one line\n")
    IN.record_skip(conn, n, "no-signal (test)", {"body_chars": 9, "min_df": 200})
    IN.record_skip(conn, n, "no-signal (test)", {"body_chars": 9, "min_df": 200})
    conn.commit()
    row = conn.execute("SELECT * FROM admission_skips WHERE path = ?",
                       (str(common),)).fetchone()
    check("ledger row exists", row is not None, True)
    if row is not None:
        check("ledger upserts on path (one row, times=2)", row["times"], 2)
        check("ledger records the evidence, not just a verdict",
              (row["body_chars"], row["min_df"]), (9, 200))
        check("ledger is timestamped", bool(row["first_seen"] and row["last_seen"]), True)
    check("one path → one row",
          conn.execute("SELECT COUNT(*) FROM admission_skips").fetchone()[0], 1)
    # Idempotent DDL — a second reindex must not blow up on the existing table.
    check("ensure_skip_ledger is idempotent", IN.ensure_skip_ledger(conn), True)

    conn.close()

    if failures:
        print("FAIL — admission gate:")
        print("\n".join(failures))
        return 1
    print("PASS — admission gate: body/frontmatter/rare-token admission, "
          "short-Decision + distilled-session false-rejection guards, fail-open on "
          "broken machinery, upserting skip ledger")
    return 0


if __name__ == "__main__":
    sys.exit(run())
