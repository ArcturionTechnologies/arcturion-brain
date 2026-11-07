"""Phase 2 harvesters — create non-file nodes (Agent, Application, Event, External, Concept)
and emit edges connecting them to existing file-backed nodes.

Each harvester is idempotent — re-running upserts nodes by stable id and only
inserts edges that don't already exist (UNIQUE constraint).
"""
