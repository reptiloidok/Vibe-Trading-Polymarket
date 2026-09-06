"""Curated read/write classification for Polymarket SDK operations.

The trading layer classifies each connector operation as READ or WRITE so the
live gate can keep writes behind the mandate. Polymarket is a direct-SDK
connector (not MCP), so the keys here are this module's own ``sdk.py``
function names. Anything not listed and not a known read resolves to WRITE
(fail-closed) when the live gate consults this map. Both write operations
here only ever touch the local paper ledger in this version — see
``profiles.py`` for why live order placement is not shipped yet.
"""

from __future__ import annotations

from src.live.classification import ToolClass

#: Polymarket SDK operation read/write catalog.
POLYMARKET_TOOL_CLASS: dict[str, ToolClass] = {
    # READ
    "get_account_snapshot": ToolClass.READ,
    "get_positions": ToolClass.READ,
    "get_open_orders": ToolClass.READ,
    "get_quote": ToolClass.READ,
    "get_historical_bars": ToolClass.READ,
    # WRITE (paper ledger only — see module docstring)
    "place_order": ToolClass.WRITE,
    "cancel_order": ToolClass.WRITE,
}
