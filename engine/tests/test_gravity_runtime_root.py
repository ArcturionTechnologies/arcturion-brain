"""Regression guard for the gravity runtime-root hardcode (Leg 1, 2026-08-04, no pytest).

`maintenance/gravity.py`'s `_classify_path` and `audit()` used to hardcode the
External Runtime prong as `C.DESKTOP_ROOT / "ARCTURION_EXTERNAL_RUNTIME"`, ignoring
the `ARCTURION_RUNTIME_ROOT` env override that `C.RUNTIME_ROOT` already honors (and
that every other prong root — C.ATLAS_ROOT etc — is read from directly). This locks
in that both call sites now read C.RUNTIME_ROOT, so a Founder machine or CI box that
overrides ARCTURION_RUNTIME_ROOT classifies correctly instead of silently falling
back to the hardcoded Desktop path.

Run:  python3 -m engine.tests.test_gravity_runtime_root
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from .. import config as C
from .. import db as DB
from ..maintenance import gravity as G


def run() -> int:
    failures = []
    real_runtime_root = C.RUNTIME_ROOT
    fake_runtime_root = Path("/tmp/fake_runtime")

    try:
        # --- 1. default (unmodified) RUNTIME_ROOT still classifies its own tree ---
        default_path = str(real_runtime_root / "SERVICE_CATALOG.md")
        if G._classify_path(default_path) != "RUNTIME":
            failures.append(f"default RUNTIME_ROOT path misclassified: {G._classify_path(default_path)}")

        # bare relative-prefix form (e.g. as stored on a node) still matches, derived
        # from RUNTIME_ROOT.name rather than a hardcoded literal.
        bare = f"{real_runtime_root.name}/README.md"
        if G._classify_path(bare) != "RUNTIME":
            failures.append(f"bare relative-prefix path misclassified: {G._classify_path(bare)}")

        # --- 2. simulate ARCTURION_RUNTIME_ROOT override by patching C.RUNTIME_ROOT ---
        C.RUNTIME_ROOT = fake_runtime_root
        try:
            overridden_path = str(fake_runtime_root / "SERVICE_CATALOG.md")
            if G._classify_path(overridden_path) != "RUNTIME":
                failures.append(
                    f"override RUNTIME_ROOT path NOT honored: {G._classify_path(overridden_path)}"
                )

            # the OLD hardcoded location (DESKTOP_ROOT/ARCTURION_EXTERNAL_RUNTIME) must no
            # longer be special-cased once the override is active and doesn't share that name.
            old_hardcoded_path = str(C.DESKTOP_ROOT / "ARCTURION_EXTERNAL_RUNTIME" / "SERVICE_CATALOG.md")
            classified = G._classify_path(old_hardcoded_path)
            if classified == "RUNTIME":
                failures.append(
                    "old hardcoded Desktop/ARCTURION_EXTERNAL_RUNTIME path still classifies as "
                    "RUNTIME after override — fix did not remove the hardcode"
                )

            # audit()'s manifest path must honor the override too.
            tmp = Path(tempfile.mkdtemp(prefix="gravity_runtime_root_test_"))
            try:
                db = tmp / "brain.sqlite"
                conn = DB.connect(db)
                DB.init_db(conn)
                conn.commit()
                conn.close()
                report = G.audit(db_path=db, desktop_root=tmp, with_answer_quality=False)
                manifest = report["runtime"]["boundary_manifest"]
                if not manifest.startswith(str(fake_runtime_root)):
                    failures.append(f"audit() manifest path did not honor override: {manifest}")
            finally:
                import shutil
                shutil.rmtree(tmp, ignore_errors=True)
        finally:
            C.RUNTIME_ROOT = real_runtime_root
    finally:
        C.RUNTIME_ROOT = real_runtime_root

    if failures:
        print("FAIL — gravity runtime-root hardcode regression:")
        print("\n".join(f"  {f}" for f in failures))
        return 1

    print("PASS — gravity runtime-root: _classify_path and audit() both honor "
          "C.RUNTIME_ROOT / ARCTURION_RUNTIME_ROOT override")
    return 0


if __name__ == "__main__":
    sys.exit(run())
