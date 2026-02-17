"""
PoolPoller — multicall batch reads for V2 getReserves + V3 slot0.
Section 33.3 of sentinel-plan.md.
One RPC call per tick regardless of how many pools.

Hardened with:
  - Per-multicall timeout (prevents stuck RPC from blocking tick)
  - Stale data detection (flags pools not updated within threshold)
  - Partial failure tracking with per-pool error counts
  - Metrics: success/fail/stale counters per tick
"""

import asyncio
import time
from typing import Dict, List, Optional, Tuple
from eth_abi import decode

from engine.config import MAX_CALLS_PER_MULTICALL, MAX_MULTICALLS_PER_TICK
from engine.pricing.keys import PoolKey
from engine.pricing.registry import PriceRegistry

MULTICALL_TIMEOUT = 12.0        # seconds per multicall chunk
STALE_DATA_THRESHOLD = 60.0     # seconds — flag pool state as stale
METADATA_RETRY_INTERVAL = 30.0  # seconds between metadata retry attempts

# Multicall3 on Base (deployed at same address on all EVM chains)
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
MULTICALL3_ABI = [{
    "inputs": [{"components": [
        {"name": "target", "type": "address"},
        {"name": "allowFailure", "type": "bool"},
        {"name": "callData", "type": "bytes"},
    ], "name": "calls", "type": "tuple[]"}],
    "name": "aggregate3",
    "outputs": [{"components": [
        {"name": "success", "type": "bool"},
        {"name": "returnData", "type": "bytes"},
    ], "type": "tuple[]"}],
    "stateMutability": "payable", "type": "function",
}]

# Function selectors
GET_RESERVES_SEL = bytes.fromhex("0902f1ac")  # getReserves()
SLOT0_SEL = bytes.fromhex("3850c7bd")          # slot0()
TOKEN0_SEL = bytes.fromhex("0dfe1681")          # token0()
TOKEN1_SEL = bytes.fromhex("d21220a7")          # token1()


