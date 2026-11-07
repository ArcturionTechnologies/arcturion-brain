from __future__ import annotations

import importlib.util
import plistlib
import hashlib
import json
import sqlite3
import sys
import tempfile
import math
from datetime import datetime, timezone
from pathlib import Path

import pytest

BRAIN = Path(__file__).resolve().parents[2]
TEST_HOME = Path(__file__).resolve().parent
# Staged review layout keeps this test beside `engine/`; installed layout keeps it
# in `engine/tests/`. Exercise the same source files in both locations.
HERE = TEST_HOME / "engine" if (TEST_HOME / "engine").is_dir() else TEST_HOME.parent
sys.path.insert(0, str(BRAIN))

import engine  # noqa: E402
import engine.config  # noqa: E402
import engine.validator  # noqa: E402


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


DB = load("engine.db", HERE / "db.py")
EVAL = load("engine.retrieval_eval_review", HERE / "retrieval_eval.py")
COVERAGE = load("engine.coverage_check_review", HERE / "coverage_check.py")
MARKDOWN = load("engine.extractors.markdown_review", HERE / "extractors" / "markdown.py")
CONFIG = load("engine.extractors.config_file_review", HERE / "extractors" / "config_file.py")
PLIST = load("engine.extractors.plist_review", HERE / "extractors" / "plist.py")
APPS = load("engine.harvesters.applications_review", HERE / "harvesters" / "applications.py")
RUN_ALL = load("engine.harvesters.run_all_review", HERE / "harvesters" / "run_all.py")
EVENTS = load("engine.harvesters.events_review", HERE / "harvesters" / "events.py")
INGEST = load("engine.ingest_review", HERE / "ingest.py")
INVENTORY = load("engine.operating_inventory_review", HERE / "operating_inventory.py")




def result(recall=.50, top1=.36, guards=.92, paraphrase=.27, common=.18,
           exact=.91, recency=.64, negative=.91, contamination=1.0):
    return {
        "positive": {"recall5_rate": recall, "top1_rate": top1},
        "guards": {"pass_rate": guards},
        "by_class": {
            "paraphrase": {"recall5_rate": paraphrase},
            "common-token": {"recall5_rate": common},
            "exact-token": {"recall5_rate": exact},
            "recency": {"recall5_rate": recency},
            "negative": {"pass_rate": negative},
            "contamination": {"pass_rate": contamination},
        },
    }


def test_useful_quality_is_separate_from_regression() -> None:
    status, gaps = EVAL.quality_verdict(result())
    assert status == EVAL.FAIL
    assert any("positive.recall5_rate=0.500" in gap for gap in gaps)

    strong = result(recall=.75, top1=.60, guards=.95, paraphrase=.60, common=.50,
                    exact=.95, recency=.75, negative=.90)
    assert EVAL.quality_verdict(strong) == (EVAL.PASS, [])


def test_representative_quality_fixture_is_disjoint_and_pinned() -> None:
    fixture = HERE / "tests" / "fixtures" / "representative_quality.jsonl"
    policy_path = HERE / "tests" / "fixtures" / "representative_quality_policy.json"
    policy = json.loads(policy_path.read_text())
    assert hashlib.sha256(fixture.read_bytes()).hexdigest() == policy["fixture_sha256"]
    assert "development diagnostic" in policy["purpose"]
    assert policy["independent_confirmation"].startswith("heldout.jsonl")
    representative = EVAL.load_golden(fixture)
    golden = EVAL.load_golden(BRAIN / "engine" / "tests" / "fixtures" / "golden.jsonl")
    heldout = EVAL.load_golden(BRAIN / "engine" / "tests" / "fixtures" / "heldout.jsonl")
    assert {e["q"] for e in representative}.isdisjoint({e["q"] for e in golden + heldout})
    assert {e["id"] for e in representative}.isdisjoint({e["id"] for e in golden + heldout})


def test_quality_missing_metric_fails_closed() -> None:
    status, gaps = EVAL.quality_verdict({"positive": {}})
    assert status == EVAL.FAIL
    assert any("QUALITY METRIC INVALID" in gap for gap in gaps)
    for bad in (True, float("nan"), float("inf"), -0.1, 1.1):
        candidate = result(recall=bad)
        assert EVAL.quality_verdict(candidate)[0] == EVAL.FAIL


