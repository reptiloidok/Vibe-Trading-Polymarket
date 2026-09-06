"""Polymarket connector: public Gamma/CLOB reads plus a local paper ledger.

Polymarket has no vendor-hosted demo account, so "paper" here means: real
market data from the public Gamma API (``https://gamma-api.polymarket.com``,
no key required) and the public CLOB price-history endpoint, with orders
filled locally against the current outcome price and recorded in a JSON
ledger under the runtime root (``~/.vibe-trading/polymarket/paper.json``).
This mirrors the standalone ``Poly_trade`` virtual trading bot this connector
was adapted from.

``live-readonly`` reads a real wallet's positions from the public Data API
(``https://data-api.polymarket.com``) by wallet address only; no private key
or API credential is read, stored, or required. Live order placement is not
implemented — see the module docstring in ``profiles.py`` before adding it.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

import requests

from src.config.paths import get_runtime_root

CONFIG_FILENAME = "polymarket.json"
GAMMA_BASE = "https://gamma-api.polymarket.com"
DATA_API_BASE = "https://data-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
PAPER_GUARD = "local_simulated_ledger"
LIVE_GUARD = "wallet_address_readonly"

#: Profiles this connector understands and their account environment.
PROFILE_ENVIRONMENTS = {"paper": "paper", "live-readonly": "live"}

DEFAULT_PAPER_BALANCE = 1000.0

_BAR_INTERVALS = {"1h": "1h", "6h": "6h", "1d": "1d", "1w": "1w", "1m": "1m", "max": "max"}

#: Per-call override keys a caller may set on top of the saved config. No
#: credential lives here: paper needs none, and live-readonly's only setting
#: is a public wallet address.
_OVERRIDE_KEYS = ("wallet_address", "starting_balance", "timeout")


class PolymarketConfigError(RuntimeError):
    """Raised when the connector configuration is missing or invalid."""


class PolymarketAPIError(RuntimeError):
    """Raised when Polymarket's public API returns an HTTP, network, or JSON error."""


@dataclass(frozen=True)
class PolymarketConfig:
    """Polymarket connector connection settings.

    Args:
        profile: ``paper`` or ``live-readonly``.
        wallet_address: Public wallet address to read live positions for.
            Used only by ``live-readonly``; never a private key.
        starting_balance: Simulated USDC balance a fresh paper ledger starts
            with.
        timeout: Network timeout in seconds.
        readonly: Always true for ``live-readonly``. Paper may write to its
            own local ledger; the profile (not this flag) decides that.
    """

    profile: str = "paper"
    wallet_address: str = ""
    starting_balance: float = DEFAULT_PAPER_BALANCE
    timeout: float = 20.0
    readonly: bool = True

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None = None) -> "PolymarketConfig":
        """Build a config from a JSON-like mapping, normalizing the profile."""
        payload = dict(data or {})
        profile = str(payload.get("profile") or "paper").strip().lower()
        if profile not in PROFILE_ENVIRONMENTS:
            raise PolymarketConfigError("profile must be 'paper' or 'live-readonly'")
        return cls(
            profile=profile,
            wallet_address=str(payload.get("wallet_address") or "").strip(),
            starting_balance=float(payload.get("starting_balance") or DEFAULT_PAPER_BALANCE),
            timeout=float(payload.get("timeout") or 20.0),
            readonly=bool(payload.get("readonly", True)),
        )

    @property
    def environment(self) -> str:
        """Return ``paper`` or ``live`` for this profile."""
        return PROFILE_ENVIRONMENTS.get(self.profile, "paper")


def config_path() -> Path:
    """Return the user-level Polymarket config path."""
    return get_runtime_root() / CONFIG_FILENAME


