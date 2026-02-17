"""
TriggerEngine — evaluate job conditions against latest prices, enqueue execution.
Section 33.5 of sentinel-plan.md.

Hardened with:
  - Dedup protection: won't re-enqueue same order within cooldown
  - Graduated backpressure: warn at 60%, skip at 80%
  - Trigger metrics: total triggers, drops, dedup skips
  - Price sanity checks: reject zero/negative/NaN prices
"""

import asyncio
import math
import time
from typing import Dict

from engine.pricing.registry import PriceRegistry

# Dedup: don't re-trigger same order within this window (seconds)
TRIGGER_DEDUP_COOLDOWN = 15.0
# Backpressure thresholds (fraction of queue maxsize)
BACKPRESSURE_WARN = 0.6
BACKPRESSURE_SKIP = 0.8
# Max price sanity: reject obviously wrong prices (> $1M per token)
MAX_SANE_PRICE_USD = 1_000_000.0


class TriggerEngine:
    """Evaluate all subscribed job conditions against latest route prices."""

    def __init__(self, registry: PriceRegistry, db, trigger_queue: asyncio.Queue):
        self.registry = registry
        self.db = db
        self.trigger_queue = trigger_queue
        self._backpressure_warned = False

        # Dedup: order_id → last trigger timestamp
        self._last_trigger_at: Dict[str, float] = {}

        # Metrics
        self._total_evaluations = 0
        self._total_triggers = 0
        self._total_drops = 0
        self._total_dedup_skips = 0
        self._total_backpressure_skips = 0

    async def evaluate(self, route_prices: Dict[str, float]):
        """Called after every poll tick. Check every job's condition."""
        self._total_evaluations += 1

        # Backpressure: graduated response
        if hasattr(self.trigger_queue, 'maxsize') and self.trigger_queue.maxsize > 0:
            fill_ratio = self.trigger_queue.qsize() / self.trigger_queue.maxsize
            if fill_ratio >= BACKPRESSURE_SKIP:
                if not self._backpressure_warned:
                    print(f"[TRIGGER] ⚠️  Backpressure SKIP: queue {self.trigger_queue.qsize()}"
                          f"/{self.trigger_queue.maxsize} ({fill_ratio:.0%})")
                    self._backpressure_warned = True
                self._total_backpressure_skips += 1
                return
            elif fill_ratio >= BACKPRESSURE_WARN:
                if not self._backpressure_warned:
                    print(f"[TRIGGER] ⚠️  Backpressure WARN: queue {self.trigger_queue.qsize()}"
                          f"/{self.trigger_queue.maxsize} ({fill_ratio:.0%})")
                    self._backpressure_warned = True
                # Continue evaluating but log warning
            else:
                self._backpressure_warned = False

        now = time.time()

        for rk_str, subscriber_jobs in self.registry.subscribers.items():
            price = route_prices.get(rk_str)
            # Price sanity: reject zero, negative, NaN, inf, absurdly high
            if price is None or price <= 0:
                continue
            if math.isnan(price) or math.isinf(price) or price > MAX_SANE_PRICE_USD:
                continue

            for job_id in list(subscriber_jobs):
                job = self.registry.job_conditions.get(job_id)
                if job is None:
                    continue

                triggered = False
                trigger_type = None

                side = job.get("side", "buy")
                limit_price = float(job.get("limit_price_usd", 0))

                if limit_price <= 0:
                    continue

                # Buy limit: trigger when price drops to or below limit
                if side == "buy" and price <= limit_price:
                    triggered = True
                    trigger_type = "entry_fill"

                # Sell limit: trigger when price rises to or above limit
                elif side == "sell" and price >= limit_price:
                    triggered = True
                    trigger_type = "entry_fill"

                if not triggered:
                    continue

                order_id = job.get("order_id")

                # Dedup: skip if same order was triggered recently
                if order_id and order_id in self._last_trigger_at:
                    elapsed = now - self._last_trigger_at[order_id]
                    if elapsed < TRIGGER_DEDUP_COOLDOWN:
                        self._total_dedup_skips += 1
                        continue

                print(f"[TRIGGER] Job {job_id} order {order_id}: "
                      f"price=${price:.6f} hit {side} limit=${limit_price:.6f}")

                try:
                    self.trigger_queue.put_nowait({
                        "type": trigger_type,
                        "order": job.get("order_data", {}),
                        "order_id": order_id,
                        "job_id": job_id,
                        "trigger_price_usd": price,
                        "route_key": rk_str,
                    })
                    self._total_triggers += 1
                    if order_id:
                        self._last_trigger_at[order_id] = now
                except asyncio.QueueFull:
                    print(f"[TRIGGER] ⚠️  Queue full — trigger for job {job_id} DROPPED")
                    self._total_drops += 1
                    continue

                # Unsubscribe so we don't re-trigger
                self.registry.unsubscribe(job_id)

                # Record trigger event in DB (non-blocking — don't fail the trigger)
                try:
                    await self.db.insert_price_event(
                        job_id, order_id, rk_str, price, trigger_type,
                    )
                except Exception as e:
                    print(f"[TRIGGER] Failed to record price event: {e}")

        # Periodic cleanup of stale dedup entries (older than 5 min)
        if self._total_evaluations % 100 == 0:
            cutoff = now - 300
            self._last_trigger_at = {
                k: v for k, v in self._last_trigger_at.items() if v > cutoff
            }

    def metrics(self) -> dict:
        return {
            "evaluations": self._total_evaluations,
            "triggers": self._total_triggers,
            "drops": self._total_drops,
            "dedup_skips": self._total_dedup_skips,
            "backpressure_skips": self._total_backpressure_skips,
            "dedup_cache_size": len(self._last_trigger_at),
        }