def test_freshness_rejects_naive_and_future_timestamps() -> None:
    now = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)
    assert COVERAGE._age_hours("2026-09-09T10:00:00", now) is None
    assert COVERAGE._age_hours("2026-09-09T13:00:00+00:00", now) is None


def memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    DB.init_db(conn)
    return conn


def source_node(node_id="config", path: str | None = None) -> dict:
    return {
        "id": node_id, "type": "Config", "family": "code", "title": node_id,
        "path": path or f"ALTAIR/{node_id}.json", "owner": "ALTAIR", "status": "active",
        "governance_tier": 0, "content_hash": "abc", "summary": "fixture",
        "body_excerpt": "fixture", "centrality": 0.0, "created": None,
        "last_modified": "2026-09-09T10:00:00+00:00", "last_reviewed": None,
        "metadata": {},
    }


def test_declared_endpoint_is_materialized_before_edge() -> None:
    conn = memory_db()
    DB.upsert_node(conn, source_node())
    edge = {
        "from": "config", "to": "service_scan_router", "predicate": "configures",
        "family": "code", "asserted_by": "STATIC_NAME",
        "asserted_at": "2026-09-09T10:00:00+00:00", "evidence_span": "filename-pattern",
        "metadata": {"endpoint_node": {"id": "service_scan_router", "type": "Service",
                                          "family": "application", "title": "scan router"}},
    }
    assert DB.insert_edge(conn, edge)
    endpoint = conn.execute(
        "SELECT type,status FROM nodes WHERE id='service_scan_router'"
    ).fetchone()
    assert endpoint["type"] == "Service"
    assert endpoint["status"] == "configured"
    assert DB.edge_health(conn)["edges_dangling"] == 0


def test_prose_endpoint_is_quarantined_with_original_payload() -> None:
    conn = memory_db()
    DB.upsert_node(conn, source_node())
    edge = {
        "from": "config", "to": "Cards 1 ✅. Independent of Card 0",
        "predicate": "depends_on", "family": "reasoning", "asserted_by": "STATIC_FRONTMATTER",
        "metadata": {"invalid_endpoint_reason": "prose_frontmatter_reference"},
    }
    assert not DB.insert_edge(conn, edge)
    row = conn.execute("SELECT reason,payload_json FROM edge_admission_quarantine").fetchone()
    assert row["reason"] == "prose_frontmatter_reference"
    assert "Cards 1" in row["payload_json"]


def test_extractors_declare_or_quarantine_endpoints(tmp_path: Path) -> None:
    cfg = tmp_path / "scan_router_config.json"
    cfg.write_text("{}")
    _node, edges = CONFIG.extract_config(cfg)
    assert edges[0]["to"] == "service_scan_router"
    assert edges[0]["metadata"]["endpoint_node"]["id"] == "service_scan_router"

    note = tmp_path / "card.md"
    note.write_text("---\ntitle: Card\ndepends_on: Cards 1 ✅. Independent of Card 0\n---\nBody")
    _node, edges = MARKDOWN.extract_markdown(note)
    assert edges[0]["metadata"]["invalid_endpoint_reason"] == "prose_frontmatter_reference"


def test_launchd_manifest_materializes_walkable_service_program_chain(tmp_path: Path) -> None:
    script = tmp_path / "brain_worker.py"
    script.write_text("print('ok')\n")
    state_db = tmp_path / "state.sqlite"
    manifest = tmp_path / "com.arcturion.brain-worker.plist"
    manifest.write_bytes(plistlib.dumps({
        "Label": "com.arcturion.brain-worker",
        "ProgramArguments": ["/usr/bin/python3", str(script), "--state", str(state_db)],
    }))
    node, edges = PLIST.extract_plist(manifest)
    assert node["status"] == "configured"
    conn = memory_db()
    DB.upsert_node(conn, node)
    assert all(DB.insert_edge(conn, edge) for edge in edges)
    service = "service_com_arcturion_brain_worker"
    assert conn.execute("SELECT status FROM nodes WHERE id=?", (service,)).fetchone()[0] == "configured"
    assert conn.execute(
        "SELECT 1 FROM edges WHERE from_id=? AND predicate='executes'", (service,)
    ).fetchone()
    assert conn.execute(
        """SELECT 1 FROM edges e JOIN nodes n ON n.id=e.to_id
            WHERE e.from_id=? AND e.predicate='executes' AND n.type='Executable'""",
        (service,),
    ).fetchone()
    assert not conn.execute("SELECT 1 FROM nodes WHERE path=?", (str(state_db),)).fetchone()


