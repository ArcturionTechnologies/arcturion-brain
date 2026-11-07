from __future__ import annotations

import json
from pathlib import Path

from engine import brain
from engine import config as C
from engine.semantic_verde import verify_evidence


def test_durable_source_authority_is_bounded_and_reversible(monkeypatch) -> None:
    durable = {"path": "ALTAIR/000 Altair/Knowledge/Lessons/example.md"}
    session = {"path": "ALTAIR/000 Altair/Memory/Sessions/distilled/example.md"}
    monkeypatch.setattr(C, "DURABLE_SOURCE_AUTHORITY", True)
    monkeypatch.setattr(C, "DURABLE_SOURCE_BOOST", 4.0)
    assert brain._durable_source_score(durable) == 4.0
    assert brain._durable_source_score(session) == 0.0
    monkeypatch.setattr(C, "RECENCY_DURABLE_BOOST", 12.0)
    assert brain._recency_durable_source_score(durable, "what is current") == 12.0
    assert brain._recency_durable_source_score(durable, "what was historical") == 0.0
    monkeypatch.setattr(C, "DURABLE_SOURCE_AUTHORITY", False)
    assert brain._durable_source_score(durable) == 0.0
    assert brain._recency_durable_source_score(durable, "what is current") == 0.0


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_semantic_verde_evidence_contract(tmp_path: Path, monkeypatch) -> None:
    baseline = {
        "positive": {
            "recall5_rate": 0.477,
            "top1_rate": 0.318,
            "pass_rate": 0.477,
            "precision5_rate": 0.123,
            "mrr": 0.396,
            "superseded_hit_rate": 0.0,
            "wrong_context_rate": 0.877,
        },
        "guards": {"pass_rate": 0.462},
        "by_class": {"exact-token": {"recall5_rate": 0.909}},
        "_floors": {"paraphrase": 0.15, "common-token": 0.05, "exact-token": 0.5,
                    "recency": 0.25, "negative": 0.2, "contamination": 1.0},
    }
    monkeypatch.setattr("engine.semantic_verde.E.load_baseline", lambda: baseline)
    golden = {
        "positive": {
            "recall5_rate": 0.523,
            "top1_rate": 0.386,
            "pass_rate": 0.500,
            "precision5_rate": 0.132,
            "mrr": 0.439,
            "superseded_hit_rate": 0.0,
            "wrong_context_rate": 0.824,
        },
        "guards": {"pass_rate": 0.923},
        "by_class": {
            "paraphrase": {"recall5_rate": 0.364},
            "common-token": {"recall5_rate": 0.182},
            "exact-token": {"recall5_rate": 0.909},
            "recency": {"recall5_rate": 0.636},
            "negative": {"pass_rate": 0.909},
            "contamination": {"pass_rate": 1.0},
        },
    }
    heldout = {
        "positive": {"recall5_rate": 0.400, "top1_rate": 0.350},
        "guards": {"pass_rate": 0.500},
        "by_class": {"exact-token": {"recall5_rate": 1.0}},
    }
    probes = {"probes": [
        {"id": "known", "kind": "positive", "passed": True},
        {"id": "unknown", "kind": "low-confidence", "node_count": 0,
         "low_confidence": True},
    ]}
    latency = {"before": {"p50_ms": 320, "p95_ms": 400},
               "after": {"p50_ms": 321, "p95_ms": 401}}
    for name, value in (("golden-final.json", golden), ("heldout-final.json", heldout),
                        ("live-probes.json", probes), ("latency.json", latency)):
        _write_json(tmp_path / name, value)
    assert verify_evidence(tmp_path) == []

    heldout["positive"]["recall5_rate"] = 0.399
    _write_json(tmp_path / "heldout-final.json", heldout)
    assert any("held-out recall5_rate" in e for e in verify_evidence(tmp_path))
