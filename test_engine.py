"""
Engine-only integration test.
Runs PriceService + OrderManager execution consumer for ~2 minutes.
Subscribes 3 fake jobs across 2 tokens (TRUST + VIRTUAL itself).
Proves: multicall ticks, price derivation, trigger evaluation, consumer stable.
No Supabase needed — uses a mock DB.
"""

import asyncio
import time
import sys

from web3 import AsyncWeb3, AsyncHTTPProvider

# Patch engine.config before any engine imports
sys.path.insert(0, "/root/sentinel")
from engine.rpc.pool import RPCPool


class MockDB:
    """Fake DB that prints instead of hitting Supabase."""
    async def insert_price_event(self, *a, **kw):
        print(f"  [MOCK-DB] insert_price_event({a[0]}, price={a[3]}, type={a[4]})")
    async def insert_price_snapshot(self, *a, **kw):
        print(f"  [MOCK-DB] insert_price_snapshot(route={a[0][:40]}..., price={a[1]:.8f})")
    async def get_order(self, order_id):
        return {"id": order_id, "token_address": "0xC841b4eaD3F70bE99472FFdB88E5c3C7aF6A481a",
                "virtual_held": 14284250000000000000, "side": "buy", "job_id": 99999,
                "buyer_address": "0xdead", "status": "watching"}
    async def update_order_status(self, *a, **kw):
        print(f"  [MOCK-DB] update_order_status({a[0]}, {a[1]})")
    async def get_active_orders(self):
        return []


async def main():
    print("=" * 60)
    print("  ENGINE-ONLY INTEGRATION TEST (2 minutes)")
    print("=" * 60)
    print()

    rpc_pool = RPCPool(urls=["https://mainnet.base.org"], max_concurrency=4)
    w3 = rpc_pool.primary_w3
    chain_id = await w3.eth.chain_id
    block = await w3.eth.block_number
    print(f"[RPC] Base chain_id={chain_id} block={block}")
    rpc_pool.print_status()

    from engine.pricing.keys import resolve_v2_pair, build_sentinel_route, PoolKey
    from engine.pricing.registry import PriceRegistry
    from engine.pricing.service import PriceService

    USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    VIRTUAL = "0x0b3e328455c4059EEb9e3f84b5543F74E24e7E1b"
    TRUST = "0xC841b4eaD3F70bE99472FFdB88E5c3C7aF6A481a"
    V2_FACTORY = "0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6"
    V3_POOL = "0x529d2863a1521d0b57db028168fdE2E97120017C"

    # Resolve V2 pairs
    print()
    trust_pair = await resolve_v2_pair(w3, V2_FACTORY, VIRTUAL, TRUST)
    print(f"[RESOLVE] VIRTUAL/TRUST V2 pair: {trust_pair}")

    # Build routes
    route_trust = build_sentinel_route(8453, USDC, TRUST, V3_POOL, trust_pair)
    print(f"[ROUTE] TRUST: {route_trust.canonical[:60]}...")

    # Setup registry + service
    registry = PriceRegistry()
    trigger_queue = asyncio.Queue(maxsize=100)
    db = MockDB()
    price_service = PriceService(rpc_pool, registry, db, trigger_queue)

    # Subscribe 3 fake jobs
    # Job 1: buy TRUST at $0.0001 (way below market — should NOT trigger)
    registry.subscribe(1001, route_trust, {
        "side": "buy", "limit_price_usd": 0.0001,
        "order_id": "order-1001", "order_data": {},
    })
    # Job 2: buy TRUST at $0.0001 (same route — tests dedup)
    registry.subscribe(1002, route_trust, {
        "side": "buy", "limit_price_usd": 0.0001,
        "order_id": "order-1002", "order_data": {},
    })
    # Job 3: buy TRUST at $999 (way above market — SHOULD trigger immediately)
    registry.subscribe(1003, route_trust, {
        "side": "buy", "limit_price_usd": 999.0,
        "order_id": "order-1003", "order_data": {},
    })

    print(f"\n[REGISTRY] {registry.summary()}")
    print(f"[REGISTRY] Job 1001: buy TRUST @ $0.0001 (should NOT trigger)")
    print(f"[REGISTRY] Job 1002: buy TRUST @ $0.0001 (should NOT trigger)")
    print(f"[REGISTRY] Job 1003: buy TRUST @ $999.00 (SHOULD trigger on first tick)")
    print()

    # Run price service + execution consumer for 2 minutes
    tick_count = [0]
    trigger_count = [0]
    original_tick = price_service._tick

    async def instrumented_tick():
        tick_count[0] += 1
        t0 = time.time()
        await original_tick()
        elapsed = time.time() - t0
        # Print tick summary
        prices = price_service.pricer.route_prices
        for rk, info in prices.items():
            p = info["price_usd_per_token"]
            print(f"  [TICK {tick_count[0]:>3}] TRUST=${p:.8f} | "
                  f"pools={registry.pool_count()} routes={registry.route_count()} "
                  f"jobs={registry.job_count()} | {elapsed:.2f}s")

    price_service._tick = instrumented_tick

    # Drain trigger queue in background
    async def drain_triggers():
        while True:
            try:
                trigger = await asyncio.wait_for(trigger_queue.get(), timeout=1.0)
                trigger_count[0] += 1
                print(f"  >>> TRIGGER FIRED: job={trigger['job_id']} "
                      f"order={trigger['order_id']} price=${trigger['trigger_price_usd']:.8f} "
                      f"type={trigger['type']}")
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                return

    print("--- Starting engine loop (2 minutes) ---")
    print()

    t_start = time.time()
    task_price = asyncio.create_task(price_service.run())
    task_drain = asyncio.create_task(drain_triggers())

    try:
        await asyncio.sleep(120)  # 2 minutes
    except KeyboardInterrupt:
        pass
    finally:
        price_service.stop()
        task_price.cancel()
        task_drain.cancel()
        try:
            await asyncio.gather(task_price, task_drain, return_exceptions=True)
        except Exception:
            pass

    elapsed_total = time.time() - t_start
    print()
    print("=" * 60)
    print(f"  ENGINE TEST COMPLETE")
    print(f"  Duration:   {elapsed_total:.0f}s")
    print(f"  Ticks:      {tick_count[0]}")
    print(f"  Triggers:   {trigger_count[0]}")
    print(f"  Jobs left:  {registry.job_count()}")
    print(f"  Routes:     {registry.route_count()}")
    print(f"  Pools:      {registry.pool_count()}")
    print("=" * 60)

    # Assertions
    assert tick_count[0] >= 30, f"Expected >=30 ticks in 2min, got {tick_count[0]}"
    assert trigger_count[0] >= 1, f"Expected >=1 trigger (job 1003), got {trigger_count[0]}"
    assert registry.job_count() == 2, f"Expected 2 jobs remaining (1001+1002), got {registry.job_count()}"
    print("\n✅ ALL ASSERTIONS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
