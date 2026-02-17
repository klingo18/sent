"""
Order Manager — manages order lifecycle, execution consumer, cancel, recovery.
Implements Section 32.3 single-wallet custody rules (global lock, balance snapshots).
"""

import asyncio
import json
import time
from datetime import datetime, timezone

from engine.config import (
    VIRTUAL_ADDRESS,
    USDC_ADDRESS,
    UNISWAP_V2_FACTORY,
    USDC_VIRTUAL_V3_POOL,
    BASE_CHAIN_ID,
)
from engine.executor.swap_v2 import SwapExecutor
from engine.acp import deliverable

MAX_ENTRY_RETRIES = 2           # how many times _execute_entry can be re-triggered
ENTRY_COOLDOWN_SEC = 10.0       # min seconds between re-trigger attempts
MAX_PRICE_IMPACT_PCT = 0.02     # 2% max price impact on leg 2 quote
MAX_POOL_SHARE = 0.02           # max share of VIRTUAL reserve to trade

class OrderManager:
    """Handles order lifecycle: create → watch → trigger → execute → deliver."""

    def __init__(self, db, swap_executor, hot_wallet, trigger_queue: asyncio.Queue):
        self.db = db
        self.executor = swap_executor
        self.wallet = hot_wallet
        self.trigger_queue = trigger_queue
        self._execution_lock = asyncio.Lock()  # Section 32.3 Rule 1: global lock
        self._acp_jobs = {}  # job_id → ACP detail object (for deliver)
        self._price_registry = None  # set via set_price_registry()
        self._w3 = None  # set via set_w3()
        # Per-order retry tracking: order_id → {"attempts": N, "last_attempt_at": float}
        self._entry_attempts = {}

    def set_price_registry(self, registry):
        self._price_registry = registry

    def set_w3(self, w3):
        self._w3 = w3

    async def _record_snapshot(self, order_id: str, label: str, token_address: str = ""):
        """Record a full balance snapshot for custody tracking."""
        try:
            if token_address:
                usdc_bal, virtual_bal, token_bal, eth_bal = await asyncio.gather(
                    self.wallet.get_token_balance(USDC_ADDRESS),
                    self.wallet.get_token_balance(VIRTUAL_ADDRESS),
                    self.wallet.get_token_balance(token_address),
                    self.wallet.get_eth_balance(),
                )
            else:
                usdc_bal, virtual_bal, eth_bal = await asyncio.gather(
                    self.wallet.get_token_balance(USDC_ADDRESS),
                    self.wallet.get_token_balance(VIRTUAL_ADDRESS),
                    self.wallet.get_eth_balance(),
                )
                token_bal = 0

            await self.db.record_snapshot({
                "order_id": order_id,
                "label": label,
                "usdc_balance": usdc_bal,
                "virtual_balance": virtual_bal,
                "token_balance": token_bal,
                "token_address": token_address or None,
                "eth_balance": eth_bal,
            })
        except Exception as e:
            print(f"[ORDERS] Snapshot {label} failed: {e}")

    async def _refund_usdc(self, buyer_address: str, amount_usdc: float) -> bool:
        if not buyer_address or amount_usdc <= 0:
            return False
        amount_raw = int(amount_usdc * 1e6)
        try:
            transfer = await self.executor.transfer_erc20(
                USDC_ADDRESS, buyer_address, amount_raw,
            )
            return transfer.get("success", False)
        except Exception as e:
            print(f"[ORDERS] USDC refund failed: {e}")
            return False

    # ------------------------------------------------------------------
    # Step 5: Execution Consumer — processes triggers from Price Service
    # ------------------------------------------------------------------

    async def run_execution_consumer(self):
        """Consume triggers from Price Service trigger queue. Runs forever."""
        print("[ORDERS] Execution consumer started")
        while True:
            try:
                trigger = await self.trigger_queue.get()
                await self._handle_trigger(trigger)
            except asyncio.CancelledError:
                return
            except Exception as e:
                print(f"[ORDERS] Execution consumer error: {e}")

    async def _handle_trigger(self, trigger: dict):
        """Route trigger to correct handler. Uses global lock."""
        trigger_type = trigger["type"]
        order_id = trigger.get("order_id")
        job_id = trigger.get("job_id")

        # Fetch full order from DB (trigger only carries IDs now)
        order = await self.db.get_order(order_id) if order_id else trigger.get("order")
        if not order:
            print(f"[ORDERS] Trigger for unknown order {order_id} — skipping")
            return
        order_id = order["id"]

        # Check retry budget and cooldown
        attempts = self._entry_attempts.get(order_id, {"attempts": 0, "last_at": 0})
        if attempts["attempts"] >= MAX_ENTRY_RETRIES:
            print(f"[ORDERS] Order {order_id}: max retries ({MAX_ENTRY_RETRIES}) exhausted — failing")
            job_id_for_fail = order.get("job_id")
            detail = self._acp_jobs.get(job_id_for_fail)
            if detail:
                await self._deliver_failure(
                    job_id_for_fail, detail,
                    f"Execution failed after {MAX_ENTRY_RETRIES} attempts",
                    token_address=order.get("token_address", ""),
                    token_symbol=order.get("token_symbol", ""),
                    amount_usdc=float(order.get("usdc_amount", 0)),
                    virtual_to_sell=int(order.get("virtual_held", 0)),
                    buyer_address=order.get("buyer_address", ""),
                )
            await self.db.update_order(order_id, {
                "status": "failed",
                "failed_at": datetime.now(timezone.utc).isoformat(),
            })
            self._entry_attempts.pop(order_id, None)
            return

        if time.time() - attempts["last_at"] < ENTRY_COOLDOWN_SEC:
            print(f"[ORDERS] Order {order_id}: cooldown — skipping trigger")
            await self._resubscribe_order(order)
            return

        print(f"[ORDERS] Processing trigger: {trigger_type} for order {order_id} "
              f"(attempt {attempts['attempts'] + 1}/{MAX_ENTRY_RETRIES})")

        async with self._execution_lock:
            try:
                await self.db.update_order_status(order_id, "executing")
                if trigger_type == "entry_fill":
                    await self._execute_entry(order, trigger.get("trigger_price_usd", 0))
                    # Success — clear retry tracking
                    self._entry_attempts.pop(order_id, None)
                else:
                    print(f"[ORDERS] Unknown trigger type: {trigger_type}")
            except Exception as e:
                print(f"[ORDERS] Execution failed for order {order_id}: {e}")
                # Sync nonce in case tx was sent but receipt failed
                try:
                    await self.wallet.sync_nonce()
                except Exception:
                    pass
                # Track attempt
                self._entry_attempts[order_id] = {
                    "attempts": attempts["attempts"] + 1,
                    "last_at": time.time(),
                }
                await self.db.update_order_status(order_id, "watching")
                # Re-subscribe so Price Service can re-trigger this order.
                await self._resubscribe_order(order)

    # ------------------------------------------------------------------
    # Create order from ACP job
    # ------------------------------------------------------------------

    async def create_order_from_job(self, job_id: int, detail, req: dict):
        """Create a new limit order from an accepted ACP job."""
        token_address = req.get("token_address") or req.get("token", "")
        token_symbol = req.get("token_symbol", token_address[:10] if token_address else "")
        amount_usdc = float(req.get("amount_usdc", 0))
        limit_price = float(req.get("limit_price_usd", 0))
        side = str(req.get("side", "buy")).lower()
        buyer_address = (getattr(detail, "client_address", None)
                         or getattr(detail, "buyer_address", None) or "")
        if buyer_address:
            buyer_address = buyer_address.lower()

        if side != "buy":
            refund_ok = await self._refund_usdc(buyer_address, amount_usdc)
            refund_note = "USDC refunded" if refund_ok else "USDC refund failed"
            await self._deliver_failure(
                job_id, detail, f"Only 'buy' side is supported in MVP ({refund_note})",
                token_address=token_address, token_symbol=token_symbol,
                amount_usdc=amount_usdc,
                buyer_address=buyer_address,
            )
            return

        # Store ACP detail for later delivery
        self._acp_jobs[job_id] = detail

        # Convert USDC to on-chain amount (6 decimals)
        usdc_amount_raw = int(amount_usdc * 1e6)

        # Notify buyer that funds arrived (Section 7 step 7)
        try:
            await asyncio.to_thread(
                detail.create_notification,
                deliverable.fund_response(self.wallet.address),
            )
        except Exception as e:
            print(f"[ORDERS] Job {job_id}: fund_response notification failed: {e}")

        # Resolve V2 pair address for this token (needed for route + pricing)
        route = None
        route_key_str = ""
        v2_pair = None
        if self._price_registry and self._w3:
            from engine.pricing.keys import (
                PoolKey, RouteKey, resolve_v2_pair, build_sentinel_route, V2_PAIR_ABI_MINIMAL,
            )
            v2_pair = await resolve_v2_pair(
                self._w3, UNISWAP_V2_FACTORY, VIRTUAL_ADDRESS, token_address,
            )
            if v2_pair:
                route = build_sentinel_route(
                    BASE_CHAIN_ID, USDC_ADDRESS, token_address,
                    USDC_VIRTUAL_V3_POOL, v2_pair,
                )
                route_key_str = route.canonical
            else:
                print(f"[ORDERS] Job {job_id}: WARNING no V2 pair for {token_address[:10]}")

        # Fetch token decimals for correct amount normalization in deliverables
        token_decimals = 18
        try:
            token_decimals = await self.executor._get_decimals(token_address)
        except Exception:
            pass  # default 18 is safe for most Virtuals tokens

        # Create order record BEFORE Leg 1 (crash-safe)
        order_data = {
            "job_id": job_id,
            "buyer_address": buyer_address,
            "token_address": token_address,
            "token_symbol": token_symbol,
            "token_decimals": token_decimals,
            "side": side,
            "usdc_amount": amount_usdc,
            "usdc_amount_raw": usdc_amount_raw,
            "limit_price_usd": limit_price,
            "virtual_held": 0,
            "status": "funded",
            "route_key": route_key_str,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        order_id = await self.db.insert_order(order_data)
        print(f"[ORDERS] Order {order_id} created — funding confirmed")

        if self._price_registry and not route:
            await self.db.update_order(order_id, {
                "status": "failed",
                "failed_at": datetime.now(timezone.utc).isoformat(),
            })
            refund_ok = await self._refund_usdc(buyer_address, amount_usdc)
            refund_note = "USDC refunded" if refund_ok else "USDC refund failed"
            await self._deliver_failure(
                job_id, detail, f"No V2 pair found for token ({refund_note})",
                token_address=token_address, token_symbol=token_symbol,
                amount_usdc=amount_usdc,
                buyer_address=buyer_address,
            )
            return

        # Liquidity/impact guard for leg 2
        if self._price_registry and self._w3 and v2_pair:
            try:
                virtual_estimate = await self.executor._estimate_v3_out(
                    USDC_ADDRESS, VIRTUAL_ADDRESS, usdc_amount_raw,
                )
                if virtual_estimate <= 0:
                    virtual_estimate = await self.executor.get_quote_v2(
                        USDC_ADDRESS, VIRTUAL_ADDRESS, usdc_amount_raw,
                    )

                if virtual_estimate > 0:
                    spot_out = await self.executor.get_quote_v2(
                        VIRTUAL_ADDRESS, token_address, int(1e18),
                    )
                    trade_out = await self.executor.get_quote_v2(
                        VIRTUAL_ADDRESS, token_address, virtual_estimate,
                    )
                    if spot_out > 0 and trade_out > 0:
                        spot_ratio = float(spot_out) / 1e18
                        trade_ratio = float(trade_out) / float(virtual_estimate)
                        price_impact = max(0.0, 1 - (trade_ratio / spot_ratio))
                        if price_impact > MAX_PRICE_IMPACT_PCT:
                            await self.db.update_order(order_id, {
                                "status": "failed",
                                "failed_at": datetime.now(timezone.utc).isoformat(),
                            })
                            refund_ok = await self._refund_usdc(buyer_address, amount_usdc)
                            refund_note = "USDC refunded" if refund_ok else "USDC refund failed"
                            await self._deliver_failure(
                                job_id,
                                detail,
                                ("Order too large for pool depth "
                                 f"(impact {price_impact * 100:.2f}%) — {refund_note}"),
                                token_address=token_address,
                                token_symbol=token_symbol,
                                amount_usdc=amount_usdc,
                                buyer_address=buyer_address,
                            )
                            return

                    pair = self._w3.eth.contract(address=v2_pair, abi=V2_PAIR_ABI_MINIMAL)
                    reserves = await pair.functions.getReserves().call()
                    token0 = await pair.functions.token0().call()
                    token1 = await pair.functions.token1().call()
                    virtual_reserve = reserves[0] if token0.lower() == VIRTUAL_ADDRESS.lower() else reserves[1]

                    usdc_amount_h = usdc_amount_raw / 1e6
                    virtual_est_h = virtual_estimate / 1e18
                    if virtual_reserve > 0 and virtual_est_h > 0 and usdc_amount_h > 0:
                        virtual_per_usdc = virtual_est_h / usdc_amount_h
                        usdc_per_virtual = 1 / virtual_per_usdc if virtual_per_usdc > 0 else 0
                        max_order_usdc = (virtual_reserve / 1e18) * usdc_per_virtual * MAX_POOL_SHARE
                        if max_order_usdc > 0 and amount_usdc > max_order_usdc:
                            await self.db.update_order(order_id, {
                                "status": "failed",
                                "failed_at": datetime.now(timezone.utc).isoformat(),
                            })
                            refund_ok = await self._refund_usdc(buyer_address, amount_usdc)
                            refund_note = "USDC refunded" if refund_ok else "USDC refund failed"
                            await self._deliver_failure(
                                job_id,
                                detail,
                                ("Order exceeds max pool share "
                                 f"({max_order_usdc:.2f} USDC cap) — {refund_note}"),
                                token_address=token_address,
                                token_symbol=token_symbol,
                                amount_usdc=amount_usdc,
                                buyer_address=buyer_address,
                            )
                            return
            except Exception as e:
                print(f"[ORDERS] Liquidity guard skipped: {e}")

        # Leg 1: USDC → VIRTUAL (immediately, before watching)
        async with self._execution_lock:
            await self._record_snapshot(order_id, "pre_leg1", token_address)
            print(f"[ORDERS] Job {job_id}: Leg 1 — {amount_usdc} USDC → VIRTUAL")
            try:
                leg1 = await SwapExecutor.execute_with_retry(
                    lambda: self.executor.execute_leg1_usdc_to_virtual(usdc_amount_raw)
                )
            except Exception as e:
                print(f"[ORDERS] Job {job_id}: Leg 1 FAILED after retries — {e}")
                await self.db.update_order(order_id, {
                    "status": "failed",
                    "failed_at": datetime.now(timezone.utc).isoformat(),
                })
                refund_ok = await self._refund_usdc(buyer_address, amount_usdc)
                refund_note = "USDC refunded" if refund_ok else "USDC refund failed"
                await self._deliver_failure(
                    job_id, detail, f"Leg 1 swap failed: {e} ({refund_note})",
                    token_address=token_address, token_symbol=token_symbol,
                    amount_usdc=amount_usdc,
                    buyer_address=buyer_address,
                )
                return

            if not leg1["success"]:
                print(f"[ORDERS] Job {job_id}: Leg 1 FAILED — {leg1}")
                await self.db.update_order(order_id, {
                    "status": "failed",
                    "failed_at": datetime.now(timezone.utc).isoformat(),
                })
                refund_ok = await self._refund_usdc(buyer_address, amount_usdc)
                refund_note = "USDC refunded" if refund_ok else "USDC refund failed"
                await self._deliver_failure(
                    job_id, detail, f"Leg 1 swap failed ({refund_note})",
                    token_address=token_address, token_symbol=token_symbol,
                    amount_usdc=amount_usdc,
                    buyer_address=buyer_address,
                )
                return

            virtual_received = leg1["actual_received"]
            print(f"[ORDERS] Job {job_id}: Leg 1 OK — received {virtual_received / 1e18:.4f} VIRTUAL")
            await self._record_snapshot(order_id, "post_leg1", token_address)
            await self.db.update_order(order_id, {
                "virtual_held": virtual_received,
                "status": "watching",
                "leg1_tx_hash": leg1["tx_hash"],
                "leg1_gas_used": leg1["gas_used"],
            })

        # Subscribe to Price Service for price monitoring
        if self._price_registry and route_key_str and route:
            self._price_registry.subscribe(job_id, route, {
                "side": side,
                "limit_price_usd": limit_price,
                "order_id": order_id,
                "order_data": order_data,
            })
            print(f"[ORDERS] Job {job_id} subscribed to route {route_key_str[:40]}...")

    # ------------------------------------------------------------------
    # Entry fill execution
    # ------------------------------------------------------------------

    async def _execute_entry(self, order: dict, trigger_price: float):
        """Execute Leg 2: VIRTUAL → TOKEN when limit is hit."""
        order_id = order["id"]
        token_address = order["token_address"]
        virtual_held = int(order.get("virtual_held", 0))

        if virtual_held <= 0:
            raise RuntimeError(f"Order {order_id}: virtual_held is {virtual_held} — cannot execute")

        # Record balance snapshot BEFORE swap (Section 29.5)
        await self._record_snapshot(order_id, "pre_leg2", token_address)

        # Get expected output for slippage calc
        try:
            expected_out = await self.executor.get_quote_v2(
                VIRTUAL_ADDRESS,
                token_address,
                virtual_held,
            )
            if expected_out <= 0:
                raise RuntimeError("invalid quote")
            min_out = int(expected_out * 0.95)  # 5% slippage for entry
        except Exception as e:
            raise RuntimeError(f"Leg 2 quote failed: {e}")

        print(f"[ORDERS] Order {order_id}: Leg 2 — {virtual_held / 1e18:.4f} VIRTUAL → TOKEN")
        leg2 = await SwapExecutor.execute_with_retry(
            lambda: self.executor.execute_leg2_virtual_to_token(
                token_address, virtual_held, min_out,
            )
        )

        if not leg2["success"]:
            raise RuntimeError(f"Leg 2 swap failed: tx={leg2.get('tx_hash', '?')}")

        tokens_received = leg2["actual_received"]
        print(f"[ORDERS] Order {order_id}: Leg 2 OK — received {tokens_received} tokens")

        # Record balance snapshot AFTER swap
        await self._record_snapshot(order_id, "post_leg2", token_address)

        # Compute slippage
        slippage_pct = 0.0
        if expected_out and expected_out > 0:
            slippage_pct = round((expected_out - tokens_received) / expected_out * 100, 4)

        # Record fill
        await self.db.record_fill(order_id, {
            "fill_type": "entry",
            "tx_hash": leg2["tx_hash"],
            "amount_in": virtual_held,
            "expected_out": expected_out,
            "actual_out": tokens_received,
            "trigger_price_usd": trigger_price,
            "gas_used": leg2["gas_used"],
            "slippage_pct": slippage_pct,
            "filled_at": datetime.now(timezone.utc).isoformat(),
        })

        # Transfer tokens to buyer
        job_id = order.get("job_id")
        buyer_address = order.get("buyer_address", "")
        transfer_ok = False

        if buyer_address:
            print(f"[ORDERS] Order {order_id}: transferring tokens to {buyer_address[:10]}...")
            try:
                transfer = await self.executor.transfer_erc20(
                    token_address, buyer_address, tokens_received,
                )
                transfer_ok = transfer["success"]
                if transfer_ok:
                    print(f"[ORDERS] Order {order_id}: transfer OK tx={transfer['tx_hash']}")
                else:
                    print(f"[ORDERS] Order {order_id}: transfer REVERTED")
            except Exception as e:
                print(f"[ORDERS] Order {order_id}: transfer FAILED: {e}")
                # Tokens are still in hot wallet — mark for manual recovery

        # Mark completed
        await self.db.update_order(order_id, {
            "status": "completed",
            "tokens_received": tokens_received,
            "transfer_ok": transfer_ok,
            "leg2_tx_hash": leg2["tx_hash"],
            "leg2_gas_used": leg2["gas_used"],
            "slippage_pct": slippage_pct,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        })

        # Deliver via ACP
        if job_id and job_id in self._acp_jobs:
            await self._deliver_success(job_id, order_id, tokens_received, leg2["tx_hash"])

    # ------------------------------------------------------------------
    # Cancel order
    # ------------------------------------------------------------------

    async def cancel_order(self, original_job_id: int, cancel_job_id: int, detail):
        """Cancel an active order and recover funds."""
        # Unsubscribe from price monitoring
        if self._price_registry:
            self._price_registry.unsubscribe(original_job_id)

        order = await self.db.get_order_by_job_id(original_job_id)
        if not order:
            await asyncio.to_thread(
                detail.deliver, json.dumps({"error": "Order not found"})
            )
            return

        order_id = order["id"]
        status = order["status"]

        if status in ("completed", "cancelled", "failed"):
            await asyncio.to_thread(
                detail.deliver,
                json.dumps({"error": f"Order already {status}"}),
            )
            return

        async with self._execution_lock:
            usdc_recovered = 0
            sell_tx = None
            if status in ("watching", "funded", "executing"):
                # Still holding VIRTUAL — sell back to USDC via V3
                virtual_held = int(order.get("virtual_held", 0))
                if virtual_held > 0:
                    print(f"[ORDERS] Cancel order {order_id}: selling {virtual_held / 1e18:.4f} VIRTUAL → USDC")
                    try:
                        sell = await self.executor.execute_sell_virtual_to_usdc(virtual_held)
                        if sell["success"]:
                            usdc_recovered = sell["actual_received"]
                            sell_tx = sell["tx_hash"]
                            print(f"[ORDERS] Cancel order {order_id}: recovered {usdc_recovered / 1e6:.2f} USDC")
                        else:
                            print(f"[ORDERS] Cancel order {order_id}: VIRTUAL→USDC sell REVERTED")
                    except Exception as e:
                        print(f"[ORDERS] Cancel order {order_id}: recovery swap FAILED: {e}")
                        try:
                            await self.wallet.sync_nonce()
                        except Exception:
                            pass

            # Return recovered USDC to buyer
            buyer_address = order.get("buyer_address", "")
            usdc_transfer_ok = False
            if buyer_address and usdc_recovered > 0:
                print(f"[ORDERS] Cancel order {order_id}: transferring {usdc_recovered / 1e6:.2f} USDC to {buyer_address[:10]}...")
                try:
                    transfer = await self.executor.transfer_erc20(
                        USDC_ADDRESS, buyer_address, usdc_recovered,
                    )
                    usdc_transfer_ok = transfer["success"]
                    if usdc_transfer_ok:
                        print(f"[ORDERS] Cancel order {order_id}: USDC transfer OK tx={transfer['tx_hash']}")
                    else:
                        print(f"[ORDERS] Cancel order {order_id}: USDC transfer REVERTED")
                except Exception as e:
                    print(f"[ORDERS] Cancel order {order_id}: USDC transfer FAILED: {e}")

            await self.db.update_order(order_id, {
                "status": "cancelled",
                "usdc_recovered": usdc_recovered / 1e6 if usdc_recovered else 0,
                "cancel_tx_hash": sell_tx,
                "cancelled_at": datetime.now(timezone.utc).isoformat(),
            })

        # Clean up retry tracking
        self._entry_attempts.pop(order_id, None)

        try:
            await asyncio.to_thread(
                detail.deliver,
                deliverable.close_job_and_withdraw(
                    f"Order {order_id} cancelled. Recovered {usdc_recovered / 1e6:.2f} USDC. "
                    f"Transfer to buyer: {'OK' if usdc_transfer_ok else 'FAILED'}"
                ),
            )
        except Exception as e:
            print(f"[ORDERS] Cancel deliver failed for order {order_id}: {e}")
        finally:
            # Clean up: remove ACP detail for ORIGINAL job (cancel job detail is not stored)
            self._acp_jobs.pop(original_job_id, None)

    # ------------------------------------------------------------------
    # Startup recovery (Section 32.3 Rule 3)
    # ------------------------------------------------------------------

    async def recover_on_startup(self):
        """Reconcile active orders after crash/restart. Re-subscribe to Price Service.

        Key safety rule: if an order was 'executing' and has a tx_hash,
        check the receipt before re-arming — prevents double-execution.
        """
        active = await self.db.get_active_orders()
        if not active:
            print("[ORDERS] Recovery: no active orders")
            return

        print(f"[ORDERS] Recovery: found {len(active)} active orders")
        for order in active:
            status = order["status"]
            order_id = order["id"]

            if status == "executing":
                await self._recover_executing_order(order)
                # Re-fetch to get updated status
                order = await self.db.get_order(order_id)
                if not order:
                    continue
                status = order["status"]

            if status in ("watching", "funded"):
                print(f"[ORDERS] Recovery: order {order_id} OK ({status})")
                await self._resubscribe_order(order)
            elif status not in ("completed", "failed", "cancelled"):
                print(f"[ORDERS] Recovery: order {order_id} unexpected status: {status}")

    async def _recover_executing_order(self, order: dict):
        """Handle an order that was 'executing' when we crashed.

        Cases:
          A) leg2_tx_hash exists → check receipt → finalize or re-arm
          B) leg1 done (virtual_held > 0) but no leg2 → back to watching
          C) no leg1 yet → back to funded (USDC still intact)
        """
        order_id = order["id"]
        leg2_tx = order.get("leg2_tx_hash")
        virtual_held = int(order.get("virtual_held", 0))

        # Case A: leg 2 was sent — check if it actually landed
        if leg2_tx and self._w3:
            try:
                receipt = await self._w3.eth.get_transaction_receipt(leg2_tx)
                if receipt and receipt["status"] == 1:
                    print(f"[ORDERS] Recovery: order {order_id} leg2 tx {leg2_tx[:10]}... CONFIRMED — finalizing")
                    # Check if fill already recorded (idempotency)
                    existing_fills = await self.db.get_fills_for_order(order_id)
                    has_entry_fill = any(f.get("fill_type") == "entry" for f in (existing_fills or []))
                    if not has_entry_fill:
                        # Record the fill we missed
                        await self.db.record_fill(order_id, {
                            "fill_type": "entry",
                            "tx_hash": leg2_tx,
                            "amount_in": virtual_held,
                            "expected_out": 0,
                            "actual_out": 0,
                            "trigger_price_usd": 0,
                            "gas_used": receipt.get("gasUsed", 0),
                            "slippage_pct": 0,
                            "filled_at": datetime.now(timezone.utc).isoformat(),
                        })
                    await self.db.update_order(order_id, {
                        "status": "completed",
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                    })
                    print(f"[ORDERS] Recovery: order {order_id} marked completed (receipt verified)")
                    return
                elif receipt and receipt["status"] == 0:
                    print(f"[ORDERS] Recovery: order {order_id} leg2 tx REVERTED — back to watching")
                    await self.db.update_order_status(order_id, "watching")
                    return
            except Exception as e:
                print(f"[ORDERS] Recovery: order {order_id} receipt check failed: {e}")

        # Case B: leg 1 done, holding VIRTUAL, leg 2 not sent
        if virtual_held > 0:
            print(f"[ORDERS] Recovery: order {order_id} has VIRTUAL, no leg2 tx → watching")
            await self.db.update_order_status(order_id, "watching")
            return

        # Case C: no leg 1 yet — USDC should still be in wallet
        print(f"[ORDERS] Recovery: order {order_id} no VIRTUAL held → funded")
        await self.db.update_order_status(order_id, "funded")

    async def _resubscribe_order(self, order: dict):
        """Re-subscribe a recovered order to the Price Service."""
        if not self._price_registry or not self._w3:
            return

        job_id = order.get("job_id")
        token_address = order.get("token_address", "")
        route_key_str = order.get("route_key", "")

        if not token_address or not job_id:
            return

        from engine.pricing.keys import (
            PoolKey, RouteKey, resolve_v2_pair, build_sentinel_route,
        )
        v2_pair = await resolve_v2_pair(
            self._w3, UNISWAP_V2_FACTORY, VIRTUAL_ADDRESS, token_address,
        )
        if not v2_pair:
            print(f"[ORDERS] Recovery: no V2 pair for order {order['id']}")
            return

        route = build_sentinel_route(
            BASE_CHAIN_ID, USDC_ADDRESS, token_address,
            USDC_VIRTUAL_V3_POOL, v2_pair,
        )
        self._price_registry.subscribe(job_id, route, {
            "side": order.get("side", "buy"),
            "limit_price_usd": float(order.get("limit_price_usd", 0)),
            "order_id": order["id"],
            "order_data": order,
        })
        print(f"[ORDERS] Recovery: order {order['id']} re-subscribed to Price Service")

    # ------------------------------------------------------------------
    # ACP delivery helpers
    # ------------------------------------------------------------------

    async def _deliver_success(self, job_id: int, order_id, tokens_received: int, tx_hash: str):
        """Deliver open_position payload to ACP buyer (Section 15)."""
        detail = self._acp_jobs.get(job_id)
        if not detail:
            return
        order = await self.db.get_order(order_id)
        token_addr = order.get("token_address", "") if order else ""
        token_sym = order.get("token_symbol", token_addr[:10]) if order else ""
        token_decimals = order.get("token_decimals", 18) if order else 18
        payload = deliverable.open_position(
            token_address=token_addr,
            token_symbol=token_sym,
            amount_tokens=tokens_received / (10 ** token_decimals),
            tp_price=0,
            sl_price=0,
        )
        try:
            await asyncio.to_thread(detail.deliver, payload)
            print(f"[ORDERS] Delivered job {job_id}")
            await self.db.update_order(order_id, {
                "delivered_at": datetime.now(timezone.utc).isoformat(),
                "evaluation_status": "pending",
            })
        except Exception as e:
            print(f"[ORDERS] Deliver failed job {job_id}: {e}")
        finally:
            self._acp_jobs.pop(job_id, None)

    async def _deliver_failure(self, job_id: int, detail, reason: str, *,
                               token_address: str = "", token_symbol: str = "",
                               amount_usdc: float = 0, virtual_to_sell: int = 0,
                               buyer_address: str = ""):
        """Deliver unfulfilled_position payload to ACP buyer (Section 15).
        If virtual_to_sell > 0, attempts to sell that VIRTUAL back to USDC.
        Callers MUST specify the exact amount to avoid selling VIRTUAL from other orders.
        """
        usdc_recovered = 0.0
        usdc_recovered_raw = 0
        if virtual_to_sell > 0:
            try:
                # Verify we actually have enough before selling
                virt_bal = await self.wallet.get_token_balance(VIRTUAL_ADDRESS)
                sell_amount = min(virtual_to_sell, virt_bal)
                if sell_amount > 0:
                    print(f"[ORDERS] Failure recovery: selling {sell_amount / 1e18:.4f} VIRTUAL → USDC")
                    sell = await self.executor.execute_sell_virtual_to_usdc(sell_amount)
                    if sell["success"]:
                        usdc_recovered_raw = sell["actual_received"]
                        usdc_recovered = usdc_recovered_raw / 1e6
                        print(f"[ORDERS] Recovered {usdc_recovered:.2f} USDC")
            except Exception as e:
                print(f"[ORDERS] Failure recovery swap failed: {e}")
                try:
                    await self.wallet.sync_nonce()
                except Exception:
                    pass

        usdc_transfer_ok = False
        if buyer_address and usdc_recovered_raw > 0:
            try:
                transfer = await self.executor.transfer_erc20(
                    USDC_ADDRESS, buyer_address, usdc_recovered_raw,
                )
                usdc_transfer_ok = transfer.get("success", False)
            except Exception as e:
                print(f"[ORDERS] Failure recovery USDC transfer failed: {e}")

        payload = deliverable.unfulfilled_position(
            token_address=token_address,
            token_symbol=token_symbol,
            amount_usdc=amount_usdc,
            reason=(
                f"{reason} (recovered {usdc_recovered:.2f} USDC, "
                f"transfer {'OK' if usdc_transfer_ok else 'FAILED'})"
                if usdc_recovered else reason
            ),
            error_type="ERROR",
        )
        try:
            await asyncio.to_thread(detail.deliver, payload)
            try:
                order = await self.db.get_order_by_job_id(job_id)
                if order:
                    await self.db.update_order(order["id"], {
                        "delivered_at": datetime.now(timezone.utc).isoformat(),
                        "evaluation_status": "pending",
                    })
            except Exception as e:
                print(f"[ORDERS] Deliver failure DB update failed job {job_id}: {e}")
        except Exception as e:
            print(f"[ORDERS] Deliver failure failed job {job_id}: {e}")
        finally:
            self._acp_jobs.pop(job_id, None)
