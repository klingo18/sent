"""
SENTINEL Offering Registration Helper.

Generates the exact JSON to paste into the Virtuals ACP portal when
registering SENTINEL as a seller agent.

Usage:
    python3 scripts/register_offerings.py

Output: JSON for each offering in priceV2 format, ready to copy-paste.
Also prints the buyer-side service_requirement format for testing.
"""

import json
import os
from pathlib import Path

# Try to load SUPABASE_URL from .env if not in environment
_env_file = Path(__file__).resolve().parent.parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

_SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://<YOUR_SUPABASE_PROJECT>.supabase.co")
_FUNCTIONS_BASE = f"{_SUPABASE_URL}/functions/v1"

# ============================================================
# MVP Offerings (Section 32.2: limit_order + cancel_order only)
# ============================================================

SENTINEL_OFFERINGS = [
    {
        "name": "limit_order",
        "description": (
            "Place a limit buy order for any Virtuals agent token on Base. "
            "SENTINEL executes USDC→VIRTUAL→TOKEN when your target price is hit. "
            "Routing: V3 (0.3%) for leg 1, V2 for leg 2."
        ),
        "priceV2": {"type": "fixed", "value": 1.00},
        "requiredFunds": True,
        "requirement": {
            "type": "object",
            "properties": {
                "token_address": {
                    "type": "string",
                    "description": "Contract address of the token to buy (0x...)",
                },
                "token_symbol": {
                    "type": "string",
                    "description": "Token symbol for display (e.g. TRUST)",
                },
                "side": {
                    "type": "string",
                    "enum": ["buy"],
                    "description": "Order side — MVP supports 'buy' only",
                },
                "amount_usdc": {
                    "type": "number",
                    "minimum": 1,
                    "maximum": 10000,
                    "description": "USDC amount to spend (1-10000)",
                },
                "limit_price_usd": {
                    "type": "number",
                    "minimum": 0,
                    "description": "Target USD price per token to trigger fill",
                },
            },
            "required": ["token_address", "side", "amount_usdc", "limit_price_usd"],
        },
        "deliverable": {
            "type": "object",
            "description": "Returns open_position or position_fulfilled payload",
        },
    },
    {
        "name": "cancel_order",
        "description": (
            "Cancel an active SENTINEL limit order. "
            "Recovers funds (sells held tokens back to USDC) and returns to buyer."
        ),
        "priceV2": {"type": "fixed", "value": 0.10},
        "requiredFunds": False,
        "requirement": {
            "type": "object",
            "properties": {
                "original_job_id": {
                    "type": "integer",
                    "description": "ACP job ID of the order to cancel",
                },
            },
            "required": ["original_job_id"],
        },
        "deliverable": {
            "type": "object",
            "description": "Returns close_job_and_withdraw payload with recovery details",
        },
    },
]

# ============================================================
# ACP Resources (Section 32.6: 5 Edge Functions)
# ============================================================

SENTINEL_RESOURCES = [
    {
        "name": "get_service_status",
        "description": "Is SENTINEL online? Returns version, chain, active order count.",
        "url": f"{_FUNCTIONS_BASE}/get-service-status",
        "parameters": {},
    },
    {
        "name": "is_token_supported",
        "description": "Check if a token has a V2 pair with VIRTUAL (required for trading).",
        "url": f"{_FUNCTIONS_BASE}/is-token-supported",
        "parameters": {
            "type": "object",
            "properties": {
                "token_address": {"type": "string", "description": "Token contract address"},
            },
            "required": ["token_address"],
        },
    },
    {
        "name": "get_limit_order_quote",
        "description": "Pre-trade quote: estimated output, fees, routing, price impact.",
        "url": f"{_FUNCTIONS_BASE}/get-limit-order-quote",
        "parameters": {
            "type": "object",
            "properties": {
                "token_address": {"type": "string"},
                "amount_usdc": {"type": "number"},
                "side": {"type": "string", "enum": ["buy"]},
            },
            "required": ["token_address", "amount_usdc"],
        },
    },
    {
        "name": "get_order_status",
        "description": "Get status and fill details for a specific order.",
        "url": f"{_FUNCTIONS_BASE}/get-order-status",
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "UUID of the order"},
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "get_active_orders",
        "description": "All open (non-terminal) orders for a buyer wallet.",
        "url": f"{_FUNCTIONS_BASE}/get-active-orders",
        "parameters": {
            "type": "object",
            "properties": {
                "clientAddress": {"type": "string", "description": "Buyer wallet address"},
            },
            "required": ["clientAddress"],
        },
    },
    {
        "name": "get_yield_options",
        "description": (
            "Base stablecoin yield options ranked by risk tier. "
            "Returns top pools from Aave, Moonwell, Morpho, Aerodrome, etc. "
            "Data sourced from DeFiLlama, refreshed every 10 minutes."
        ),
        "url": f"{_FUNCTIONS_BASE}/get-yield-options",
        "parameters": {
            "type": "object",
            "properties": {
                "risk_tier": {
                    "type": "string",
                    "enum": ["low", "medium", "higher"],
                    "description": "Filter by risk tier (optional)",
                },
                "stablecoin": {
                    "type": "string",
                    "description": "Filter by stablecoin symbol, e.g. 'USDC' (optional)",
                },
                "min_tvl": {
                    "type": "number",
                    "description": "Minimum pool TVL in USD (optional)",
                },
                "min_apy": {
                    "type": "number",
                    "description": "Minimum APY percentage (optional)",
                },
            },
        },
    },
]

# ============================================================
# Output
# ============================================================

def main():
    print("=" * 60)
    print("  SENTINEL — ACP Offering Registration Helper")
    print("=" * 60)
    print()

    print("## 1. OFFERINGS (paste into ACP portal → Agent → Offerings)")
    print()
    for offering in SENTINEL_OFFERINGS:
        print(f"### {offering['name']} — ${offering['priceV2']['value']:.2f} USDC")
        print(f"    {offering['description'][:80]}...")
        print()
        print(json.dumps(offering, indent=2))
        print()

    print("-" * 60)
    print()
    print("## 2. RESOURCES (paste into ACP portal → Agent → Resources)")
    if "<YOUR_SUPABASE_PROJECT>" in _FUNCTIONS_BASE:
        print("   ⚠️  Set SUPABASE_URL in .env to get real URLs")
    else:
        print(f"   ✅ URLs using: {_FUNCTIONS_BASE}")
    print()
    for resource in SENTINEL_RESOURCES:
        print(f"### {resource['name']}")
        print(json.dumps(resource, indent=2))
        print()

    print("-" * 60)
    print()
    print("## 3. BUYER TEST COMMANDS (for testing with a buyer agent)")
    print()
    print("# Buy $10 worth of TRUST token at $0.001 limit:")
    print(json.dumps({
        "name": "limit_order",
        "requirement": {
            "token_address": "0xC841b4eaD3F70bE99472FFdB88E5c3C7aF6A481a",
            "token_symbol": "TRUST",
            "side": "buy",
            "amount_usdc": 10,
            "limit_price_usd": 0.001,
        },
    }, indent=2))
    print()
    print("# Cancel an existing order:")
    print(json.dumps({
        "name": "cancel_order",
        "requirement": {
            "original_job_id": 12345,
        },
    }, indent=2))
    print()

    print("-" * 60)
    print()
    print("## 4. FULL OFFERING JSON ARRAY (for bulk import if supported)")
    print()
    print(json.dumps(SENTINEL_OFFERINGS, indent=2))


if __name__ == "__main__":
    main()
