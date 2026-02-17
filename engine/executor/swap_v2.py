"""
Swap Executor — Leg 1 (USDC→VIRTUAL via V3) + Leg 2 (VIRTUAL→TOKEN via V2).
Balance-delta verification on every swap (Section 29.5).
"""

import asyncio
import json
from decimal import Decimal, getcontext
from pathlib import Path

from web3 import AsyncWeb3

from engine.config import (
    USDC_ADDRESS,
    VIRTUAL_ADDRESS,
    UNISWAP_V2_ROUTER,
    UNISWAP_V3_SWAP_ROUTER,
    USDC_VIRTUAL_V3_POOL,
    BASE_CHAIN_ID,
)

# Load ABIs
_ABI_DIR = Path(__file__).resolve().parent.parent.parent / "abis"

# Minimal ABIs inlined for MVP (no external JSON files needed yet)
ERC20_ABI = json.loads('[{"inputs":[{"type":"address"},{"type":"uint256"}],"name":"approve","outputs":[{"type":"bool"}],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"type":"address"},{"type":"address"}],"name":"allowance","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},{"inputs":[{"type":"address"}],"name":"balanceOf","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"decimals","outputs":[{"type":"uint8"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"symbol","outputs":[{"type":"string"}],"stateMutability":"view","type":"function"},{"inputs":[{"type":"address"},{"type":"uint256"}],"name":"transfer","outputs":[{"type":"bool"}],"stateMutability":"nonpayable","type":"function"}]')

UNI_V2_ROUTER_ABI = json.loads('[{"inputs":[{"type":"uint256"},{"type":"address[]"}],"name":"getAmountsOut","outputs":[{"type":"uint256[]"}],"stateMutability":"view","type":"function"},{"inputs":[{"type":"uint256","name":"amountIn"},{"type":"uint256","name":"amountOutMin"},{"type":"address[]","name":"path"},{"type":"address","name":"to"},{"type":"uint256","name":"deadline"}],"name":"swapExactTokensForTokens","outputs":[{"type":"uint256[]"}],"stateMutability":"nonpayable","type":"function"}]')

UNI_V3_ROUTER_ABI = json.loads('[{"inputs":[{"components":[{"name":"tokenIn","type":"address"},{"name":"tokenOut","type":"address"},{"name":"fee","type":"uint24"},{"name":"recipient","type":"address"},{"name":"amountIn","type":"uint256"},{"name":"amountOutMinimum","type":"uint256"},{"name":"sqrtPriceLimitX96","type":"uint160"}],"name":"params","type":"tuple"}],"name":"exactInputSingle","outputs":[{"type":"uint256"}],"stateMutability":"payable","type":"function"}]')

V3_POOL_ABI_MINIMAL = json.loads('[{"inputs":[],"name":"slot0","outputs":[{"type":"uint160","name":"sqrtPriceX96"},{"type":"int24","name":"tick"},{"type":"uint16","name":"observationIndex"},{"type":"uint16","name":"observationCardinality"},{"type":"uint16","name":"observationCardinalityNext"},{"type":"uint8","name":"feeProtocol"},{"type":"bool","name":"unlocked"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"token0","outputs":[{"type":"address"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"token1","outputs":[{"type":"address"}],"stateMutability":"view","type":"function"}]')

MAX_UINT256 = 2**256 - 1
DEFAULT_SLIPPAGE_BPS = 200  # 2%
SL_SLIPPAGE_BPS = 1000  # 10% for stop-loss


