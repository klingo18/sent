"""
DeFiLlama Yields API client — fetches and filters Base stablecoin pools.

API: GET https://yields.llama.fi/pools
Returns ~12,000+ pools across all chains. We filter client-side for:
  - chain == "Base"
  - stablecoin exposure (USDC, USDbC, DAI, USDS, EURC, GHO, crvUSD, DOLA, etc.)
  - TVL above minimum threshold
  - Non-zero APY
  - Not flagged as outlier

DeFiLlama pool schema (verified live Feb 2026):
  pool:             UUID string (unique pool identifier)
  chain:            string ("Base", "Ethereum", etc.)
  project:          string ("aave-v3", "morpho-v1", "aerodrome-v1", etc.)
  symbol:           string ("USDC", "USDC-USDbC", etc.)
  tvlUsd:           float
  apyBase:          float or None (organic APY from protocol fees/interest)
  apyReward:        float or None (incentive APY from reward tokens)
  apy:              float (total = apyBase + apyReward)
  rewardTokens:     list[str] or None
  stablecoin:       bool
  ilRisk:           "no" or "yes"
  exposure:         "single" or "multi"
  predictions:      dict with predictedClass, predictedProbability, binnedConfidence
  poolMeta:         string or None
  mu:               float (mean APY — DeFiLlama's statistical model)
  sigma:            float (APY standard deviation)
  count:            int (number of data points)
  outlier:          bool
  underlyingTokens: list[str]
  apyPct1D:         float or None (1-day APY change)
  apyPct7D:         float or None (7-day APY change)
  apyPct30D:        float or None (30-day APY change)
  apyBase7d:        float or None
  apyMean30d:       float
  volumeUsd1d:      float or None
  volumeUsd7d:      float or None
"""

import asyncio
import time
from typing import List, Dict, Optional

import aiohttp


LLAMA_YIELDS_URL = "https://yields.llama.fi/pools"

# Poll no more than once per 5 minutes (DeFiLlama is free, be respectful)
MIN_POLL_INTERVAL_SEC = 300

# Stablecoin symbols we care about for parking USDC
# The API has a `stablecoin` boolean flag which is our primary filter,
# but we also whitelist specific symbols for known stables that might
# not always get flagged.
STABLE_SYMBOLS = frozenset({
    "USDC", "USDBC", "DAI", "USDS", "EURC", "GHO", "CRVUSD",
    "DOLA", "USDT", "FRAX", "LUSD", "SUSD", "PYUSD", "USD+",
    "USDC-USDBC", "USDC-DAI", "USDC-USDT", "USDC-DOLA",
    "USDC-EURC", "USDBC-USDC",
})

# Minimum TVL to filter out micro-pools (junk, dead, or exploitable)
MIN_TVL_USD = 50_000

# Maximum APY — anything above this is almost certainly unsustainable
# or an outlier the API missed
MAX_SANE_APY = 50.0

# HTTP timeout for the API call
HTTP_TIMEOUT_SEC = 30


class LlamaClient:
    """Async client for DeFiLlama yields API with built-in rate limiting."""

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._last_fetch_ts: float = 0.0
        self._last_raw: List[dict] = []
        self._fetch_count: int = 0
        self._fetch_errors: int = 0

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SEC)
            self._session = aiohttp.ClientSession(timeout=timeout)

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def fetch_base_stable_pools(
        self,
        min_tvl: float = MIN_TVL_USD,
        max_apy: float = MAX_SANE_APY,
        force: bool = False,
    ) -> List[dict]:
        """Fetch and filter Base stablecoin pools from DeFiLlama.

        Returns a list of pool dicts matching our filters.
        Rate-limited to one call per MIN_POLL_INTERVAL_SEC unless force=True.
        """
        now = time.time()
        if not force and (now - self._last_fetch_ts) < MIN_POLL_INTERVAL_SEC:
            return self._filter_pools(self._last_raw, min_tvl, max_apy)

        await self._ensure_session()

        try:
            async with self._session.get(LLAMA_YIELDS_URL) as resp:
                if resp.status != 200:
                    print(f"[LLAMA] HTTP {resp.status} from yields API")
                    self._fetch_errors += 1
                    return self._filter_pools(self._last_raw, min_tvl, max_apy)

                data = await resp.json()
                pools = data.get("data", [])
                self._last_raw = pools
                self._last_fetch_ts = now
                self._fetch_count += 1

                filtered = self._filter_pools(pools, min_tvl, max_apy)
                print(f"[LLAMA] Fetched {len(pools)} total pools, "
                      f"{len(filtered)} Base stable pools pass filters")
                return filtered

        except asyncio.TimeoutError:
            print(f"[LLAMA] Timeout fetching yields API ({HTTP_TIMEOUT_SEC}s)")
            self._fetch_errors += 1
            return self._filter_pools(self._last_raw, min_tvl, max_apy)
        except Exception as e:
            print(f"[LLAMA] Error fetching yields API: {e}")
            self._fetch_errors += 1
            return self._filter_pools(self._last_raw, min_tvl, max_apy)

    def _filter_pools(
        self,
        pools: List[dict],
        min_tvl: float,
        max_apy: float,
    ) -> List[dict]:
        """Apply all filters to raw pool list."""
        result = []
        for p in pools:
            # Must be on Base
            if p.get("chain") != "Base":
                continue

            # Must have stablecoin exposure
            is_stable = p.get("stablecoin", False)
            symbol_upper = (p.get("symbol") or "").upper().replace(" ", "")
            if not is_stable and symbol_upper not in STABLE_SYMBOLS:
                # Check if any component of a multi-token symbol is a stable
                parts = set(symbol_upper.split("-"))
                if not parts.intersection(STABLE_SYMBOLS):
                    continue

            # TVL floor
            tvl = p.get("tvlUsd") or 0
            if tvl < min_tvl:
                continue

            # Must have positive APY
            apy = p.get("apy") or 0
            if apy <= 0:
                continue

            # Sanity cap — filter unsustainable yields
            if apy > max_apy:
                continue

            # DeFiLlama's own outlier flag
            if p.get("outlier", False):
                continue

            result.append(p)

        return result

    def metrics(self) -> dict:
        return {
            "fetch_count": self._fetch_count,
            "fetch_errors": self._fetch_errors,
            "last_fetch_ts": self._last_fetch_ts,
            "cached_total_pools": len(self._last_raw),
        }
