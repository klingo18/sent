# SENTINEL — Autonomous Execution Agent for Virtuals ACP

Limit orders for any Virtuals agent token on Base — via Butler.

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env — fill in RPC URL, wallet keys, Supabase creds

# 3. Smoke test
python3 main.py --smoke

# 4. Run engine (requires real .env values + Supabase tables)
python3 main.py
```

## Project Structure

```
sentinel/
├── main.py                        # Entry point — smoke test + full engine
├── engine/
│   ├── config.py                  # Env var loading + validation
│   ├── acp/
│   │   ├── handler.py             # SDK callback bridge + polling + expiry
│   │   ├── offerings.py           # Offering definitions + validators
│   │   └── deliverable.py         # Payload builders (Section 15 formats)
│   ├── pricing/                   # Price Service (Section 33)
│   │   ├── keys.py                # PoolKey, RouteKey dataclasses
│   │   ├── registry.py            # Subscribe/unsubscribe, active pools
│   │   ├── poller.py              # Multicall3 batch reads (1 RPC/tick)
│   │   ├── pricer.py              # Route price derivation (V2+V3)
│   │   ├── triggers.py            # Job threshold evaluation
│   │   └── service.py             # Orchestrator (poll→price→trigger)
│   ├── executor/
│   │   └── swap_v2.py             # V3 Leg 1 + V2 Leg 2 + retry + balance delta
│   ├── orders/
│   │   └── state_machine.py       # Order lifecycle, execution, cancel, recovery
│   ├── wallets/
│   │   └── hot_wallet.py          # Single hot wallet for MVP (Section 32.3)
│   ├── tokens/
│   │   └── resolver.py            # 3-layer token resolution (cache→DexScreener→on-chain)
│   ├── yields/                    # Yield Scanner (Section 36)
│   │   ├── llama_client.py        # DeFiLlama API client (Base stablecoin pools)
│   │   ├── scanner.py             # Background poll loop + DB persistence
│   │   └── ranker.py              # 3-tier risk classification
│   └── db/
│       └── client.py              # Supabase CRUD wrapper
├── supabase/functions/            # 6 Edge Functions (Section 32.6 + 36.20)
│   ├── get-service-status/        # Is SENTINEL online?
│   ├── is-token-supported/        # V2 pair check for any token
│   ├── get-limit-order-quote/     # Pre-trade price + fee estimate
│   ├── get-order-status/          # Single order details
│   ├── get-active-orders/         # All open orders for a buyer
│   └── get-yield-options/         # Base stablecoin yield options (DeFiLlama)
├── abis/                          # 7 canonical ABI JSON files (verified from official sources)
├── scripts/
│   ├── register_offerings.py      # Generate ACP portal JSON for offerings + resources
│   └── test_acp_connection.py     # Verify SDK connects, fetch agent info + jobs
├── schema.sql                     # 10 tables, 13 indexes
├── test_engine.py                 # Engine integration test (multicall, pricing, triggers)
├── requirements.txt
├── .env.example
├── Makefile
└── README.md
```

## Build Status (Section 32.9)

| Step | Status | Detail |
|------|--------|--------|
| 1. Scaffold | ✅ Done | Project structure, .env, smoke test |
| 2. ACP Lifecycle | ✅ Code done | SDK bridge, offerings, payable, deliver, registration scripts |
| 3. Swap Execution | ✅ Code done | V3 Leg 1, V2 Leg 2, balance delta, retry |
| 4. Price Service | ✅ Verified | 32 ticks/2min, TRUST=$0.00029, multicall |
| 5. Full Happy Path | ⚠️ Needs live test | All wired — needs agent + funded wallet |
| 6. DB + Resources | 🔧 In progress | Schema tested, 6 Edge Functions written |
| 7. Cancel + Recovery | ✅ Code done | Cancel, expiry, retry, startup recovery |
| 8. Audit + Hardening | ✅ Done | ABIs verified, SDK bugs fixed, RPCPool, backpressure, orphan fix |
| 9. Yield Scanner | ✅ Done | DeFiLlama client, 3-tier ranker, DB persistence, ACP resource |

## Before First Run

1. Register SENTINEL agent on Virtuals → get entity_id + wallet
2. Run `python3 scripts/register_offerings.py` → paste JSON into ACP portal
3. Fund hot wallet with ETH for gas + USDC for test orders
4. Create Supabase project → run `schema.sql` → deploy Edge Functions
5. Fill in `.env` with real credentials
6. Run `python3 scripts/test_acp_connection.py` to verify SDK connects
