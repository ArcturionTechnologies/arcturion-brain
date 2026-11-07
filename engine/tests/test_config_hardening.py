"""Regression guard for four config.py hardening fixes (Leg 1, 2026-08-04, no pytest).

Covers:
  Finding 2 — `_reg()` resolves the canonical External Runtime registry without
              mutating sys.path; a missing registry falls back with a visible warning.
  Finding 3 — `prong_for_path()` classification does not depend on process cwd.
  Finding 4 — malformed int env vars fall back to the default (with a stderr warning)
              instead of crashing every consumer at import time.
  Finding 5 — CONSTELLATION_AGENTS / ACTIVE_CONSTELLATION_AGENTS drift fails loudly.

Run:  python3 -m engine.tests.test_config_hardening
"""
from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stderr
from pathlib import Path

from .. import config as C


def _check_reg_no_sys_path_pollution(failures: list[str]) -> None:
    before = list(sys.path)
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        result = C._reg("BRAIN_ROOT", Path("/some/fallback"))
    after = list(sys.path)

    if after != before:
        failures.append(f"_reg() mutated sys.path: added {[p for p in after if p not in before]}")

    pr = str(C.PATH_REGISTRY_ROOT)
    if pr in after:
        failures.append(f"_reg() left the path_registry dir on sys.path: {pr}")

    expected = C.DESKTOP_ROOT / "ARCTURION_Brain"
    normalized_result = Path(os.path.normpath(result))
    if normalized_result != expected:
        failures.append(
            f"_reg() did not resolve the canonical registry: {normalized_result} != {expected}"
        )
    if stderr.getvalue().strip():
        failures.append(f"canonical registry resolution warned unexpectedly: {stderr.getvalue()}")

    original_root = C.PATH_REGISTRY_ROOT
    missing = Path("/definitely/missing/arcturion-path-registry")
    fallback_stderr = io.StringIO()
    try:
        C.PATH_REGISTRY_ROOT = missing
        with redirect_stderr(fallback_stderr):
            fallback_result = C._reg("BRAIN_ROOT", Path("/some/fallback"))
    finally:
        C.PATH_REGISTRY_ROOT = original_root
    if fallback_result != Path("/some/fallback"):
        failures.append(f"_reg() did not use the explicit fallback: {fallback_result}")
    if not fallback_stderr.getvalue().strip():
        failures.append("_reg() missing-registry fallback did not warn to stderr")


def _check_prong_for_path_cwd_independent(failures: list[str]) -> None:
    cwd_before = os.getcwd()
    try:
        # Relative "already-relative universe path" — must resolve the same regardless
        # of process cwd (the launchd cwd="/" case from the finding).
        rel = f"{C.RUNTIME_ROOT.name}/README.md"
        results = set()
        for chdir_to in (str(Path.home()), "/"):
            try:
                os.chdir(chdir_to)
            except OSError:
                continue
            results.add(C.prong_for_path(rel))
        if len(results) != 1:
            failures.append(f"prong_for_path(relative) varies by cwd: {results}")
        if results and next(iter(results)) != "runtime":
            failures.append(f"prong_for_path({rel!r}) expected 'runtime', got {results}")

        # Absolute path classification must also be stable across cwd changes.
        abs_path = str(C.ATLAS_ROOT / "foo.md")
        abs_results = set()
        for chdir_to in (str(Path.home()), "/"):
            try:
                os.chdir(chdir_to)
            except OSError:
                continue
            abs_results.add(C.prong_for_path(abs_path))
        if len(abs_results) != 1 or next(iter(abs_results)) != "atlas":
            failures.append(f"prong_for_path(absolute) unstable/wrong across cwd: {abs_results}")
    finally:
        os.chdir(cwd_before)


