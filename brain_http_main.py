"""Launchd entry shim for the Brain HTTP server (Brain v2 — engine package).
Direct-file invocation avoids a launchd XPC sandbox quirk with `-m`.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from engine.http_server import main

if __name__ == "__main__":
    sys.exit(main() or 0)
