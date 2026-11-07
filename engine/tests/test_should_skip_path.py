"""Hermetic test for archive/backup skip robustness (no pytest).

Regression guard for the 2026-06 reorg case-sensitivity gap: SKIP_DIR_NAMES only
held the UPPER emoji form "99 🗄️ ARCHIVE", so the mixed-case folders the reorg
actually created ("99 🗄️ Archive", "Reorg_Backups", "_memory-pre-restructure_*")
slipped past should_skip_path and got indexed as status=active — dropping the
answer-quality top-1-canonical rate. resolver.ARCHIVE_MARKERS already flagged them
non-canonical, so the walker disagreed with the resolver. This locks them in sync.

Run:  python3 -m engine.tests.test_should_skip_path
"""
from __future__ import annotations

import sys
from pathlib import Path

from ..util import should_skip_path


def run() -> int:
    # --- must be SKIPPED (archive/backup copies, any case) ---
    skip = [
        "ALTAIR/99 🗄️ Archive/Reorg_Backups/_memory-pre-restructure_2026-06-09/project_voice-mode-architecture.md",
        "ALTAIR/99 🗄️ ARCHIVE/old.md",            # original UPPER form still skipped
        "COVENANT/99 🗄️ archive/x.md",            # lowercase emoji variant
        "LYRA/Reorg_Backups/snapshot.md",
        "IO/_memory-pre-restructure_2026-06-09/n.md",
        "HQ/99_ARCHIVE/legacy.md",                  # ascii archive still skipped
        "x/node_modules/pkg/index.js",              # unrelated existing skip still works
        # --- config.EXCLUDED_AGENTS, enforced at the chokepoint (Leg 4, 2026-08-05).
        # Regression guard for the landmine: the exclusion was declared in config and
        # read only by the REPORTING module, so 33 THERAPIST files indexed anyway and a
        # DB-only purge was undone by the very next nightly walk. Any agent directory
        # named in EXCLUDED_AGENTS must be skipped by the one function the walker, the
        # watcher, engine.ingest and the 7c stale sweep all share.
        "THERAPIST/02 🧠 Mental Health & Stabilization/coping.md",
        "01 🛎 LOBBY & DIRECTORY/0104_AGENT_IDENTITIES/THERAPIST/soul.md",
        "01_IDENTITY/0104_AGENT_IDENTITIES/THERAPIST/soul.md",
        "ATLAS/15. 🧬 Universe & Agentic HQ/SAMPLE/03 ⭐ Domain 2/x/🏠 home.md",
        "04 🛠️ SKILLS/agent_specialized/ARCTURUS_CEO/README.md",
    ]
    # --- must be KEPT (live canonical content) ---
    keep = [
        "ALTAIR/projects/voice-mode-architecture.md",
        "COVENANT/00_DOCTRINE/commandments.md",
        "LYRA/engine/walker.py",
        "IO/notes/archive-strategy.md",             # "archive" in a FILE name is fine
        # The other half of the EXCLUDED_AGENTS rule, and the reason it is a whole-
        # COMPONENT test and not a substring test: an excluded agent's NAME appearing in
        # another agent's filename is that other agent's document. Explicitly in scope to
        # keep (Leg 4 brief). A substring test would have de-indexed COVENANT's entire
        # conformance ledger and the Brain's own entity card.
        "COVENANT/07 🌡 Drift & Anomaly Detection/Ledger/2026-08-04_conformance-THERAPIST.md",
        "ARCTURION_Brain/entities/agents/THERAPIST.md",
        "11 📜 LOGS/1210_MIGRATIONS/inbox-repair-20260720/THERAPIST-inbox.md",
        "ARCTURUS/88 📦 Deliverables/x/ai-hermes-gateway-therapist.workflow.json",
        "ALTAIR/000 🤖 Altair/Memory/Decisions/therapist-excluded-agents-not-enforced.md",
    ]

    # The rule must read config, not a copy of it: if a key is ever added to or removed
    # from EXCLUDED_AGENTS, this assertion is what notices that the walker followed.
    from .. import config as C
    for agent in C.EXCLUDED_AGENTS:
        if not should_skip_path(Path(f"{agent}/anything.md")):
            print(f"FAIL — EXCLUDED_AGENTS key {agent!r} is declared but not skipped")
            return 1

    failures = []
    for p in skip:
        if not should_skip_path(Path(p)):
            failures.append(f"  EXPECTED SKIP but kept: {p}")
    for p in keep:
        if should_skip_path(Path(p)):
            failures.append(f"  EXPECTED KEEP but skipped: {p}")

    if failures:
        print("FAIL — should_skip_path archive robustness:")
        print("\n".join(failures))
        return 1

    print(f"PASS — should_skip_path: {len(skip)} archive/backup paths skipped "
          f"(case-insensitive), {len(keep)} live paths kept")
    return 0


if __name__ == "__main__":
    sys.exit(run())
