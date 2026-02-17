"""
PriceRegistry — subscription manager for jobs → routes → pools.
Section 33.2 / 33.6 of sentinel-plan.md.
"""

import time
from typing import Dict, Set, Optional, Any

from engine.pricing.keys import PoolKey, RouteKey


class PriceRegistry:
    """Tracks which jobs subscribe to which routes, and which pools are active."""

    def __init__(self):
        # route_key canonical string → RouteKey object
        self._routes: Dict[str, RouteKey] = {}
        # route_key canonical → set of job_ids subscribed
        self._subscribers: Dict[str, Set[int]] = {}
        # job_id → route_key canonical
        self._job_routes: Dict[int, str] = {}
        # job_id → condition dict (side, limit_price_usd, order_id, ...)
        self._job_conditions: Dict[int, dict] = {}
        # pool canonical → PoolKey
        self._active_pools: Dict[str, PoolKey] = {}
        # pool canonical → set of route_key canonicals that use it
        self._pool_routes: Dict[str, Set[str]] = {}
        # route_key canonical → last unsubscribe time (for warm-pool grace period)
        self._route_cooldown: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Subscribe / Unsubscribe
    # ------------------------------------------------------------------

    def subscribe(self, job_id: int, route: RouteKey, condition: dict):
        """Subscribe a job to a route. Activates pools if route is new."""
        rk = route.canonical

        # Register route if new
        if rk not in self._routes:
            self._routes[rk] = route
            self._subscribers[rk] = set()
            # Activate all pools in this route
            for pool in route.pools:
                pk = pool.canonical
                self._active_pools[pk] = pool
                self._pool_routes.setdefault(pk, set()).add(rk)
            print(f"[REGISTRY] New route: {rk} ({len(route.pools)} pools)")

        self._subscribers[rk].add(job_id)
        self._job_routes[job_id] = rk
        self._job_conditions[job_id] = condition
        # Clear cooldown if route was warming down
        self._route_cooldown.pop(rk, None)

        print(f"[REGISTRY] Job {job_id} subscribed to {rk} "
              f"(route has {len(self._subscribers[rk])} subscribers)")

    def unsubscribe(self, job_id: int):
        """Remove a job's subscription. May deactivate pools if route empty."""
        rk = self._job_routes.pop(job_id, None)
        if rk is None:
            return

        self._job_conditions.pop(job_id, None)
        subs = self._subscribers.get(rk)
        if subs:
            subs.discard(job_id)

        if subs is not None and len(subs) == 0:
            # Route has no subscribers — start cooldown (keep warm 5 min)
            self._route_cooldown[rk] = time.time()
            print(f"[REGISTRY] Route {rk} has 0 subscribers — cooldown started")

    def cleanup_expired_routes(self, grace_seconds: float = 300.0):
        """Remove routes (and their pools) that have been empty for grace_seconds."""
        now = time.time()
        expired = [
            rk for rk, ts in self._route_cooldown.items()
            if now - ts > grace_seconds
        ]
        for rk in expired:
            self._route_cooldown.pop(rk, None)
            route = self._routes.pop(rk, None)
            self._subscribers.pop(rk, None)
            if route:
                for pool in route.pools:
                    pk = pool.canonical
                    refs = self._pool_routes.get(pk)
                    if refs:
                        refs.discard(rk)
                        if len(refs) == 0:
                            self._active_pools.pop(pk, None)
                            self._pool_routes.pop(pk, None)
                print(f"[REGISTRY] Route {rk} expired — pools cleaned")

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    @property
    def active_pools(self) -> list:
        """All pools that need polling this tick."""
        return list(self._active_pools.values())

    @property
    def active_routes(self) -> Dict[str, RouteKey]:
        """All routes with ≥1 subscriber (or in cooldown)."""
        return dict(self._routes)

    @property
    def subscribers(self) -> Dict[str, Set[int]]:
        return self._subscribers

    @property
    def job_conditions(self) -> Dict[int, dict]:
        return self._job_conditions

    def get_route_for_job(self, job_id: int) -> Optional[str]:
        return self._job_routes.get(job_id)

    def get_route(self, route_key: str) -> Optional[RouteKey]:
        return self._routes.get(route_key)

    def pool_count(self) -> int:
        return len(self._active_pools)

    def route_count(self) -> int:
        return len(self._routes)

    def job_count(self) -> int:
        return len(self._job_routes)

    def summary(self) -> str:
        return (f"Registry: {self.job_count()} jobs, "
                f"{self.route_count()} routes, "
                f"{self.pool_count()} pools")
