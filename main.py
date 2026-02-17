"""
SENTINEL Engine — Entry point.
Wires all components and runs them as concurrent asyncio tasks.

Usage:
    python3 main.py              # Full engine (ACP + watcher + executor)
    python3 main.py --smoke      # Smoke test only (connect + exit)
"""

import argparse
import asyncio
import sys

from web3 import AsyncWeb3, AsyncHTTPProvider

from engine.config import (
    BASE_RPC_URL,
    BASE_RPC_URLS,
    BASE_CHAIN_ID,
    SUPABASE_URL,
    SUPABASE_KEY,
    HOT_WALLET_PRIVATE_KEY,
    SENTINEL_WALLET,
    SENTINEL_PRIVATE_KEY,
    SENTINEL_ENTITY_ID,
    MAX_RPC_CONCURRENCY,
    TRIGGER_QUEUE_MAXSIZE,
    print_config_summary,
)
from engine.rpc.pool import RPCPool
from engine.db.client import init_supabase, health_check, Database
from engine.wallets.hot_wallet import HotWallet
from engine.executor.swap_v2 import SwapExecutor
from engine.pricing.registry import PriceRegistry
from engine.pricing.service import PriceService
from engine.orders.state_machine import OrderManager
from engine.tokens.resolver import TokenResolver
from engine.yields.scanner import YieldScanner


