# Polymarket live trading — design plan (Phase 2, not implemented)

Status: **planning only**. No live-trade code exists yet. This document scopes
a future, separately-reviewed PR that adds `polymarket-live-trade` (real
orders, real funds) on top of the paper + live-readonly connector shipped in
`feat/polymarket-connector`. Nothing here should be implemented casually —
see "Why a separate PR" below.

## Why a separate PR

Every other connector's live order path is gated by one shared,
safety-critical surface: `InstrumentType` / `AssetClass` in
`agent/src/live/mandate/model.py`, consulted by the mandate gate, the
kill-switch sweep, and the audit ledger for **every** broker at once (grep
shows 12 files touching these enums). Adding a new enum member is low-risk in
isolation, but the PR that adds it is exactly the kind of change
`AGENT_CONTRIBUTOR_GUIDE.md` calls out as safety-critical "even when a change
appears small" and asks for explicit maintainer/operator sign-off before any
live-order-affecting code runs. Bundling it into the paper-connector PR would
force reviewers to approve shared-gate changes to review paper trading, which
is the opposite of a minimal, reviewable diff.

## Scope

1. New mandate enum members
   - `InstrumentType.PREDICTION_MARKET = "prediction_market"`
   - `AssetClass.PREDICTION_MARKET = "prediction_market"`
   - Additive only: an existing mandate's `allowed_instruments` /
     `allowed_asset_classes` will not contain `"prediction_market"` unless a
     user explicitly adds it, so this is fail-closed by construction — no
     existing user gains prediction-market trading by upgrading.

2. New profile: `polymarket-live-trade`
   - `environment="live"`, `capabilities=READ_CAPABILITIES + ("orders.place.requires_mandate",)`, `readonly=False`.
   - Auth: a Polygon-network wallet private key (or a browser-exported CLOB
     API key/secret/passphrase triple — Polymarket's CLOB supports both L1
     wallet-signed and L2 API-key auth; prefer L2 API-key auth so the raw
     private key never has to be handled by this process at all).

3. `sdk.py` additions
   - `place_order`: real orders via `py-clob-client`. Unlike the paper
     connector (market-only, fills instantly at the last traded price),
     Polymarket's CLOB is a real resting order book — this should support
     genuine limit orders (`order_type="limit"` with `limit_price`), not
     just market fills, since faking a market order against a real order
     book by crossing the spread blindly is worse execution than exposing
     the limit-order primitive Polymarket already gives us.
   - `cancel_order`: real cancel via `py-clob-client`.
   - Both keep the existing `if cfg.environment != "paper": ...` structural
     guard pattern for any *non*-live-trade profile (paper, live-readonly)
     that reaches these functions — the new live-trade path is additive, not
     a replacement of the existing hard refusal for the other two profiles.

4. `trading/service.py`
   - `_CONNECTOR_INSTRUMENT["polymarket"] = ("prediction_market", "prediction_market")`
     so `_order_classification` can resolve `(InstrumentType, AssetClass)` for
     the mandate gate — mirrors the OKX/Binance crypto entry exactly.

5. `classification.py`
   - No change needed: `place_order`/`cancel_order` are already pinned
     `ToolClass.WRITE`.

## Credentials

- L2 API-key auth (preferred): `POLY_API_KEY` / `POLY_API_SECRET` /
  `POLY_PASSPHRASE`, generated from an already-funded wallet via
  `py-clob-client`'s `create_or_derive_api_creds()`. These are bearer
  credentials for order placement but cannot move funds or change the
  wallet's approvals — a meaningfully smaller blast radius than a private key.
- If L1 (raw private key) support is added anyway for users who cannot run
  the derivation step interactively: store only via the OS keyring
  (`ConnectionStore`/`credentials.py`, matching every other connector), never
  in `polymarket.json`, never logged, never included in an error message or
  audit-ledger entry. `SECURITY.md`'s existing rule already covers this; no
  new policy needed, just correct implementation.
- `onboarding.py`'s `_BUILTIN["polymarket"]` entry gains the live-trade
  credential fields (all `secret=True` except a derived/public proxy address
  if one is surfaced), following the OKX/Binance shape.

## Test plan (mirrors existing order-gate suites)

- `test_sdk_order_gate.py`-style coverage: a fake/mocked `py-clob-client` so
  no test ever reaches the real network or a real wallet.
- `test_mandate_enforcement.py`-style coverage: an order outside the user's
  mandate (wrong instrument type, size over cap, exposure over cap, daily
  count over cap) is refused before any CLOB call — same shape as the
  existing crypto/equity cases, just with `instrument_type="prediction_market"`.
- `test_killswitch_blocks_orders.py`-style coverage: a tripped kill switch
  blocks `polymarket-live-trade` exactly like every other live profile.
- `test_paper_capped_connectors_refuse_live.py` stays green unmodified: this
  connector moves from "no live path" to "gated live path," which is a
  different test than the paper-capped one (that test is for connectors with
  *no* structural discriminator at all — Polymarket's discriminator becomes
  "does the mandate gate admit `prediction_market`", same shape as crypto).
- `test_the_pinned_broker_count_matches_the_shipped_connectors` and the
  README broker-table tests do **not** need a broker-count bump (Polymarket
  already counts as one of the 15) — only the capability note in the broker
  table changes ("no live order placement yet" → whatever the shipped
  capability line becomes).

## Rollout

- Off by default in the sense every live connector already is: a fresh
  install's mandate has an empty `allowed_instruments`, so nothing trades
  until a user explicitly opts in.
- README: update the Polymarket broker-table row (drop "no live order
  placement yet") across all six locales, same mechanical edit pattern used
  for the paper connector's landing.
- `CHANGELOG.md`: a normal `[Unreleased]` entry, written at PR/merge time
  with a real PR number — not pre-written here.

## Open questions for maintainers before starting implementation

1. L2 API-key auth vs raw private key: is L1 support wanted at all, or is
   L2-only (derived, revocable, funds-incapable) an acceptable — and safer —
   scope cut for v1?
2. Limit vs market orders: confirm the plan to expose real CLOB limit orders
   (rather than mimicking every other connector's `order_type="market"`
   default) is the right call for this asset class, given Polymarket has no
   continuous market-maker guaranteeing a fair "market" fill the way an
   equity/crypto venue does.
3. Whether `AssetClass.PREDICTION_MARKET` should be a single bucket or
   split further (e.g. by category — politics/sports/crypto-price markets
   already excluded by the standalone bot's own filter) — a single bucket
   matches how `AssetClass.CRYPTO` is used today and seems sufficient unless
   a maintainer wants finer-grained mandate control from day one.
