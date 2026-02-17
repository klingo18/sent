"""
RPCPool — health-scored, sharded RPC endpoint manager.

Features:
  - Multiple endpoints with tier classification (primary/secondary/public)
  - Health scoring: tracks 429s, timeouts, latency per endpoint
  - Cooldown: temporarily disables unhealthy endpoints
  - Stable hashing: same pool/route key → same RPC (cache locality)
  - Global concurrency semaphore
  - Fallback ladder: primary → secondary → public
"""

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from typing import List, Optional, Callable, Any

from web3 import AsyncWeb3, AsyncHTTPProvider

# Defaults
DEFAULT_CALL_TIMEOUT = 15.0          # seconds per RPC call
CIRCUIT_BREAKER_THRESHOLD = 5        # consecutive errors to open circuit
CIRCUIT_BREAKER_RESET_SEC = 60.0     # how long circuit stays open
MAX_RETRY_ATTEMPTS = 2               # retries on different endpoints


# ---------------------------------------------------------------------------
# Endpoint health tracking
# ---------------------------------------------------------------------------

@dataclass
class RPCEndpoint:
    """Single RPC endpoint with health metrics."""
    url: str
    tier: str  # "primary", "secondary", "public"
    w3: AsyncWeb3 = field(repr=False, default=None)

    # Health counters (reset periodically)
    total_calls: int = 0
    errors_429: int = 0
    errors_timeout: int = 0
    errors_other: int = 0
    total_latency_ms: float = 0.0

    # Cooldown
    last_error_at: float = 0.0
    cooldown_until: float = 0.0

    # Circuit breaker — consecutive errors without a success
    _consecutive_errors: int = 0
    _circuit_open_until: float = 0.0

    # Lifetime counters (never reset)
    lifetime_calls: int = 0
    lifetime_errors: int = 0
    lifetime_successes: int = 0

    # Latency histogram buckets (ms)
    _latency_samples: list = field(default_factory=list)

    # Rolling window (reset every WINDOW_SECONDS)
    _window_start: float = field(default_factory=time.time)

    WINDOW_SECONDS: float = 60.0
    COOLDOWN_BASE_SECONDS: float = 5.0
    COOLDOWN_MAX_SECONDS: float = 120.0
    MAX_LATENCY_SAMPLES: int = 100

    def __post_init__(self):
        if self.w3 is None:
            self.w3 = AsyncWeb3(AsyncHTTPProvider(self.url))

    @property
    def is_cooled_down(self) -> bool:
        now = time.time()
        return now < self.cooldown_until or now < self._circuit_open_until

    @property
    def avg_latency_ms(self) -> float:
        return (self.total_latency_ms / self.total_calls) if self.total_calls > 0 else 0.0

    @property
    def p95_latency_ms(self) -> float:
        if not self._latency_samples:
            return 0.0
        sorted_samples = sorted(self._latency_samples)
        idx = int(len(sorted_samples) * 0.95)
        return sorted_samples[min(idx, len(sorted_samples) - 1)]

    @property
    def error_rate(self) -> float:
        if self.total_calls == 0:
            return 0.0
        return (self.errors_429 + self.errors_timeout + self.errors_other) / self.total_calls

    def score(self) -> float:
        """Health score — higher is better. Range roughly 0-100."""
        if self.is_cooled_down:
            return -1.0

        # Base score by tier
        tier_bonus = {"primary": 30, "secondary": 20, "public": 0}.get(self.tier, 0)

        # Penalty for errors (heavy weight on 429s)
        error_penalty = (self.errors_429 * 15) + (self.errors_timeout * 10) + (self.errors_other * 5)

        # Penalty for high latency (>200ms starts hurting)
        latency_penalty = max(0, (self.avg_latency_ms - 200) / 50)

        return max(0, 100 + tier_bonus - error_penalty - latency_penalty)

    def record_success(self, latency_ms: float):
        self._maybe_reset_window()
        self.total_calls += 1
        self.total_latency_ms += latency_ms
        self.lifetime_calls += 1
        self.lifetime_successes += 1
        self._consecutive_errors = 0  # reset circuit breaker
        # Track latency samples for percentile
        self._latency_samples.append(latency_ms)
        if len(self._latency_samples) > self.MAX_LATENCY_SAMPLES:
            self._latency_samples = self._latency_samples[-self.MAX_LATENCY_SAMPLES:]

    def record_error(self, error_type: str):
        """Record an error, apply cooldown, and check circuit breaker."""
        self._maybe_reset_window()
        self.total_calls += 1
        self.lifetime_calls += 1
        self.lifetime_errors += 1
        self.last_error_at = time.time()
        self._consecutive_errors += 1

        if error_type == "429":
            self.errors_429 += 1
            consecutive = self.errors_429
        elif error_type == "timeout":
            self.errors_timeout += 1
            consecutive = self.errors_timeout
        else:
            self.errors_other += 1
            consecutive = self.errors_other

        # Exponential cooldown: 5s, 10s, 20s, ... up to 120s
        cooldown = min(
            self.COOLDOWN_BASE_SECONDS * (2 ** (consecutive - 1)),
            self.COOLDOWN_MAX_SECONDS,
        )
        self.cooldown_until = time.time() + cooldown

        # Circuit breaker: open after N consecutive errors
        if self._consecutive_errors >= CIRCUIT_BREAKER_THRESHOLD:
            self._circuit_open_until = time.time() + CIRCUIT_BREAKER_RESET_SEC
            print(f"[RPC-POOL] 🔴 Circuit OPEN for {self.url[:30]}... "
                  f"({self._consecutive_errors} consecutive errors)")

    def _maybe_reset_window(self):
        """Reset counters every WINDOW_SECONDS to avoid stale penalties."""
        now = time.time()
        if now - self._window_start > self.WINDOW_SECONDS:
            self.total_calls = 0
            self.errors_429 = 0
            self.errors_timeout = 0
            self.errors_other = 0
            self.total_latency_ms = 0.0
            self._window_start = now


