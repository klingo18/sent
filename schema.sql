-- SENTINEL Supabase Schema
-- Run this in your Supabase SQL Editor to create all tables.

-- Enable UUID generation
create extension if not exists "uuid-ossp";

-- ============================================================
-- Orders table — one row per limit order
-- ============================================================
create table if not exists orders (
    id              uuid primary key default uuid_generate_v4(),
    job_id          integer unique,                     -- ACP job on-chain ID
    buyer_address   text not null default '',
    token_address   text not null,
    token_symbol    text not null default '',            -- e.g. TRUST, ATEAM
    side            text not null default 'buy',        -- buy | sell
    usdc_amount     numeric not null,                   -- human-readable (e.g. 10.00)
    usdc_amount_raw bigint not null,                    -- on-chain (6 decimals)
    limit_price_usd numeric not null,
    virtual_held    numeric not null default 0,          -- VIRTUAL held after leg 1 (wei)
    tokens_received numeric default 0,                   -- tokens received after leg 2 (wei)
    status          text not null default 'pending',    -- pending|funded|watching|executing|completed|cancelled|expired|failed
    route_key       text,                           -- canonical route string (Section 33)
    leg1_tx_hash    text,
    leg1_gas_used   integer,
    leg2_tx_hash    text,
    leg2_gas_used   integer,
    transfer_ok     boolean default false,
    slippage_pct    numeric,
    usdc_recovered  numeric default 0,
    cancel_tx_hash  text,
    evaluation_status text default 'pending',           -- pending|approved|rejected|expired_unevaluated
    created_at      timestamptz default now(),
    completed_at    timestamptz,
    cancelled_at    timestamptz,
    delivered_at    timestamptz,
    failed_at       timestamptz,
    idempotency_key text,                              -- dedup key for trigger→execution (restart safe)
    constraint chk_orders_status check (
        status in ('pending','funded','watching','executing','completed','cancelled','expired','failed')
    )
);

create index if not exists idx_orders_status on orders(status);
create index if not exists idx_orders_job_id on orders(job_id);
create index if not exists idx_orders_buyer on orders(buyer_address);
create unique index if not exists ux_orders_idempotency_key
    on orders(idempotency_key) where idempotency_key is not null;

-- ============================================================
-- Fills table — one row per swap execution (entry or exit)
-- ============================================================
create table if not exists fills (
    id              uuid primary key default uuid_generate_v4(),
    order_id        uuid references orders(id) on delete cascade,
    fill_type       text not null,                      -- entry | exit_tp | exit_sl
    tx_hash         text not null unique,              -- dedup: one fill per tx
    amount_in       numeric not null,
    expected_out    numeric,
    actual_out      numeric not null,
    trigger_price_usd numeric,
    gas_used        integer,
    slippage_pct    numeric,
    filled_at       timestamptz default now()
);

create index if not exists idx_fills_order on fills(order_id);

-- ============================================================
-- Balance snapshots — before/after every swap (Section 32.3)
-- ============================================================
create table if not exists balance_snapshots (
    id              uuid primary key default uuid_generate_v4(),
    order_id        uuid references orders(id) on delete set null,
    label           text not null,                      -- pre_leg1 | post_leg1 | pre_leg2 | post_leg2
    usdc_balance    numeric,
    virtual_balance numeric,
    token_balance   numeric,
    token_address   text,
    eth_balance     numeric,
    recorded_at     timestamptz default now()
);

create index if not exists idx_snapshots_order on balance_snapshots(order_id);

-- ============================================================
-- Tracked routes — active price monitoring routes (Section 33.8)
-- ============================================================
create table if not exists tracked_routes (
    route_key       text primary key,           -- canonical string
    in_token        text not null,
    out_token       text not null,
    pools_json      jsonb not null,             -- array of pool addresses
    subscriber_count integer default 0,
    last_price_usd  numeric,
    updated_at      timestamptz default now()
);

-- ============================================================
-- Price trigger events — always stored (Section 33.5)
-- ============================================================
create table if not exists price_events (
    id              uuid primary key default uuid_generate_v4(),
    job_id          integer,
    order_id        uuid references orders(id) on delete set null,
    route_key       text not null,
    price_usd       numeric not null,
    trigger_type    text not null,              -- entry_fill | tp_trigger | sl_trigger
    created_at      timestamptz default now()
);

