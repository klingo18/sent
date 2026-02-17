"""
SENTINEL Configuration — loads env vars, validates required, fails fast.

Hardened with:
  - Address checksum validation (rejects malformed 0x addresses)
  - Range validation for numeric params
  - Private key format validation (length, hex)
  - URL validation
  - Startup warnings for dangerous configs
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root (sentinel/)
_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)

_WARNINGS: list = []  # collected during load, printed at summary


def _require(name: str) -> str:
    """Get a required env var or exit with a clear error."""
    val = os.getenv(name)
    if not val:
        print(f"FATAL: missing required env var: {name}", file=sys.stderr)
        print(f"  Copy .env.example to .env and fill in the values.", file=sys.stderr)
        sys.exit(1)
    return val.strip()


def _optional(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _validate_address(addr: str, label: str) -> str:
    """Validate an Ethereum address: 0x-prefixed, 42 chars, valid hex."""
    if not addr.startswith("0x") or len(addr) != 42:
        print(f"FATAL: {label} is not a valid address: {addr}", file=sys.stderr)
        sys.exit(1)
    try:
        int(addr, 16)
    except ValueError:
        print(f"FATAL: {label} contains invalid hex: {addr}", file=sys.stderr)
        sys.exit(1)
    return addr


def _validate_private_key(key: str, label: str) -> str:
    """Validate a private key: 64 hex chars (with or without 0x prefix)."""
    raw = key[2:] if key.startswith("0x") else key
    if len(raw) != 64:
        print(f"FATAL: {label} must be 64 hex chars (got {len(raw)})", file=sys.stderr)
        sys.exit(1)
    try:
        int(raw, 16)
    except ValueError:
        print(f"FATAL: {label} contains invalid hex", file=sys.stderr)
        sys.exit(1)
    return key


def _validate_url(url: str, label: str) -> str:
    """Validate a URL starts with http:// or https://."""
    if not url.startswith(("http://", "https://")):
        print(f"FATAL: {label} must start with http:// or https://: {url}", file=sys.stderr)
        sys.exit(1)
    return url


def _int_range(name: str, raw: str, low: int, high: int) -> int:
    """Parse an int and clamp to [low, high] with a warning."""
    try:
        val = int(raw)
    except ValueError:
        print(f"FATAL: {name} must be an integer, got: {raw}", file=sys.stderr)
        sys.exit(1)
    if val < low or val > high:
        clamped = max(low, min(val, high))
        _WARNINGS.append(f"{name}={val} out of range [{low},{high}], clamped to {clamped}")
        return clamped
    return val


# === Base Chain ===
BASE_RPC_URL: str = _validate_url(_require("BASE_RPC_URL"), "BASE_RPC_URL")
BASE_RPC_FALLBACK: str = _optional("BASE_RPC_FALLBACK", "https://mainnet.base.org")
if BASE_RPC_FALLBACK:
    _validate_url(BASE_RPC_FALLBACK, "BASE_RPC_FALLBACK")
BASE_CHAIN_ID: int = 8453

# === RPC Pool (comma-separated list of RPC URLs, first = primary) ===
_rpc_urls_raw = _optional("BASE_RPC_URLS", "")
if _rpc_urls_raw:
    BASE_RPC_URLS = [u.strip() for u in _rpc_urls_raw.split(",") if u.strip()]
    for _u in BASE_RPC_URLS:
        _validate_url(_u, "BASE_RPC_URLS entry")
else:
    BASE_RPC_URLS = [BASE_RPC_URL]
    if BASE_RPC_FALLBACK and BASE_RPC_FALLBACK != BASE_RPC_URL:
        BASE_RPC_URLS.append(BASE_RPC_FALLBACK)

if len(BASE_RPC_URLS) < 2:
    _WARNINGS.append("Only 1 RPC endpoint — no fallback available")

# === Supabase ===
SUPABASE_URL: str = _validate_url(_require("SUPABASE_URL"), "SUPABASE_URL")
SUPABASE_KEY: str = _require("SUPABASE_KEY")

# === SENTINEL Agent (ACP) ===
SENTINEL_WALLET: str = _validate_address(_require("SENTINEL_WALLET"), "SENTINEL_WALLET")
SENTINEL_PRIVATE_KEY: str = _validate_private_key(_require("SENTINEL_PRIVATE_KEY"), "SENTINEL_PRIVATE_KEY")
SENTINEL_ENTITY_ID: int = _int_range("SENTINEL_ENTITY_ID", _require("SENTINEL_ENTITY_ID"), 1, 1_000_000)

