"""
Yield Ranker — classifies DeFiLlama pools into 3 risk tiers and ranks within each.

Risk classification uses DeFiLlama's own signals (verified from live API response):
  - ilRisk: "no" or "yes" (impermanent loss risk)
  - exposure: "single" (one token) or "multi" (LP pair)
  - stablecoin: bool
  - predictions.predictedClass: "Stable/Up", "Down", or None
  - sigma: APY volatility (lower = more stable)
  - project: protocol name (known safe protocols get tier boost)

Tiers:
  LOW     — Lending/supply, single-asset, no IL, battle-tested protocols
  MEDIUM  — Stable-stable LP, low IL, or newer lending protocols
  HIGHER  — Incentive-heavy, volatile rewards, complex strategies
"""

from typing import List, Dict, Tuple


# Protocols we've verified as battle-tested lending on Base
# (from sentinel-plan.md Section 36.2 + DeFiLlama live data)
LENDING_PROJECTS = frozenset({
    "aave-v3",
    "moonwell",
    "morpho-v1",
    "compound-v3",
    "euler-v2",
    "spark-savings",
    "sparklend",
    "seamless-protocol",
    "fluid",
})

# Stable-stable DEX pools (medium risk — IL is minimal between pegged assets)
STABLE_DEX_PROJECTS = frozenset({
    "aerodrome-v1",
    "aerodrome-slipstream",
    "curve-dex",
    "balancer-v2",
    "velodrome-v3",
})

# Aggregators / optimizers (risk depends on underlying)
OPTIMIZER_PROJECTS = frozenset({
    "beefy",
    "yearn-finance",
    "pendle",
    "sommelier",
    "harvest-finance",
})


def classify_pool(pool: dict) -> str:
    """Classify a single pool into LOW, MEDIUM, or HIGHER risk tier.

    Returns one of: "low", "medium", "higher"
    """
    project = (pool.get("project") or "").lower()
    il_risk = pool.get("ilRisk", "no")
    exposure = pool.get("exposure", "single")
    is_stable = pool.get("stablecoin", False)
    sigma = pool.get("sigma") or 0
    apy_reward = pool.get("apyReward") or 0
    apy_base = pool.get("apyBase") or 0
    apy_total = pool.get("apy") or 0

    # ── LOW RISK ──
    # Single-asset lending on known protocols, no IL, organic yield
    if (project in LENDING_PROJECTS
            and exposure == "single"
            and il_risk == "no"):
        # Even lending can be "medium" if yield is mostly from reward tokens
        # (incentive farming masquerading as lending)
        if apy_total > 0 and apy_reward > 0:
            reward_pct = apy_reward / apy_total
            if reward_pct > 0.7:
                # >70% of yield from rewards = incentive-dependent
                return "medium"
        return "low"

    # ── MEDIUM RISK ──
    # Stable-stable LP (minimal IL), or optimizers over lending
    if il_risk == "no" and exposure == "multi" and is_stable:
        return "medium"

    if (project in STABLE_DEX_PROJECTS
            and is_stable
            and il_risk in ("no", "yes")):
        # Stable-stable pairs on known DEXes — IL is minimal
        return "medium"

    if project in OPTIMIZER_PROJECTS and exposure == "single":
        return "medium"

    # Single-asset on unknown protocol but no IL — cautiously medium
    if exposure == "single" and il_risk == "no" and is_stable:
        return "medium"

    # ── HIGHER RISK ──
    # Everything else: volatile LP, incentive-heavy, complex, unknown
    return "higher"


def rank_pools(pools: List[dict]) -> Dict[str, List[dict]]:
    """Classify and rank all pools into tiers.

    Returns dict with keys "low", "medium", "higher", each containing
    a list of pools sorted by total APY descending.
    Each pool dict gets an extra "risk_tier" field.
    """
    tiers: Dict[str, List[dict]] = {"low": [], "medium": [], "higher": []}

    for pool in pools:
        tier = classify_pool(pool)
        pool["risk_tier"] = tier
        tiers[tier].append(pool)

    # Sort each tier by APY descending (best yields first)
    for tier_name in tiers:
        tiers[tier_name].sort(key=lambda p: p.get("apy") or 0, reverse=True)

    return tiers


def top_recommendations(
    tiers: Dict[str, List[dict]],
    n_per_tier: int = 5,
) -> List[dict]:
    """Extract top N pools per tier as recommendations.

    Returns flat list of recommendation dicts ready for DB insertion.
    """
    recs = []
    for tier_name, pools in tiers.items():
        for i, pool in enumerate(pools[:n_per_tier]):
            recs.append({
                "pool_id": pool.get("pool", ""),
                "risk_tier": tier_name,
                "rank_in_tier": i + 1,
                "project": pool.get("project", ""),
                "symbol": pool.get("symbol", ""),
                "apy": pool.get("apy") or 0,
                "apy_base": pool.get("apyBase") or 0,
                "apy_reward": pool.get("apyReward") or 0,
                "tvl_usd": pool.get("tvlUsd") or 0,
                "apy_mean_30d": pool.get("apyMean30d") or 0,
                "sigma": pool.get("sigma") or 0,
                "pool_meta": pool.get("poolMeta"),
                "underlying_tokens": pool.get("underlyingTokens") or [],
                "il_risk": pool.get("ilRisk", "no"),
                "exposure": pool.get("exposure", "single"),
                "predicted_class": (pool.get("predictions") or {}).get("predictedClass"),
                "url": _pool_url(pool),
            })
    return recs


def _pool_url(pool: dict) -> str:
    """Best-effort URL for a pool's protocol page."""
    project = pool.get("project", "")
    # DeFiLlama doesn't include direct URLs, but we can construct
    # a DeFiLlama yields link
    pool_id = pool.get("pool", "")
    if pool_id:
        return f"https://defillama.com/yields/pool/{pool_id}"
    return ""


def summary(tiers: Dict[str, List[dict]]) -> str:
    """Human-readable summary of tier counts and top yields."""
    lines = []
    for tier_name in ("low", "medium", "higher"):
        pools = tiers.get(tier_name, [])
        if pools:
            best = pools[0]
            lines.append(
                f"  {tier_name.upper()}: {len(pools)} pools, "
                f"best={best.get('apy', 0):.2f}% "
                f"({best.get('project')}/{best.get('symbol')})"
            )
        else:
            lines.append(f"  {tier_name.upper()}: 0 pools")
    return "\n".join(lines)