create index if not exists idx_price_events_order on price_events(order_id);
create index if not exists idx_price_events_route on price_events(route_key);

-- ============================================================
-- Price snapshots — downsampled every 30s (Section 33.7)
-- ============================================================
create table if not exists price_snapshots (
    id              uuid primary key default uuid_generate_v4(),
    route_key       text not null,
    price_usd       numeric not null,
    block_number    integer,
    created_at      timestamptz default now()
);

create index if not exists idx_snapshots_route_time on price_snapshots(route_key, created_at);

-- ============================================================
-- Token registry — known tradeable tokens (Section 25)
-- ============================================================
create table if not exists token_registry (
    address         text primary key,
    symbol          text not null,
    name            text,
    decimals        integer default 18,
    pair_address    text,                      -- V2 pair with VIRTUAL
    tradeable       boolean default true,
    liquidity_usd   numeric,
    last_price_usd  numeric,
    first_seen      timestamptz default now(),
    updated_at      timestamptz default now()
);

create index if not exists idx_token_symbol on token_registry(symbol);

-- ═══════════════════════════════════════════════════════════════════
-- YIELD SCANNER — DeFiLlama-powered Base stablecoin yield discovery
-- ═══════════════════════════════════════════════════════════════════

-- Latest snapshot per pool (upserted every scan tick)
create table if not exists yield_pools (
    pool_id             text primary key,           -- DeFiLlama UUID
    chain               text not null default 'Base',
    project             text not null,              -- e.g. "aave-v3", "morpho-v1"
    symbol              text not null,              -- e.g. "USDC", "USDC-USDbC"
    tvl_usd             numeric default 0,
    apy                 numeric default 0,          -- total APY (base + reward)
    apy_base            numeric default 0,          -- organic APY
    apy_reward          numeric default 0,          -- incentive APY
    apy_mean_30d        numeric default 0,
    apy_pct_1d          numeric,                    -- 1-day APY change
    apy_pct_7d          numeric,                    -- 7-day APY change
    apy_pct_30d         numeric,                    -- 30-day APY change
    il_risk             text default 'no',          -- "no" or "yes"
    exposure            text default 'single',      -- "single" or "multi"
    stablecoin          boolean default true,
    reward_tokens       jsonb default '[]',
    underlying_tokens   jsonb default '[]',
    pool_meta           text,
    mu                  numeric default 0,          -- mean APY (DeFiLlama stat)
    sigma               numeric default 0,          -- APY volatility
    predicted_class     text,                       -- "Stable/Up", "Down", null
    risk_tier           text default 'higher',      -- "low", "medium", "higher"
    volume_usd_1d       numeric,
    volume_usd_7d       numeric,
    updated_at          timestamptz default now()
);

create index if not exists idx_yield_pools_tier on yield_pools(risk_tier);
create index if not exists idx_yield_pools_project on yield_pools(project);

-- Top recommendations per risk tier (refreshed every scan tick)
create table if not exists yield_recommendations (
    id                  uuid primary key default uuid_generate_v4(),
    pool_id             text not null,
    risk_tier           text not null,              -- "low", "medium", "higher"
    rank_in_tier        integer not null,           -- 1 = best in tier
    project             text not null,
    symbol              text not null,
    apy                 numeric default 0,
    apy_base            numeric default 0,
    apy_reward          numeric default 0,
    tvl_usd             numeric default 0,
    apy_mean_30d        numeric default 0,
    il_risk             text default 'no',
    exposure            text default 'single',
    url                 text,
    updated_at          timestamptz default now()
);

create index if not exists idx_yield_recs_tier on yield_recommendations(risk_tier, rank_in_tier);

-- Time-series snapshots (optional — for historical APY tracking)
create table if not exists yield_snapshots (
    id                  uuid primary key default uuid_generate_v4(),
    pool_id             text not null,
    apy                 numeric not null,
    apy_base            numeric,
    apy_reward          numeric,
    tvl_usd             numeric,
    recorded_at         timestamptz default now()
);

create index if not exists idx_yield_snap_pool on yield_snapshots(pool_id, recorded_at);
