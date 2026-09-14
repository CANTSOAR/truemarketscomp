# Automated Strategies Platform — Full Product Spec

**True Markets | Internal Document**

---

## Executive Summary

True Markets' Automated Strategies Platform enables users to run persistent, server-side trading strategies without writing a single line of code — and without burning LLM tokens on every decision. The platform supports two distinct strategy types that serve fundamentally different use cases:

**Rules-Based Strategies** are hosted entirely on True Markets infrastructure. The user defines deterministic IF/THEN conditions (price levels, technical indicators, inventory state) through a visual rule builder. True Markets evaluates those conditions continuously against live market data and fires orders automatically. No external agent is needed. No tokens are burned per decision.

**Agent Strategies (BYOA)** connect an external algorithmic agent — a Python script, an LLM reasoning loop, a custom model — to True Markets via a secure webhook. The agent makes its own decisions and sends order signals. True Markets executes them after running mandatory risk checks.

Both types share the same risk engine, the same margin framework, the same kill switch mechanics, and the same performance dashboard. The split is at the decision layer only.

True Markets acts solely as a **technology provider and execution venue** in both cases. Users author their own strategies. True Markets never advises, recommends, or modifies a user's strategy logic.

---

## Table of Contents

1. Legal Positioning
2. Architecture
3. Strategy Types
4. Existing API Surface
5. New Endpoints to Build
6. Database Schema
7. Per-Strategy Margin Rules
8. Risk Engine
9. Kill Switch Mechanics
10. Strategy Dashboard UI
11. Implementation Status
12. Regulatory Guardrails
13. Implementation Workflow

---

## 1. Legal Positioning

### Investment Advisers Act of 1940

An investment advisor is defined as any person who, **for compensation**, is in the business of **advising others** as to the **value of securities or the advisability of investing in, purchasing, or selling securities**. All three prongs must be present. True Markets avoids advisory status because:

- **True Markets never creates the strategy logic.** For rules strategies, the user defines all conditions and actions. For agent strategies, the external agent defines the decision logic. True Markets evaluates or executes the output — it does not generate it.
- **True Markets exercises no discretion.** Rules are evaluated mechanically. Orders are executed as submitted. No True Markets system modifies, reorders, or overrides a user's strategy logic except for the mandatory risk guardrails, which are safety controls, not investment advice.
- **Compensation is for execution, not advice.** True Markets charges execution fees. There are no success fees, performance allocations, or advisory fees tied to strategy outcomes.

### Why Hosting Rules Is Legally Defensible

The regulatory risk is not about *where* the rules live. It is about *who exercises discretion*. True Markets stores a user's rule (e.g., `IF RSI(14) < 30 THEN buy 0.001 BTC`) and evaluates it mechanically. The user made the investment judgment when they wrote the rule. True Markets is a conditional execution engine — the same legal character as Interactive Brokers' conditional orders, TradingView's Pine Script auto-execution, and Alpaca's rule triggers. None of these platforms are registered investment advisors.

The line is crossed only if True Markets recommends which rules to use, charges based on strategy performance, or modifies rules on the user's behalf.

### Commodity Trading Advisor (CFTC)

BTC and most crypto assets are classified as commodities under CFTC jurisdiction, not securities under SEC jurisdiction. Rules-based trading of commodities could implicate the Commodity Trading Advisor (CTA) registration requirement under the Commodity Exchange Act. The **solely incidental exemption** applies: advice is solely incidental to True Markets' principal business as an execution venue. This exemption requires that True Markets does not hold itself out as a CTA and does not receive separate compensation for trading advice.

### Non-Negotiable Legal Guardrails

1. No rule recommendations — True Markets never suggests which rules a user should use.
2. User attestation at strategy creation — users sign an acknowledgment that they authored the strategy.
3. No performance-based fees — flat execution fees only.
4. Templates are illustrative, never prescriptive — any pre-built rule examples are clearly labeled as non-recommendations.
5. True Markets does not store or evaluate the decision logic of Agent Strategies — only the resulting order signals pass through the webhook.

---

## 2. Architecture

### Two-Path Execution Model

```
┌─────────────────────────────────────┐    ┌──────────────────────────────────────┐
│         RULES-BASED STRATEGY        │    │          AGENT STRATEGY (BYOA)        │
│                                     │    │                                       │
│  User defines conditions + actions  │    │  External agent makes decisions       │
│  via dashboard rule builder.        │    │  (Python, LLM loop, custom model).    │
│  True Markets evaluates rules       │    │  Agent sends order signal to          │
│  server-side on a live market feed. │    │  True Markets webhook.                │
│                                     │    │                                       │
│  No external agent. No token burn.  │    │  Agent incurs its own compute cost.   │
│  Deterministic, always-on.          │    │  Flexible, arbitrary logic.           │
└────────────────┬────────────────────┘    └──────────────────┬────────────────────┘
                 │                                            │
                 │  Internal rule fire                        │  POST /v1/agent/webhook
                 ▼                                            ▼
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                           True Markets Strategies Gateway                            │
│                                                                                     │
│   1. Auth Check        →  verify identity (scoped key or internal rules engine)     │
│   2. Strategy Lookup   →  load strategy config + margin rules from DB               │
│   3. Risk Engine       →  10-point check: status, pairs, position, margin,          │
│                           drawdown, daily loss, volume cap, wash trade               │
│   4. Order Router      →  POST /v1/conductor/orders  (lib/execution.py)             │
│   5. Fill Tracker      →  write to strategy_orders, enqueue PnL rollup              │
│                                                                                     │
└────────────────────────────────────┬────────────────────────────────────────────────┘
                                     │
                                     ▼
                    True Markets Conductor API
                    ├── CeFi order book
                    ├── Solana (DeFi)
                    └── Base / ETH L2 (DeFi)
```

### Key Shared Infrastructure

- **Single risk engine** — both strategy types pass through the same 10-point check before any order reaches the Conductor.
- **Single `strategy_orders` ledger** — every execution is tagged with `strategy_id` and `source` (`rules_engine` or `webhook`) for a clean audit trail.
- **Single margin framework** — same margin allocation, drawdown limits, and kill switch mechanics regardless of strategy type.
- **Single dashboard** — performance, fill history, and controls are unified across both types.

