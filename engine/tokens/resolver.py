"""
Token Resolution — 3-layer system to map symbols/addresses to tradeable tokens.
Section 25 of sentinel-plan.md.

Layer 1: Local cache (in-memory dict with TTL, bootstrapped from DB if available)
Layer 2: DexScreener API (rate-limited, fallback)
Layer 3: On-chain resolution (validate ERC20, check V2 pair exists)

Hardened with:
  - TTL cache: entries expire after CACHE_TTL_SECONDS
  - Negative cache: remember "not found" to avoid re-querying (shorter TTL)
  - DexScreener rate limiting: max N requests per minute
  - Bounded cache size: evict oldest on overflow
  - Resolve timeout: per-resolution timeout to prevent blocking
"""

import asyncio
import time
from typing import Optional, Dict

from engine.config import VIRTUAL_ADDRESS, UNISWAP_V2_FACTORY

# Cache tuning
CACHE_TTL_SECONDS = 3600.0          # positive cache: 1 hour
NEGATIVE_CACHE_TTL = 300.0           # negative cache: 5 minutes
MAX_CACHE_SIZE = 5000                # max cached tokens
RESOLVE_TIMEOUT = 10.0               # per-resolution timeout (seconds)

# DexScreener rate limiting
DEXSCREENER_RATE_LIMIT = 10         # max requests per window
DEXSCREENER_RATE_WINDOW = 60.0      # seconds
DEXSCREENER_TIMEOUT = 5.0           # HTTP timeout (seconds)


