"""
forex_cascade.py  —  ⛔ DEPRECATED / DO NOT USE
════════════════════════════════════════════════
This file has been superseded by tick_follower.py (v2.8.0, 2026-08-15).

The Gold→Forex cascade strategy was replaced with the Ant-on-Sugar Tick
Velocity Follower — a zero-cost stall-exit follower that captures every
directional move on AUDUSD (24/7), EURUSD, and GBPUSD.

This file is intentionally left as a stub so Python never crashes if an old
import statement refers to it.  broker.py does NOT import it.
"""

raise ImportError(
    "forex_cascade is DEPRECATED. Use tick_follower instead. "
    "Check broker.py — it imports tick_follower, not this file."
)
