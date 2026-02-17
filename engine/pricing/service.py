"""
PriceService — orchestrates poller, pricer, triggers on a fixed cadence.
Section 33.9 of sentinel-plan.md.

Hardened with:
  - Tick duration tracking and error budget
  - Graceful degradation: backs off poll interval on consecutive errors
  - Component metrics aggregation
  - Per-tick timeout to prevent stuck polls from blocking the loop
"""

import asyncio
import time

from engine.config import PRICE_CHECK_INTERVAL
from engine.pricing.registry import PriceRegistry
from engine.pricing.poller import PoolPoller
from engine.pricing.pricer import RoutePricer
from engine.pricing.triggers import TriggerEngine

# Error budget: after N consecutive errors, double the poll interval (up to cap)
MAX_CONSECUTIVE_ERRORS = 5
DEGRADED_INTERVAL_CAP = 30.0   # max poll interval in degraded mode (seconds)
TICK_TIMEOUT = 20.0             # per-tick timeout (seconds)
METRICS_LOG_INTERVAL = 120.0    # log aggregate metrics every 2 min


class PriceService:
    """Top-level price service. Runs poll→price→trigger loop every tick."""

    def __init__(self, rpc_pool, registry: PriceRegistry, db, trigger_queue: asyncio.Queue):
        self.rpc_pool = rpc_pool
        self.registry = registry
        self.db = db
        self.poller = PoolPoller(rpc_pool, registry)
        self.pricer = RoutePricer(registry)
        self.trigger_engine = TriggerEngine(registry, db, trigger_queue)
        self._running = False
        self._snapshot_interval = 30.0
        self._last_snapshot = 0.0
        self._route_sync_interval = 30.0
        self._last_route_sync = 0.0
        self._health_log_interval = 60.0
        self._last_health_log = 0.0
        self._last_metrics_log = 0.0

        # Tick metrics
        self._tick_count = 0
        self._tick_errors = 0
        self._consecutive_errors = 0
        self._total_tick_ms = 0.0
        self._max_tick_ms = 0.0
        self._last_tick_duration_ms = 0.0

    async def run(self):
        """Main loop: poll → price → trigger, every PRICE_CHECK_INTERVAL seconds."""
        self._running = True
        print(f"[PRICE] Service started — polling every {PRICE_CHECK_INTERVAL}s")

        while self._running:
            t0 = time.monotonic()
            try:
                await asyncio.wait_for(self._tick(), timeout=TICK_TIMEOUT)
                self._consecutive_errors = 0
            except asyncio.TimeoutError:
                self._tick_errors += 1
                self._consecutive_errors += 1
                print(f"[PRICE] Tick TIMEOUT after {TICK_TIMEOUT}s "
                      f"(consecutive errors: {self._consecutive_errors})")
            except asyncio.CancelledError:
                self._running = False
                return
            except Exception as e:
                self._tick_errors += 1
                self._consecutive_errors += 1
                print(f"[PRICE] Tick error ({self._consecutive_errors}x): {e}")

            # Track tick duration
            duration_ms = (time.monotonic() - t0) * 1000
            self._tick_count += 1
            self._total_tick_ms += duration_ms
            self._last_tick_duration_ms = duration_ms
            if duration_ms > self._max_tick_ms:
                self._max_tick_ms = duration_ms

            # Graceful degradation: slow down on consecutive errors
            interval = PRICE_CHECK_INTERVAL
            if self._consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                interval = min(
                    PRICE_CHECK_INTERVAL * (2 ** (self._consecutive_errors - MAX_CONSECUTIVE_ERRORS + 1)),
                    DEGRADED_INTERVAL_CAP,
                )
                print(f"[PRICE] ⚠️  Degraded mode: interval={interval:.1f}s")

            await asyncio.sleep(interval)

    async def _tick(self):
        """One poll-price-trigger cycle."""
        self.registry.cleanup_expired_routes(grace_seconds=300.0)

        pools = self.registry.active_pools
        if not pools:
            return

        # 1. Poll all active pools via multicall
        pool_states = await self.poller.poll_tick()

        # 2. Derive route prices from pool states
        route_prices = self.pricer.compute_all(pool_states)

        # 3. Evaluate triggers for all subscribed jobs
        await self.trigger_engine.evaluate(route_prices)

        now = time.time()

        # 4. Periodic tracked route sync (every 30s)
        if now - self._last_route_sync >= self._route_sync_interval:
            self._last_route_sync = now
            await self._sync_tracked_routes(route_prices)

        # 5. Periodic price snapshots (every 30s, not every tick)
        if now - self._last_snapshot >= self._snapshot_interval:
            self._last_snapshot = now
            await self._save_snapshots(route_prices)

        # 6. Periodic RPC health log (every 60s)
        if now - self._last_health_log >= self._health_log_interval:
            self._last_health_log = now
            if hasattr(self.rpc_pool, 'print_status'):
                self.rpc_pool.print_status()

        # 7. Periodic aggregate metrics log (every 2 min)
        if now - self._last_metrics_log >= METRICS_LOG_INTERVAL:
            self._last_metrics_log = now
            self._log_metrics()

    async def _sync_tracked_routes(self, route_prices: dict):
        """Upsert tracked routes with subscriber counts and last prices."""
        errors = 0
        for rk_str, route in self.registry.active_routes.items():
            subs = self.registry.subscribers.get(rk_str, set())
            pools_json = [
                {"dex": p.dex, "address": p.pool_address}
                for p in route.pools
            ]
            price = route_prices.get(rk_str)
            try:
                await self.db.upsert_tracked_route(
                    route_key=rk_str,
                    in_token=route.in_token,
                    out_token=route.out_token,
                    pools_json=pools_json,
                    subscriber_count=len(subs),
                    last_price_usd=price if price is not None else None,
                )
            except Exception as e:
                errors += 1
                if errors <= 2:
                    print(f"[PRICE] Tracked route upsert failed for {rk_str[:30]}: {e}")

        if errors > 2:
            print(f"[PRICE] ... and {errors - 2} more tracked route upsert failures")

    async def _save_snapshots(self, route_prices: dict):
        """Downsample: write one price snapshot per active route every 30s."""
        # Get block number once for all snapshots
        block = 0
        state = self.poller.pool_states
        for ps in state.values():
            b = ps.get("block_number", 0)
            if b > block:
                block = b

        snapshot_errors = 0
        for rk_str, price in route_prices.items():
            try:
                await self.db.insert_price_snapshot(rk_str, price, block)
            except Exception as e:
                snapshot_errors += 1
                if snapshot_errors <= 2:  # only log first 2 to avoid spam
                    print(f"[PRICE] Snapshot save failed for {rk_str[:30]}: {e}")

        if snapshot_errors > 2:
            print(f"[PRICE] ... and {snapshot_errors - 2} more snapshot save failures")

    def _log_metrics(self):
        """Log aggregate metrics from all components."""
        m = self.metrics()
        print(f"[PRICE] Metrics: ticks={m['ticks']} errors={m['tick_errors']} "
              f"avg={m['avg_tick_ms']:.0f}ms max={m['max_tick_ms']:.0f}ms "
              f"routes={m['priced_routes']} pools={m['active_pools']} "
              f"triggers={m['trigger_metrics']['triggers']}")

    def stop(self):
        self._running = False

    def metrics(self) -> dict:
        avg_tick = (self._total_tick_ms / max(self._tick_count, 1))
        return {
            "ticks": self._tick_count,
            "tick_errors": self._tick_errors,
            "consecutive_errors": self._consecutive_errors,
            "avg_tick_ms": round(avg_tick, 1),
            "max_tick_ms": round(self._max_tick_ms, 1),
            "last_tick_ms": round(self._last_tick_duration_ms, 1),
            "active_pools": len(self.registry.active_pools),
            "priced_routes": len(self.pricer.route_prices),
            "trigger_metrics": self.trigger_engine.metrics(),
            "rpc_metrics": self.rpc_pool.metrics() if hasattr(self.rpc_pool, 'metrics') else {},
        }

    def summary(self) -> str:
        m = self.metrics()
        return (f"PriceService: {self.registry.summary()} | "
                f"{m['priced_routes']} priced routes | "
                f"ticks={m['ticks']} err={m['tick_errors']} "
                f"avg={m['avg_tick_ms']:.0f}ms")
