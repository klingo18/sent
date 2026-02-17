"""
ACP Offering definitions + validators.
MVP scope (Section 32.2): limit_order + cancel_order only.
"""

# MVP Offerings — registered on the Virtuals ACP platform.
# Each offering has:
#   name:       unique identifier (matches ACP job requirement.name)
#   min_price:  minimum service fee in USDC
#   description: shown to Butler for discovery

OFFERINGS = {
    "limit_order": {
        "name": "limit_order",
        "description": (
            "Place a limit buy order for any Virtuals agent token. "
            "SENTINEL swaps USDC→VIRTUAL→TOKEN when target price is hit."
        ),
        "min_price": 0.50,
        "max_price": 10000.0,
        "required_fields": ["token_address", "amount_usdc", "limit_price_usd"],
        "optional_fields": ["side"],
    },
    "cancel_order": {
        "name": "cancel_order",
        "description": (
            "Cancel an active limit order. "
            "SENTINEL recovers funds and returns USDC to buyer."
        ),
        "min_price": 0.10,
        "max_price": 1.0,
        "required_fields": ["original_job_id"],
        "optional_fields": [],
    },
}


def validate_limit_order(req: dict) -> tuple:
    """Validate limit_order requirements. Returns (valid, reason)."""
    token = req.get("token") or req.get("token_address")
    if not token:
        return False, "Missing 'token' or 'token_address'"

    if not token.startswith("0x") or len(token) != 42:
        return False, f"Invalid token address format: {token}"

    amount = req.get("amount_usdc")
    if not amount:
        return False, "Missing 'amount_usdc'"
    try:
        amt = float(amount)
        if amt < 1.0:
            return False, "Minimum order size is $1.00 USDC"
        if amt > 10000.0:
            return False, "Maximum order size is $10,000 USDC"
    except (ValueError, TypeError):
        return False, f"Invalid amount_usdc: {amount}"

    limit_price = req.get("limit_price_usd")
    if not limit_price:
        return False, "Missing 'limit_price_usd'"
    try:
        lp = float(limit_price)
        if lp <= 0:
            return False, "limit_price_usd must be positive"
    except (ValueError, TypeError):
        return False, f"Invalid limit_price_usd: {limit_price}"

    side = req.get("side")
    if side and str(side).lower() != "buy":
        return False, "Only 'buy' side is supported in MVP"

    return True, None


def validate_cancel_order(req: dict) -> tuple:
    """Validate cancel_order requirements. Returns (valid, reason)."""
    original_job_id = req.get("original_job_id")
    if not original_job_id:
        return False, "Missing 'original_job_id'"
    try:
        int(original_job_id)
    except (ValueError, TypeError):
        return False, f"Invalid original_job_id: {original_job_id}"
    return True, None


# Mapping of offering name → validator function
VALIDATORS = {
    "limit_order": validate_limit_order,
    "cancel_order": validate_cancel_order,
}
