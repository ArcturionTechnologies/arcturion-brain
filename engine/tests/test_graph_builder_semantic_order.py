from __future__ import annotations

import inspect

from engine import graph_builder


def test_agent_anchors_precede_label_resolution_and_inverses_are_last():
    source = inspect.getsource(graph_builder.reindex)
    anchors = source.index("from .harvesters import agents")
    inference = source.index("from .edge_inference import run")
    sessions = source.index("from .harvesters import sessions")
    registry = source.index("from . import registry_harvester")
    inverses = source.index("inv_count = derive_inverses(conn)")
    assert anchors < inference < sessions < registry < inverses
