"""Built-in Polymarket connector profiles.

Polymarket ships no vendor-hosted demo/sandbox account, unlike OKX or Binance.
So "paper" here means: real market data from the public Gamma API (no key
required), with orders filled locally against the current best price and
recorded in a JSON ledger under the runtime root
(``~/.vibe-trading/polymarket/paper.json``). This is the same paper-vs-live
split every other connector uses, just with a locally-simulated paper side
instead of an exchange-hosted one — mirroring the standalone ``Poly_trade``
virtual trading bot this connector was adapted from.

Live order placement (``orders.place.requires_mandate``) is deliberately NOT
shipped in this version. Wiring it up needs ``py-clob-client`` wallet signing
plus new ``InstrumentType``/``AssetClass`` mandate enum members — a shared,
safety-critical surface consulted by every connector's live order gate — and
belongs in its own dedicated, carefully reviewed follow-up PR. The
``polymarket-live-readonly`` profile below reads a public wallet's positions
by address only; it never reads, stores, or requires a private key.
"""

from __future__ import annotations

from src.trading.types import READ_CAPABILITIES, TradingProfile

POLYMARKET_PROFILES: tuple[TradingProfile, ...] = (
    TradingProfile(
        id="polymarket-paper-readonly",
        connector="polymarket",
        label="Polymarket Paper · Read-Only",
        environment="paper",
        transport="broker_sdk",
        capabilities=READ_CAPABILITIES,
        readonly=True,
        config={"profile": "paper"},
        notes=(
            "Reads the local simulated Polymarket paper ledger. Market data is "
            "real (public Gamma API); the account, positions, and fills are a "
            "local simulation, not an exchange-hosted demo account."
        ),
    ),
    TradingProfile(
        id="polymarket-paper-trade",
        connector="polymarket",
        label="Polymarket Paper · Simulated Trading",
        environment="paper",
        transport="broker_sdk",
        capabilities=READ_CAPABILITIES + ("orders.place",),
        readonly=False,
        config={"profile": "paper"},
        notes=(
            "Places market-only orders against a local simulated ledger, filled "
            "at the current Gamma API outcome price. No real funds, wallet, or "
            "order ever reaches Polymarket. Shares the same ledger as "
            "polymarket-paper-readonly."
        ),
    ),
    TradingProfile(
        id="polymarket-live-readonly",
        connector="polymarket",
        label="Polymarket Live · Read-Only",
        environment="live",
        transport="broker_sdk",
        capabilities=READ_CAPABILITIES,
        readonly=True,
        config={"profile": "live-readonly"},
        notes=(
            "Reads a real Polymarket wallet's positions via the public Data API "
            "from a wallet address only — no private key or API credential is "
            "read, stored, or accepted by this profile. Live order placement is "
            "not implemented; see the module docstring in this file before "
            "adding it."
        ),
    ),
)
