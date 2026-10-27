"""Fictional exact-ID reviewer memory for the v2 review service (Steps 20-21).

Memory remembers dated prior annotations about exact reference-record IDs and
shows them as display-only context in new review packets. It never links by
name similarity, never states identity, wrongdoing, sanctions status or
clearance, and never changes candidate ranking. Importing this package loads
no model and opens no file.

Main entry points: ``MemoryConfig`` / ``MemoryStore`` (``store``),
``Publisher`` (``publisher``), ``MemoryContextProvider`` (``provider``),
``withdraw`` / ``supersede`` / ``plan_retention`` / ``apply_retention``
(``lifecycle``), ``inspect_history`` / ``check_freshness`` (``lookup``).
"""

from watchlist.case_memory.state import MemoryConfig

__all__ = ["MemoryConfig"]
