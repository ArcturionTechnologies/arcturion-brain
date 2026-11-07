"""Infinite Brain — Universal Neural Pathway over ARCTURION_UNIVERSE.

Public API:
    from brain import query, status, reindex
    pack = query("credential management", radius=2, token_budget=4000)

CLI:
    brain-query.sh "<topic>" --radius 2 --budget 4000
    brain-reindex.sh [--full]
"""

from .brain import query, status
from .graph_builder import reindex

__all__ = ["query", "status", "reindex"]