def test_launchd_shell_grammar_materializes_program_and_script_only(tmp_path: Path) -> None:
    script = tmp_path / "broker.py"
    script.write_text("print('ok')\n")
    state_db = tmp_path / "state.sqlite"
    manifest = tmp_path / "com.arcturion.broker.plist"
    manifest.write_bytes(plistlib.dumps({
        "Label": "com.arcturion.broker",
        "ProgramArguments": [
            "/bin/bash", "-lc",
            f"exec /usr/bin/python3 {script} --state {state_db}",
        ],
    }))
    _node, edges = PLIST.extract_plist(manifest)
    paths = {
        edge["metadata"]["endpoint_node"]["path"]
        for edge in edges if edge["predicate"] == "executes"
    }
    assert paths == {"/bin/bash", "/usr/bin/python3", str(script)}
    assert str(state_db) not in paths


def test_plist_rejects_non_script_interpreter_forms_and_marks_invalid(tmp_path: Path) -> None:
    cases = [
        ["/usr/bin/python3", "-c", "print(1)", "/tmp/data"],
        ["/usr/bin/python3", "-m", "module", "/tmp/data"],
        ["/bin/bash", "-c", "exec /usr/bin/python3 -c 'print(1)' /tmp/data"],
        ["/usr/bin/python3", "$HOME/direct.py"],
    ]
    for i, argv in enumerate(cases):
        manifest = tmp_path / f"case-{i}.plist"
        manifest.write_bytes(plistlib.dumps({"Label": f"case-{i}", "ProgramArguments": argv}))
        _node, edges = PLIST.extract_plist(manifest)
        paths = {e["metadata"]["endpoint_node"]["path"] for e in edges
                 if e["predicate"] == "executes"}
        assert "/tmp/data" not in paths
        assert str(Path.home() / "direct.py") not in paths

    invalid = tmp_path / "invalid.plist"
    invalid.write_text("not a plist")
    node, edges = PLIST.extract_plist(invalid)
    assert node["status"] == "error"
    assert node["metadata"]["extract_error"].startswith("parse:")
    assert edges == []


def test_invalid_plist_records_failed_source_watermark(tmp_path: Path, monkeypatch) -> None:
    desktop = tmp_path / "Desktop"
    source = desktop / "ALTAIR" / "Knowledge" / "invalid.plist"
    source.parent.mkdir(parents=True)
    source.write_text("not a plist")
    db_path = tmp_path / "brain.sqlite"
    original_connect = DB.connect
    monkeypatch.setattr(INGEST, "DB", DB)
    monkeypatch.setattr(INGEST.C, "DESKTOP_ROOT", desktop)
    monkeypatch.setattr(INGEST.walker, "walk_paths",
                        lambda _paths: iter([(source, "automation", "Automation")]))
    monkeypatch.setattr(INGEST.DB, "connect", lambda _path=None: original_connect(db_path))
    monkeypatch.setattr(INGEST, "route", lambda *_args: PLIST.extract_plist(source))
    assert INGEST.ingest_paths([source], embed=False)["failures"] == 1
    conn = original_connect(db_path)
    row = conn.execute("SELECT * FROM source_ingest_watermarks").fetchone()
    assert row["status"] == "error"
    assert row["last_error"].startswith("parse:")
    assert conn.execute("SELECT status FROM nodes").fetchone()[0] == "error"


def test_application_runtime_anchors_exist() -> None:
    conn = memory_db()
    assert APPS.ensure_runtime_anchors(conn, "2026-09-09T10:00:00+00:00") == 3
    ids = {r[0] for r in conn.execute("SELECT id FROM nodes")}
    assert {"hw_macbook_m5", "tool_homebrew", "tool_claude_code"} <= ids
    assert {r[0] for r in conn.execute("SELECT DISTINCT status FROM nodes")} == {"configured"}


def test_application_runtime_anchor_preserves_observed_state() -> None:
    conn = memory_db()
    observed = source_node("hw_macbook_m5")
    observed.update({"type": "Hardware", "family": "hardware", "status": "active",
                     "metadata": {"runtime_observed": True, "source": "live_probe"}})
    DB.upsert_node(conn, observed)
    assert APPS.ensure_runtime_anchors(conn, "2026-09-09T10:00:00+00:00") == 2
    row = conn.execute("SELECT status, metadata FROM nodes WHERE id='hw_macbook_m5'").fetchone()
    assert row["status"] == "active"
    assert json.loads(row["metadata"])["runtime_observed"] is True


