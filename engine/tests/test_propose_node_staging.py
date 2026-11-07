"""Regression: brain_propose_node must stage into the canonical, registry-resolved
brain staging dir (BRAIN_ROOT/staging/proposals) — NOT a hardcoded UNIVERSE_ROOT shelf
that can dangle.

Root cause guarded (2026-06-28): the 2026-06-17 restructure hardcoded
    STAGING_DIR = UNIVERSE_ROOT / "06_MEMORY_SYSTEM" / "0611_LLM_WIKI" / "staging" / "proposals"
abandoning the canonical BRAIN_ROOT/staging/proposals (where all 29 historical proposals
live + COVENANT reviews). When the HQ compat-layer later (re)created `06_MEMORY_SYSTEM` as a
*dangling* symlink (-> a nonexistent emoji shelf), `mkdir(parents=True, exist_ok=True)` raised
`[Errno 17] File exists` — a dangling symlink "exists" (EEXIST) but `is_dir()` is False, so
`exist_ok=True` cannot suppress it — and EVERY node/edge proposal aborted universe-wide.

Guards:
  1. config.STAGING_DIR resolves UNDER BRAIN_ROOT and never through the `06_MEMORY_SYSTEM`
     shelf — the hardcoding that broke it.
  2. _handle_propose_node succeeds and writes the proposal jsonl even when the staging dir
     ALREADY EXISTS (the exact idempotent case the fix must keep working).
  3. a SECOND proposal (dir now populated) also succeeds — repeat-idempotent.
  4. the returned staged_path is a clean relative display string (no ValueError from staging
     living outside UNIVERSE_ROOT).

Run:  python3 -m engine.tests.test_propose_node_staging
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from .. import config as C
from .. import mcp_server as M


def run() -> int:
    # --- Guard 1: production default resolves under BRAIN_ROOT, not the broken shelf ---
    assert "06_MEMORY_SYSTEM" not in str(C.STAGING_DIR), \
        f"STAGING_DIR must not hardcode the 06_MEMORY_SYSTEM shelf: {C.STAGING_DIR}"
    assert str(C.STAGING_DIR).startswith(str(C.BRAIN_ROOT)), \
        f"STAGING_DIR must live under BRAIN_ROOT ({C.BRAIN_ROOT}): {C.STAGING_DIR}"
    assert M.STAGING_DIR == C.STAGING_DIR, \
        "mcp_server.STAGING_DIR must follow config.STAGING_DIR (no second hardcoding)"

    # --- Guards 2-4: hermetic functional proposal into a temp staging dir ---
    orig_staging = M.STAGING_DIR
    orig_inbox = C.COVENANT_INBOX
    tmp = Path(tempfile.mkdtemp(prefix="brain_staging_test_"))
    try:
        M.STAGING_DIR = tmp / "staging" / "proposals"
        C.COVENANT_INBOX = tmp / "inbox.md"

        # Guard 2: staging dir ALREADY EXISTS before the first proposal — must still succeed.
        M.STAGING_DIR.mkdir(parents=True, exist_ok=True)

        args = dict(node_type="concept", family="engineering",
                    title="staging-regression-probe-1", summary="hermetic regression probe",
                    body="benign body", asserted_by="ALTAIR", governance_tier=0)
        r1 = M._handle_propose_node(args)
        assert "error" not in r1, f"first proposal errored: {r1}"
        assert r1.get("proposal_id"), f"missing proposal_id: {r1}"
        assert isinstance(r1.get("staged_path"), str) and r1["staged_path"], \
            f"staged_path must be a non-empty display string: {r1}"
        files1 = sorted(M.STAGING_DIR.glob("*.jsonl"))
        assert len(files1) == 1, f"expected 1 staged file, got {files1}"
        # the jsonl is real, parseable, and carries the proposal
        rec = json.loads(files1[0].read_text(encoding="utf-8").splitlines()[0])
        assert rec["proposal_id"] == r1["proposal_id"] and rec["kind"] == "node"

        # Guard 3: a SECOND proposal (dir now populated) also succeeds — repeat-idempotent.
        # _write_proposal folds each proposal's proposal_id into the filename hash (fixed
        # 2026-06-29; see test_propose_burst_collision.py), so two distinct same-second
        # same-source proposals are guaranteed distinct filenames — no clobber. We therefore
        # assert BOTH a clean two-file landing AND that each id is on disk exactly once.
        args2 = dict(args, title="staging-regression-probe-2", summary="second probe")
        r2 = M._handle_propose_node(args2)
        assert "error" not in r2, f"second proposal errored: {r2}"
        assert r2.get("proposal_id"), f"second proposal missing proposal_id: {r2}"
        files2 = sorted(M.STAGING_DIR.glob("*.jsonl"))
        assert len(files2) == 2, \
            f"expected 2 distinct staged files after two proposals, got {files2}"
        staged_ids = [
            json.loads(line)["proposal_id"]
            for f in files2
            for line in f.read_text(encoding="utf-8").splitlines()
        ]
        assert set(staged_ids) == {r1["proposal_id"], r2["proposal_id"]}, \
            f"both proposal ids must be on disk exactly: {staged_ids}"

        # COVENANT queue was appended (created on demand)
        assert C.COVENANT_INBOX.exists(), "COVENANT inbox should be created/appended"

        print("PASS — propose_node stages under BRAIN_ROOT, idempotent when staging dir "
              "pre-exists, repeat-safe, clean staged_path")
        return 0
    finally:
        M.STAGING_DIR = orig_staging
        C.COVENANT_INBOX = orig_inbox
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(run())