---

## 3. Strategy Types

### 3.1 Rules-Based Strategies

A rules-based strategy is a set of one or more **rules**, each consisting of a **condition** and an **action**. True Markets evaluates all active rules for a strategy on a configurable interval (default 5 seconds) using a live market data feed. When a condition is met, the action is passed through the risk engine and executed.

**Appropriate use cases:**
- Stop-loss / take-profit triggers
- Dollar-cost averaging (DCA) on price dips
- RSI or moving average crossover entries
- Inventory rebalancing (buy when position falls below a threshold)
- Time-based orders (e.g., execute at market open)

**Supported condition indicators:**

| Indicator | Parameters | Description |
|---|---|---|
| `last_price` | — | Current last traded price |
| `mid_price` | — | (best_bid + best_ask) / 2 |
| `sma` | `period` (candles) | Simple moving average |
| `ema` | `period` (candles) | Exponential moving average |
| `rsi` | `period` (candles) | Relative Strength Index |
| `vwap` | `window_secs` | Volume-weighted average price over window |
| `volume` | `window_secs` | Traded volume over window vs. rolling average |
| `inventory_btc` | — | Strategy's current net BTC position |
| `realized_pnl` | — | Strategy's realized PnL since session start |
| `spread` | — | best_ask − best_bid |

**Supported operators:** `gt` (>), `lt` (<), `gte` (≥), `lte` (≤), `eq` (=), `cross_above`, `cross_below`

**Supported actions:**

| Action | Parameters | Description |
|---|---|---|
| `place_market_order` | `side`, `qty`, `qty_unit` | Market order at current price |
| `place_limit_order` | `side`, `qty`, `qty_unit`, `price_type`, `price_value` | Limit order; price can be absolute or offset from current bid/ask by % |
| `cancel_all_orders` | — | Cancel all open orders for this strategy |
| `cancel_side` | `side` | Cancel all open orders on one side |

**Rule cooldown:** Each rule has an optional `cooldown_secs` parameter (default 60s). After a rule fires, it cannot fire again until the cooldown expires. This prevents a sustained RSI < 30 condition from placing a new buy every 5 seconds.

**Condition composition:** Multiple conditions within a rule are combined with `AND` or `OR` logic. Complex rules can nest condition groups.

**Example rule (JSON):**
```json
{
  "name": "RSI dip buy",
  "cooldown_secs": 300,
  "condition": {
    "logic": "AND",
    "conditions": [
      { "indicator": "rsi", "params": { "period": 14 }, "operator": "lt", "value": 30 },
      { "indicator": "mid_price", "operator": "gt", "value": 50000 }
    ]
  },
  "action": {
    "type": "place_market_order",
    "side": "buy",
    "qty": "100",
    "qty_unit": "quote",
    "base_asset": "BTC",
    "quote_asset": "USDC"
  }
}
```

**Rules Engine internal loop:**

```
Every evaluation_interval_secs (default 5s):
  For each active rules strategy:
    Load all active rules for strategy
    Fetch current indicator values from market data cache
    For each rule:
      If cooldown has not expired → skip
      Evaluate condition against current indicator values
      If condition TRUE:
        Pass action through GatewayRiskEngine (same as webhook path)
        If risk check passes → ExecutionClient.place_order()
        Write to strategy_orders with source = 'rules_engine'
        Set rule.last_fired_at = NOW(), increment rule.fire_count
        Start cooldown timer
```

The market data cache is maintained by a background feed process that subscribes to the TrueMarkets native DEPTH WebSocket (`wss://api.truex.co/api/v1`) and the Coinbase WebSocket (`wss://ws-feed.exchange.coinbase.com`) — the same feeds already used by `lib/coinbase_feed.py` and `notebooks/maker.ipynb`.

---

### 3.2 Agent Strategies (BYOA)

An agent strategy connects an external decision-making process to True Markets via a secure webhook. The agent is responsible for all logic: when to trade, how much, at what price. True Markets is responsible only for risk enforcement and execution.

**Appropriate use cases:**
- LLM-driven research loops that reason about macro conditions
- Proprietary Python models with complex signal generation
- Multi-venue strategies that incorporate data from sources True Markets doesn't natively support
- Any strategy that requires reasoning beyond deterministic IF/THEN conditions

**How it works:**
1. User generates a scoped `trade_only` API key bound to the strategy
2. The user's agent mints a JWT using that key (same ES256 flow as master keys)
3. The agent sends order signals to `POST /v1/agent/webhook` with the JWT in the Authorization header
4. True Markets runs the risk engine check and routes the order to the Conductor
5. The result (order_id, status) is returned synchronously to the agent

The agent decides what to trade. True Markets decides whether it's safe to execute.

---

## 4. Existing API Surface

All True Markets Conductor API endpoints are available today and are already implemented in `lib/execution.py`.

**Base URLs:**

| Environment | URL |
|---|---|
| Production | `https://api.truemarkets.co/v1/conductor` |
| UAT / Sandbox | `https://api.uat.truemarkets.co/v1/conductor` |

**Authentication** (`lib/execution.py:113–133`):

```
POST /v1/auth/api-key/token
  Body: { key_id, timestamp, signature }   # ES256 sign of "{key_id}.{timestamp}"
  Response: { access_token, refresh_token }

POST /v1/auth/token/refresh
  Body: { refresh_token }
  Response: { access_token, refresh_token }
```

**Conductor endpoints in use:**

| Method | Path | Location | Purpose |
|---|---|---|---|
| `POST` | `/v1/conductor/quotes` | `lib/execution.py:393` | Non-binding CeFi price preview |
| `POST` | `/v1/conductor/orders` | `lib/execution.py:234` | Create market or limit order |
| `POST` | `/v1/conductor/orders/{id}/execute` | `lib/execution.py:257` | Submit client signatures (DeFi orders) |
| `GET` | `/v1/conductor/orders` | `lib/execution.py:379` | List orders with status filter + pagination |
| `GET` | `/v1/conductor/orders/{id}/status` | `lib/execution.py:319` | Poll single order status |
| `DELETE` | `/v1/conductor/orders/{id}` | `lib/execution.py:326` | Cancel CeFi limit order |
| `POST` | `/v1/conductor/transfers` | `lib/execution.py:456` | Create on-chain transfer |
| `POST` | `/v1/conductor/transfers/{id}/execute` | `lib/execution.py:462` | Execute signed transfer |
| `GET` | `/v1/conductor/transfers/{id}` | `lib/execution.py:471` | Get transfer status |
| `GET` | `/v1/conductor/balances` | `lib/execution.py:350` | Account balances (summed by symbol) |
| `GET` | `/v1/conductor/balances/unified` | `openapi.json` | Unified CeFi + DeFi balances |
| `GET` | `/v1/conductor/assets` | `lib/execution.py:417` | Asset catalog with pagination |

