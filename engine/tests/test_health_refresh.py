"""Isolated behavior checks: real health functions, no production DB/model/network."""
from __future__ import annotations
import ast
import io
import json
import pathlib
import sys
import threading
import time
import types
import unittest
from unittest.mock import patch

SRC = pathlib.Path(__file__).parents[1] / "http_server.py"
CONSUMER = pathlib.Path("/example/external-health-consumer.py")  # Supply an external consumer source to run this legacy check.


def fixture(payload=None):
    names = {"_refresh_health_cache", "_health_snapshot", "_warmup"}
    funcs = [n for n in ast.parse(SRC.read_text()).body
             if isinstance(n, ast.FunctionDef) and n.name in names]
    workers = []
    def start_worker(**kwargs):
        worker = threading.Thread(**kwargs)
        workers.append(worker)
        return worker
    cache = {"ts": 0.0, "observed_at_epoch": None, "data": None,
             "refreshing": False, "error": None, "retry_after": 0.0}
    ns = {"DB": types.SimpleNamespace(connect=lambda: types.SimpleNamespace(close=lambda: None)),
          "time": time, "threading": types.SimpleNamespace(Thread=start_worker),
          "_HEALTH_CACHE": cache, "_HEALTH_LOCK": threading.Lock(), "_HEALTH_TTL": 60.0,
          "_health_payload": payload or (lambda conn: {"nodes": 7, "edges": 9,
              "edges_walkable": 8, "source_integrity": {"unknown_is_fresh": False}}),
          "B": types.SimpleNamespace(query=lambda **kw: None), "__package__": "engine"}
    exec(compile(ast.Module(body=funcs, type_ignores=[]), str(SRC), "exec"), ns)
    return ns, workers


class HealthRefreshTests(unittest.TestCase):
    def test_cold_diagnostics_are_unknown(self):
        entered, release = threading.Event(), threading.Event()
        def payload(conn):
            entered.set()
            if not release.wait(2): raise TimeoutError()
            return {"nodes": 1}
        ns, workers = fixture(payload)
        try:
            got = ns["_health_snapshot"]()
            self.assertTrue(entered.wait(1))
            self.assertFalse(got["diagnostics_available"])
            self.assertEqual(got["diagnostic_freshness"], "unknown")
            self.assertNotIn("nodes", got)
        finally:
            release.set()
            for worker in workers: worker.join(2)

    def test_expired_requests_return_while_one_scan_is_blocked(self):
        entered, release, returned = threading.Event(), threading.Event(), threading.Event()
        calls, replies = [], []
        def payload(conn):
            calls.append(1); entered.set()
            if not release.wait(2): raise TimeoutError()
            return {"nodes": 2}
        ns, workers = fixture(payload)
        ns["_HEALTH_CACHE"].update(data={"nodes": 1}, ts=time.monotonic()-61, observed_at_epoch=1)
        def request():
            replies.append(ns["_health_snapshot"]()); returned.set()
        first = threading.Thread(target=request)
        first.start()
        clients = []
        try:
            self.assertTrue(entered.wait(1))
            self.assertTrue(returned.wait(1), "health request waited for SQL")
            for _ in range(20):
                client = threading.Thread(target=request); clients.append(client); client.start()
            for client in clients: client.join(1)
            self.assertTrue(all(not client.is_alive() for client in clients))
            self.assertEqual(len(workers), 1)
            self.assertEqual(len(calls), 1)
            self.assertTrue(all(x["nodes"] == 1 and x["diagnostic_freshness"] == "stale" for x in replies))
        finally:
            release.set(); first.join(2)
            for client in clients: client.join(2)
            for worker in workers: worker.join(2)
        self.assertEqual(ns["_HEALTH_CACHE"]["data"], {"nodes": 2})

    def test_failed_scan_preserves_data_and_limits_retries(self):
        def fail(conn): raise ValueError("private error detail")
        ns, workers = fixture(fail)
        ns["_HEALTH_CACHE"].update(data={"nodes": 3}, ts=time.monotonic()-61, observed_at_epoch=2)
        ns["_refresh_health_cache"]()
        got = ns["_health_snapshot"]()
        self.assertEqual(got["nodes"], 3)
        self.assertEqual(got["observed_at_epoch"], 2)
        self.assertEqual(got["diagnostic_freshness"], "stale")
        self.assertEqual(got["refresh_error"], "ValueError")
        self.assertFalse(got["refreshing"])
        self.assertEqual(workers, [])
        self.assertNotIn("private error detail", json.dumps(got))

    def test_worker_start_failure_is_explicit(self):
        ns, workers = fixture()
        def fail_start(**kwargs): raise RuntimeError("private detail")
        ns["threading"].Thread = fail_start
        got = ns["_health_snapshot"]()
        self.assertFalse(got["diagnostics_available"])
        self.assertFalse(got["refreshing"])
        self.assertEqual(got["refresh_error"], "RuntimeError")

    def test_success_keeps_complete_payload(self):
        ns, workers = fixture()
        ns["_refresh_health_cache"]()
        got = ns["_health_snapshot"]()
        self.assertEqual(got["edges_walkable"], 8)
        self.assertEqual(got["source_integrity"], {"unknown_is_fresh": False})
        self.assertEqual(got["diagnostic_freshness"], "fresh")
        self.assertIsInstance(got["observed_at_epoch"], float)
        self.assertEqual(workers, [])

    def test_warmup_shares_refresh_path(self):
        ns, workers = fixture()
        seen = []; ns["_health_snapshot"] = lambda: seen.append("health")
        engine = types.ModuleType("engine"); engine.__path__ = []
        vs = types.ModuleType("engine.vector_seed"); vs.prewarm = lambda: seen.append("vector")
        with patch.dict(sys.modules, {"engine": engine, "engine.vector_seed": vs}):
            ns["_warmup"]()
        self.assertEqual(seen, ["health", "vector"])

    def test_consumer_distinguishes_stale_unknown_and_fresh(self):
        func = next(n for n in ast.parse(CONSUMER.read_text()).body
                    if isinstance(n, ast.FunctionDef) and n.name == "check_brain")
        for freshness, available, expected in (("unknown", False, "Y"), ("stale", True, "Y"), (None, None, "Y"), ("fresh", True, "G")):
            health = {"diagnostic_freshness": freshness, "diagnostics_available": available,
                      "edges_dangling": 0, "source_integrity": {"authored_unknown_review": 0}}
            request = types.SimpleNamespace(urlopen=lambda *a, **kw: Response(health))
            ns = {"urllib": types.SimpleNamespace(request=request), "json": json,
                  "BRAIN_URL": "fixture", "run": lambda *a, **kw: (0, '{"errors":0,"nodes":1}', ""),
                  "G": "G", "Y": "Y", "R": "R"}
            exec(compile(ast.Module(body=[func], type_ignores=[]), str(CONSUMER), "exec"), ns)
            self.assertEqual(ns["check_brain"]()[0], expected)


class Response(io.BytesIO):
    status = 200
    def __init__(self, data): super().__init__(json.dumps(data).encode())


if __name__ == "__main__":
    unittest.main()