# Minimal ABIs for on-chain resolution
ERC20_META_ABI = [
    {"inputs": [], "name": "symbol", "outputs": [{"type": "string"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "name", "outputs": [{"type": "string"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "decimals", "outputs": [{"type": "uint8"}], "stateMutability": "view", "type": "function"},
]

V2_FACTORY_ABI = [
    {
        "inputs": [{"type": "address"}, {"type": "address"}],
        "name": "getPair",
        "outputs": [{"type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "allPairsLength",
        "outputs": [{"type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"type": "uint256"}],
        "name": "allPairs",
        "outputs": [{"type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
]

PAIR_ABI = [
    {"inputs": [], "name": "token0", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "token1", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
]

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


class _CacheEntry:
    """Cache entry with TTL."""
    __slots__ = ("data", "expires_at", "is_negative")

    def __init__(self, data: Optional[dict], ttl: float, is_negative: bool = False):
        self.data = data
        self.expires_at = time.time() + ttl
        self.is_negative = is_negative

    @property
    def expired(self) -> bool:
        return time.time() > self.expires_at


class TokenResolver:
    """Resolves token symbols/addresses to verified, tradeable token info."""

    def __init__(self, w3, db=None):
        self.w3 = w3
        self.db = db
        self._cache: Dict[str, _CacheEntry] = {}  # address.lower() → CacheEntry
        self._symbol_index: Dict[str, str] = {}    # symbol.upper() → address.lower()
        self._factory = w3.eth.contract(
            address=w3.to_checksum_address(UNISWAP_V2_FACTORY),
            abi=V2_FACTORY_ABI,
        )

        # DexScreener rate limiting
        self._ds_requests: list = []  # timestamps of recent requests

        # Metrics
        self._cache_hits = 0
        self._cache_misses = 0
        self._negative_hits = 0
        self._ds_queries = 0
        self._ds_rate_limited = 0
        self._onchain_queries = 0

    async def resolve(self, token: str) -> Optional[dict]:
        """
        Resolve a token symbol or address to full info.
        Returns dict with: address, symbol, name, decimals, pair_address, tradeable
        Returns None if not found/not tradeable.
        """
        try:
            return await asyncio.wait_for(
                self._resolve_inner(token), timeout=RESOLVE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            print(f"[TOKENS] Resolve timeout for {token[:20]}")
            return None

    async def _resolve_inner(self, token: str) -> Optional[dict]:
        if token.startswith("0x") and len(token) == 42:
            return await self._resolve_by_address(token)
        else:
            return await self._resolve_by_symbol(token)

    async def _resolve_by_address(self, address: str) -> Optional[dict]:
        """Layer 1 → Layer 3: resolve by contract address."""
        key = address.lower()

        # Layer 0: DB cache (persistent)
        if self.db:
            try:
                cached = await self.db.get_token_registry(address)
                if cached and cached.get("tradeable"):
                    self._set_cache(key, cached)
                    symbol = cached.get("symbol")
                    if symbol:
                        self._symbol_index[str(symbol).upper()] = key
                    return cached
            except Exception as e:
                print(f"[TOKENS] DB cache lookup failed for {address[:10]}...: {e}")

        # Layer 1: cache (with TTL)
        entry = self._cache.get(key)
        if entry and not entry.expired:
            if entry.is_negative:
                self._negative_hits += 1
                return None
            self._cache_hits += 1
            return entry.data

        self._cache_misses += 1

        # Layer 3: on-chain validation
        result = await self._resolve_onchain(address)
        if result is None:
            # Negative cache: remember this failure
            self._set_cache(key, None, negative=True)
        return result

    async def _resolve_by_symbol(self, symbol: str) -> Optional[dict]:
        """Layer 1 → Layer 2 → Layer 3: resolve by symbol."""
        sym = symbol.upper().lstrip("$")

        # Layer 1: cache via symbol index
        if sym in self._symbol_index:
            addr = self._symbol_index[sym]
            entry = self._cache.get(addr)
            if entry and not entry.expired:
                if entry.is_negative:
                    self._negative_hits += 1
                    return None
                self._cache_hits += 1
                return entry.data

        self._cache_misses += 1

        # Layer 2: DexScreener API (rate-limited)
        info = await self._query_dexscreener(sym)
        if info:
            return info

        return None

    async def _resolve_onchain(self, address: str) -> Optional[dict]:
        """Layer 3: validate ERC20 + check V2 pair with VIRTUAL."""
        self._onchain_queries += 1
        try:
            checksum = self.w3.to_checksum_address(address)
            contract = self.w3.eth.contract(address=checksum, abi=ERC20_META_ABI)

            # Get token metadata (parallel calls)
            symbol, name, decimals = await asyncio.gather(
                contract.functions.symbol().call(),
                contract.functions.name().call(),
                contract.functions.decimals().call(),
            )

            # Validate basic sanity
            if not symbol or not isinstance(decimals, int) or decimals > 30:
                print(f"[TOKENS] Invalid metadata for {address[:10]}...: "
                      f"symbol={symbol} decimals={decimals}")
                return None

            # Check V2 pair exists
            pair_address = await self._factory.functions.getPair(
                self.w3.to_checksum_address(VIRTUAL_ADDRESS),
                checksum,
            ).call()

            tradeable = pair_address.lower() != ZERO_ADDRESS

            info = {
                "address": address.lower(),
                "symbol": symbol,
                "name": name,
                "decimals": decimals,
                "pair_address": pair_address.lower() if tradeable else None,
                "tradeable": tradeable,
            }

            # Cache it
            self._set_cache(address.lower(), info)
            self._symbol_index[symbol.upper()] = address.lower()
            if self.db:
                try:
                    await self.db.upsert_token_registry(info)
                except Exception as e:
                    print(f"[TOKENS] DB upsert failed for {address[:10]}...: {e}")
            print(f"[TOKENS] Resolved {symbol} ({address[:10]}...): "
                  f"pair={'yes' if tradeable else 'NO'}")
            return info

        except Exception as e:
            print(f"[TOKENS] On-chain resolve failed for {address[:10]}...: {e}")
            return None

    def _set_cache(self, key: str, data: Optional[dict], negative: bool = False):
        """Set cache entry, evicting oldest if over size limit."""
        ttl = NEGATIVE_CACHE_TTL if negative else CACHE_TTL_SECONDS
        self._cache[key] = _CacheEntry(data, ttl, is_negative=negative)

        # Evict expired entries and oldest if over limit
        if len(self._cache) > MAX_CACHE_SIZE:
            now = time.time()
            # First pass: remove expired
            expired_keys = [k for k, v in self._cache.items() if v.expires_at < now]
            for k in expired_keys:
                del self._cache[k]

            # Second pass: if still over, remove oldest entries
            if len(self._cache) > MAX_CACHE_SIZE:
                sorted_keys = sorted(
                    self._cache.keys(),
                    key=lambda k: self._cache[k].expires_at,
                )
                for k in sorted_keys[:len(self._cache) - MAX_CACHE_SIZE + 100]:
                    del self._cache[k]

    def _check_ds_rate_limit(self) -> bool:
        """Check DexScreener rate limit. Returns True if within limits."""
        now = time.time()
        cutoff = now - DEXSCREENER_RATE_WINDOW
        self._ds_requests = [t for t in self._ds_requests if t > cutoff]
        if len(self._ds_requests) >= DEXSCREENER_RATE_LIMIT:
            return False
        self._ds_requests.append(now)
        return True

    async def _query_dexscreener(self, symbol: str) -> Optional[dict]:
        """Layer 2: query DexScreener for token address by symbol on Base."""
        if not self._check_ds_rate_limit():
            self._ds_rate_limited += 1
            return None

        self._ds_queries += 1
        try:
            import aiohttp
            url = f"https://api.dexscreener.com/latest/dex/search?q={symbol}"
            timeout = aiohttp.ClientTimeout(total=DEXSCREENER_TIMEOUT)
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=timeout) as resp:
                    if resp.status == 429:
                        print(f"[TOKENS] DexScreener rate limited (429)")
                        self._ds_rate_limited += 1
                        return None
                    if resp.status != 200:
                        return None
                    data = await resp.json()

            # Find Base chain pair with VIRTUAL
            for pair in data.get("pairs", []):
                if pair.get("chainId") != "base":
                    continue
                base_token = pair.get("baseToken", {})
                quote_token = pair.get("quoteToken", {})

                # Match symbol (base or quote side)
                matched_addr = None
                if base_token.get("symbol", "").upper() == symbol:
                    matched_addr = base_token.get("address")
                elif quote_token.get("symbol", "").upper() == symbol:
                    matched_addr = quote_token.get("address")

                if matched_addr:
                    # Validate on-chain before trusting DexScreener
                    return await self._resolve_onchain(matched_addr)

            return None
        except Exception as e:
            print(f"[TOKENS] DexScreener query failed for {symbol}: {e}")
            return None

    async def is_tradeable(self, address: str) -> bool:
        """Quick check: does a V2 VIRTUAL pair exist for this token?"""
        info = await self.resolve(address)
        return info is not None and info.get("tradeable", False)

    def get_cached(self, address: str) -> Optional[dict]:
        """Get cached token info without any network calls."""
        entry = self._cache.get(address.lower())
        if entry and not entry.expired and not entry.is_negative:
            return entry.data
        return None

    @property
    def cache_size(self) -> int:
        return len(self._cache)

    def metrics(self) -> dict:
        active = sum(1 for e in self._cache.values() if not e.expired and not e.is_negative)
        negative = sum(1 for e in self._cache.values() if not e.expired and e.is_negative)
        return {
            "cache_total": len(self._cache),
            "cache_active": active,
            "cache_negative": negative,
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "negative_hits": self._negative_hits,
            "dexscreener_queries": self._ds_queries,
            "dexscreener_rate_limited": self._ds_rate_limited,
            "onchain_queries": self._onchain_queries,
        }
