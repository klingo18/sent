"""
Yield Scanner — poll loop that fetches DeFiLlama data, ranks pools,
caches in memory, and persists to Supabase.

Designed to run as a background asyncio task alongside the price service.
Polls every SCAN_INTERVAL_SEC (default 10 min). No RPC calls — pure HTTP.
"""

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from engine.yields.llama_client import LlamaClient
from engine.yields.ranker import rank_pools, top_recommendations, summary


SCAN_INTERVAL_SEC = 600  # 10 minutes — DeFiLlama data updates ~every 15 min
RECOMMENDATIONS_PER_TIER = 5


class YieldScanner:
    """Background yield scanner powered by DeFiLlama.

    Usage:
        scanner = YieldScanner(db)
        asyncio.create_task(scanner.run())

        # Query cached results anytime:
        recs = scanner.get_recommendations("low")
        all_pools = scanner.get_pools()
    """

    def __init__(self, db=None):
        """
        Args:
            db: Database instance (engine.db.client.Database) for persistence.
                If None, scanner runs in memory-only mode (still useful for
                testing or when Supabase isn't configured yet).
        """
        self.db = db
        self.client = LlamaClient()

        # In-memory cache
        self._pools: List[dict] = []
        self._tiers: Dict[str, List[dict]] = {"low": [], "medium": [], "higher": []}
        self._recommendations: List[dict] = []
        self._last_scan_ts: float = 0.0
        self._scan_count: int = 0
        self._scan_errors: int = 0

    # ------------------------------------------------------------------
    # Public query API (read from cache — always fast)
    # ------------------------------------------------------------------

    def get_pools(self, risk_tier: Optional[str] = None) -> List[dict]:
        """Get cached pools, optionally filtered by risk tier."""
        if risk_tier:
            return list(self._tiers.get(risk_tier, []))
        return list(self._pools)

    def get_recommendations(
        self,
        risk_tier: Optional[str] = None,
        min_tvl: float = 0,
        min_apy: float = 0,
    ) -> List[dict]:
        """Get top recommendations, optionally filtered."""
        recs = self._recommendations
        if risk_tier:
            recs = [r for r in recs if r["risk_tier"] == risk_tier]
        if min_tvl > 0:
            recs = [r for r in recs if r["tvl_usd"] >= min_tvl]
        if min_apy > 0:
            recs = [r for r in recs if r["apy"] >= min_apy]
        return recs

    def get_best_for_usdc(self, risk_tier: str = "low") -> Optional[dict]:
        """Get the single best pool for parking USDC in the given risk tier.

        Filters for pools whose symbol contains 'USDC' (our primary stablecoin).
        """
        tier_pools = self._tiers.get(risk_tier, [])
        for pool in tier_pools:
            symbol = (pool.get("symbol") or "").upper()
            if "USDC" in symbol:
                return pool
        return None

    @property
    def last_scan_ts(self) -> float:
        return self._last_scan_ts

    @property
    def is_stale(self) -> bool:
        """True if data is older than 2x the scan interval."""
        if self._last_scan_ts == 0:
            return True
        return (time.time() - self._last_scan_ts) > (SCAN_INTERVAL_SEC * 2)

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    async def run(self):
        """Main loop — runs forever, polls DeFiLlama every SCAN_INTERVAL_SEC."""
        print("[YIELD] Scanner starting...")
        # Initial scan immediately
        await self._scan_tick()

        while True:
            try:
                await asyncio.sleep(SCAN_INTERVAL_SEC)
                await self._scan_tick()
            except asyncio.CancelledError:
                print("[YIELD] Scanner shutting down")
                await self.client.close()
                return
            except Exception as e:
                self._scan_errors += 1
                print(f"[YIELD] Scan loop error: {e}")
                await asyncio.sleep(30)  # brief cooldown on error

    async def scan_once(self):
        """Run a single scan tick (useful for testing or on-demand refresh)."""
        await self._scan_tick()

    async def _scan_tick(self):
        """Single scan cycle: fetch → rank → cache → persist."""
        t0 = time.monotonic()

        # 1. Fetch from DeFiLlama
        pools = await self.client.fetch_base_stable_pools()
        if not pools:
            print("[YIELD] No pools returned from DeFiLlama")
            return

        # 2. Rank into tiers
        tiers = rank_pools(pools)

        # 3. Generate recommendations
        recs = top_recommendations(tiers, n_per_tier=RECOMMENDATIONS_PER_TIER)

        # 4. Update in-memory cache
        self._pools = pools
        self._tiers = tiers
        self._recommendations = recs
        self._last_scan_ts = time.time()
        self._scan_count += 1

        elapsed_ms = (time.monotonic() - t0) * 1000
        print(f"[YIELD] Scan #{self._scan_count} complete ({elapsed_ms:.0f}ms):\n"
              f"{summary(tiers)}")

        # 5. Persist to DB (non-blocking — don't let DB errors kill the scanner)
        if self.db:
            try:
                await self._persist_to_db(pools, recs)
            except Exception as e:
                print(f"[YIELD] DB persistence error (non-fatal): {e}")

    async def _persist_to_db(self, pools: List[dict], recs: List[dict]):
        """Upsert yield data to Supabase tables."""
        now_iso = datetime.now(timezone.utc).isoformat()

        # Upsert yield_pools (latest snapshot per pool)
        pool_rows = []
        for p in pools:
            pool_rows.append({
                "pool_id": p.get("pool", ""),
                "chain": p.get("chain", "Base"),
                "project": p.get("project", ""),
                "symbol": p.get("symbol", ""),
                "tvl_usd": p.get("tvlUsd") or 0,
                "apy": p.get("apy") or 0,
                "apy_base": p.get("apyBase") or 0,
                "apy_reward": p.get("apyReward") or 0,
                "apy_mean_30d": p.get("apyMean30d") or 0,
                "apy_pct_1d": p.get("apyPct1D"),
                "apy_pct_7d": p.get("apyPct7D"),
                "apy_pct_30d": p.get("apyPct30D"),
                "il_risk": p.get("ilRisk", "no"),
                "exposure": p.get("exposure", "single"),
                "stablecoin": p.get("stablecoin", False),
                "reward_tokens": json.dumps(p.get("rewardTokens") or []),
                "underlying_tokens": json.dumps(p.get("underlyingTokens") or []),
                "pool_meta": p.get("poolMeta"),
                "mu": p.get("mu") or 0,
                "sigma": p.get("sigma") or 0,
                "predicted_class": (p.get("predictions") or {}).get("predictedClass"),
                "risk_tier": p.get("risk_tier", "higher"),
                "volume_usd_1d": p.get("volumeUsd1d"),
                "volume_usd_7d": p.get("volumeUsd7d"),
                "updated_at": now_iso,
            })

        if pool_rows:
            await self.db._exec(
                lambda: self.db.client.table("yield_pools")
                    .upsert(pool_rows, on_conflict="pool_id")
                    .execute(),
                label="upsert_yield_pools",
            )

        # Upsert yield_recommendations (top N per tier)
        rec_rows = []
        for r in recs:
            rec_rows.append({
                "pool_id": r["pool_id"],
                "risk_tier": r["risk_tier"],
                "rank_in_tier": r["rank_in_tier"],
                "project": r["project"],
                "symbol": r["symbol"],
                "apy": r["apy"],
                "apy_base": r["apy_base"],
                "apy_reward": r["apy_reward"],
                "tvl_usd": r["tvl_usd"],
                "apy_mean_30d": r["apy_mean_30d"],
                "il_risk": r["il_risk"],
                "exposure": r["exposure"],
                "url": r["url"],
                "updated_at": now_iso,
            })

        if rec_rows:
            # Clear old recommendations and insert fresh
            await self.db._exec(
                lambda: self.db.client.table("yield_recommendations")
                    .delete()
                    .neq("pool_id", "")
                    .execute(),
                label="clear_yield_recommendations",
            )
            await self.db._exec(
                lambda: self.db.client.table("yield_recommendations")
                    .insert(rec_rows)
                    .execute(),
                label="insert_yield_recommendations",
            )

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def metrics(self) -> dict:
        return {
            "scan_count": self._scan_count,
            "scan_errors": self._scan_errors,
            "last_scan_ts": self._last_scan_ts,
            "is_stale": self.is_stale,
            "cached_pools": len(self._pools),
            "tiers": {t: len(p) for t, p in self._tiers.items()},
            "recommendations": len(self._recommendations),
            "llama": self.client.metrics(),
        }