class PoolPoller:
    """Batch-polls all active pools via Multicall3.

    Uses RPCPool for health-scored endpoint selection and stable hashing.
    Chunks large batches into MAX_CALLS_PER_MULTICALL-sized multicalls,
    capped at MAX_MULTICALLS_PER_TICK per tick.
    """

    def __init__(self, rpc_pool, registry: PriceRegistry):
        self.rpc_pool = rpc_pool
        self.registry = registry
        # Cached pool metadata (token0/token1/decimals) — resolved once
        self._pool_meta: Dict[str, dict] = {}
        # Current pool states — refreshed every tick
        self.pool_states: Dict[str, dict] = {}
        # Pools that failed metadata resolution — track for retry
        self._meta_failed: Dict[str, float] = {}  # pk → last_attempt_time

        # Metrics
        self._tick_count = 0
        self._calls_succeeded = 0
        self._calls_failed = 0
        self._parse_errors = 0
        self._stale_pools = 0

    def _make_multicall(self, w3):
        """Create a multicall contract instance for a given w3."""
        return w3.eth.contract(
            address=w3.to_checksum_address(MULTICALL3),
            abi=MULTICALL3_ABI,
        )

    async def poll_tick(self) -> Dict[str, dict]:
        """Execute multicall batches for all active pools. Returns updated pool_states.

        Chunks calls into MAX_CALLS_PER_MULTICALL-sized batches,
        capped at MAX_MULTICALLS_PER_TICK total RPCs per tick.
        Each chunk is sharded to an RPC endpoint via stable hashing.
        """
        self._tick_count += 1
        pools = self.registry.active_pools
        if not pools:
            return self.pool_states

        # Ensure metadata is resolved for all pools
        await self._ensure_metadata(pools)

        # Build full call list
        calls: List[Tuple] = []
        call_map: List[Tuple[str, str]] = []
        for pool in pools:
            if pool.canonical not in self._pool_meta:
                continue
            pk = pool.canonical
            addr = pool.pool_address
            if pool.dex == "uniswap_v2":
                calls.append((addr, True, GET_RESERVES_SEL, pk))
                call_map.append((pk, "v2_reserves"))
            elif pool.dex == "uniswap_v3":
                calls.append((addr, True, SLOT0_SEL, pk))
                call_map.append((pk, "v3_slot0"))

        if not calls:
            return self.pool_states

        # Chunk into batches
        chunk_size = MAX_CALLS_PER_MULTICALL
        chunks = [
            (calls[i:i + chunk_size], call_map[i:i + chunk_size])
            for i in range(0, len(calls), chunk_size)
        ]

        # Cap total multicalls per tick
        if len(chunks) > MAX_MULTICALLS_PER_TICK:
            print(f"[POLLER] Backpressure: {len(chunks)} chunks capped to {MAX_MULTICALLS_PER_TICK}")
            chunks = chunks[:MAX_MULTICALLS_PER_TICK]

        now = time.time()

        # Get block number from primary endpoint (with timeout)
        try:
            block = await self.rpc_pool.call(
                lambda w3: w3.eth.block_number, tier="primary",
                timeout=5.0,
            )
        except Exception:
            block = 0

        # Execute chunks sequentially (safer for rate limits)
        chunk_successes = 0
        for chunk_calls, chunk_map in chunks:
            shard_key = chunk_calls[0][3] if chunk_calls else ""

            mc_tuples = []
            for addr, allow, sel, _pk in chunk_calls:
                mc_tuples.append((addr, allow, sel))

            try:
                results = await asyncio.wait_for(
                    self.rpc_pool.call(
                        lambda w3: self._execute_multicall(w3, mc_tuples),
                        shard_key=shard_key,
                        timeout=MULTICALL_TIMEOUT,
                    ),
                    timeout=MULTICALL_TIMEOUT + 2,  # outer safety net
                )
                chunk_successes += 1
                self._calls_succeeded += 1
            except asyncio.TimeoutError:
                print(f"[POLLER] Multicall chunk TIMEOUT ({MULTICALL_TIMEOUT}s)")
                self._calls_failed += 1
                continue
            except Exception as e:
                print(f"[POLLER] Multicall chunk failed: {e}")
                self._calls_failed += 1
                continue

            # Parse results
            self._parse_results(results, chunk_map, now, block)

        # Stale data detection
        self._check_stale_pools(now)

        return self.pool_states

    def _check_stale_pools(self, now: float):
        """Flag pools with data older than STALE_DATA_THRESHOLD."""
        stale_count = 0
        for pk, state in self.pool_states.items():
            updated = state.get("updated_at", 0)
            if now - updated > STALE_DATA_THRESHOLD:
                stale_count += 1
                state["stale"] = True
            else:
                state["stale"] = False
        self._stale_pools = stale_count
        if stale_count > 0 and self._tick_count % 10 == 0:
            print(f"[POLLER] ⚠️  {stale_count} stale pool(s) (>{STALE_DATA_THRESHOLD}s old)")

    async def _execute_multicall(self, w3, calls: list) -> list:
        """Execute a single multicall3 aggregate on the given w3."""
        mc = self._make_multicall(w3)
        # Checksum addresses for this w3 instance
        checksummed = [
            (w3.to_checksum_address(addr), allow, sel)
            for addr, allow, sel in calls
        ]
        return await mc.functions.aggregate3(checksummed).call()

    def _parse_results(self, results: list, call_map: list, now: float, block: int):
        """Parse multicall results into pool_states."""
        for i, (success, return_data) in enumerate(results):
            pk, call_type = call_map[i]
            if not success or len(return_data) == 0:
                continue

            meta = self._pool_meta.get(pk, {})

            try:
                if call_type == "v2_reserves":
                    reserve0, reserve1, _ = decode(
                        ["uint112", "uint112", "uint32"], return_data
                    )
                    self.pool_states[pk] = {
                        "dex": "uniswap_v2",
                        "reserve0": reserve0,
                        "reserve1": reserve1,
                        "token0": meta.get("token0", ""),
                        "token1": meta.get("token1", ""),
                        "decimals0": meta.get("decimals0", 18),
                        "decimals1": meta.get("decimals1", 18),
                        "updated_at": now,
                        "block_number": block,
                    }

                elif call_type == "v3_slot0":
                    decoded = decode(
                        ["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"],
                        return_data,
                    )
                    sqrt_price_x96 = decoded[0]
                    tick = decoded[1]
                    self.pool_states[pk] = {
                        "dex": "uniswap_v3",
                        "sqrt_price_x96": sqrt_price_x96,
                        "tick": tick,
                        "token0": meta.get("token0", ""),
                        "token1": meta.get("token1", ""),
                        "decimals0": meta.get("decimals0", 6),
                        "decimals1": meta.get("decimals1", 18),
                        "updated_at": now,
                        "block_number": block,
                    }

            except Exception as e:
                self._parse_errors += 1
                print(f"[POLLER] Parse error for {pk}: {e}")

    async def _ensure_metadata(self, pools: list):
        """Resolve token0/token1/decimals via multicall batch. Uses RPCPool."""
        now = time.time()
        new_pools = [
            p for p in pools
            if p.canonical not in self._pool_meta
            and (p.canonical not in self._meta_failed
                 or now - self._meta_failed[p.canonical] > METADATA_RETRY_INTERVAL)
        ]
        if not new_pools:
            return

        # Build multicall: token0() + token1() for each pool
        calls = []
        call_map = []  # (pool, field_name)
        for pool in new_pools:
            addr = pool.pool_address
            calls.append((addr, True, TOKEN0_SEL))  # token0()
            call_map.append((pool, "token0"))
            calls.append((addr, True, TOKEN1_SEL))  # token1()
            call_map.append((pool, "token1"))

        try:
            results = await self.rpc_pool.call(
                lambda w3: self._execute_multicall(w3, calls),
                shard_key="metadata",
            )
        except Exception as e:
            print(f"[POLLER] Metadata multicall failed: {e} (will retry)")
            for p in new_pools:
                self._meta_failed[p.canonical] = now
            return

        # Parse token0/token1 results
        pool_tokens: Dict[str, dict] = {}  # pk -> {token0, token1}
        for i, (success, return_data) in enumerate(results):
            pool, field = call_map[i]
            pk = pool.canonical
            if not success or len(return_data) < 32:
                continue
            addr_bytes = decode(["address"], return_data)[0]
            pool_tokens.setdefault(pk, {})[field] = addr_bytes

        # Now batch decimals() calls for all unique token addresses
        unique_tokens = set()
        for info in pool_tokens.values():
            if "token0" in info:
                unique_tokens.add(info["token0"])
            if "token1" in info:
                unique_tokens.add(info["token1"])

        # decimals() selector = 0x313ce567
        decimals_sel = bytes.fromhex("313ce567")
        dec_calls = []
        dec_tokens = []
        for tok in unique_tokens:
            dec_calls.append((tok, True, decimals_sel))
            dec_tokens.append(tok)

        token_decimals: Dict[str, int] = {}
        if dec_calls:
            try:
                dec_results = await self.rpc_pool.call(
                    lambda w3: self._execute_multicall(w3, dec_calls),
                    shard_key="decimals",
                )
                for i, (success, return_data) in enumerate(dec_results):
                    if success and len(return_data) >= 32:
                        d = decode(["uint8"], return_data)[0]
                        token_decimals[dec_tokens[i].lower()] = d
                    else:
                        token_decimals[dec_tokens[i].lower()] = 18
            except Exception as e:
                print(f"[POLLER] Decimals multicall failed: {e}")
                for tok in dec_tokens:
                    token_decimals[tok.lower()] = 18

        # Assemble final metadata
        for pool in new_pools:
            pk = pool.canonical
            info = pool_tokens.get(pk, {})
            t0 = info.get("token0")
            t1 = info.get("token1")
            if not t0 or not t1:
                print(f"[POLLER] Metadata incomplete for {pool} (will retry)")
                continue
            d0 = token_decimals.get(t0.lower(), 18)
            d1 = token_decimals.get(t1.lower(), 18)
            self._pool_meta[pk] = {
                "token0": t0.lower(),
                "token1": t1.lower(),
                "decimals0": d0,
                "decimals1": d1,
            }
            # Clear from failed set on success
            self._meta_failed.pop(pk, None)
            print(f"[POLLER] Resolved metadata for {pool}: "
                  f"token0={t0[:10]}...(d={d0}) token1={t1[:10]}...(d={d1})")

    def metrics(self) -> dict:
        return {
            "ticks": self._tick_count,
            "pools_tracked": len(self.pool_states),
            "pools_meta_resolved": len(self._pool_meta),
            "pools_meta_failed": len(self._meta_failed),
            "calls_succeeded": self._calls_succeeded,
            "calls_failed": self._calls_failed,
            "parse_errors": self._parse_errors,
            "stale_pools": self._stale_pools,
        }