def test_harvester_watermark_ignores_elapsed_time() -> None:
    a = {"family": "application", "nodes": 3, "edges": 2,
         "source_digest": "a" * 64, "elapsed_sec": 1.2}
    b = {**a, "elapsed_sec": 99.9}
    assert RUN_ALL.result_watermark(a) == RUN_ALL.result_watermark(b)
    assert RUN_ALL.result_watermark(a) == RUN_ALL.result_watermark({**a, "nodes": 4})
    assert RUN_ALL.result_watermark({"nodes": 4}) == ""
    assert RUN_ALL.result_watermark({**a, "source_digest": "b" * 64}) != RUN_ALL.result_watermark(a)


def test_run_all_fails_if_failure_watermark_cannot_persist(monkeypatch) -> None:
    monkeypatch.setattr(RUN_ALL.agents, "harvest",
                        lambda: (_ for _ in ()).throw(RuntimeError("source down")))
    monkeypatch.setattr(RUN_ALL.DB, "connect",
                        lambda: (_ for _ in ()).throw(sqlite3.OperationalError("db locked")))
    with pytest.raises(RuntimeError, match="cannot persist failed freshness for agents"):
        RUN_ALL.main(concepts_limit=1)


def test_reminders_payload_contract_rejects_false_empty_success() -> None:
    class Client:
        def __init__(self, text): self.text = text
        def call_tool(self, *_args, **_kwargs): return self.text

    current = "# Reminders\n\n  • ⚡ DEADLINES  [7 items]  · POLARIS\n  • empty  [0 items] · POLARIS"
    assert EVENTS._list_populated_reminder_lists(Client(current)) == ["⚡ DEADLINES"]
    with pytest.raises(RuntimeError):
        EVENTS._list_populated_reminder_lists(Client("Error: EventKit unavailable"))
    assert EVENTS._parse_reminders_payload("[]", "empty") == []
    with pytest.raises(RuntimeError):
        EVENTS._parse_reminders_payload("permission denied", "blocked")


def test_incremental_failure_updates_same_canonical_watermark(tmp_path: Path, monkeypatch) -> None:
    desktop = tmp_path / "Desktop"
    source = desktop / "ALTAIR" / "Knowledge" / "fact.md"
    source.parent.mkdir(parents=True)
    source.write_text("answer-bearing source")
    db_path = tmp_path / "brain.sqlite"
    original_connect = DB.connect
    monkeypatch.setattr(INGEST, "DB", DB)
    monkeypatch.setattr(INGEST.C, "DESKTOP_ROOT", desktop)
    monkeypatch.setattr(INGEST.walker, "walk_paths",
                        lambda _paths: iter([(source, "document", "Knowledge")]))
    monkeypatch.setattr(INGEST.DB, "connect", lambda _path=None: original_connect(db_path))
    good = source_node("fact", "ALTAIR/Knowledge/fact.md")
    monkeypatch.setattr(INGEST, "route", lambda *_args: (good, []))
    assert INGEST.ingest_paths([source], embed=False)["failures"] == 0

    def fail(*_args):
        raise ValueError("broken extractor")
    monkeypatch.setattr(INGEST, "route", fail)
    assert INGEST.ingest_paths([source], embed=False)["failures"] == 1

    conn = original_connect(db_path)
    rows = conn.execute("SELECT * FROM source_ingest_watermarks").fetchall()
    assert len(rows) == 1
    assert rows[0]["source_key"] == "file:ALTAIR/Knowledge/fact.md"
    assert rows[0]["status"] == "error"
    assert rows[0]["last_success_at"]
    source_row = next(r for r in COVERAGE.source_freshness_results(
        conn, ["ALTAIR"], now=datetime.now(timezone.utc)) if r[0].endswith(":ALTAIR"))
    assert source_row[1] != COVERAGE.PASS


def test_operating_inventory_is_manifest_backed_and_expires(tmp_path: Path) -> None:
    plist = tmp_path / "com.arcturion.example.plist"
    plist.write_bytes(plistlib.dumps({
        "Label": "com.arcturion.example",
        "ProgramArguments": ["/usr/bin/python3", "/tmp/example.py"],
        "RunAtLoad": True,
    }))
    records, rejected = INVENTORY.collect_launchagents(
        [plist], observed_at="2026-09-09T10:00:00+00:00", ttl_hours=28)
    assert rejected == []
    assert records[0]["state"] == "configured"
    assert records[0]["source_sha256"]
    assert records[0]["expires_at"] == "2026-09-10T14:00:00+00:00"


