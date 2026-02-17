"""
RoutePricer — derive USD prices from pool states for all active routes.
Section 33.4 of sentinel-plan.md.
"""

from typing import Dict, Optional

from engine.pricing.keys import PoolKey, RouteKey
from engine.pricing.registry import PriceRegistry


class RoutePricer:
    """Compute token prices from pool reserve/slot0 data."""

    def __init__(self, registry: PriceRegistry):
        self.registry = registry
        # route_key canonical → derived price info
        self.route_prices: Dict[str, dict] = {}

    def compute_all(self, pool_states: Dict[str, dict]) -> Dict[str, float]:
        """Recompute prices for all active routes from current pool states.
        Returns dict of route_key canonical → price_usd_per_out_token.
        """
        result: Dict[str, float] = {}

        for rk_str, route in self.registry.active_routes.items():
            price = self._compute_route_price(route, pool_states)
            if price is not None and price > 0:
                self.route_prices[rk_str] = {
                    "price_usd_per_token": price,
                    "updated_at": _latest_update(route, pool_states),
                }
                result[rk_str] = price

        return result

    def _compute_route_price(self, route: RouteKey, pool_states: Dict[str, dict]) -> Optional[float]:
        """Compute USD price of out_token for a multi-hop route.

        For SENTINEL's standard route (USDC → VIRTUAL via V3, VIRTUAL → TOKEN via V2):
          Step 1: From V3 pool state → price of VIRTUAL in USDC
          Step 2: From V2 pool state → price of TOKEN in VIRTUAL
          Result: price_TOKEN_in_USDC = price_VIRTUAL_in_USDC × price_TOKEN_in_VIRTUAL
                  → invert to get USD per TOKEN
        """
        if len(route.pools) == 0:
            return None

        # Accumulate price through each hop
        # We want: how much out_token do you get per 1 in_token?
        cumulative_price = 1.0  # start with 1 unit of in_token

        # We need to track which token we're "holding" through the hops
        # For USDC→VIRTUAL→TOKEN:
        #   Hop 1: pool has USDC and VIRTUAL → how many VIRTUAL per USDC?
        #   Hop 2: pool has VIRTUAL and TOKEN → how many TOKEN per VIRTUAL?

        current_token = route.in_token.lower()

        for pool in route.pools:
            pk = pool.canonical
            state = pool_states.get(pk)
            if state is None:
                return None

            if pool.dex == "uniswap_v2":
                hop_price = self._v2_spot_price(state, current_token)
            elif pool.dex == "uniswap_v3":
                hop_price = self._v3_spot_price(state, current_token)
            else:
                return None

            if hop_price is None or hop_price <= 0:
                return None

            cumulative_price *= hop_price

            # Determine next token (the other token in the pair)
            if current_token == state["token0"]:
                current_token = state["token1"]
            else:
                current_token = state["token0"]

        # cumulative_price = how many out_tokens per 1 in_token
        # For price in USD: if in_token is USDC, then USD_per_out_token = 1 / cumulative_price
        if cumulative_price <= 0:
            return None

        price_usd_per_token = 1.0 / cumulative_price
        return price_usd_per_token

    def _v2_spot_price(self, state: dict, token_in: str) -> Optional[float]:
        """Spot price from V2 reserves: how many token_out per 1 token_in."""
        r0 = state.get("reserve0", 0)
        r1 = state.get("reserve1", 0)
        if r0 == 0 or r1 == 0:
            return None

        d0 = state.get("decimals0", 18)
        d1 = state.get("decimals1", 18)
        t0 = state.get("token0", "")

        # Normalize reserves to human-readable
        nr0 = r0 / (10 ** d0)
        nr1 = r1 / (10 ** d1)

        if token_in == t0:
            # Selling token0 for token1: price = reserve1/reserve0
            return nr1 / nr0
        else:
            # Selling token1 for token0: price = reserve0/reserve1
            return nr0 / nr1

    def _v3_spot_price(self, state: dict, token_in: str) -> Optional[float]:
        """Spot price from V3 sqrtPriceX96: how many token_out per 1 token_in."""
        sqrt_price_x96 = state.get("sqrt_price_x96", 0)
        if sqrt_price_x96 == 0:
            return None

        d0 = state.get("decimals0", 6)
        d1 = state.get("decimals1", 18)
        t0 = state.get("token0", "")

        # V3 price formula: price = (sqrtPriceX96 / 2^96)^2
        # This gives: price of token0 in terms of token1
        # i.e., 1 token0 = price × token1 (adjusted for decimals)
        price_raw = (sqrt_price_x96 / (2 ** 96)) ** 2

        # Adjust for decimals: multiply by 10^(d0-d1) to normalize
        price_adjusted = price_raw * (10 ** (d0 - d1))

        if token_in == t0:
            # Selling token0 for token1: price of token0 in token1
            return price_adjusted
        else:
            # Selling token1 for token0: invert
            if price_adjusted <= 0:
                return None
            return 1.0 / price_adjusted

    def get_price(self, route_key: str) -> Optional[float]:
        """Get the latest computed price for a route."""
        info = self.route_prices.get(route_key)
        return info["price_usd_per_token"] if info else None


def _latest_update(route: RouteKey, pool_states: Dict[str, dict]) -> float:
    """Return the most recent updated_at across all pools in a route."""
    times = []
    for pool in route.pools:
        state = pool_states.get(pool.canonical)
        if state:
            times.append(state.get("updated_at", 0))
    return max(times) if times else 0