**Order lifecycle:**

```
initialized → (sign payloads) → pending → complete
                                        → active        (limit order resting in book)
                                        → canceled
                                        → failed
pending → cancel_pending → canceled
                         → complete     (race: filled before cancel landed)
```

---

## 5. New Endpoints to Build

All paths below are new and do not exist in the current Conductor API.

---

### 5.1 Shared Strategy Management

**`POST /v1/strategies`** — Create a strategy (either type).

```json
Request:
{
  "name": "RSI Dip Buyer",
  "type": "rules",
  "description": "Buy BTC dips on RSI < 30",
  "base_asset": "BTC",
  "quote_asset": "USDC",
  "margin_usd": 1000.00,
  "max_position_usd": 600.00,
  "max_daily_loss_usd": 150.00,
  "max_drawdown_pct": 20.0,
  "margin_call_threshold_pct": 80.0,
  "daily_volume_cap_usd": 50000.00,
  "allowed_venues": ["cefi"],
  "user_attestation": true
}

Response 201:
{
  "strategy_id": "strat_abc123",
  "type": "rules",
  "name": "RSI Dip Buyer",
  "status": "active",
  "created_at": "2026-07-08T00:00:00Z"
}
```

`user_attestation: true` is required. If false or absent, return 400. This is the legal guardrail — the user is affirming they authored the strategy.

**`GET /v1/strategies`** — List all strategies with 24h PnL summary.

**`GET /v1/strategies/{strategy_id}`** — Full strategy details.

**`PATCH /v1/strategies/{strategy_id}`** — Update margin parameters. Blocked if open positions exist.

**`DELETE /v1/strategies/{strategy_id}`** — Cancel open orders and archive the strategy.

---

### 5.2 Rules Engine (Rules Strategies only)

**`POST /v1/strategies/{strategy_id}/rules`** — Add a rule to a rules strategy.

```json
Request:
{
  "name": "RSI dip buy",
  "evaluation_interval_secs": 5,
  "cooldown_secs": 300,
  "condition": {
    "logic": "AND",
    "conditions": [
      { "indicator": "rsi", "params": { "period": 14 }, "operator": "lt", "value": 30 },
      { "indicator": "mid_price", "operator": "gt", "value": 50000 }
    ]
  },
  "action": {
    "type": "place_market_order",
    "side": "buy",
    "qty": "100",
    "qty_unit": "quote",
    "base_asset": "BTC",
    "quote_asset": "USDC"
  }
}

Response 201:
{
  "rule_id": "rule_xyz789",
  "strategy_id": "strat_abc123",
  "name": "RSI dip buy",
  "is_active": true,
  "fire_count": 0,
  "last_fired_at": null,
  "created_at": "2026-07-08T00:00:00Z"
}
```

**`GET /v1/strategies/{strategy_id}/rules`** — List all rules for a strategy.

**`PATCH /v1/strategies/{strategy_id}/rules/{rule_id}`** — Enable/disable a rule or update its parameters. Changes take effect on the next evaluation cycle.

**`DELETE /v1/strategies/{strategy_id}/rules/{rule_id}`** — Remove a rule.

**`POST /v1/strategies/{strategy_id}/rules/{rule_id}/test`** — Dry-run: evaluate the rule's condition against the current live market data and return whether it would fire. Does not place any order.

```json
Response 200:
{
  "rule_id": "rule_xyz789",
  "would_fire": false,
  "current_values": {
    "rsi_14": 42.3,
    "mid_price": 108430.50
  },
  "cooldown_remaining_secs": 0,
  "risk_check": "would_pass"
}
```

---

### 5.3 Agent Webhook (Agent Strategies only)

**`POST /v1/agent/webhook`**

Auth: `Authorization: Bearer <scoped_trade_only_jwt>`

```json
Request:
{
  "strategy_id": "strat_abc123",
  "base_asset": "BTC",
  "quote_asset": "USDC",
  "side": "buy",
  "type": "limit",
  "qty": "0.001",
  "qty_unit": "base",
  "price": "107500.00",
  "chain": null
}

Response 200 (accepted):
{
  "webhook_id": "wh_def456",
  "order_id": "ord_ghi789",
  "status": "pending",
  "risk_check": "passed"
}

Response 403 (risk block):
{
  "webhook_id": "wh_def456",
  "order_id": null,
  "status": "rejected",
  "risk_check": "failed",
  "reason": "margin_call: margin_used 85.3% exceeds threshold 80.0%"
}
```

Rate limit: 60 requests/minute per scoped key, enforced server-side independent of the Conductor's rate limit window. Exceeding returns 429.

Internal execution flow:
1. Verify JWT, check scoped key is active
2. Load strategy from DB, verify `type = 'agent'` and `status = 'active'`
3. Run GatewayRiskEngine 10-point check
4. Call `ExecutionClient.place_order()` → Conductor
5. If DeFi payloads returned, sign and call execute endpoint (existing `lib/execution.py:253–266`)
6. Write to `strategy_orders` with `source = 'webhook'`
7. Return result

---

### 5.4 Scoped API Key Management (Agent Strategies only)

**`POST /v1/agent/api-keys`** — Generate a `trade_only` scoped key bound to an agent strategy.

```json
Request:
{
  "strategy_id": "strat_abc123",
  "label": "maker-bot-prod"
}

Response 201:
{
  "key_id": "key_xyz789",
  "strategy_id": "strat_abc123",
  "permissions": ["trade"],
  "created_at": "2026-07-08T00:00:00Z"
}
```

