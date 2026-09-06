"""Tests for the Polymarket connector (paper ledger + live-readonly guards)."""

from __future__ import annotations

from typing import Any

import pytest

from src.live.classification import ToolClass
from src.trading import profiles, service
from src.trading.connectors.polymarket import sdk as pm_sdk
from src.trading.connectors.polymarket.classification import POLYMARKET_TOOL_CLASS

pytestmark = pytest.mark.unit

_MARKET_ROW = {
    "id": "12345",
    "condition_id": "0xabc",
    "question": "Will it happen?",
    "outcomes": ["Yes", "No"],
    "prices": [0.65, 0.35],
    "closed": False,
    "active": True,
    "end_date": "2026-12-31T00:00:00Z",
    "liquidity": 10000.0,
    "volume": 50000.0,
    "raw": {"clobTokenIds": '["tok-yes", "tok-no"]'},
}


@pytest.fixture(autouse=True)
def _stub_find_market(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Every test in this module stays offline and gets its own paper ledger.

    The paper ledger deliberately persists across calls with the same profile
    (a fresh balance survives restarts, like the standalone virtual trading
    bot it was adapted from) — so tests must not share the real runtime root,
    or a balance left over from one test would leak into the next.
    """

    def _fake_find_market(symbol: str, *, timeout: float = 20.0) -> dict[str, Any]:
        return dict(_MARKET_ROW)

    monkeypatch.setattr(pm_sdk, "_find_market", _fake_find_market)
    monkeypatch.setattr(pm_sdk, "get_runtime_root", lambda: tmp_path)

    def _fail_get(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("unexpected direct Polymarket API call in a unit test")

    monkeypatch.setattr(pm_sdk, "_get", _fail_get)


def test_polymarket_profiles_registered() -> None:
    ids = {profile.id for profile in profiles.list_profiles()}
    assert {
        "polymarket-paper-readonly",
        "polymarket-paper-trade",
        "polymarket-live-readonly",
    } <= ids

    paper = profiles.profile_by_id("polymarket-paper-trade")
    live = profiles.profile_by_id("polymarket-live-readonly")
    assert paper.connector == "polymarket"
    assert paper.environment == "paper"
    assert paper.readonly is False
    assert "orders.place" in paper.capabilities
    assert live.environment == "live"
    assert live.readonly is True
    assert "orders.place" not in live.capabilities


def test_polymarket_registered_as_sdk_connector() -> None:
    assert service._SDK_CONNECTOR_MODULES["polymarket"] == "src.trading.connectors.polymarket.sdk"
    assert service._sdk_module("polymarket") is pm_sdk


def test_polymarket_classification_fails_closed_for_writes() -> None:
    assert POLYMARKET_TOOL_CLASS["place_order"] is ToolClass.WRITE
    assert POLYMARKET_TOOL_CLASS["cancel_order"] is ToolClass.WRITE
    assert POLYMARKET_TOOL_CLASS["get_positions"] is ToolClass.READ


def test_paper_place_order_buys_and_updates_ledger() -> None:
    cfg = pm_sdk.PolymarketConfig(profile="paper", starting_balance=1000.0)

    first = pm_sdk.place_order(cfg, symbol="12345", side="yes", quantity=100)
    assert first["status"] == "ok"
    assert first["fill"]["cost"] == pytest.approx(65.0)
    assert first["balance"] == pytest.approx(935.0)

    snapshot = pm_sdk.get_account_snapshot(cfg)
    assert snapshot["account"]["balance"] == pytest.approx(935.0)

    positions = pm_sdk.get_positions(cfg)["positions"]
    assert len(positions) == 1
    assert positions[0]["outcome"] == "Yes"
    assert positions[0]["shares"] == pytest.approx(100.0)

    # A second buy averages into the same position.
    second = pm_sdk.place_order(cfg, symbol="12345", side="yes", quantity=50)
    assert second["status"] == "ok"
    positions = pm_sdk.get_positions(cfg)["positions"]
    assert positions[0]["shares"] == pytest.approx(150.0)


def test_paper_place_order_rejects_insufficient_balance() -> None:
    cfg = pm_sdk.PolymarketConfig(profile="paper", starting_balance=10.0)
    result = pm_sdk.place_order(cfg, symbol="12345", side="yes", quantity=100)
    assert result["status"] == "error"
    assert "insufficient" in result["error"]


def test_paper_cancel_order_always_not_found() -> None:
    cfg = pm_sdk.PolymarketConfig(profile="paper")
    result = pm_sdk.cancel_order(cfg, "does-not-exist")
    assert result["status"] == "error"


def test_live_readonly_requires_wallet_address_without_network_call() -> None:
    cfg = pm_sdk.PolymarketConfig(profile="live-readonly", wallet_address="")
    status = pm_sdk.check_status(cfg)
    assert status["status"] == "error"
    assert "wallet_address" in status["error"]

    positions = pm_sdk.get_positions(cfg)
    assert positions["status"] == "error"


def test_live_readonly_never_places_orders() -> None:
    cfg = pm_sdk.PolymarketConfig(profile="live-readonly", wallet_address="0xdead")
    result = pm_sdk.place_order(cfg, symbol="12345", side="yes", quantity=1)
    assert result["status"] == "error"