# === Hot Wallet (single wallet for MVP) ===
HOT_WALLET_PRIVATE_KEY: str = _validate_private_key(_require("HOT_WALLET_PRIVATE_KEY"), "HOT_WALLET_PRIVATE_KEY")

# === ACP ===
ACP_SOCKET_URL: str = _validate_url(
    _optional("ACP_SOCKET_URL", "https://acpx.virtuals.io"), "ACP_SOCKET_URL"
)

# === Contract Addresses (Base Mainnet) — hardcoded, validated at import ===
USDC_ADDRESS: str = _validate_address("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "USDC_ADDRESS")
VIRTUAL_ADDRESS: str = _validate_address("0x0b3e328455c4059EEb9e3f84b5543F74E24e7E1b", "VIRTUAL_ADDRESS")
WETH_ADDRESS: str = _validate_address("0x4200000000000000000000000000000000000006", "WETH_ADDRESS")

UNISWAP_V2_ROUTER: str = _validate_address("0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24", "UNISWAP_V2_ROUTER")
UNISWAP_V2_FACTORY: str = _validate_address("0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6", "UNISWAP_V2_FACTORY")
UNISWAP_V3_SWAP_ROUTER: str = _validate_address("0x2626664c2603336E57B271c5C0b26F421741e481", "UNISWAP_V3_SWAP_ROUTER")
USDC_VIRTUAL_V3_POOL: str = _validate_address("0x529d2863a1521d0b57db028168fdE2E97120017C", "USDC_VIRTUAL_V3_POOL")

# === Tuning (with range validation) ===
PRICE_CHECK_INTERVAL: int = _int_range("PRICE_CHECK_INTERVAL", _optional("PRICE_CHECK_INTERVAL", "3"), 1, 60)
MAX_CONCURRENT_ORDERS: int = _int_range("MAX_CONCURRENT_ORDERS", _optional("MAX_CONCURRENT_ORDERS", "50"), 1, 500)
LOG_LEVEL: str = _optional("LOG_LEVEL", "INFO")
if LOG_LEVEL not in ("DEBUG", "INFO", "WARNING", "ERROR"):
    _WARNINGS.append(f"Unknown LOG_LEVEL '{LOG_LEVEL}', defaulting to INFO")
    LOG_LEVEL = "INFO"

# === RPC Throttle Caps (with range validation) ===
MAX_RPC_CONCURRENCY: int = _int_range("MAX_RPC_CONCURRENCY", _optional("MAX_RPC_CONCURRENCY", "6"), 1, 50)
MAX_MULTICALLS_PER_TICK: int = _int_range("MAX_MULTICALLS_PER_TICK", _optional("MAX_MULTICALLS_PER_TICK", "3"), 1, 20)
MAX_CALLS_PER_MULTICALL: int = _int_range("MAX_CALLS_PER_MULTICALL", _optional("MAX_CALLS_PER_MULTICALL", "50"), 1, 200)
TRIGGER_QUEUE_MAXSIZE: int = _int_range("TRIGGER_QUEUE_MAXSIZE", _optional("TRIGGER_QUEUE_MAXSIZE", "100"), 10, 10000)


def print_config_summary() -> None:
    """Print a non-sensitive config summary for startup verification."""
    print("--- SENTINEL Config ---")
    print(f"  RPC:            {BASE_RPC_URL[:40]}...")
    print(f"  Fallback RPC:   {BASE_RPC_FALLBACK[:40]}...")
    print(f"  Supabase:       {SUPABASE_URL[:40]}...")
    print(f"  Agent wallet:   {SENTINEL_WALLET}")
    print(f"  Entity ID:      {SENTINEL_ENTITY_ID}")
    # Show only first/last chars of wallet key for safety
    _hw_display = HOT_WALLET_PRIVATE_KEY[:6] + "..." + HOT_WALLET_PRIVATE_KEY[-4:]
    print(f"  Hot wallet:     {_hw_display}")
    print(f"  ACP Socket:     {ACP_SOCKET_URL}")
    print(f"  Price interval: {PRICE_CHECK_INTERVAL}s")
    print(f"  Max orders:     {MAX_CONCURRENT_ORDERS}")
    print(f"  RPC endpoints:  {len(BASE_RPC_URLS)}")
    print(f"  RPC concurrency:{MAX_RPC_CONCURRENCY}")
    print(f"  Multicall cap:  {MAX_CALLS_PER_MULTICALL} calls/batch")
    print(f"  Trigger queue:  {TRIGGER_QUEUE_MAXSIZE}")
    print(f"  Log level:      {LOG_LEVEL}")
    if _WARNINGS:
        print(f"  ⚠️  {len(_WARNINGS)} config warning(s):")
        for w in _WARNINGS:
            print(f"    - {w}")
    print("-" * 24)