async def smoke_test():
    """Smoke test: connect to RPC + Supabase, print status, exit."""
    print("=" * 50)
    print("  SENTINEL Engine — Smoke Test")
    print("=" * 50)
    print()
    print_config_summary()
    print()

    # RPC
    print("[RPC] Connecting to Base...")
    w3 = AsyncWeb3(AsyncHTTPProvider(BASE_RPC_URL))
    try:
        chain_id = await w3.eth.chain_id
        block = await w3.eth.block_number
        print(f"[RPC] ✅ Base chain_id={chain_id} block_number={block}")
        if chain_id != BASE_CHAIN_ID:
            print(f"[RPC] ⚠️  Expected chain_id {BASE_CHAIN_ID}, got {chain_id}", file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print(f"[RPC] ❌ Failed to connect: {e}", file=sys.stderr)
        sys.exit(1)

    # Wallet
    print()
    print("[WALLET] Loading hot wallet...")
    wallet = HotWallet(HOT_WALLET_PRIVATE_KEY, w3)
    eth_bal = await wallet.get_eth_balance()
    print(f"[WALLET] ✅ Address: {wallet.address}")
    print(f"[WALLET]    ETH balance: {w3.from_wei(eth_bal, 'ether'):.6f} ETH")
    if eth_bal == 0:
        print("[WALLET] ⚠️  Hot wallet has 0 ETH — will need gas funding before swaps")

    # Supabase
    print()
    print("[DB] Connecting to Supabase...")
    sb = init_supabase(SUPABASE_URL, SUPABASE_KEY)
    ok = await health_check(sb)
    print(f"[DB] {'✅ supabase ok' if ok else '❌ Client init failed'}")

    print()
    print("=" * 50)
    print("  ✅ SMOKE TEST PASSED")
    print(f"  Chain: Base ({chain_id}) | Block: {block}")
    print(f"  Wallet: {wallet.address}")
    print("=" * 50)


async def run_engine():
    """Full engine: ACP handler + price watcher + execution consumer."""
    print("=" * 50)
    print("  SENTINEL Engine — Starting")
    print("=" * 50)
    print()
    print_config_summary()
    print()

    # 1. Connect to Base RPC via RPCPool
    print(f"[INIT] Creating RPC pool ({len(BASE_RPC_URLS)} endpoints, concurrency={MAX_RPC_CONCURRENCY})...")
    rpc_pool = RPCPool(urls=BASE_RPC_URLS, max_concurrency=MAX_RPC_CONCURRENCY)
    w3 = rpc_pool.primary_w3  # best endpoint for non-sharded calls
    chain_id = await w3.eth.chain_id
    block = await w3.eth.block_number
    print(f"[INIT] ✅ Base chain_id={chain_id} block={block}")
    rpc_pool.print_status()

    if chain_id != BASE_CHAIN_ID:
        print(f"[INIT] FATAL: wrong chain {chain_id}", file=sys.stderr)
        sys.exit(1)

    # 2. Init hot wallet
    print("[INIT] Loading hot wallet...")
    hot_wallet = HotWallet(HOT_WALLET_PRIVATE_KEY, w3)
    eth_bal = await hot_wallet.get_eth_balance()
    print(f"[INIT] ✅ Wallet: {hot_wallet.address} ({w3.from_wei(eth_bal, 'ether'):.6f} ETH)")

    if eth_bal == 0:
        print("[INIT] ⚠️  Hot wallet has 0 ETH — swaps will fail until funded")

    # 3. Init Supabase
    print("[INIT] Connecting to Supabase...")
    sb_client = init_supabase(SUPABASE_URL, SUPABASE_KEY)
    db = Database(sb_client)
    print("[INIT] ✅ DB connected")

    # 4. Init components
    swap_executor = SwapExecutor(w3, hot_wallet)

    # Price Service (Section 33): registry + multicall poller + trigger engine
    price_registry = PriceRegistry()
    trigger_queue = asyncio.Queue(maxsize=TRIGGER_QUEUE_MAXSIZE)
    price_service = PriceService(rpc_pool, price_registry, db, trigger_queue)

    # Token resolver (Section 25): validate token tradeability
    token_resolver = TokenResolver(w3, db)

    # Yield scanner (Section 36): DeFiLlama-powered yield discovery
    yield_scanner = YieldScanner(db)
    print("[INIT] ✅ Yield scanner ready (DeFiLlama, 10-min poll)")

    # Order manager: consumes triggers, subscribes to price registry
    order_manager = OrderManager(db, swap_executor, hot_wallet, trigger_queue)
    order_manager.set_price_registry(price_registry)
    order_manager.set_w3(w3)

    # 5. Init ACP (lazy import — only if virtuals_acp is installed)
    acp_handler = None
    try:
        from virtuals_acp.client import VirtualsACP
        from virtuals_acp.contract_clients.contract_client_v2 import ACPContractClientV2
        from engine.acp.handler import ACPHandler

        # Safe callback bridge — VirtualsACP connects Socket.IO during __init__,
        # so events can arrive before our handler exists. The bridge absorbs
        # early events safely, then forwards once handler is assigned.
        _handler_ref = [None]  # mutable container for closure

        def _on_new_task(job, memo):
            h = _handler_ref[0]
            if h:
                h.on_sdk_new_task(job, memo)

        def _on_evaluate(job):
            h = _handler_ref[0]
            if h:
                h.on_sdk_evaluate(job)

        acp = VirtualsACP(
            acp_contract_clients=ACPContractClientV2(
                wallet_private_key=SENTINEL_PRIVATE_KEY,
                agent_wallet_address=SENTINEL_WALLET,
                entity_id=SENTINEL_ENTITY_ID,
            ),
            on_new_task=_on_new_task,
            on_evaluate=_on_evaluate,
        )
        acp_handler = ACPHandler(acp, db, order_manager, hot_wallet, token_resolver)
        acp_handler.set_event_loop(asyncio.get_running_loop())
        _handler_ref[0] = acp_handler  # now bridge forwards to real handler
        print(f"[INIT] ✅ ACP agent: {acp.agent_address} (SDK Socket.IO active)")
    except ImportError:
        print("[INIT] ⚠️  virtuals_acp not installed — ACP disabled (watcher-only mode)")
    except Exception as e:
        print(f"[INIT] ⚠️  ACP init failed: {e} — watcher-only mode")

    # 6. Startup recovery
    print()
    print("[INIT] Running startup recovery...")
    await order_manager.recover_on_startup()

    # 7. Launch concurrent tasks
    print()
    print("=" * 50)
    print("  SENTINEL Engine — Running")
    print("=" * 50)
    print("  Ctrl+C to stop")
    print()

    tasks = []

    # Price Service (always runs — multicall poller + trigger evaluator)
    tasks.append(asyncio.create_task(price_service.run(), name="price_service"))

    # Execution consumer (always runs — processes triggers from Price Service)
    tasks.append(asyncio.create_task(order_manager.run_execution_consumer(), name="exec_consumer"))

    # Yield scanner (always runs — DeFiLlama HTTP, no RPC)
    tasks.append(asyncio.create_task(yield_scanner.run(), name="yield_scanner"))

    # ACP handler (if available)
    # Socket.IO is managed by the SDK — we only need the polling fallback
    if acp_handler:
        tasks.append(asyncio.create_task(acp_handler.poll_jobs(), name="acp_poll"))

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    except KeyboardInterrupt:
        pass
    finally:
        print("\n[ENGINE] Shutting down...")
        price_service.stop()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        print("[ENGINE] Stopped.")


def main():
    parser = argparse.ArgumentParser(description="SENTINEL Engine")
    parser.add_argument("--smoke", action="store_true", help="Smoke test only (connect + exit)")
    args = parser.parse_args()

    try:
        if args.smoke:
            asyncio.run(smoke_test())
        else:
            asyncio.run(run_engine())
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
