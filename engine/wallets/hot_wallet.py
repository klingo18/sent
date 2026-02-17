"""
Hot Wallet — single MVP wallet with nonce management, balance guards,
gas estimation, and stuck-tx handling.

Section 32.3: single-wallet custody, global asyncio.Lock in OrderManager.
This class handles the signing/sending layer underneath that lock.
"""

import asyncio
import time
from typing import Optional

from eth_account import Account
from web3 import AsyncWeb3


# Minimal ERC20 ABI for balance checks (avoids circular import with swap_v2)
_BAL_ABI = [{"inputs":[{"type":"address"}],"name":"balanceOf",
             "outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"}]

# Gas defaults (Base L2 — cheap, but must not be zero)
MIN_ETH_FOR_GAS = 10**15            # 0.001 ETH — ~20 swaps on Base
GAS_PRICE_STALENESS_SEC = 15.0      # re-fetch gas price after this
DEFAULT_GAS_LIMIT = 300_000
TX_RECEIPT_TIMEOUT = 90              # seconds to wait for mining
TX_RECEIPT_POLL = 1.0                # poll interval for receipt
SPEED_UP_AFTER_SEC = 30.0            # speed up a stuck tx after this
SPEED_UP_GAS_BUMP_PCT = 20           # bump maxFeePerGas by 20%


class NonceManager:
    """Local nonce tracker to avoid collisions under async concurrency.

    Syncs from chain on init and after errors, then increments locally.
    This is safe because OrderManager holds a global asyncio.Lock for
    all execution, so only one tx is in-flight at a time.
    """

    def __init__(self):
        self._local_nonce: Optional[int] = None
        self._lock = asyncio.Lock()

    async def get_nonce(self, w3: AsyncWeb3, address: str) -> int:
        async with self._lock:
            if self._local_nonce is None:
                self._local_nonce = await w3.eth.get_transaction_count(address, "pending")
            nonce = self._local_nonce
            self._local_nonce += 1
            return nonce

    async def sync_from_chain(self, w3: AsyncWeb3, address: str):
        """Re-sync after a tx error or on startup."""
        async with self._lock:
            self._local_nonce = await w3.eth.get_transaction_count(address, "pending")

    async def reset(self):
        async with self._lock:
            self._local_nonce = None


