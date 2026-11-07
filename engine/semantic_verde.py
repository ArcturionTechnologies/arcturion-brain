"""Request-bound evidence gate for the 2026-09-05 semantic-verde repair.

This module never runs or reads the held-out fixture. It verifies already-produced,
request-fresh scorecards against thresholds fixed before implementation. That separation
keeps the final held-out read a measurement rather than a tuning input.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import retrieval_eval as E


HELDOUT_FLOORS = {"recall5_rate": 0.400, "top1_rate": 0.350, "guard_rate": 0.500}
LATENCY_SLO_MS = 1500.0
LATENCY_NOISE_BUDGET = 1.10


def _load(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def verify_evidence(root: Path) -> list[str]:
    """Return invariant violations; an empty list is semantic verde."""
    root = Path(root)
    golden = _load(root / "golden-final.json")
    heldout = _load(root / "heldout-final.json")
    probes = _load(root / "live-probes.json")
    latency = _load(root / "latency.json")
    baseline = E.load_baseline()
    errors: list[str] = []

    if not baseline:
        errors.append("golden pinned baseline is unavailable")
    else:
        for key in ("recall5_rate", "top1_rate", "pass_rate", "precision5_rate", "mrr"):
            got = float(golden["positive"][key])
            floor = float(baseline["positive"][key])
            if got < floor:
                errors.append(f"golden {key} {got:.3f} < pinned {floor:.3f}")
        for key in ("wrong_context_rate", "superseded_in_top5_rate"):
            got = golden["positive"].get(key)
            prior = baseline["positive"].get(key)
            if got is not None and prior is not None and float(got) > float(prior):
                errors.append(f"golden {key} {float(got):.3f} > pinned {float(prior):.3f}")
        got_guards = float(golden["guards"]["pass_rate"])
        base_guards = float(baseline["guards"]["pass_rate"])
        if got_guards < base_guards:
            errors.append(f"golden guards {got_guards:.3f} < pinned {base_guards:.3f}")
        floors = E.load_class_floors(baseline)
        for cls, floor in floors.items():
            block = golden["by_class"][cls]
            key = "recall5_rate" if cls in E.POSITIVE_CLASSES else "pass_rate"
            got = float(block[key])
            if got < floor:
                errors.append(f"golden {cls} {key} {got:.3f} < configured floor {floor:.3f}")
        exact_floor = float(baseline["by_class"]["exact-token"]["recall5_rate"])
        exact_now = float(golden["by_class"]["exact-token"]["recall5_rate"])
        if exact_now < exact_floor:
            errors.append(f"golden exact-token {exact_now:.3f} < pinned {exact_floor:.3f}")

    if float(golden["by_class"]["contamination"]["pass_rate"]) != 1.0:
        errors.append("golden contamination safeguard is not perfect")

    hpos = heldout["positive"]
    for key in ("recall5_rate", "top1_rate"):
        got = float(hpos[key])
        floor = HELDOUT_FLOORS[key]
        if got < floor:
            errors.append(f"held-out {key} {got:.3f} < fixed floor {floor:.3f}")
    hguards = float(heldout["guards"]["pass_rate"])
    if hguards < HELDOUT_FLOORS["guard_rate"]:
        errors.append(
            f"held-out guards {hguards:.3f} < fixed floor {HELDOUT_FLOORS['guard_rate']:.3f}")
    hexact = float(heldout["by_class"]["exact-token"]["recall5_rate"])
    if hexact < 1.0:
        errors.append(f"held-out exact-token {hexact:.3f} < fixed floor 1.000")

    for probe in probes.get("probes", []):
        if probe.get("kind") == "low-confidence" and (
                probe.get("node_count") != 0 or not probe.get("low_confidence")):
            errors.append(f"low-confidence probe returned unsupported context: {probe.get('id')}")
        if probe.get("kind") == "positive" and not probe.get("passed"):
            errors.append(f"positive live probe failed: {probe.get('id')}")

    before = latency["before"]
    after = latency["after"]
    for key in ("p50_ms", "p95_ms"):
        got = float(after[key])
        prior = float(before[key])
        if got > prior * LATENCY_NOISE_BUDGET:
            errors.append(
                f"warm {key} regressed {prior:.1f}ms -> {got:.1f}ms beyond 10% noise budget")
    if float(after["p95_ms"]) > LATENCY_SLO_MS:
        errors.append(
            f"warm p95 {float(after['p95_ms']):.1f}ms exceeds {LATENCY_SLO_MS:.0f}ms hook budget")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-evidence", type=Path, required=True)
    args = parser.parse_args(argv)
    errors = verify_evidence(args.verify_evidence)
    print(json.dumps({"ok": not errors, "errors": errors}, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