# ---------------------------------------------------------------------------
# RPC Pool
# ---------------------------------------------------------------------------

def _classify_error(e: Exception) -> str:
    """Classify an RPC error for health tracking."""
    msg = str(e).lower()
    # HTTP status codes
    if "429" in msg or "rate limit" in msg or "too many" in msg:
        return "429"
    if "timeout" in msg or "timed out" in msg or "asyncio.timeout" in msg:
        return "timeout"
    if "502" in msg or "503" in msg or "504" in msg:
        return "timeout"  # treat server errors as temporary
    if "connection" in msg or "refused" in msg or "reset" in msg:
        return "timeout"
    return "other"


class RPCPool:
    """Health-scored RPC pool with stable hashing, concurrency limits, and fallback."""

    def __init__(
        self,
        urls: List[str],
        tiers: Optional[List[str]] = None,
        max_concurrency: int = 6,
    ):
        if not urls:
            raise ValueError("RPCPool requires at least one URL")

        if tiers is None:
            tiers = []
            for i, url in enumerate(urls):
                if i == 0:
                    tiers.append("primary")
                elif any(pub in url for pub in (
                    "mainnet.base.org", "publicnode", "public",
                    "ankr.com/rpc", "drpc.org",
                )):
                    tiers.append("public")
                else:
                    tiers.append("secondary")

        self.endpoints: List[RPCEndpoint] = [
            RPCEndpoint(url=url, tier=tier)
            for url, tier in zip(urls, tiers)
        ]
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._max_concurrency = max_concurrency
        self._total_retries = 0
        self._created_at = time.time()

    # ------------------------------------------------------------------
    # Endpoint selection
    # ------------------------------------------------------------------

    def pick(self, shard_key: str = "") -> RPCEndpoint:
        """Pick the best healthy endpoint.

        If shard_key is provided, uses stable hashing to prefer a consistent
        endpoint (cache locality). Falls back to best-scored if that one is down.
        """
        healthy = [ep for ep in self.endpoints if not ep.is_cooled_down]
        if not healthy:
            # All endpoints are cooling down — use the one with shortest cooldown
            healthy = sorted(self.endpoints, key=lambda ep: ep.cooldown_until)
            print(f"[RPC-POOL] ⚠️  All endpoints cooling down — forced pick {healthy[0].url[:40]}")
            return healthy[0]

        if shard_key:
            # Stable hash: consistent assignment for cache locality
            h = int(hashlib.md5(shard_key.encode()).hexdigest(), 16)
            idx = h % len(healthy)
            return healthy[idx]

        # No shard key — pick by best health score
        return max(healthy, key=lambda ep: ep.score())

    def pick_by_tier(self, max_tier: str = "public") -> RPCEndpoint:
        """Pick best endpoint up to the given tier.
        Useful for: hot paths use primary/secondary only, cold paths can use public.
        """
        tier_order = {"primary": 0, "secondary": 1, "public": 2}
        max_level = tier_order.get(max_tier, 2)
        eligible = [
            ep for ep in self.endpoints
            if not ep.is_cooled_down and tier_order.get(ep.tier, 2) <= max_level
        ]
        if not eligible:
            return self.pick()  # fallback to any
        return max(eligible, key=lambda ep: ep.score())

    # ------------------------------------------------------------------
    # Guarded RPC call
    # ------------------------------------------------------------------

    async def call(
        self,
        coro_factory: Callable,
        shard_key: str = "",
        tier: str = "public",
        timeout: float = DEFAULT_CALL_TIMEOUT,
        retry: bool = True,
    ) -> Any:
        """Execute an RPC call with health tracking, concurrency control,
        per-call timeout, and automatic retry on a different endpoint.

        Args:
            coro_factory: callable(w3) -> awaitable — creates the actual RPC call
            shard_key: stable hash key for endpoint assignment
            tier: max tier to use ("primary", "secondary", "public")
            timeout: per-call timeout in seconds
            retry: if True, retry on a different endpoint on failure

        Returns: the result of the coroutine
        Raises: the original exception after recording it
        """
        max_attempts = MAX_RETRY_ATTEMPTS if retry else 1
        last_err = None
        tried_eps = set()

        for attempt in range(max_attempts):
            # Pick endpoint, avoiding already-tried ones on retry
            ep = self._pick_for_attempt(shard_key, tier, tried_eps)
            tried_eps.add(id(ep))

            async with self._semaphore:
                t0 = time.monotonic()
                try:
                    result = await asyncio.wait_for(
                        coro_factory(ep.w3), timeout=timeout
                    )
                    latency = (time.monotonic() - t0) * 1000
                    ep.record_success(latency)
                    return result
                except asyncio.TimeoutError:
                    ep.record_error("timeout")
                    last_err = RuntimeError(
                        f"RPC call timed out after {timeout}s on {ep.url[:30]}"
                    )
                    self._total_retries += 1
                except Exception as e:
                    ep.record_error(_classify_error(e))
                    last_err = e
                    self._total_retries += 1

        raise last_err

    def _pick_for_attempt(self, shard_key: str, tier: str, tried: set) -> 'RPCEndpoint':
        """Pick an endpoint, preferring untried ones on retry."""
        if shard_key:
            primary = self.pick(shard_key)
            if id(primary) not in tried:
                return primary
        # Fall through to tier-based or any healthy
        for ep in sorted(self.endpoints, key=lambda e: e.score(), reverse=True):
            if id(ep) not in tried and not ep.is_cooled_down:
                return ep
        # All tried or all down — pick best overall
        return self.pick()

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    @property
    def primary_w3(self) -> AsyncWeb3:
        """Best single w3 instance for non-sharded use (wallets, swap executor, etc.)."""
        return self.pick().w3

    @property
    def healthy_count(self) -> int:
        return sum(1 for ep in self.endpoints if not ep.is_cooled_down)

    def metrics(self) -> dict:
        """Aggregate pool metrics for monitoring."""
        uptime = time.time() - self._created_at
        total_lifetime = sum(ep.lifetime_calls for ep in self.endpoints)
        total_errors = sum(ep.lifetime_errors for ep in self.endpoints)
        return {
            "endpoints": len(self.endpoints),
            "healthy": self.healthy_count,
            "uptime_sec": round(uptime),
            "lifetime_calls": total_lifetime,
            "lifetime_errors": total_errors,
            "error_rate": round(total_errors / max(total_lifetime, 1), 4),
            "total_retries": self._total_retries,
        }

    def summary(self) -> str:
        parts = []
        for ep in self.endpoints:
            circuit = "⛔" if time.time() < ep._circuit_open_until else ""
            status = "🔴" if ep.is_cooled_down else "🟢"
            parts.append(
                f"{status}{circuit} {ep.tier}:{ep.url[:30]}... "
                f"score={ep.score():.0f} calls={ep.total_calls} "
                f"429={ep.errors_429} lat={ep.avg_latency_ms:.0f}ms "
                f"p95={ep.p95_latency_ms:.0f}ms life={ep.lifetime_calls}"
            )
        m = self.metrics()
        return (
            f"RPCPool: {m['healthy']}/{m['endpoints']} healthy, "
            f"concurrency={self._max_concurrency}, "
            f"retries={m['total_retries']}, "
            f"err_rate={m['error_rate']:.1%}\n  " + "\n  ".join(parts)
        )

    def print_status(self):
        print(f"[RPC-POOL] {self.summary()}")
