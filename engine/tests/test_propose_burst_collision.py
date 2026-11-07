"""Regression: two proposals from the SAME source in the SAME second must each
survive as a distinct staged file — no silent overwrite.

Root cause guarded (2026-06-29): _write_proposal derived the staged filename hash
from only `f"{source}:{ts}:{len(proposals)}"` where `ts` is second-resolution
(`%Y%m%dT%H%M%SZ`). Two single-proposal stages from the same agent within the same
wall-clock second therefore produced an IDENTICAL `{ts}_{source}_{h}.jsonl` name, and
the second write silently clobbered the first — data loss for the COVENANT review
queue under burst proposals (a harvester or an agent staging several nodes rapidly).

Guard: with the clock frozen to a single second, staging two DISTINCT proposals from
the same source yields two distinct files on disk, and BOTH proposal_ids are present.

Run:  python3 -m engine.tests.test_propose_burst_collision
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .. import config as C
from .. import mcp_server as M


class _FrozenClock:
    """Stand-in for mcp_server.datetime whose now() always returns the same second."""
    _FROZEN = datetime(2026, 6, 29, 12, 0, 0, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls._FROZEN


def run() -> int:
    orig_staging = M.STAGING_DIR
    orig_inbox = C.COVENANT_INBOX
    orig_datetime = M.datetime
    tmp = Path(tempfile.mkdtemp(prefix="brain_burst_test_"))
    try:
        M.STAGING_DIR = tmp / "staging" / "proposals"
        C.COVENANT_INBOX = tmp / "inbox.md"
        M.datetime = _FrozenClock  # freeze second-resolution timestamp → force collision path

        base = dict(node_type="concept", family="engineering",
                    body="benign body", asserted_by="ALTAIR", governance_tier=0)

        r1 = M._handle_propose_node(
            dict(base, title="burst-probe-1", summary="first burst proposal"))
        r2 = M._handle_propose_node(
            dict(base, title="burst-probe-2", summary="second burst proposal"))

        assert "error" not in r1, f"first proposal errored: {r1}"
        assert "error" not in r2, f"second proposal errored: {r2}"
        assert r1["proposal_id"] != r2["proposal_id"], \
            f"probe invalid — proposal_ids collided: {r1} {r2}"

        files = sorted(M.STAGING_DIR.glob("*.jsonl"))
        assert len(files) == 2, (
            f"expected 2 distinct staged files for two same-second same-source "
            f"proposals, got {len(files)}: {[f.name for f in files]} — the second "
            f"proposal silently overwrote the first")

        staged_ids = set()
        for f in files:
            for line in f.read_text(encoding="utf-8").splitlines():
                staged_ids.add(json.loads(line)["proposal_id"])
        assert {r1["proposal_id"], r2["proposal_id"]} <= staged_ids, \
            f"both proposals must survive on disk; found {staged_ids}"

        print("PASS — same-second same-source proposals stage to distinct files; "
              "no silent overwrite")
        return 0
    finally:
        M.STAGING_DIR = orig_staging
        C.COVENANT_INBOX = orig_inbox
        M.datetime = orig_datetime
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(run())