class HotWallet:
    """Single hot wallet with production-grade tx management."""

    def __init__(self, private_key: str, w3: AsyncWeb3):
        self.account = Account.from_key(private_key)
        self.address = self.account.address
        self.w3 = w3
        self.nonce_mgr = NonceManager()

        # Cached gas price
        self._gas_price_cache: Optional[dict] = None
        self._gas_price_ts: float = 0.0

        # Metrics
        self._tx_count: int = 0
        self._tx_failures: int = 0
        self._total_gas_used: int = 0

    # ------------------------------------------------------------------
    # Balance queries
    # ------------------------------------------------------------------

    async def get_eth_balance(self) -> int:
        return await self.w3.eth.get_balance(self.address)

    async def get_token_balance(self, token_address: str) -> int:
        contract = self.w3.eth.contract(
            address=self.w3.to_checksum_address(token_address), abi=_BAL_ABI,
        )
        return await contract.functions.balanceOf(self.address).call()

    async def ensure_gas_funds(self, min_eth: int = MIN_ETH_FOR_GAS) -> bool:
        """Check if wallet has enough ETH for gas. Returns True if OK."""
        bal = await self.get_eth_balance()
        if bal < min_eth:
            print(f"[WALLET] ⚠️  Low ETH: {bal / 1e18:.6f} ETH "
                  f"(need {min_eth / 1e18:.6f})")
            return False
        return True

    # ------------------------------------------------------------------
    # Nonce management
    # ------------------------------------------------------------------

    async def get_nonce(self) -> int:
        """Get next nonce via local tracking (avoids RPC per-tx)."""
        return await self.nonce_mgr.get_nonce(self.w3, self.address)

    async def sync_nonce(self):
        """Force re-sync nonce from chain (call after errors)."""
        await self.nonce_mgr.sync_from_chain(self.w3, self.address)
        print(f"[WALLET] Nonce re-synced from chain")

    # ------------------------------------------------------------------
    # Gas estimation
    # ------------------------------------------------------------------

    async def estimate_gas_params(self) -> dict:
        """Get EIP-1559 gas parameters for Base L2.

        Caches for GAS_PRICE_STALENESS_SEC to avoid per-tx RPC.
        Returns dict with maxFeePerGas and maxPriorityFeePerGas.
        """
        now = time.time()
        if (self._gas_price_cache is not None
                and now - self._gas_price_ts < GAS_PRICE_STALENESS_SEC):
            return dict(self._gas_price_cache)

        try:
            # Base L2 supports EIP-1559
            latest = await self.w3.eth.get_block("latest")
            base_fee = latest.get("baseFeePerGas", 0)
            if base_fee == 0:
                # Fallback to legacy gas price
                gas_price = await self.w3.eth.gas_price
                self._gas_price_cache = {"gasPrice": gas_price}
            else:
                # EIP-1559: baseFee * 2 + priority tip
                priority_fee = 100_000  # 0.1 gwei — typical for Base
                max_fee = base_fee * 2 + priority_fee
                self._gas_price_cache = {
                    "maxFeePerGas": max_fee,
                    "maxPriorityFeePerGas": priority_fee,
                }
            self._gas_price_ts = now
        except Exception as e:
            print(f"[WALLET] Gas estimation failed: {e}")
            if self._gas_price_cache is None:
                # Absolute fallback — 0.5 gwei is safe on Base
                self._gas_price_cache = {"gasPrice": 500_000_000}

        return dict(self._gas_price_cache)

    # ------------------------------------------------------------------
    # Transaction signing + sending
    # ------------------------------------------------------------------

    def sign_transaction(self, tx_dict: dict) -> bytes:
        signed = self.w3.eth.account.sign_transaction(tx_dict, self.account.key)
        return signed.raw_transaction

    async def send_transaction(self, tx_dict: dict) -> dict:
        """Sign, send, wait for receipt with stuck-tx handling.

        Returns the transaction receipt.
        Raises RuntimeError on failure after speed-up attempt.
        """
        # Inject gas params if not present
        if "gasPrice" not in tx_dict and "maxFeePerGas" not in tx_dict:
            gas_params = await self.estimate_gas_params()
            tx_dict.update(gas_params)

        raw = self.sign_transaction(tx_dict)
        tx_hash = await self.w3.eth.send_raw_transaction(raw)
        self._tx_count += 1

        # Wait for receipt with stuck-tx detection
        receipt = await self._wait_for_receipt(tx_hash, tx_dict)

        if receipt["status"] == 1:
            self._total_gas_used += receipt["gasUsed"]
        else:
            self._tx_failures += 1
            # Nonce was consumed even on revert — no re-sync needed
            print(f"[WALLET] ⚠️  Tx reverted: {tx_hash.hex()}")

        return receipt

    async def _wait_for_receipt(self, tx_hash, original_tx: dict) -> dict:
        """Wait for tx receipt. If stuck, attempt speed-up."""
        start = time.monotonic()
        speed_up_attempted = False

        while True:
            elapsed = time.monotonic() - start

            # Check if mined
            try:
                receipt = await self.w3.eth.get_transaction_receipt(tx_hash)
                if receipt is not None:
                    return receipt
            except Exception:
                pass  # not mined yet

            # Timeout
            if elapsed > TX_RECEIPT_TIMEOUT:
                await self.nonce_mgr.sync_from_chain(self.w3, self.address)
                raise RuntimeError(
                    f"Tx {tx_hash.hex()} not mined after {TX_RECEIPT_TIMEOUT}s"
                )

            # Speed up if stuck
            if not speed_up_attempted and elapsed > SPEED_UP_AFTER_SEC:
                speed_up_attempted = True
                new_hash = await self._try_speed_up(original_tx)
                if new_hash:
                    tx_hash = new_hash

            await asyncio.sleep(TX_RECEIPT_POLL)

    async def _try_speed_up(self, original_tx: dict) -> Optional[bytes]:
        """Attempt to speed up a stuck tx by re-submitting with higher gas."""
        try:
            bumped = dict(original_tx)
            if "maxFeePerGas" in bumped:
                bumped["maxFeePerGas"] = int(
                    bumped["maxFeePerGas"] * (100 + SPEED_UP_GAS_BUMP_PCT) / 100
                )
                bumped["maxPriorityFeePerGas"] = int(
                    bumped.get("maxPriorityFeePerGas", 0)
                    * (100 + SPEED_UP_GAS_BUMP_PCT) / 100
                )
            elif "gasPrice" in bumped:
                bumped["gasPrice"] = int(
                    bumped["gasPrice"] * (100 + SPEED_UP_GAS_BUMP_PCT) / 100
                )
            else:
                return None

            raw = self.sign_transaction(bumped)
            new_hash = await self.w3.eth.send_raw_transaction(raw)
            print(f"[WALLET] ⚡ Speed-up tx sent: {new_hash.hex()}")
            return new_hash
        except Exception as e:
            # Speed-up can fail if original already mined or nonce moved
            print(f"[WALLET] Speed-up failed (non-fatal): {e}")
            return None

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def metrics(self) -> dict:
        return {
            "address": self.address,
            "tx_count": self._tx_count,
            "tx_failures": self._tx_failures,
            "total_gas_used": self._total_gas_used,
        }

    def __repr__(self):
        return f"HotWallet({self.address})"