class SwapExecutor:
    """Executes V2/V3 swaps with balance-delta verification."""

    def __init__(self, w3: AsyncWeb3, hot_wallet):
        self.w3 = w3
        self.wallet = hot_wallet
        self.v2_router = w3.eth.contract(
            address=w3.to_checksum_address(UNISWAP_V2_ROUTER),
            abi=UNI_V2_ROUTER_ABI,
        )
        self.v3_router = w3.eth.contract(
            address=w3.to_checksum_address(UNISWAP_V3_SWAP_ROUTER),
            abi=UNI_V3_ROUTER_ABI,
        )
        self.v3_pool = w3.eth.contract(
            address=w3.to_checksum_address(USDC_VIRTUAL_V3_POOL),
            abi=V3_POOL_ABI_MINIMAL,
        )
        self._decimals_cache = {}
        # Metrics
        self._swap_count = 0
        self._swap_failures = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_quote_v2(self, token_in: str, token_out: str, amount_in: int) -> int:
        """Get expected output from V2 router. Free read call."""
        if amount_in <= 0:
            raise ValueError(f"get_quote_v2: amount_in must be > 0, got {amount_in}")
        path = [
            self.w3.to_checksum_address(token_in),
            self.w3.to_checksum_address(token_out),
        ]
        amounts = await self.v2_router.functions.getAmountsOut(amount_in, path).call()
        return amounts[-1]

    async def execute_leg1_usdc_to_virtual(self, usdc_amount: int, slippage_bps: int = DEFAULT_SLIPPAGE_BPS) -> dict:
        """Leg 1: USDC → VIRTUAL via Uniswap V3 exactInputSingle."""
        if usdc_amount <= 0:
            raise ValueError(f"Leg 1: usdc_amount must be > 0, got {usdc_amount}")

        usdc = self.w3.to_checksum_address(USDC_ADDRESS)
        virtual = self.w3.to_checksum_address(VIRTUAL_ADDRESS)

        # Pre-check: do we have enough USDC?
        usdc_bal = await self._get_balance(usdc, self.wallet.address)
        if usdc_bal < usdc_amount:
            raise RuntimeError(
                f"Leg 1: insufficient USDC — have {usdc_bal}, need {usdc_amount}"
            )

        # Pre-check: do we have ETH for gas?
        if not await self.wallet.ensure_gas_funds():
            raise RuntimeError("Leg 1: insufficient ETH for gas")

        # Balance snapshot before
        bal_before = await self._get_balance(virtual, self.wallet.address)

        # Ensure approval
        await self._ensure_approval(usdc, UNISWAP_V3_SWAP_ROUTER, usdc_amount)

        # Get quote for slippage calc (use V2 as rough estimate, fallback to V3 pool)
        try:
            estimated_out = await self.get_quote_v2(USDC_ADDRESS, VIRTUAL_ADDRESS, usdc_amount)
        except Exception:
            estimated_out = 0
        if estimated_out <= 0:
            estimated_out = await self._estimate_v3_out(USDC_ADDRESS, VIRTUAL_ADDRESS, usdc_amount)
        if estimated_out <= 0:
            raise RuntimeError("Leg 1: unable to estimate output for slippage guard")
        min_out = int(estimated_out * (10000 - slippage_bps) / 10000)

        # Build V3 exactInputSingle
        deadline = (await self.w3.eth.get_block("latest"))["timestamp"] + 120
        params = {
            "tokenIn": usdc,
            "tokenOut": virtual,
            "fee": 3000,
            "recipient": self.wallet.address,
            "amountIn": usdc_amount,
            "amountOutMinimum": min_out,
            "sqrtPriceLimitX96": 0,
        }

        gas_estimate = await self._estimate_gas(
            self.v3_router.functions.exactInputSingle(params), 300_000
        )
        tx = await self.v3_router.functions.exactInputSingle(params).build_transaction({
            "from": self.wallet.address,
            "gas": gas_estimate,
            "nonce": await self.wallet.get_nonce(),
            "chainId": BASE_CHAIN_ID,
        })

        receipt = await self._sign_and_send(tx)
        self._swap_count += 1

        # Balance snapshot after
        bal_after = await self._get_balance(virtual, self.wallet.address)
        actual_received = bal_after - bal_before

        success = receipt["status"] == 1 and actual_received > 0
        if not success:
            self._swap_failures += 1

        return {
            "success": success,
            "tx_hash": receipt["transactionHash"].hex(),
            "amount_in": usdc_amount,
            "estimated_out": estimated_out,
            "actual_received": actual_received,
            "gas_used": receipt["gasUsed"],
        }

    async def execute_leg2_virtual_to_token(self, token_address: str, virtual_amount: int, min_out: int) -> dict:
        """Leg 2: VIRTUAL → TOKEN via Uniswap V2 swapExactTokensForTokens."""
        if virtual_amount <= 0:
            raise ValueError(f"Leg 2: virtual_amount must be > 0, got {virtual_amount}")

        virtual = self.w3.to_checksum_address(VIRTUAL_ADDRESS)
        token = self.w3.to_checksum_address(token_address)

        # Pre-check: do we have enough VIRTUAL?
        virt_bal = await self._get_balance(virtual, self.wallet.address)
        if virt_bal < virtual_amount:
            raise RuntimeError(
                f"Leg 2: insufficient VIRTUAL — have {virt_bal}, need {virtual_amount}"
            )

        # Balance snapshot before
        bal_before = await self._get_balance(token, self.wallet.address)

        # Ensure approval
        await self._ensure_approval(virtual, UNISWAP_V2_ROUTER, virtual_amount)

        # Build V2 swap
        deadline = (await self.w3.eth.get_block("latest"))["timestamp"] + 120
        path = [virtual, token]

        swap_fn = self.v2_router.functions.swapExactTokensForTokens(
            virtual_amount, min_out, path, self.wallet.address, deadline,
        )
        gas_estimate = await self._estimate_gas(swap_fn, 300_000)
        tx = await swap_fn.build_transaction({
            "from": self.wallet.address,
            "gas": gas_estimate,
            "nonce": await self.wallet.get_nonce(),
            "chainId": BASE_CHAIN_ID,
        })

        receipt = await self._sign_and_send(tx)
        self._swap_count += 1

        # Balance snapshot after
        bal_after = await self._get_balance(token, self.wallet.address)
        actual_received = bal_after - bal_before

        success = receipt["status"] == 1 and actual_received > 0
        if not success:
            self._swap_failures += 1

        return {
            "success": success,
            "tx_hash": receipt["transactionHash"].hex(),
            "amount_in": virtual_amount,
            "actual_received": actual_received,
            "gas_used": receipt["gasUsed"],
        }

    async def execute_sell_token_to_virtual(self, token_address: str, token_amount: int, min_out: int) -> dict:
        """Sell: TOKEN → VIRTUAL via V2."""
        if token_amount <= 0:
            raise ValueError(f"sell_token: token_amount must be > 0, got {token_amount}")

        token = self.w3.to_checksum_address(token_address)
        virtual = self.w3.to_checksum_address(VIRTUAL_ADDRESS)

        # Pre-check: do we have enough TOKEN?
        tok_bal = await self._get_balance(token, self.wallet.address)
        if tok_bal < token_amount:
            raise RuntimeError(
                f"sell_token: insufficient balance — have {tok_bal}, need {token_amount}"
            )

        bal_before = await self._get_balance(virtual, self.wallet.address)
        await self._ensure_approval(token, UNISWAP_V2_ROUTER, token_amount)

        deadline = (await self.w3.eth.get_block("latest"))["timestamp"] + 120
        swap_fn = self.v2_router.functions.swapExactTokensForTokens(
            token_amount, min_out, [token, virtual], self.wallet.address, deadline,
        )
        gas_estimate = await self._estimate_gas(swap_fn, 300_000)
        tx = await swap_fn.build_transaction({
            "from": self.wallet.address,
            "gas": gas_estimate,
            "nonce": await self.wallet.get_nonce(),
            "chainId": BASE_CHAIN_ID,
        })

        receipt = await self._sign_and_send(tx)
        self._swap_count += 1
        bal_after = await self._get_balance(virtual, self.wallet.address)
        actual = bal_after - bal_before

        success = receipt["status"] == 1 and actual > 0
        if not success:
            self._swap_failures += 1

        return {
            "success": success,
            "tx_hash": receipt["transactionHash"].hex(),
            "amount_in": token_amount,
            "actual_received": actual,
            "gas_used": receipt["gasUsed"],
        }

    async def execute_sell_virtual_to_usdc(self, virtual_amount: int, slippage_bps: int = DEFAULT_SLIPPAGE_BPS) -> dict:
        """Reverse of Leg 1: VIRTUAL → USDC via V3 (for cancel/recovery)."""
        if virtual_amount <= 0:
            raise ValueError(f"sell_virtual: amount must be > 0, got {virtual_amount}")

        virtual = self.w3.to_checksum_address(VIRTUAL_ADDRESS)
        usdc = self.w3.to_checksum_address(USDC_ADDRESS)

        # Pre-check: do we have enough VIRTUAL?
        virt_bal = await self._get_balance(virtual, self.wallet.address)
        if virt_bal < virtual_amount:
            raise RuntimeError(
                f"sell_virtual: insufficient VIRTUAL — have {virt_bal}, need {virtual_amount}"
            )

        bal_before = await self._get_balance(usdc, self.wallet.address)
        await self._ensure_approval(virtual, UNISWAP_V3_SWAP_ROUTER, virtual_amount)

        # Estimate output (V2 quote fallback to V3 pool)
        try:
            estimated_out = await self.get_quote_v2(VIRTUAL_ADDRESS, USDC_ADDRESS, virtual_amount)
        except Exception:
            estimated_out = 0
        if estimated_out <= 0:
            estimated_out = await self._estimate_v3_out(VIRTUAL_ADDRESS, USDC_ADDRESS, virtual_amount)
        if estimated_out <= 0:
            raise RuntimeError("sell_virtual: unable to estimate output for slippage guard")
        min_out = int(estimated_out * (10000 - slippage_bps) / 10000)

        deadline = (await self.w3.eth.get_block("latest"))["timestamp"] + 120
        params = {
            "tokenIn": virtual,
            "tokenOut": usdc,
            "fee": 3000,
            "recipient": self.wallet.address,
            "amountIn": virtual_amount,
            "amountOutMinimum": min_out,
            "sqrtPriceLimitX96": 0,
        }

        gas_estimate = await self._estimate_gas(
            self.v3_router.functions.exactInputSingle(params), 300_000
        )
        tx = await self.v3_router.functions.exactInputSingle(params).build_transaction({
            "from": self.wallet.address,
            "gas": gas_estimate,
            "nonce": await self.wallet.get_nonce(),
            "chainId": BASE_CHAIN_ID,
        })

        receipt = await self._sign_and_send(tx)
        self._swap_count += 1
        bal_after = await self._get_balance(usdc, self.wallet.address)
        actual = bal_after - bal_before

        success = receipt["status"] == 1 and actual > 0
        if not success:
            self._swap_failures += 1

        return {
            "success": success,
            "tx_hash": receipt["transactionHash"].hex(),
            "amount_in": virtual_amount,
            "estimated_out": estimated_out,
            "actual_received": actual,
            "gas_used": receipt["gasUsed"],
        }

    async def transfer_erc20(self, token_address: str, to: str, amount: int) -> dict:
        """Transfer ERC20 tokens to buyer."""
        if amount <= 0:
            raise ValueError(f"transfer: amount must be > 0, got {amount}")

        # Pre-check balance
        tok_bal = await self._get_balance(token_address, self.wallet.address)
        if tok_bal < amount:
            raise RuntimeError(
                f"transfer: insufficient balance — have {tok_bal}, need {amount}"
            )

        token = self.w3.eth.contract(
            address=self.w3.to_checksum_address(token_address), abi=ERC20_ABI,
        )
        transfer_fn = token.functions.transfer(
            self.w3.to_checksum_address(to), amount,
        )
        gas_estimate = await self._estimate_gas(transfer_fn, 100_000)
        tx = await transfer_fn.build_transaction({
            "from": self.wallet.address,
            "gas": gas_estimate,
            "nonce": await self.wallet.get_nonce(),
            "chainId": BASE_CHAIN_ID,
        })
        receipt = await self._sign_and_send(tx)
        return {
            "success": receipt["status"] == 1,
            "tx_hash": receipt["transactionHash"].hex(),
            "gas_used": receipt["gasUsed"],
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_balance(self, token_address: str, wallet_address: str) -> int:
        token = self.w3.eth.contract(
            address=self.w3.to_checksum_address(token_address), abi=ERC20_ABI,
        )
        return await token.functions.balanceOf(self.w3.to_checksum_address(wallet_address)).call()

    async def _ensure_approval(self, token_address: str, spender: str, amount: int):
        """Approve spender if current allowance is insufficient."""
        token = self.w3.eth.contract(
            address=self.w3.to_checksum_address(token_address), abi=ERC20_ABI,
        )
        current = await token.functions.allowance(
            self.wallet.address, self.w3.to_checksum_address(spender),
        ).call()
        if current >= amount:
            return

        # Some tokens require resetting allowance to 0 before setting a new one
        if current > 0:
            reset_tx = await token.functions.approve(
                self.w3.to_checksum_address(spender), 0,
            ).build_transaction({
                "from": self.wallet.address,
                "gas": 100000,
                "nonce": await self.wallet.get_nonce(),
                "chainId": BASE_CHAIN_ID,
            })
            reset_receipt = await self._sign_and_send(reset_tx)
            if reset_receipt["status"] != 1:
                raise RuntimeError(f"Approval reset failed for {token_address} → {spender}")

        tx = await token.functions.approve(
            self.w3.to_checksum_address(spender), amount,
        ).build_transaction({
            "from": self.wallet.address,
            "gas": 100000,
            "nonce": await self.wallet.get_nonce(),
            "chainId": BASE_CHAIN_ID,
        })
        receipt = await self._sign_and_send(tx)
        if receipt["status"] != 1:
            raise RuntimeError(f"Approval failed for {token_address} → {spender}")
        print(f"[SWAP] Approved {token_address[:10]}... for {spender[:10]}... amount={amount}")

    async def _estimate_v3_out(self, token_in: str, token_out: str, amount_in: int) -> int:
        """Estimate output using USDC/VIRTUAL V3 pool spot price."""
        if amount_in <= 0:
            return 0
        try:
            token0 = await self.v3_pool.functions.token0().call()
            token1 = await self.v3_pool.functions.token1().call()
            slot0 = await self.v3_pool.functions.slot0().call()
            sqrt_price_x96 = slot0[0]
        except Exception:
            return 0

        token0 = token0.lower()
        token1 = token1.lower()
        token_in_l = token_in.lower()
        token_out_l = token_out.lower()

        if {token_in_l, token_out_l} != {token0, token1}:
            return 0

        d0 = await self._get_decimals(token0)
        d1 = await self._get_decimals(token1)

        getcontext().prec = 40
        price_raw = (Decimal(sqrt_price_x96) / Decimal(2 ** 96)) ** 2
        price_adjusted = price_raw * (Decimal(10) ** (d0 - d1))

        if price_adjusted <= 0:
            return 0

        if token_in_l == token0 and token_out_l == token1:
            amount_in_h = Decimal(amount_in) / (Decimal(10) ** d0)
            amount_out_h = amount_in_h * price_adjusted
            return int(amount_out_h * (Decimal(10) ** d1))

        if token_in_l == token1 and token_out_l == token0:
            amount_in_h = Decimal(amount_in) / (Decimal(10) ** d1)
            amount_out_h = amount_in_h / price_adjusted
            return int(amount_out_h * (Decimal(10) ** d0))

        return 0

    async def _get_decimals(self, token_address: str) -> int:
        key = token_address.lower()
        cached = self._decimals_cache.get(key)
        if cached is not None:
            return cached
        try:
            token = self.w3.eth.contract(
                address=self.w3.to_checksum_address(token_address), abi=ERC20_ABI,
            )
            decimals = await token.functions.decimals().call()
            decimals = int(decimals)
        except Exception:
            decimals = 18
        self._decimals_cache[key] = decimals
        return decimals

    async def _estimate_gas(self, contract_fn, fallback: int) -> int:
        """Estimate gas for a contract call, with a buffer and fallback."""
        try:
            estimate = await contract_fn.estimate_gas({"from": self.wallet.address})
            # Add 20% buffer — Base gas estimation can be tight
            return int(estimate * 1.2)
        except Exception:
            return fallback

    async def _sign_and_send(self, tx_dict: dict) -> dict:
        """Sign locally, send raw, wait for receipt with stuck-tx handling."""
        return await self.wallet.send_transaction(tx_dict)

    @staticmethod
    async def execute_with_retry(swap_fn, max_retries: int = 3) -> dict:
        """Retry swap on revert with nonce-aware recovery. Section 18 pattern."""
        last_err = None
        for attempt in range(max_retries):
            try:
                result = await swap_fn()
                return result
            except Exception as e:
                last_err = e
                err_str = str(e).lower()
                # Retryable: revert, insufficient output, timeout
                retryable = any(kw in err_str for kw in (
                    "revert", "output", "execution reverted",
                    "insufficient", "expired", "timeout", "not mined",
                ))
                if retryable and attempt < max_retries - 1:
                    wait = 2 ** attempt
                    print(f"[SWAP] Attempt {attempt+1}/{max_retries} failed: {e}. "
                          f"Retry in {wait}s")
                    await asyncio.sleep(wait)
                    continue
                raise  # non-retryable or last attempt
        raise RuntimeError(f"Swap failed after {max_retries} retries: {last_err}")
