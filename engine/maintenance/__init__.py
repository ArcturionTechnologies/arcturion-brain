"""Brain maintenance — reciprocity, dedup, compaction.

Each module is idempotent and safe to wire into the 6h heartbeat or run
manually via `python3 -m engine.maintenance.<name>`.
"""