def test_inventory_is_walked_in_same_reindex_cycle() -> None:
    assert INVENTORY.INVENTORY_MD.parent == INVENTORY.C.BRAIN_ROOT / "docs"
    source = (HERE / "reindex.py").read_text(encoding="utf-8")
    inventory_pos = source.index('"engine.operating_inventory"')
    graph_pos = source.index('"engine.graph_builder"', inventory_pos)
    assert inventory_pos < graph_pos


def test_expired_inventory_fails_despite_fresh_file_watermark(tmp_path: Path) -> None:
    inventory = tmp_path / "operating_inventory.json"
    inventory.write_text(json.dumps({
        "schema_version": 1,
        "generated_at": "2026-09-08T12:00:00+00:00",
        "records": [{
            "label": "com.arcturion.example", "state": "configured",
            "source_path": "/tmp/example.plist", "source_sha256": "a" * 64,
            "observed_at": "2026-09-08T12:00:00+00:00",
            "expires_at": "2026-09-09T14:00:00+00:00",
        }],
        "rejected_manifests": [],
    }))
    now = datetime(2026, 9, 9, 15, tzinfo=timezone.utc)
    check = COVERAGE.operating_inventory_result(inventory, now=now)
    assert check[1] == COVERAGE.FAIL
    assert "expired" in check[2]

    conn = memory_db()
    DB.upsert_node(conn, source_node("inventory-doc", "ARCTURION_Brain/docs/operating_inventory.md"))
    DB.record_source_watermark(
        conn, source_key="file:ARCTURION_Brain/docs/operating_inventory.md",
        source_kind="file", source_path="ARCTURION_Brain/docs/operating_inventory.md",
        observed_at="2026-09-09T14:59:00+00:00", success=True,
        content_hash="fresh-file-hash", node_id="inventory-doc")
    # The file watermark does not participate in inventory expiry validation.
    assert COVERAGE.operating_inventory_result(inventory, now=now)[1] == COVERAGE.FAIL


def test_inventory_rejects_future_malformed_and_rejected_manifests(tmp_path: Path) -> None:
    base = {
        "schema_version": 1, "generated_at": "2026-09-09T14:00:00+00:00",
        "records": [{"label": "svc", "state": "configured", "source_path": "/x",
                     "source_sha256": "b" * 64,
                     "observed_at": "2026-09-09T14:00:00+00:00",
                     "expires_at": "2026-09-10T14:00:00+00:00"}],
        "rejected_manifests": [],
    }
    path = tmp_path / "inventory.json"; now = datetime(2026, 9, 9, 15, tzinfo=timezone.utc)
    path.write_text(json.dumps(base))
    assert COVERAGE.operating_inventory_result(path, now)[1] == COVERAGE.PASS
    path.write_text(json.dumps({**base, "generated_at": "2026-09-10T14:00:00+00:00"}))
    assert COVERAGE.operating_inventory_result(path, now)[1] == COVERAGE.FAIL
    path.write_text(json.dumps({**base, "rejected_manifests": [{"error": "bad"}]}))
    assert COVERAGE.operating_inventory_result(path, now)[1] == COVERAGE.FAIL
    path.write_text(json.dumps({**base, "schema_version": True}))
    assert COVERAGE.operating_inventory_result(path, now)[1] == COVERAGE.FAIL
    bad_record = {**base["records"][0], "source_sha256": "z" * 64}
    path.write_text(json.dumps({**base, "records": [bad_record]}))
    assert COVERAGE.operating_inventory_result(path, now)[1] == COVERAGE.FAIL
    ancient = {**base["records"][0],
               "observed_at": "2026-09-07T00:00:00+00:00",
               "expires_at": "2026-09-10T00:00:00+00:00"}
    path.write_text(json.dumps({**base, "records": [ancient]}))
    assert COVERAGE.operating_inventory_result(path, now)[1] == COVERAGE.FAIL
    far_expiry = {**base["records"][0], "expires_at": "2026-09-12T14:00:00+00:00"}
    path.write_text(json.dumps({**base, "records": [far_expiry]}))
    assert COVERAGE.operating_inventory_result(path, now)[1] == COVERAGE.FAIL
    path.write_text("not json")
    assert COVERAGE.operating_inventory_result(path, now)[1] == COVERAGE.FAIL