def _check_int_env_helper(failures: list[str]) -> None:
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        val = C._int_env("ARCTURION_TEST_DOES_NOT_EXIST_MALFORMED", 42)
    if val != 42:
        failures.append(f"_int_env() with unset var should return default 42, got {val}")

    key = "ARCTURION_TEST_MALFORMED_INT"
    os.environ[key] = "not-an-int"
    try:
        stderr2 = io.StringIO()
        with redirect_stderr(stderr2):
            val2 = C._int_env(key, 99)
        if val2 != 99:
            failures.append(f"_int_env() with malformed value should fall back to 99, got {val2}")
        if not stderr2.getvalue().strip():
            failures.append("_int_env() malformed value did not warn to stderr")
    finally:
        del os.environ[key]

    key2 = "ARCTURION_TEST_VALID_INT"
    os.environ[key2] = "12345"
    try:
        val3 = C._int_env(key2, 1)
        if val3 != 12345:
            failures.append(f"_int_env() with valid value should return 12345, got {val3}")
    finally:
        del os.environ[key2]

    # The actual module-level constants must have imported successfully as ints —
    # proof the crash-at-import-on-malformed-env bug is fixed for real consumers.
    for name in ("SQLITE_MMAP_BYTES", "SQLITE_CACHE_KIB",
                 "EXPAND_MAX_EDGES_PER_HOP", "EXPAND_MAX_REACHED"):
        if not isinstance(getattr(C, name), int):
            failures.append(f"C.{name} is not an int: {getattr(C, name)!r}")


def _check_agents_drift_assertion(failures: list[str]) -> None:
    # Real config values must currently be in sync (no exception at import — config
    # already imported cleanly above, but re-run the check function directly too).
    try:
        C._assert_agents_in_sync(C.CONSTELLATION_AGENTS, C.ACTIVE_CONSTELLATION_AGENTS)
    except AssertionError as e:
        failures.append(f"real CONSTELLATION_AGENTS/ACTIVE_CONSTELLATION_AGENTS drifted: {e}")

    # Member values must be preserved exactly (order + contents).
    if list(C.CONSTELLATION_AGENTS) != [
        "ALTAIR", "ARCTURUS", "CASSIOPEIA", "COVENANT", "IO", "LILITH", "LYRA",
        "NEMESIS", "ORION", "POLARIS", "SIRIUS", "VEGA",
    ]:
        failures.append(f"CONSTELLATION_AGENTS members changed: {C.CONSTELLATION_AGENTS}")
    if tuple(C.ACTIVE_CONSTELLATION_AGENTS) != (
        "ALTAIR", "ARCTURUS", "CASSIOPEIA", "COVENANT", "IO", "LILITH",
        "LYRA", "NEMESIS", "ORION", "POLARIS", "SIRIUS", "VEGA",
    ):
        failures.append(f"ACTIVE_CONSTELLATION_AGENTS members changed: {C.ACTIVE_CONSTELLATION_AGENTS}")

    # A simulated drift (phantom CODEX re-added to only one list) must raise loudly.
    drifted_all = list(C.CONSTELLATION_AGENTS) + ["CODEX"]
    try:
        C._assert_agents_in_sync(drifted_all, C.ACTIVE_CONSTELLATION_AGENTS)
        failures.append("_assert_agents_in_sync() did not raise on a simulated CODEX drift")
    except AssertionError:
        pass


def run() -> int:
    failures: list[str] = []
    _check_reg_no_sys_path_pollution(failures)
    _check_prong_for_path_cwd_independent(failures)
    _check_int_env_helper(failures)
    _check_agents_drift_assertion(failures)

    if failures:
        print("FAIL — config hardening regression:")
        print("\n".join(f"  {f}" for f in failures))
        return 1

    print("PASS — config hardening: canonical registry resolution + fallback logs, "
          "prong_for_path() cwd-independent, malformed int env falls back safely, "
          "CONSTELLATION_AGENTS drift assertion fires")
    return 0


if __name__ == "__main__":
    sys.exit(run())
