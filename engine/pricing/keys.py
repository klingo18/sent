"""
PoolKey and RouteKey — canonical identifiers for pools and multi-hop routes.
Section 33.2 of sentinel-plan.md.
"""

from dataclasses import dataclass
from typing import Optional, Tuple

from web3 import Web3


@dataclass(frozen=True)
class PoolKey:
    """Unique identifier for an on-chain liquidity pool."""
    chain_id: int
    dex: str            # "uniswap_v2" | "uniswap_v3"
    pool_address: str   # checksummed

    @property
    def canonical(self) -> str:
        return f"{self.chain_id}:{self.dex}:{self.pool_address.lower()}"

    def __repr__(self) -> str:
        return f"Pool({self.dex}:{self.pool_address[:10]}...)"


@dataclass(frozen=True)
class RouteKey:
    """Unique identifier for a multi-hop swap route."""
    chain_id: int
    in_token: str
    out_token: str
    pools: Tuple[PoolKey, ...]
    fee_tiers: Optional[Tuple[Optional[int], ...]] = None

    @property
    def canonical(self) -> str:
        pool_str = "|".join(p.pool_address.lower() for p in self.pools)
        return f"{self.chain_id}:{self.in_token.lower()}->{self.out_token.lower()}@{pool_str}"

    def __repr__(self) -> str:
        return f"Route({self.in_token[:8]}...->{self.out_token[:8]}... via {len(self.pools)} pools)"


# ---------------------------------------------------------------------------
# V2 pair address resolution (deterministic from factory)
# ---------------------------------------------------------------------------

# Uniswap V2 Pair ABI (minimal)
V2_PAIR_ABI_MINIMAL = [
    {"inputs": [], "name": "getReserves", "outputs": [
        {"type": "uint112", "name": "reserve0"},
        {"type": "uint112", "name": "reserve1"},
        {"type": "uint32", "name": "blockTimestampLast"},
    ], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "token0", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "token1", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
]

V2_FACTORY_ABI_GETPAIR = [
    {"inputs": [{"type": "address"}, {"type": "address"}],
     "name": "getPair", "outputs": [{"type": "address"}],
     "stateMutability": "view", "type": "function"},
]

# Uniswap V3 pool ABI (minimal for slot0 + liquidity)
V3_POOL_ABI_MINIMAL = [
    {"inputs": [], "name": "slot0", "outputs": [
        {"type": "uint160", "name": "sqrtPriceX96"},
        {"type": "int24", "name": "tick"},
        {"type": "uint16", "name": "observationIndex"},
        {"type": "uint16", "name": "observationCardinality"},
        {"type": "uint16", "name": "observationCardinalityNext"},
        {"type": "uint8", "name": "feeProtocol"},
        {"type": "bool", "name": "unlocked"},
    ], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "liquidity", "outputs": [{"type": "uint128"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "token0", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "token1", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
]


async def resolve_v2_pair(w3, factory_address: str, token_a: str, token_b: str) -> Optional[str]:
    """Look up the V2 pair address from factory. Returns None if no pair."""
    factory = w3.eth.contract(
        address=w3.to_checksum_address(factory_address),
        abi=V2_FACTORY_ABI_GETPAIR,
    )
    pair = await factory.functions.getPair(
        w3.to_checksum_address(token_a),
        w3.to_checksum_address(token_b),
    ).call()
    if pair == "0x0000000000000000000000000000000000000000":
        return None
    return Web3.to_checksum_address(pair)


def build_sentinel_route(
    chain_id: int,
    usdc_address: str,
    token_address: str,
    v3_pool_address: str,
    v2_pair_address: str,
) -> RouteKey:
    """Build the standard SENTINEL 2-hop route: USDC->VIRTUAL(V3)->TOKEN(V2)."""
    return RouteKey(
        chain_id=chain_id,
        in_token=usdc_address,
        out_token=token_address,
        pools=(
            PoolKey(chain_id, "uniswap_v3", Web3.to_checksum_address(v3_pool_address)),
            PoolKey(chain_id, "uniswap_v2", Web3.to_checksum_address(v2_pair_address)),
        ),
        fee_tiers=(3000, None),
    )