The raw key material is returned once and never again. No withdrawal call from a scoped key reaches the Conductor — blocked at the gateway middleware layer regardless of payload.

**`DELETE /v1/agent/api-keys/{key_id}`** — Revoke a key.

---

### 5.5 Kill Switch

**`POST /v1/strategies/{strategy_id}/kill`** — Hard manual kill switch.

Immediately:
1. Sets strategy `status = 'disabled'`
2. Revokes the associated scoped API key (agent strategies) or pauses the rules evaluation loop (rules strategies)
3. Cancels all open orders tagged to this `strategy_id`

```json
Response 200:
{
  "strategy_id": "strat_abc123",
  "status": "disabled",
  "orders_canceled": 3,
  "rules_paused": 2
}
```

---

### 5.6 Performance

**`GET /v1/strategies/{strategy_id}/performance`**

```json
Response 200:
{
  "strategy_id": "strat_abc123",
  "type": "rules",
  "window": "24h",
  "realized_pnl_usd": 12.43,
  "unrealized_pnl_usd": -2.10,
  "total_pnl_usd": 10.33,
  "trade_count": 47,
  "volume_usd": 14230.00,
  "margin_allocated_usd": 1000.00,
  "margin_used_usd": 342.10,
  "margin_utilization_pct": 34.21,
  "status": "active",
  "last_rule_fire": "2026-07-08T11:22:00Z",
  "last_updated": "2026-07-08T12:34:56Z"
}
```

`unrealized_pnl_usd` uses Coinbase mid as the mark price. Falls back to TM book mid if Coinbase feed is stale beyond 3 seconds. Returns `null` if both are unavailable — never a fabricated value.

---

## 6. Database Schema