def test_failed_observation_does_not_refresh_last_success() -> None:
    conn = memory_db()
    DB.record_source_watermark(
        conn, source_key="file:ALTAIR/000 x/Memory/a.md", source_kind="file",
        source_path="ALTAIR/000 x/Memory/a.md", observed_at="2026-09-09T10:00:00+00:00",
        success=True, content_hash="old", source_mtime_ns=1, node_id="a")
    DB.record_source_watermark(
        conn, source_key="file:ALTAIR/000 x/Memory/a.md", source_kind="file",
        source_path="ALTAIR/000 x/Memory/a.md", observed_at="2026-09-09T11:00:00+00:00",
        success=False, content_hash="new", source_mtime_ns=2, error="extract failed")
    row = conn.execute("SELECT * FROM source_ingest_watermarks").fetchone()
    assert row["last_success_at"] == "2026-09-09T10:00:00+00:00"
    assert row["content_hash"] == "old"
    assert row["status"] == "error"


def test_source_watermark_participates_in_caller_transaction() -> None:
    conn = memory_db()
    conn.execute("BEGIN")
    DB.upsert_node(conn, source_node("transactional"))
    DB.upsert_node(conn, source_node("target"))
    assert DB.insert_edge(conn, {
        "from": "transactional", "to": "target", "predicate": "depends_on",
        "family": "structure", "asserted_by": "TEST",
    })
    DB.record_source_watermark(
        conn, source_key="file:ALTAIR/000 x/Memory/transactional.md",
        source_kind="file", source_path="ALTAIR/000 x/Memory/transactional.md",
        observed_at="2026-09-09T10:00:00+00:00", success=True,
        content_hash="h", node_id="transactional")
    conn.rollback()
    assert conn.execute("SELECT 1 FROM nodes WHERE id='transactional'").fetchone() is None
    assert conn.execute("SELECT 1 FROM edges WHERE from_id='transactional'").fetchone() is None
    assert conn.execute(
        "SELECT 1 FROM source_ingest_watermarks WHERE source_key LIKE '%transactional%'"
    ).fetchone() is None


def test_coverage_uses_each_source_and_event_watermark() -> None:
    conn = memory_db()
    fresh = "2026-09-09T10:00:00+00:00"
    now = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)
    DB.upsert_node(conn, source_node("a", "ALTAIR/000 x/Memory/a.md"))
    DB.record_source_watermark(
        conn, source_key="file:ALTAIR/000 x/Memory/a.md", source_kind="file",
        source_path="ALTAIR/000 x/Memory/a.md", observed_at=fresh, success=True,
        content_hash="h", node_id="a")
    for name in COVERAGE.REQUIRED_HARVESTERS:
        DB.record_source_watermark(
            conn, source_key=f"harvester:{name}",
            source_kind="event" if name == "events" else "harvester",
            source_path=f"engine.harvesters.{name}", observed_at=fresh, success=True,
            content_hash="h", event_watermark="cursor" if name == "events" else None)
    rows = COVERAGE.source_freshness_results(conn, ["ALTAIR"], now=now)
    assert all(status == COVERAGE.PASS for _, status, _ in rows)

    # A watermark for a source no longer represented in the graph cannot make the
    # current source set stale.
    DB.record_source_watermark(
        conn, source_key="file:ALTAIR/000 x/Memory/deleted.md", source_kind="file",
        source_path="ALTAIR/000 x/Memory/deleted.md",
        observed_at="2026-09-01T00:00:00+00:00", success=True,
        content_hash="ghost", node_id="deleted")
    assert all(status == COVERAGE.PASS for _, status, _ in
               COVERAGE.source_freshness_results(conn, ["ALTAIR"], now=now))

    DB.upsert_node(conn, source_node("b", "ALTAIR/000 x/Knowledge/b.md"))
    DB.record_source_watermark(
        conn, source_key="file:ALTAIR/000 x/Knowledge/b.md", source_kind="file",
        source_path="ALTAIR/000 x/Knowledge/b.md", observed_at="2026-09-07T00:00:00+00:00",
        success=True, content_hash="h2", node_id="b")
    rows = COVERAGE.source_freshness_results(conn, ["ALTAIR"], now=now)
    assert next(status for check, status, _ in rows if check.endswith(":ALTAIR")) == COVERAGE.FAIL
