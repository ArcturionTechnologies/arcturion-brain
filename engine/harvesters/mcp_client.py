"""Pure-stdlib stdio MCP client.

Spawns an MCP server binary as a subprocess, does the JSON-RPC handshake,
and provides .call_tool() / .list_tools(). Used by harvesters that need to
call other MCP servers without depending on the official `mcp` SDK.

This bypasses TCC issues — the target MCP binary already has whatever
permissions Founder granted it (Reminders, Calendar, Files, etc.), so the
heartbeat inherits those grants through the subprocess.
"""

from __future__ import annotations
import json
import subprocess
import threading
import time
from typing import Any


class MCPClientError(RuntimeError):
    pass


class StdioMCPClient:
    """Minimal stdio JSON-RPC 2.0 client speaking MCP protocol 2024-11-05."""

    def __init__(self, command_args: list[str], protocol_version: str = "2024-11-05",
                 client_name: str = "arcturion-brain-heartbeat",
                 startup_timeout: float = 8.0):
        self.proc = subprocess.Popen(
            command_args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line-buffered
        )
        self._next_id = 0
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()
        try:
            self._initialize(protocol_version, client_name, startup_timeout)
        except Exception:
            self.close()
            raise

    def _drain_stderr(self):
        try:
            for line in iter(self.proc.stderr.readline, ""):
                # Don't crash on noisy MCP servers — drop stderr silently.
                # If debugging is needed, log to a file.
                pass
        except Exception:
            pass

    def _send_raw(self, msg: dict) -> None:
        if self.proc.poll() is not None:
            raise MCPClientError(f"server exited with code {self.proc.returncode}")
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def _read_response(self, timeout: float = 30.0) -> dict:
        """Read JSON-RPC response. Tolerates the server emitting unsolicited notifications."""
        deadline = time.time() + timeout
        while True:
            if time.time() > deadline:
                raise MCPClientError("read response timed out")
            line = self.proc.stdout.readline()
            if not line:
                if self.proc.poll() is not None:
                    raise MCPClientError(f"server exited (code {self.proc.returncode}) before response")
                time.sleep(0.05)
                continue
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                # Some servers emit log lines; skip non-JSON.
                continue
            if "id" in msg:
                return msg
            # else: it's a notification; keep reading

    def _initialize(self, protocol_version: str, client_name: str, startup_timeout: float) -> None:
        self._next_id += 1
        self._send_raw({
            "jsonrpc": "2.0", "id": self._next_id, "method": "initialize",
            "params": {
                "protocolVersion": protocol_version,
                "capabilities": {},
                "clientInfo": {"name": client_name, "version": "1.0.0"},
            },
        })
        resp = self._read_response(timeout=startup_timeout)
        if "error" in resp:
            raise MCPClientError(f"initialize failed: {resp['error']}")
        # Send the post-init notification (no response expected)
        self._send_raw({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def list_tools(self, timeout: float = 10.0) -> list[dict]:
        self._next_id += 1
        self._send_raw({"jsonrpc": "2.0", "id": self._next_id, "method": "tools/list"})
        resp = self._read_response(timeout=timeout)
        if "error" in resp:
            raise MCPClientError(f"tools/list failed: {resp['error']}")
        return (resp.get("result") or {}).get("tools") or []

    def call_tool(self, name: str, arguments: dict | None = None,
                  timeout: float = 30.0) -> Any:
        self._next_id += 1
        self._send_raw({
            "jsonrpc": "2.0", "id": self._next_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        })
        resp = self._read_response(timeout=timeout)
        if "error" in resp:
            raise MCPClientError(f"tools/call {name} failed: {resp['error']}")
        result = resp.get("result") or {}
        # Most MCP tool calls return {"content": [{"type": "text", "text": "..."}]}
        content = result.get("content") or []
        if content and isinstance(content, list):
            texts = [c.get("text", "") for c in content if c.get("type") == "text"]
            if texts:
                return "\n".join(texts)
        return result

    def close(self) -> None:
        try:
            self._send_raw({"jsonrpc": "2.0", "method": "shutdown"})
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
