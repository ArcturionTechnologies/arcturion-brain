import hashlib
from pathlib import Path

import pytest

from engine import vega_corpus as corpus


PACK = {"concepts": [{"id": "backtest", "label": "Backtest", "kind": "process", "aliases": []}, {"id": "risk", "label": "Risk limit", "kind": "risk", "aliases": []}]}


def put(root, name, text):
    path = root / "VEGA" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_authored_sources_have_citations_locations_and_explicit_relation(tmp_path):
    body = "Example relation between two generic topics."
    put(tmp_path, "04 Macro/example.md", body)
    config = {"grounded": [{"pattern": "Example relation", "subject": "corpus_donchian", "object": "corpus_trailing_stop"}], "pins": []}
    [record] = list(corpus.scan(tmp_path, pack=PACK, rule_config=config))
    assert any(c["id"].startswith("shelf_") for c in record["concepts"])
    assert all(r["source"] == record["source"] and r["quote"] in body for r in record["rules"])
    rule = next(r for r in record["rules"] if r["predicate"] == "vega_corpus_connects")
    assert (rule["from"], rule["to"], rule["kind"], rule["inferred"]) == ("corpus_donchian", "corpus_trailing_stop", "hypothesis", True)
    [unconfigured] = list(corpus.scan(tmp_path, pack=PACK, rule_config={"grounded": [], "pins": []}))
    assert not any(r["predicate"] == "vega_corpus_connects" for r in unconfigured["rules"])



def test_no_read_private_subtrees_cycles_and_external_links(tmp_path):
    put(tmp_path, "000 VEGA/Memory/private.md", "private")
    put(tmp_path, "000 VEGA/Knowledge/Founder/private.md", "private")
    put(tmp_path, "07 Data/Reference/research/crypto/cycle_20260428.md", "private")
    put(tmp_path, "03 Execution/paper_trade_journal.md", "private")
    put(tmp_path, "03 Execution/safe.md", "# Safe\nAccount: private\nBacktest uses risk limit.\n")
    external = tmp_path / "outside"
    external.mkdir()
    (external / "secret.md").write_text("secret")
    (tmp_path / "VEGA" / "03 Execution" / "external").symlink_to(external, target_is_directory=True)
    records = list(corpus.scan(tmp_path, pack=PACK))
    excluded = [r for r in records if r["disposition"] == "excluded"]
    assert {"protected_subtree", "protected_file", "machine_telemetry", "symlink"} <= {r["reason"] for r in excluded}
    assert all(not r["rules"] and not r["concepts"] and r["source_hash"] is None for r in excluded)
    assert not any("Account:" in rule["quote"] for r in records for rule in r["rules"])
    [selected] = list(corpus.scan(tmp_path, paths=["VEGA/03 Execution/external/secret.md"], pack=PACK))
    assert selected["reason"] == "symlink"


def test_deleted_file_retry_and_duplicate_evidence(tmp_path):
    a = put(tmp_path, "08 Risk/a.md", "# Trading\nBacktest uses risk limit.\n")
    b = put(tmp_path, "08 Risk/b.md", a.read_text())
    records = list(corpus.scan(tmp_path, pack=PACK))
    assert records[1]["duplicate_of"] == records[0]["source"]
    assert all(r["rules"] for r in records)
    b.unlink()
    [deleted] = list(corpus.scan(tmp_path, paths=["VEGA/08 Risk/b.md"], pack=PACK))
    assert deleted["disposition"] == "removed" and deleted["source_hash"] is None
    b.write_text("# Retry\nBacktest uses risk limit.")
    [retried] = list(corpus.scan(tmp_path, paths=["VEGA/08 Risk/b.md"], pack=PACK))
    assert retried["disposition"] == "eligible" and retried["rules"]


def test_unsupported_and_negated_assertions(tmp_path):
    put(tmp_path, "09 Charts/history.csv", "do not parse")
    put(tmp_path, "09 Charts/report.pdf", "do not parse")
    put(tmp_path, "09 Charts/code.py", "do not parse")
    put(tmp_path, "09 Charts/research.md", "# Note\nNot Donchian breakout with trailing stop.\n")
    records = list(corpus.scan(tmp_path, pack=PACK))
    assert {r["reason"] for r in records} >= {"raw_data_format", "document_format", "code_format"}
    assert not any(rule["kind"] == "hypothesis" for r in records for rule in r["rules"])
    with pytest.raises(ValueError):
        list(corpus.scan(tmp_path, paths=["VEGA/../outside"], pack=PACK))


def test_exact_pins_require_original_bytes_and_selected_source(tmp_path):
    body = "Invented evidence sentence."
    source = "VEGA/04 Macro/reference.md"
    path = put(tmp_path, "04 Macro/reference.md", body)
    config = {"grounded": [], "pins": [{"source": source,
        "source_hash": hashlib.sha256(body.encode()).hexdigest(),
        "quote": "Invented evidence sentence.",
        "pairs": [["corpus_funding_proxy", "corpus_funding_rate"]]}]}
    [first] = list(corpus.scan(tmp_path, paths=[source], pack=PACK, rule_config=config))
    assert any(r["kind"] == "definition" for r in first["rules"])
    [removed] = list(corpus.scan(tmp_path, paths=[source], pack=PACK, rule_config={"grounded": [], "pins": []}))
    assert not any(r["kind"] == "definition" for r in removed["rules"])
    path.write_text(body + "changed", encoding="utf-8")
    [changed] = list(corpus.scan(tmp_path, paths=[source], pack=PACK, rule_config=config))
    assert not any(r["kind"] == "definition" for r in changed["rules"])



def test_requested_root_shelves_and_task_record_boundaries(tmp_path):
    put(tmp_path, "docs/runbooks/guide.md", "# Guide\nPaper process.")
    put(tmp_path, "09_Linked_Surfaces/GRAPH_INDEX.md", "# Chart reference\n")
    put(tmp_path, "77 Back Office/Home.md", "# Home\n")
    put(tmp_path, "77 Back Office/Task Records/progress.md", "private session log")
    put(tmp_path, "77 Back Office/Task Records/research-memo.md", "# Research memo\n")
    records = list(corpus.scan(tmp_path, pack=PACK))
    assert sum(r["disposition"] == "eligible" for r in records) == 4
    assert any(r["reason"] == "work_log_or_security_review" for r in records)


def test_retrieval_path_policy_checks_all_ancestors():
    assert corpus.admitted_markdown_path("VEGA/02 Strategies/reference.md")
    assert corpus.admitted_markdown_path("VEGA/docs/runbooks/guide.md")
    for source in ("VEGA/000 VEGA/Memory/private.md", "VEGA/03 Execution/State/strategy.md", "VEGA/77 Back Office/Task Records/progress.md", "VEGA/09 Charts/research.csv", "VEGA/../outside.md", "/VEGA/02 Strategies/a.md", "VEGA/02 Strategies//a.md"):
        assert not corpus.admitted_markdown_path(source)
