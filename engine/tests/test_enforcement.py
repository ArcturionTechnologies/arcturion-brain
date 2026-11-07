"""Regression coverage for the transport-neutral governance gates."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import enforcement  # noqa: E402


def _proc(code: int, stderr: bytes = b"") -> Mock:
    return Mock(returncode=code, stderr=stderr)


def test_run_hook_classifies_allow_and_policy_block(tmp_path):
    hook = tmp_path / "hook.py"
    hook.write_text("# fixture\n")
    with patch.object(enforcement.subprocess, "run", return_value=_proc(0)):
        assert enforcement.run_hook(hook, {}).infrastructure_failure is None
    with patch.object(enforcement.subprocess, "run", return_value=_proc(2, b"blocked")):
        result = enforcement.run_hook(hook, {})
        assert result.exit_code == 2
        assert result.infrastructure_failure is None


def test_missing_timeout_exception_and_crash_are_infrastructure_failures(tmp_path):
    missing = enforcement.run_hook(tmp_path / "missing.py", {})
    assert missing.infrastructure_failure == "missing"

    hook = tmp_path / "hook.py"
    hook.write_text("# fixture\n")
    with patch.object(enforcement.subprocess, "run",
                      side_effect=subprocess.TimeoutExpired("hook", 1)):
        assert enforcement.run_hook(hook, {}, timeout=1).infrastructure_failure == "timeout"
    with patch.object(enforcement.subprocess, "run", side_effect=OSError("cannot exec")):
        assert enforcement.run_hook(hook, {}).infrastructure_failure == "exception"
    with patch.object(enforcement.subprocess, "run", return_value=_proc(1, b"traceback")):
        assert enforcement.run_hook(hook, {}).infrastructure_failure == "unexpected_exit"


def test_write_gate_fails_closed_with_visible_degradation():
    result = enforcement.HookResult(None, "hook unavailable", "timeout")
    with patch.object(enforcement, "run_hook", return_value=result):
        verdict = enforcement.validate_write({"file_path": "/tmp/x", "content": "safe"})
    assert verdict["decision"] == "block"
    assert verdict["infrastructure_failure"]["kind"] == "timeout"
    assert "unavailable" in verdict["errors"][0].lower()


def test_risk_gate_fails_closed_with_visible_degradation():
    result = enforcement.HookResult(1, "hook crashed", "unexpected_exit")
    with patch.object(enforcement, "run_hook", return_value=result):
        verdict = enforcement.check_risk({"command": "echo safe"})
    assert verdict["decision"] == "block"
    assert verdict["rule_id"] == "governance-infrastructure-failure"
    assert verdict["infrastructure_failure"]["exit_code"] == 1


def test_allow_and_policy_block_remain_distinct():
    with patch.object(enforcement, "run_hook",
                      return_value=enforcement.HookResult(0, "⚠️ advisory")):
        assert enforcement.check_risk({"command": "echo safe"}) == {
            "decision": "allow", "rule_id": "", "message": "",
            "amber_warnings": ["⚠️ advisory"],
        }
    with patch.object(enforcement, "run_hook",
                      return_value=enforcement.HookResult(2, "Reason: denied")):
        verdict = enforcement.check_risk({"command": "unsafe"})
    assert verdict["decision"] == "block"
    assert "infrastructure_failure" not in verdict