def load_config() -> PolymarketConfig:
    """Load Polymarket settings from ``~/.vibe-trading/polymarket.json``."""
    path = config_path()
    if not path.exists():
        return PolymarketConfig()
    try:
        return PolymarketConfig.from_mapping(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise PolymarketConfigError(f"invalid Polymarket config at {path}: {exc}") from exc


def save_config(config: PolymarketConfig) -> Path:
    """Persist Polymarket settings with owner-only permissions."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(config), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def build_config(
    profile_config: Mapping[str, Any] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> "PolymarketConfig":
    """Resolve config: saved file <- profile defaults <- CLI overrides."""
    base = asdict(load_config())
    for key, value in dict(profile_config or {}).items():
        if value is not None:
            base[key] = value
    cfg = PolymarketConfig.from_mapping(base)
    clean = {
        k: v
        for k, v in dict(overrides or {}).items()
        if k in _OVERRIDE_KEYS and v not in (None, "")
    }
    if not clean:
        return cfg
    return PolymarketConfig.from_mapping({**asdict(cfg), **clean})


# ---------------------------------------------------------------------------
# Public API transport (Gamma market data + Data API + CLOB price history).
# ---------------------------------------------------------------------------


def _get(path: str, *, base: str = GAMMA_BASE, params: Mapping[str, Any] | None = None, timeout: float = 20.0) -> Any:
    try:
        resp = requests.get(
            f"{base}{path}",
            params=dict(params or {}),
            timeout=timeout,
            headers={"User-Agent": "vibe-trading-polymarket-connector/0.1"},
        )
    except requests.RequestException as exc:
        raise PolymarketAPIError(f"Polymarket request to {path} failed: {exc}") from exc
    if resp.status_code >= 400:
        raise PolymarketAPIError(f"Polymarket API error {resp.status_code} on {path}: {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise PolymarketAPIError(f"Polymarket returned a non-JSON response from {path}: {exc}") from exc


def _parse_json_list(value: Any) -> list:
    """``outcomes`` / ``outcomePrices`` / ``clobTokenIds`` arrive JSON-encoded."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []


def _market_row(raw: dict[str, Any]) -> dict[str, Any]:
    outcomes = [str(o) for o in _parse_json_list(raw.get("outcomes"))]
    prices: list[float] = []
    for p in _parse_json_list(raw.get("outcomePrices")):
        try:
            prices.append(float(p))
        except (TypeError, ValueError):
            prices.append(0.0)
    return {
        "id": str(raw.get("id", "")),
        "condition_id": str(raw.get("conditionId", "")),
        "question": raw.get("question") or "",
        "outcomes": outcomes,
        "prices": prices,
        "closed": bool(raw.get("closed")),
        "active": True if raw.get("active") is None else bool(raw.get("active")),
        "end_date": raw.get("endDate"),
        "liquidity": float(raw.get("liquidity") or 0.0),
        "volume": float(raw.get("volume") or 0.0),
        "raw": raw,
    }


def _find_market(symbol: str, *, timeout: float = 20.0) -> dict[str, Any]:
    """Resolve ``symbol`` (a Gamma market id, slug, or conditionId) to a market row."""
    token = str(symbol or "").strip()
    if not token:
        raise PolymarketAPIError("symbol is required (Gamma market id, slug, or conditionId)")
    if token.isdigit():
        payload = _get(f"/markets/{token}", timeout=timeout)
        if isinstance(payload, dict) and payload.get("id"):
            return _market_row(payload)
    for params in ({"condition_ids": token}, {"slug": token}):
        payload = _get("/markets", params=params, timeout=timeout)
        rows = payload.get("data", []) if isinstance(payload, dict) else payload
        if isinstance(rows, list) and rows:
            return _market_row(rows[0])
    raise PolymarketAPIError(f"Polymarket market not found: {token!r}")


def get_quote(symbol: str, *, config: PolymarketConfig | None = None, **_: Any) -> dict[str, Any]:
    """Fetch the current outcome prices for one Polymarket market."""
    cfg = config or load_config()
    market = _find_market(symbol, timeout=cfg.timeout)
    yes_price = next(
        (p for o, p in zip(market["outcomes"], market["prices"]) if o.strip().lower() == "yes"),
        market["prices"][0] if market["prices"] else None,
    )
    return {
        "status": "ok",
        "profile": cfg.profile,
        "symbol": symbol,
        "market": market,
        "quote": {"outcomes": market["outcomes"], "prices": market["prices"], "yes_price": yes_price},
    }


def get_historical_bars(
    symbol: str,
    *,
    config: PolymarketConfig | None = None,
    period: str = "1d",
    limit: int = 90,
    **_: Any,
) -> dict[str, Any]:
    """Fetch the Yes-token price history via the public CLOB prices-history endpoint."""
    cfg = config or load_config()
    market = _find_market(symbol, timeout=cfg.timeout)
    token_ids = _parse_json_list(market["raw"].get("clobTokenIds"))
    if not token_ids:
        return {
            "status": "error",
            "error": "market has no CLOB token ids (it may be closed or malformed)",
            "symbol": symbol,
        }
    interval = _BAR_INTERVALS.get(str(period or "1d").strip().lower(), "1d")
    payload = _get(
        "/prices-history",
        base=CLOB_BASE,
        params={"market": token_ids[0], "interval": interval, "fidelity": 60},
        timeout=cfg.timeout,
    )
    history = payload.get("history") if isinstance(payload, dict) else []
    bars = [
        {"t": pt.get("t"), "price": pt.get("p")}
        for pt in (history or [])
        if isinstance(pt, dict)
    ][-max(1, int(limit)) :]
    return {"status": "ok", "profile": cfg.profile, "symbol": symbol, "period": period, "bars": bars}


# ---------------------------------------------------------------------------
# Local paper ledger (paper profile only).
# ---------------------------------------------------------------------------


def _ledger_path(cfg: PolymarketConfig) -> Path:
    return get_runtime_root() / "polymarket" / f"{cfg.profile}.json"


def _load_ledger(cfg: PolymarketConfig) -> dict[str, Any]:
    path = _ledger_path(cfg)
    if not path.exists():
        return {"balance": cfg.starting_balance, "positions": {}, "fills": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {"balance": cfg.starting_balance, "positions": {}, "fills": []}
    data.setdefault("balance", cfg.starting_balance)
    data.setdefault("positions", {})
    data.setdefault("fills", [])
    return data


def _save_ledger(cfg: PolymarketConfig, ledger: dict[str, Any]) -> None:
    path = _ledger_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _resolve_outcome(market: dict[str, Any], side: str) -> str:
    token = str(side or "").strip().lower()
    outcomes = market["outcomes"]
    if token in ("yes", "buy"):
        return "Yes" if "Yes" in outcomes else (outcomes[0] if outcomes else "Yes")
    if token in ("no", "sell"):
        return "No" if "No" in outcomes else (outcomes[1] if len(outcomes) > 1 else "No")
    for outcome in outcomes:
        if outcome.strip().lower() == token:
            return outcome
    raise PolymarketAPIError(f"unrecognized side {side!r}; use 'yes'/'no' or an outcome label")


def _outcome_price(market: dict[str, Any], outcome: str) -> float | None:
    for o, p in zip(market["outcomes"], market["prices"]):
        if o == outcome:
            return p
    return None


# ---------------------------------------------------------------------------
# Uniform read/write interface (see ``_SDK_CONNECTOR_MODULES`` in service.py).
# ---------------------------------------------------------------------------


def check_status(config: PolymarketConfig | None = None) -> dict[str, Any]:
    """Check configuration and reachability without placing or mutating anything."""
    cfg = config or load_config()
    report: dict[str, Any] = {
        "status": "ok",
        "configured": True,
        "config": {"profile": cfg.profile, "wallet_address": cfg.wallet_address or None},
        "sdk": {"package": "requests", "installed": True},
        "paper_guard": PAPER_GUARD if cfg.profile == "paper" else LIVE_GUARD,
    }
    if cfg.profile == "live-readonly" and not cfg.wallet_address:
        report["status"] = "error"
        report["configured"] = False
        report["error"] = "polymarket-live-readonly requires wallet_address"
        return report
    try:
        snapshot = get_account_snapshot(cfg)
    except PolymarketAPIError as exc:
        report["status"] = "error"
        report["error"] = str(exc)
        return report
    if snapshot.get("status") != "ok":
        report["status"] = "error"
        report["error"] = snapshot.get("error", "polymarket account read failed")
        return report
    report["account"] = snapshot.get("account")
    return report


def get_account_snapshot(config: PolymarketConfig | None = None) -> dict[str, Any]:
    """Fetch account balance/equity for the configured profile."""
    cfg = config or load_config()
    if cfg.profile == "paper":
        ledger = _load_ledger(cfg)
        equity = ledger["balance"] + sum(
            p["shares"] * p["avg_price"] for p in ledger["positions"].values()
        )
        return {
            "status": "ok",
            "profile": cfg.profile,
            "paper_guard": PAPER_GUARD,
            "account": {"balance": ledger["balance"], "equity": equity, "currency": "USDC (simulated)"},
        }
    if not cfg.wallet_address:
        return {"status": "error", "error": "wallet_address is required for polymarket-live-readonly"}
    payload = _get("/value", base=DATA_API_BASE, params={"user": cfg.wallet_address}, timeout=cfg.timeout)
    return {
        "status": "ok",
        "profile": cfg.profile,
        "paper_guard": LIVE_GUARD,
        "account": {"wallet_address": cfg.wallet_address, "value": payload},
    }


def get_positions(config: PolymarketConfig | None = None) -> dict[str, Any]:
    """List open positions for the configured profile."""
    cfg = config or load_config()
    if cfg.profile == "paper":
        ledger = _load_ledger(cfg)
        positions = [p for p in ledger["positions"].values() if p["shares"] > 1e-9]
        return {"status": "ok", "profile": cfg.profile, "paper_guard": PAPER_GUARD, "positions": positions}
    if not cfg.wallet_address:
        return {"status": "error", "error": "wallet_address is required for polymarket-live-readonly"}
    rows = _get("/positions", base=DATA_API_BASE, params={"user": cfg.wallet_address}, timeout=cfg.timeout)
    return {
        "status": "ok",
        "profile": cfg.profile,
        "paper_guard": LIVE_GUARD,
        "positions": rows if isinstance(rows, list) else [],
    }


def get_open_orders(config: PolymarketConfig | None = None, *, include_executions: bool = False) -> dict[str, Any]:
    """List resting orders (always empty: paper fills are immediate) and fill history."""
    cfg = config or load_config()
    if cfg.profile != "paper":
        return {"status": "error", "error": "live order history is not implemented for polymarket-live-readonly"}
    ledger = _load_ledger(cfg)
    result: dict[str, Any] = {"status": "ok", "profile": cfg.profile, "paper_guard": PAPER_GUARD, "orders": []}
    if include_executions:
        result["history"] = ledger.get("fills", [])
    return result


def place_order(
    config: PolymarketConfig | None = None,
    *,
    symbol: str,
    side: str,
    quantity: float | None = None,
    notional: float | None = None,
    order_type: str = "market",
    limit_price: float | None = None,
    time_in_force: str = "day",
    request_id: str | None = None,
) -> dict[str, Any]:
    """Fill a paper order immediately at the current Gamma outcome price.

    Only market orders against the local paper ledger are supported:
    Polymarket has no vendor sandbox, so there is no resting order book to
    simulate against. ``side`` selects the outcome — ``"yes"``/``"no"`` (or
    ``"buy"``/``"sell"`` of the Yes token), or an outcome label directly —
    rather than a direction on a single instrument, matching how Polymarket
    markets are structured. ``limit_price`` and ``time_in_force`` are accepted
    for interface parity with other connectors but have no effect here.
    """
    cfg = config or load_config()
    # ---- HARD GUARD: no vendor demo/live discriminator (must run first) ----
    if cfg.environment != "paper":
        return {
            "status": "error",
            "error": "polymarket order placement is only supported on paper profiles in this version",
        }
    market = _find_market(symbol, timeout=cfg.timeout)
    outcome = _resolve_outcome(market, side)
    price = _outcome_price(market, outcome)
    if not price or price <= 0:
        return {"status": "error", "error": f"no tradable price for outcome {outcome!r}"}
    if quantity is None and notional is None:
        return {"status": "error", "error": "either quantity or notional is required"}
    shares = float(quantity) if quantity is not None else float(notional) / price
    cost = shares * price

    ledger = _load_ledger(cfg)
    if cost > ledger["balance"] + 1e-9:
        return {
            "status": "error",
            "error": f"insufficient paper balance: need {cost:.2f}, have {ledger['balance']:.2f}",
        }
    key = f"{market['condition_id']}:{outcome}"
    pos = ledger["positions"].get(
        key,
        {"condition_id": market["condition_id"], "question": market["question"], "outcome": outcome, "shares": 0.0, "avg_price": 0.0},
    )
    total_shares = pos["shares"] + shares
    pos["avg_price"] = (pos["avg_price"] * pos["shares"] + cost) / total_shares if total_shares else 0.0
    pos["shares"] = total_shares
    ledger["positions"][key] = pos
    ledger["balance"] -= cost

    fill = {
        "id": request_id or str(uuid.uuid4()),
        "symbol": symbol,
        "condition_id": market["condition_id"],
        "outcome": outcome,
        "shares": shares,
        "price": price,
        "cost": cost,
        "ts": time.time(),
    }
    ledger.setdefault("fills", []).append(fill)
    _save_ledger(cfg, ledger)
    return {"status": "ok", "profile": cfg.profile, "paper_guard": PAPER_GUARD, "fill": fill, "balance": ledger["balance"]}


def cancel_order(
    config: PolymarketConfig | None = None,
    order_id: str = "",
    *,
    symbol: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Refuse a non-paper cancel before any lookup; paper fills are immediate.

    Polymarket has no vendor demo account, so live order actions are refused
    structurally rather than routed through one. Paper fills are immediate
    (market-only), so there is never anything resting to cancel either way.
    """
    cfg = config or load_config()
    # ---- HARD GUARD: no vendor demo/live discriminator (must run first) ----
    if cfg.environment != "paper":
        return {
            "status": "error",
            "error": "polymarket order cancellation is only supported on paper profiles in this version",
            "order_id": order_id,
        }
    return {
        "status": "error",
        "error": f"no open order {order_id!r}: paper fills are immediate (market-only)",
        "order_id": order_id,
    }
