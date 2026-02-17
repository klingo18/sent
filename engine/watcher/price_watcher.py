"""
⚠️ DEPRECATED — replaced by engine/pricing/ (Section 33 Price Service).
This file is kept for reference only. Do not import or use.
Use PriceService (engine/pricing/service.py) instead.

Original: Price Watcher — polls getAmountsOut every N seconds for active orders.
"""

import asyncio
from typing import Dict, Optional

from engine.config import (
    USDC_ADDRESS,
    VIRTUAL_ADDRESS,
    PRICE_CHECK_INTERVAL,
)
from engine.executor.swap_v2 import ERC20_ABI


class PriceWatcher:
    """Monitors on-chain prices and triggers execution queue."""

    def __init__(self, w3, db, swap_executor):
        self.w3 = w3
        self.db = db
        self.executor = swap_executor
        self.execution_queue: asyncio.Queue = asyncio.Queue()
        self._running = False
        self._watched_tokens: Dict[str, float] = {}  # token_address → last_price

    async def run(self):
        """Main price polling loop. Runs forever until cancelled."""
        self._running = True
        print(f"[WATCHER] Started — polling every {PRICE_CHECK_INTERVAL}s")

        while self._running:
            try:
                await self._check_all_orders()
            except asyncio.CancelledError:
                self._running = False
                return
            except Exception as e:
                print(f"[WATCHER] Error in poll cycle: {e}")

            await asyncio.sleep(PRICE_CHECK_INTERVAL)

    def stop(self):
        self._running = False

    async def _check_all_orders(self):
        """Check prices for all active watching orders."""
        orders = await self.db.get_orders_by_status("watching")
        if not orders:
            return

        # Group orders by token to minimize RPC calls
        tokens: Dict[str, list] = {}
        for order in orders:
            addr = order["token_address"]
            tokens.setdefault(addr, []).append(order)

        for token_address, token_orders in tokens.items():
            try:
                price = await self._get_token_price(token_address)
                if price is None or price <= 0:
                    continue

                self._watched_tokens[token_address] = price

                for order in token_orders:
                    await self._evaluate_trigger(order, price)

            except Exception as e:
                print(f"[WATCHER] Price check failed for {token_address[:10]}...: {e}")

    async def _get_token_price(self, token_address: str) -> Optional[float]:
        """Get current price in VIRTUAL per TOKEN via getAmountsOut.

        We check: how many TOKEN do we get for 1 VIRTUAL?
        Price of TOKEN = 1 / (TOKEN per VIRTUAL) in VIRTUAL terms.
        For USD price: multiply by VIRTUAL/USD price from Leg 1 quote.
        """
        one_virtual = 10**18  # 1 VIRTUAL (18 decimals)

        try:
            token_amount = await self.executor.get_quote_v2(
                VIRTUAL_ADDRESS, token_address, one_virtual
            )
        except Exception:
            return None

        if token_amount <= 0:
            return None

        # Get token decimals
        token_contract = self.w3.eth.contract(
            address=self.w3.to_checksum_address(token_address),
            abi=ERC20_ABI,
        )
        try:
            decimals = await token_contract.functions.decimals().call()
        except Exception:
            decimals = 18

        # Price = VIRTUAL per TOKEN (how much VIRTUAL costs 1 TOKEN)
        tokens_per_virtual = token_amount / (10 ** decimals)
        if tokens_per_virtual <= 0:
            return None

        virtual_per_token = 1.0 / tokens_per_virtual

        # Get VIRTUAL/USD price
        usdc_amount = 1_000_000  # 1 USDC (6 decimals)
        try:
            virtual_per_usdc = await self.executor.get_quote_v2(
                USDC_ADDRESS,
                VIRTUAL_ADDRESS,
                usdc_amount,
            )
            usd_per_virtual = 1.0 / (virtual_per_usdc / 1e18) if virtual_per_usdc > 0 else 0
        except Exception:
            usd_per_virtual = 0

        price_usd = virtual_per_token * usd_per_virtual
        return price_usd

    async def _evaluate_trigger(self, order: dict, current_price_usd: float):
        """Check if order's limit price has been hit."""
        limit_price = float(order["limit_price_usd"])
        order_id = order["id"]
        side = order.get("side", "buy")

        # Buy limit: trigger when price <= limit
        if side == "buy" and current_price_usd <= limit_price:
            print(f"[WATCHER] TRIGGER order={order_id} "
                  f"price=${current_price_usd:.6f} <= limit=${limit_price:.6f}")
            await self.execution_queue.put({
                "type": "entry_fill",
                "order": order,
                "trigger_price_usd": current_price_usd,
            })
            # Mark as executing so we don't re-trigger
            await self.db.update_order_status(order_id, "executing")

        # Sell limit: trigger when price >= limit (Phase 2)
        elif side == "sell" and current_price_usd >= limit_price:
            print(f"[WATCHER] TRIGGER order={order_id} "
                  f"price=${current_price_usd:.6f} >= limit=${limit_price:.6f}")
            await self.execution_queue.put({
                "type": "entry_fill",
                "order": order,
                "trigger_price_usd": current_price_usd,
            })
            await self.db.update_order_status(order_id, "executing")