### `strategies`
```sql
CREATE TABLE strategies (
  strategy_id               TEXT PRIMARY KEY,
  user_id                   TEXT NOT NULL,
  type                      TEXT NOT NULL CHECK (type IN ('rules', 'agent')),
  name                      TEXT NOT NULL,
  description               TEXT,
  base_asset                TEXT NOT NULL,
  quote_asset               TEXT NOT NULL,
  status                    TEXT NOT NULL DEFAULT 'active'
                              CHECK (status IN ('active', 'disabled', 'margin_called')),
  margin_usd                NUMERIC(18,2) NOT NULL,
  max_position_usd          NUMERIC(18,2) NOT NULL,
  max_daily_loss_usd        NUMERIC(18,2) NOT NULL,
  max_drawdown_pct          NUMERIC(5,2)  NOT NULL,
  margin_call_threshold_pct NUMERIC(5,2)  NOT NULL,
  daily_volume_cap_usd      NUMERIC(18,2),
  allowed_venues            TEXT[],
  peak_equity_usd           NUMERIC(18,2),
  callback_url              TEXT,
  user_attested_at          TIMESTAMPTZ NOT NULL,
  created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at                TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### `rules`
```sql
CREATE TABLE rules (
  rule_id                   TEXT PRIMARY KEY,
  strategy_id               TEXT NOT NULL REFERENCES strategies(strategy_id),
  name                      TEXT NOT NULL,
  is_active                 BOOLEAN NOT NULL DEFAULT TRUE,
  evaluation_interval_secs  INTEGER NOT NULL DEFAULT 5,
  cooldown_secs             INTEGER NOT NULL DEFAULT 60,
  condition_json            JSONB NOT NULL,
  action_json               JSONB NOT NULL,
  fire_count                INTEGER NOT NULL DEFAULT 0,
  last_evaluated_at         TIMESTAMPTZ,
  last_fired_at             TIMESTAMPTZ,
  cooldown_until            TIMESTAMPTZ,
  created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at                TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ON rules (strategy_id, is_active);
```

### `agent_api_keys`
```sql
CREATE TABLE agent_api_keys (
  key_id         TEXT PRIMARY KEY,
  strategy_id    TEXT NOT NULL REFERENCES strategies(strategy_id),
  user_id        TEXT NOT NULL,
  label          TEXT,
  permissions    TEXT[] NOT NULL DEFAULT ARRAY['trade'],
  is_active      BOOLEAN NOT NULL DEFAULT TRUE,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  revoked_at     TIMESTAMPTZ
);
```

### `strategy_orders`
```sql
CREATE TABLE strategy_orders (
  id              BIGSERIAL PRIMARY KEY,
  strategy_id     TEXT NOT NULL REFERENCES strategies(strategy_id),
  rule_id         TEXT REFERENCES rules(rule_id),   -- NULL for agent strategies
  order_id        TEXT NOT NULL,                    -- TrueMarkets Conductor order_id
  webhook_id      TEXT,                             -- NULL for rules strategies
  source          TEXT NOT NULL CHECK (source IN ('rules_engine', 'webhook')),
  base_asset      TEXT NOT NULL,
  quote_asset     TEXT NOT NULL,
  side            TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
  order_type      TEXT NOT NULL CHECK (order_type IN ('market', 'limit')),
  qty             NUMERIC(28,8),
  qty_unit        TEXT,
  price           NUMERIC(18,2),
  executed_qty    NUMERIC(28,8),
  executed_vwap   NUMERIC(18,2),
  notional_usd    NUMERIC(18,2),
  pnl_usd         NUMERIC(18,4),
  status          TEXT NOT NULL,
  venue           TEXT,
  chain           TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  settled_at      TIMESTAMPTZ
);
CREATE INDEX ON strategy_orders (strategy_id, created_at DESC);
CREATE INDEX ON strategy_orders (order_id);
CREATE INDEX ON strategy_orders (rule_id, created_at DESC);
```

### `strategy_pnl_rollups`
```sql
CREATE TABLE strategy_pnl_rollups (
  strategy_id         TEXT NOT NULL REFERENCES strategies(strategy_id),
  window_start        TIMESTAMPTZ NOT NULL,
  window_end          TIMESTAMPTZ NOT NULL,
  realized_pnl_usd    NUMERIC(18,4) NOT NULL DEFAULT 0,
  unrealized_pnl_usd  NUMERIC(18,4),
  trade_count         INTEGER NOT NULL DEFAULT 0,
  volume_usd          NUMERIC(18,2) NOT NULL DEFAULT 0,
  margin_used_usd     NUMERIC(18,2) NOT NULL DEFAULT 0,
  computed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (strategy_id, window_start)
);
```

---

## 7. Per-Strategy Margin Rules

Each strategy is allocated a fixed capital budget. The Risk Engine enforces these limits on every order attempt regardless of source (rules engine or webhook). All figures are in USD notional.

### Defined Margin Table

| Strategy | Type | File | Margin Allocated | Max Open Position | Max Daily Loss | Max Drawdown | Margin Call At |
|---|---|---|---|---|---|---|---|
| `maker_v3` | Agent | `real_strats/maker_v3.py` | $1,000 | $600 notional | $150 | 20% | 80% used |
| `crossmarket` | Agent | `real_strats/crossmarket.py` | $2,000 | $1,000 per venue | $200 | 15% | 85% used |
| `maker_v2` | Agent | `real_strats/maker_v2.py` | $500 | $300 notional | $75 | 20% | 80% used |
| Default rules strategy | Rules | — | $500 | $250 notional | $100 | 20% | 75% used |
| Default BYOA strategy | Agent | — | $500 | $250 notional | $100 | 20% | 75% used |

**Margin utilization** = `open_position_usd + abs(min(unrealized_pnl_usd, 0))` as a percentage of `margin_usd`.

**Peak equity** tracks the high-water mark of `margin_usd + realized_pnl`. Drawdown is measured from this peak. `peak_equity_usd` in the `strategies` table is updated by the PnL rollup job whenever realized PnL improves.

---

## 8. Risk Engine

Both strategy types pass every order through the same `GatewayRiskEngine` before it reaches the Conductor. This class is a DB-backed, per-strategy extension of the existing in-memory `RiskManager` in `lib/risk.py`.

### Check Order (first failure stops evaluation and returns the rejection reason)

```
 1. strategy.status == 'active'               → else: reject (strategy disabled or margin_called)
 2. key is_active == true                      → else: reject (key revoked) [agent only]
 3. order pair in strategy.allowed_venues     → else: reject (pair not permitted)
 4. order venue in strategy.allowed_venues    → else: reject (venue not permitted)
 5. (open_position_usd + order_notional)
       <= max_position_usd                    → else: reject (position limit)
 6. margin_utilization_pct
       < margin_call_threshold_pct            → else: FIRE MARGIN CALL + reject
 7. realized_pnl_24h > -max_daily_loss_usd   → else: FIRE MARGIN CALL + reject
 8. drawdown_from_peak < max_drawdown_pct     → else: FIRE MARGIN CALL + reject
 9. (daily_volume_usd + order_notional)
       <= daily_volume_cap_usd               → else: reject (daily volume cap)
10. wash trade check: does incoming order
    cross against an open order from the
    same strategy on the opposite side
    on the same pair?                         → else: reject (wash trade)
```

Checks 1–5 and 9–10 are soft blocks that reject the order but leave the strategy running. Checks 6, 7, and 8 are hard blocks that fire the margin call sequence and permanently disable the strategy until a human re-enables it.

### Margin Call Sequence

When checks 6, 7, or 8 trigger:

```
1. UPDATE strategies SET status = 'margin_called'
   WHERE strategy_id = ?
   (SELECT FOR UPDATE — prevents concurrent double-fire)

2. For rules strategies: remove strategy from the rules evaluation loop immediately.
   For agent strategies: SET agent_api_keys.is_active = FALSE, revoked_at = NOW()

3. Query strategy_orders for all order_id values where status IN ('pending', 'active')

4. For each: DELETE /v1/conductor/orders/{order_id}
   Log success or failure per order (cancel failures are non-blocking)

5. Write margin_call event row to strategy_pnl_rollups

6. POST to strategies.callback_url if configured (non-blocking, best-effort)

7. Return 403 to the triggering request (webhook or internal rules engine)
```

The strategy cannot be re-enabled by the agent or the rules engine. Only the user, via the dashboard or a signed request using their master (non-scoped) key, can set `status = 'active'`. A new scoped key must be generated for agent strategies after a margin call.

---

## 9. Kill Switch Mechanics

There are two kill switch paths. Both run the same sequence from Section 8 (Margin Call Sequence).

**Manual** — User calls `POST /v1/strategies/{strategy_id}/kill` from the dashboard or programmatically. Status written is `disabled`.

**Automatic** — Risk Engine detects a breach on checks 6, 7, or 8. Status written is `margin_called`.

The only difference between the two is the `status` value and the re-enable flow. `disabled` can be re-enabled freely. `margin_called` requires the user to explicitly acknowledge the breach before re-enabling — the dashboard shows a modal with the breach details, the timestamp, and the final PnL at time of kill.

The existing `RiskManager.kill_switch_engaged` boolean in `lib/risk.py:8` is an in-memory flag used by the local strategy scripts. It does not persist across restarts and is not shared across instances. For the gateway, the authoritative kill switch state is `strategies.status` in the DB. The local flag in strategy scripts remains as a fast pre-check only.

---

## 10. Strategy Dashboard UI

Mobile-first, single-page application. Three panels.

### Panel 1 — Strategy List

- Cards for all strategies. Each card shows: type badge (RULES / AGENT), strategy name, status badge (active / disabled / margin_called), 24h PnL in USD, margin utilization bar.
- Margin bar: green below 60%, yellow 60–80%, red above 80%.
- Rules strategies show "last fired: Xm ago" below the name.
- Agent strategies show "last signal: Xm ago".
- "+ New Strategy" button opens a type-selection modal: "Rules-Based" or "Bring Your Own Agent".

### Panel 2 — Strategy Detail

Opened by tapping a card.

- **Header:** strategy name, type badge, status, kill switch button (red, requires confirm modal: "This will cancel all open orders and permanently disable this strategy. You must manually re-enable it.")
- **Stats row:** 24h PnL | Total Volume | Trade Count | Margin Utilization %
- **PnL sparkline:** 24h rolling chart from `strategy_pnl_rollups` 1-minute buckets.
- **Open positions:** pair, side, size, entry price, current mark, unrealized PnL.
- **Recent fills:** pair, side, size, fill price, timestamp, order_id, source (RULE: rule name or AGENT).

For rules strategies, a collapsible **Rules** section lists all rules with: name, active toggle, last fired timestamp, fire count, current indicator values, and a "Test Now" button (calls `POST /v1/strategies/{id}/rules/{rule_id}/test`).

### Panel 3 — Setup

For **Rules Strategies:**
- Rule builder form. Fields: name, indicator (dropdown), operator (dropdown), value, action type, action params, cooldown.
- Compound conditions: "Add condition" button with AND/OR selector.
- "Test Rule" button before saving.
- Margin settings form.

For **Agent Strategies:**
- "Generate API Key" wizard:
  1. User submits their EC P-256 public key.
  2. True Markets returns `key_id`. Raw key material is shown once.
  3. Webhook URL displayed: `POST https://api.truemarkets.co/v1/agent/webhook`
  4. Code snippet in Python / curl showing the full auth + webhook payload flow.
- "Test Webhook" button: sends a dry-run request to verify connectivity. Returns whether the current JWT is valid and the strategy is reachable.
- Margin settings form.

---

## 11. Implementation Status

### Already Built

| Component | Location | Status |
|---|---|---|
| ExecutionClient (orders, cancellation, balances, transfers) | `lib/execution.py` | Complete |
| RiskManager (in-memory kill switch, max loss, position cap) | `lib/risk.py` | Complete — needs DB persistence extension |
| Coinbase market data WebSocket feed | `lib/coinbase_feed.py` | Complete — reused by rules engine |
| TrueMarkets native DEPTH book | `notebooks/maker.ipynb` (TrueMarketsBook class) | Complete — extract to `lib/tm_feed.py` |
| Coinbase execution client | `lib/coinbase_execution.py` | Complete |
| Market making strategy (maker_v3) | `real_strats/maker_v3.py` | Complete — to be registered as agent strategy |
| Cross-market arbitrage | `real_strats/crossmarket.py` | Complete — to be registered as agent strategy |
| Rate limiter / pacing | `lib/leadlag.py` | Complete |
| True Markets Conductor API | `openapi.json` | External API — no build needed |

### Must Be Built

| Component | Type | Priority | Notes |
|---|---|---|---|
| DB schema (5 tables) | Infrastructure | P0 | Prerequisite for all else |
| Scoped API key auth + JWT middleware | Infrastructure | P0 | Blocks all gateway endpoints |
| `POST /v1/strategies` CRUD | Backend | P0 | Core config surface |
| `GatewayRiskEngine` (DB-backed, per-strategy) | Backend | P0 | Safety critical |
| Margin call sequence | Backend | P0 | Safety critical |
| `POST /v1/strategies/{id}/kill` | Backend | P0 | Safety critical |
| `POST /v1/agent/api-keys` + revocation | Backend | P0 | Agent strategies only |
| `POST /v1/agent/webhook` ingestion | Backend | P0 | Agent strategies |
| Rules evaluation loop (background service) | Backend | P0 | Rules strategies |
| Condition evaluator (indicators + operators) | Backend | P0 | Rules strategies |
| `POST /v1/strategies/{id}/rules` CRUD | Backend | P0 | Rules strategies |
| `POST /v1/strategies/{id}/rules/{id}/test` | Backend | P1 | Rules dry-run |
| Order syncer (fill detection, PnL per fill) | Backend | P1 | Required for accurate rollups |
| PnL rollup job (1-min buckets) | Backend | P1 | Powers dashboard chart |
| `GET /v1/strategies/{id}/performance` | Backend | P1 | Dashboard data feed |
| Extract `TrueMarketsBook` to `lib/tm_feed.py` | Refactor | P1 | Rules engine market data |
| Strategy Dashboard UI | Frontend | P1 | React / React Native |
| Register existing strategies (`maker_v3`, `crossmarket`) | Integration | P2 | After gateway is stable |

---

## 12. Regulatory Guardrails

These are non-negotiable implementation constraints. They are not features — they are the legal boundary conditions that define what this product is.

1. **User attestation is required at strategy creation.** `user_attested_at` must be populated. The UI presents a clear statement: *"I designed this strategy. True Markets has not recommended or advised me to use it. True Markets will execute it as I have defined."* No strategy is created without this.

2. **No withdrawal routing from scoped keys.** Any request to `/v1/conductor/transfers` from a scoped API key is rejected 403 at the gateway middleware layer, regardless of payload. This is enforced in code, not just in key metadata.

3. **True Markets does not store or evaluate agent decision logic.** The webhook receives order instructions only. The agent's reasoning, model weights, prompts, and intermediate outputs never touch True Markets infrastructure. This is what keeps agent strategies outside the advisory definition.

4. **No rule recommendations.** The UI may display example rules, but they must be labeled "example only" with no personalization, suitability matching, or implicit endorsement. "Users like you use this rule" is prohibited. A/B testing rule performance to recommend strategies to users is prohibited.

5. **No performance-based compensation.** All fees are flat execution fees. Success fees, performance allocations, or any fee structure tied to strategy P&L outcomes are prohibited. Such a structure would convert True Markets into an investment advisor.

6. **Wash trade prevention.** Before routing any order, the Risk Engine checks whether the incoming order would cross against an existing open order from the same strategy on the same pair but the opposite side. If the incoming buy price ≥ any open sell price from the same strategy, or the incoming sell price ≤ any open buy price from the same strategy, the order is rejected with 400 and logged as a wash trade attempt.

7. **Kill switch is human-gated on margin call.** Once a strategy is `margin_called`, neither the agent nor the rules engine can re-enable it. Only the account holder, authenticated with their master key, can re-enable. The dashboard requires explicit acknowledgment of the breach details before re-enable is permitted.

8. **Mark price for unrealized PnL is conservative.** Use Coinbase mid as primary. Fall back to TM book mid if Coinbase is stale beyond 3 seconds (matching `FAIR_MAX_STALE` in `maker_v3.py`). Return `null` — not zero, not a stale value — if both are unavailable. Never fabricate a PnL figure.

---

## 13. Implementation Workflow

Build order is strict. Each phase has hard dependencies on the previous one. Do not parallelize across phases; parallelize within a phase where noted.

---

### Phase 0 — Database (Day 1)

Run the five `CREATE TABLE` statements from Section 6 in dependency order:

1. `strategies` (no foreign key dependencies)
2. `rules` (FK on strategies)
3. `agent_api_keys` (FK on strategies)
4. `strategy_orders` (FK on strategies and rules)
5. `strategy_pnl_rollups` (FK on strategies)

Add all indexes. Verify schema with `\d` in psql before proceeding.

**Nothing else can start without this.**

---

### Phase 1 — Scoped Auth + Middleware (Days 1–2)

Build the authentication layer that all subsequent phases depend on.

1. **`POST /v1/agent/api-keys`** — insert into `agent_api_keys`, return `key_id`. Key material is EC P-256 — user submits public key, True Markets stores only the public key and `key_id`, same as master key flow in `lib/execution.py:73–78`.

2. **JWT minting for scoped keys** — reuse `POST /v1/auth/api-key/token`. The signing flow is identical to master keys (ES256 `{key_id}.{timestamp}`). The JWT payload must include `"permissions": ["trade"]` and `"strategy_id": "<strat_id>"`.

3. **Gateway middleware** — on every request to `/v1/strategies/*` and `/v1/agent/*`, verify the JWT, read `permissions`. If a scoped key attempts to reach `/v1/conductor/transfers` or any withdrawal-adjacent path, return 403 immediately. This block lives in middleware, not in individual handlers.

4. **`DELETE /v1/agent/api-keys/{key_id}`** — set `is_active = false`, `revoked_at = NOW()`.

**Test:** mint a JWT with a scoped key → call a protected route → call transfers endpoint and verify 403 → revoke key and verify auth fails.

---

### Phase 2 — Strategy CRUD (Days 2–3)

Pure DB operations, no external API calls. All five endpoints can be built in parallel.

- `POST /v1/strategies` — validate `user_attestation: true`, validate `max_position_usd < margin_usd` and `max_daily_loss_usd < margin_usd`, reject 400 otherwise. Initialize `peak_equity_usd = margin_usd`.
- `GET /v1/strategies` — list by `user_id`, join latest `strategy_pnl_rollups` row per strategy.
- `GET /v1/strategies/{id}` — full row plus latest rollup.
- `PATCH /v1/strategies/{id}` — block updates if open positions exist (`strategy_orders` has rows with `status IN ('pending', 'active')`).
- `DELETE /v1/strategies/{id}` — set `status = 'disabled'`. Open order cancellation is handled by the kill switch (Phase 3).

---

### Phase 3 — Risk Engine + Kill Switch (Days 3–5)

The most critical phase. Build and fully test this before touching execution.

**`GatewayRiskEngine` class:**

- Constructor takes `strategy_id` and a DB connection. Loads the strategy row from DB, not from in-memory arguments.
- Stateful fields (`open_position_usd`, `daily_volume_usd`, `realized_pnl_24h`) are derived from `strategy_pnl_rollups` and open `strategy_orders` rows — never from in-memory cache alone.
- `check_order(order_payload) → (allowed: bool, reason: str)` — runs the 10 checks from Section 8 in sequence, stops at first failure.
- For checks 6, 7, 8: calls `fire_margin_call(strategy_id, status)` then returns `(False, reason)`.

**`fire_margin_call(strategy_id, status)` — 7-step sequence from Section 8:**

Step 1 uses `SELECT FOR UPDATE` to prevent concurrent races (two webhook calls or a webhook call + rule fire both hitting the margin limit simultaneously). Only one should fire the kill — the other should read `margin_called` status and return a simple reject without re-running the sequence.

**`POST /v1/strategies/{id}/kill`:**

Calls `fire_margin_call(strategy_id, status='disabled')`. For rules strategies, also removes the strategy from the rules evaluation loop's active set (an in-memory set that the loop checks each cycle).

**Test the margin call sequence in isolation before building the execution paths.** Create a test strategy with a $1 margin limit, call `fire_margin_call` directly, verify: `strategies.status = 'margin_called'`, key revoked, open order cancellation attempted, rollup row written.

---

### Phase 4A — Agent Webhook (Days 5–7)

With Phase 1 (auth), Phase 2 (strategy config), and Phase 3 (risk engine) complete, the webhook is assembly work.

```
Handler: POST /v1/agent/webhook

1. Middleware: verify JWT, extract strategy_id from claims
2. DB: load strategy, verify type = 'agent' and status = 'active'
3. Instantiate GatewayRiskEngine(strategy_id)
4. engine.check_order(request_body) → return 403 on failure
5. Generate webhook_id (UUID)
6. ExecutionClient.place_order(session, ...) → Conductor
7. If DeFi payloads returned: sign + call execute (lib/execution.py:253–266, unchanged)
8. INSERT into strategy_orders (source='webhook', webhook_id, status from Conductor)
9. Enqueue PnL rollup update job for strategy_id
10. Return {webhook_id, order_id, status, risk_check: 'passed'}
```

Edge cases: Conductor returns None → return 502, do not write to `strategy_orders`. Order goes to `initialized` → complete sign+execute before returning, never surface `initialized` to the caller. Concurrent calls → `SELECT FOR UPDATE` in the risk engine handles double-spend on position limits.

---

### Phase 4B — Rules Engine (Days 5–8, parallel with 4A)

**Extract `TrueMarketsBook` first:** The class currently lives in `notebooks/maker.ipynb`. Extract it to `lib/tm_feed.py` so it can be imported by the rules engine without Jupyter.

**Market data cache:**

```python
class MarketDataCache:
    # Wraps CoinbaseBookA and TrueMarketsBook
    # Maintains per-symbol: last_price, bid, ask, mid, vwap, recent_candles
    # Computes indicators (SMA, EMA, RSI) on demand from candle buffer
    # Flags each price source as fresh/stale (>3s = stale)
```

**Rules evaluation loop:**

```python
async def rules_evaluation_loop():
    active_strategies = load_active_rules_strategies_from_db()

    while True:
        now = time.time()
        for strategy in active_strategies:
            for rule in strategy.active_rules:
                if rule.cooldown_until and now < rule.cooldown_until:
                    continue
                current_values = market_data_cache.get_indicators(
                    rule.condition_json, strategy.base_asset, strategy.quote_asset
                )
                if evaluate_condition(rule.condition_json, current_values):
                    risk_engine = GatewayRiskEngine(strategy.strategy_id)
                    allowed, reason = risk_engine.check_order(rule.action_json)
                    if allowed:
                        order = await exec_client.place_order(session, **rule.action_json)
                        db.insert_strategy_order(strategy_id, rule_id=rule.rule_id,
                                                  source='rules_engine', ...)
                        rule.cooldown_until = now + rule.cooldown_secs
                        rule.last_fired_at = now
                        rule.fire_count += 1
                rule.last_evaluated_at = now

        await asyncio.sleep(1)  # inner loop; per-rule interval respected via last_evaluated_at
```

The loop refreshes `active_strategies` from the DB every 30 seconds to pick up new rules and status changes without requiring a restart.

**`POST /v1/strategies/{id}/rules` CRUD** — straightforward DB operations, insert/update/delete `rules` rows. Changes picked up on the next 30-second DB refresh of the loop.

**`POST /v1/strategies/{id}/rules/{id}/test`** — call `market_data_cache.get_indicators()` and `evaluate_condition()` for the rule without firing the action. Return current indicator values and whether the condition is currently true.

---

### Phase 5 — Order Syncer (Days 8–10)

Background process. Watches for fills from the Conductor and writes them back to `strategy_orders`.

```python
async def order_syncer():
    while True:
        # Load all strategy_orders with status in ('pending', 'active')
        open_rows = db.query(
            "SELECT id, order_id, strategy_id, source FROM strategy_orders "
            "WHERE status IN ('pending', 'active')"
        )
        for row in open_rows:
            detail = await exec_client.get_order(session, row.order_id)
            if detail and detail['status'] in ('complete', 'canceled', 'failed'):
                pnl = compute_realized_pnl(row, detail)  # FIFO matching against open buys
                db.execute("""
                    UPDATE strategy_orders
                    SET status = ?, executed_qty = ?, executed_vwap = ?,
                        notional_usd = executed_qty * executed_vwap,
                        pnl_usd = ?, settled_at = NOW()
                    WHERE id = ?
                """, detail['status'], detail['executed_qty'], detail['executed_vwap'],
                    pnl, row.id)
                enqueue_pnl_rollup(row.strategy_id)

        await asyncio.sleep(5)
```

PnL per fill: for a sell fill, realized PnL = `(sell_vwap - avg_cost_basis) * executed_qty`, where `avg_cost_basis` is the FIFO-averaged entry price from matched buy rows in `strategy_orders` for the same strategy and pair.

---

### Phase 6 — PnL Rollup Job (Days 10–11)

Triggered by the order syncer's enqueue signal, or on a 1-minute fallback cron.

Per trigger for a `strategy_id`:
1. Pull all `strategy_orders` where `settled_at >= NOW() - 24h`, sum `pnl_usd` for realized PnL.
2. Pull open rows (`status IN ('pending', 'active')`), compute unrealized PnL using mark price from `MarketDataCache`.
3. Compute `margin_used_usd = open_position_notional + abs(min(unrealized_pnl_usd, 0))`.
4. Upsert into `strategy_pnl_rollups` for the current 1-minute bucket.
5. Update `strategies.peak_equity_usd` if `margin_usd + realized_pnl_24h` exceeds current peak.

---

### Phase 7 — Performance Endpoint (Day 11)

`GET /v1/strategies/{id}/performance` reads from `strategy_pnl_rollups` (pre-aggregated 24h window) plus the current mark price for unrealized PnL. `margin_utilization_pct` is computed live at read time from the latest rollup row — not cached.

---

### Phase 8 — Dashboard UI (Days 12–16)

Build against the completed API from Phases 2–7.

- Panel 1: `GET /v1/strategies` on mount, poll every 30s.
- Panel 2: `GET /v1/strategies/{id}/performance` on expand. Kill switch calls `POST /v1/strategies/{id}/kill` after confirm modal.
- Panel 3 (rules): Rule builder form → `POST /v1/strategies/{id}/rules`. "Test" → `POST /v1/strategies/{id}/rules/{id}/test`.
- Panel 3 (agent): API key wizard → `POST /v1/agent/api-keys`. Show key once with copy button.

---

### Phase 9 — Register Existing Strategies (Day 16+)

Register `maker_v3` and `crossmarket` as agent strategies using the margin values from Section 7.

1. `POST /v1/strategies` for each with `type: 'agent'` and `user_attestation: true`.
2. `POST /v1/agent/api-keys` for each, store `key_id` in respective `.env` files.
3. In each strategy file, wrap the `ExecutionClient.place_order()` call to route through `POST /v1/agent/webhook` instead. The local `RiskManager` can remain as a fast pre-check, but the gateway's DB state is authoritative.
4. Run each strategy against UAT (`https://api.uat.truemarkets.co`) for 24 hours before switching to production.

**Note on `maker_v3`:** the existing kill switch (`DAILY_LOSS_LIMIT` check in the main loop) is redundant after Phase 9 but harmless — keep it. The gateway enforces the same limit independently and durably.

---

### Build Order Summary

| Phase | What | Depends On | Days |
|---|---|---|---|
| 0 | DB schema | — | 1 |
| 1 | Scoped key auth + JWT + middleware | 0 | 1–2 |
| 2 | Strategy CRUD endpoints | 1 | 2–3 |
| 3 | Risk Engine + margin call + kill switch | 2 | 3–5 |
| 4A | Agent webhook ingestion | 1, 2, 3 | 5–7 |
| 4B | Rules engine + condition evaluator + rules CRUD | 2, 3 | 5–8 |
| 5 | Order syncer (fill detection) | 4A, 4B | 8–10 |
| 6 | PnL rollup job | 5 | 10–11 |
| 7 | Performance endpoint | 6 | 11 |
| 8 | Dashboard UI | 2–7 | 12–16 |
| 9 | Register existing strategies (UAT → prod) | 8 | 16+ |

**Minimum viable product is Phases 0–4A + 4B.** That delivers: rules-based strategies fully functional server-side, agent strategies via webhook, risk enforcement, kill switch, and a clean audit trail in `strategy_orders`. PnL charts and the full dashboard follow once execution is proven stable in UAT.
