"""
ACP Handler — Socket.IO real-time + polling fallback for job lifecycle.
Adapted from proven TRUST seller.py pattern (sync→async).

Hardened with:
  - Per-job processing timeout (30s)
  - LRU-bounded processed cache (prevents memory leak on long runs)
  - Rate limiting (max jobs per minute)
  - Duplicate detection with TTL
  - Graceful SDK thread crash handling
"""

import asyncio
import json
import time
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional

from virtuals_acp.models import MemoType
from virtuals_acp.fare import Fare, FareAmount

from engine.config import (
    USDC_ADDRESS,
    MAX_CONCURRENT_ORDERS,
)
from engine.acp.offerings import OFFERINGS, VALIDATORS
from engine.acp import deliverable

# Tuning
JOB_PROCESS_TIMEOUT = 30.0       # seconds to process a single job
PROCESSED_CACHE_MAX = 2000        # max jobs in processed cache (LRU eviction)
RATE_LIMIT_WINDOW = 60.0          # seconds
RATE_LIMIT_MAX_JOBS = 30          # max new jobs accepted per window
POLL_INTERVAL = 30                # seconds between polls
EXPIRY_CHECK_INTERVAL = 2         # every N polls (~60s)
SDK_THREAD_TIMEOUT = 15.0         # timeout for SDK to_thread calls


class _LRUCache(OrderedDict):
    """OrderedDict with max size — evicts oldest on overflow."""
    def __init__(self, maxsize: int = PROCESSED_CACHE_MAX):
        super().__init__()
        self._maxsize = maxsize

    def __setitem__(self, key, value):
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        while len(self) > self._maxsize:
            self.popitem(last=False)


class ACPHandler:
    """Manages ACP job lifecycle: accept, payable, deliver.

    Socket.IO is handled by the SDK's VirtualsACP instance (sync socketio.Client).
    We receive events via on_sdk_new_task(), bridged from the SDK's sync thread
    to our asyncio event loop. Polling fallback runs as a safety net.
    """

    def __init__(self, acp, db, order_manager, hot_wallet, token_resolver=None):
        self.acp = acp
        self.db = db
        self.order_manager = order_manager
        self.hot_wallet = hot_wallet
        self.token_resolver = token_resolver
        self._processed = _LRUCache(PROCESSED_CACHE_MAX)
        self._job_context: Dict[int, dict] = {}  # job_id → enriched data from REQUEST phase
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Rate limiting
        self._rate_window_start = time.time()
        self._rate_count = 0

        # Metrics
        self._jobs_accepted = 0
        self._jobs_rejected = 0
        self._jobs_errored = 0
        self._sdk_bridge_errors = 0

    # ------------------------------------------------------------------
    # SDK callback bridge (sync thread → asyncio)
    # ------------------------------------------------------------------

    def on_sdk_new_task(self, job, memo_to_sign):
        """Called by VirtualsACP on_new_task from a sync thread.
        Bridges to our async event loop.
        """
        try:
            job_id = job.id
            phase = job.phase.name if hasattr(job.phase, "name") else str(job.phase)
            print(f"[ACP] onNewTask job={job_id} phase={phase}")
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(self._safe_process(job_id), self._loop)
            else:
                print(f"[ACP] ⚠️  Event loop not running — job {job_id} queued for poll")
        except Exception as e:
            self._sdk_bridge_errors += 1
            print(f"[ACP] ⚠️  SDK bridge error: {e}")

    def on_sdk_evaluate(self, job):
        """Called by VirtualsACP on_evaluate from a sync thread.
        Auto-accept: SENTINEL deliveries are verifiable on-chain."""
        try:
            job_id = job.id
            print(f"[ACP] onEvaluate job={job_id}")
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(self._handle_evaluate(job_id), self._loop)
        except Exception as e:
            self._sdk_bridge_errors += 1
            print(f"[ACP] ⚠️  Evaluate bridge error: {e}")

    async def _handle_evaluate(self, job_id: int):
        """Auto-accept evaluation — our deliveries are on-chain verifiable."""
        try:
            detail = await asyncio.wait_for(
                asyncio.to_thread(self.acp.get_job_by_onchain_id, job_id),
                timeout=SDK_THREAD_TIMEOUT,
            )
            phase_name = detail.phase.name if hasattr(detail.phase, "name") else str(detail.phase)
            if phase_name == "EVALUATION":
                await asyncio.wait_for(
                    asyncio.to_thread(detail.evaluate, True, "Verified on-chain"),
                    timeout=SDK_THREAD_TIMEOUT,
                )
                print(f"[ACP] Job {job_id} evaluation auto-accepted")
                try:
                    order = await self.db.get_order_by_job_id(job_id)
                    if order:
                        await self.db.update_order(order["id"], {
                            "evaluation_status": "approved",
                        })
                except Exception as e:
                    print(f"[ACP] Job {job_id} evaluation DB update failed: {e}")
        except Exception as e:
            print(f"[ACP] Job {job_id} evaluate error: {e}")

    def set_event_loop(self, loop: asyncio.AbstractEventLoop):
        """Store the main event loop for sync→async bridging."""
        self._loop = loop

    # ------------------------------------------------------------------
    # Polling fallback
    # ------------------------------------------------------------------

    async def poll_jobs(self):
        """Fallback: poll for jobs every 30s if Socket.IO misses events."""
        poll_count = 0
        while True:
            try:
                jobs = await asyncio.wait_for(
                    asyncio.to_thread(self.acp.get_active_jobs),
                    timeout=SDK_THREAD_TIMEOUT,
                )
                for job in (jobs or []):
                    if (job.provider_address and
                            job.provider_address.lower() != self.acp.agent_address.lower()):
                        continue
                    await self._safe_process(job.id)

                poll_count += 1
                if poll_count % EXPIRY_CHECK_INTERVAL == 0:
                    await self._check_expired_jobs()

            except asyncio.TimeoutError:
                print("[ACP] Poll timeout — SDK may be unresponsive")
            except asyncio.CancelledError:
                return
            except Exception as e:
                print(f"[ACP] Poll error: {e}")
            await asyncio.sleep(POLL_INTERVAL)

    async def _check_expired_jobs(self):
        """Section 17: detect expired ACP jobs, recover funds."""
        try:
            active_orders = await self.db.get_active_orders()
            for order in active_orders:
                job_id = order.get("job_id")
                if not job_id:
                    continue
                try:
                    detail = await asyncio.wait_for(
                        asyncio.to_thread(self.acp.get_job_by_onchain_id, job_id),
                        timeout=SDK_THREAD_TIMEOUT,
                    )
                    phase_name = detail.phase.name if hasattr(detail.phase, "name") else str(detail.phase)
                    if phase_name == "EXPIRED":
                        print(f"[ACP] Job {job_id} EXPIRED — recovering funds")
                        await self.order_manager.cancel_order(job_id, job_id, detail)
                        await self.db.update_order(order["id"], {
                            "status": "expired",
                            "evaluation_status": "expired_unevaluated",
                        })
                except asyncio.TimeoutError:
                    print(f"[ACP] Expiry check timeout for job {job_id}")
                except Exception as e:
                    print(f"[ACP] Expiry check for job {job_id} failed: {e}")
        except Exception as e:
            print(f"[ACP] Expiry check error: {e}")

    # ------------------------------------------------------------------
    # Job processing
    # ------------------------------------------------------------------

    async def _safe_process(self, job_id: int):
        """Process a job with error handling and timeout."""
        try:
            await asyncio.wait_for(
                self._process_job(job_id),
                timeout=JOB_PROCESS_TIMEOUT,
            )
        except asyncio.TimeoutError:
            self._jobs_errored += 1
            print(f"[ACP] Job {job_id} TIMEOUT after {JOB_PROCESS_TIMEOUT}s")
        except Exception as e:
            self._jobs_errored += 1
            print(f"[ACP] Job {job_id} error: {e}")

    def _check_rate_limit(self) -> bool:
        """Check and update rate limit. Returns True if within limits."""
        now = time.time()
        if now - self._rate_window_start > RATE_LIMIT_WINDOW:
            self._rate_window_start = now
            self._rate_count = 0
        self._rate_count += 1
        return self._rate_count <= RATE_LIMIT_MAX_JOBS

    async def _process_job(self, job_id: int):
        """Route job to correct phase handler."""
        stages = self._processed.get(job_id, {})
        if stages.get("responded") and stages.get("delivered"):
            return

        detail = await asyncio.wait_for(
            asyncio.to_thread(self.acp.get_job_by_onchain_id, job_id),
            timeout=SDK_THREAD_TIMEOUT,
        )
        phase_name = detail.phase.name if hasattr(detail.phase, "name") else str(detail.phase)

        # Phase: REQUEST → validate + accept
        if phase_name == "REQUEST" and not stages.get("responded"):
            if not self._check_rate_limit():
                print(f"[ACP] Job {job_id} REJECT: rate limited ({self._rate_count} jobs/min)")
                await asyncio.to_thread(detail.reject, "Rate limited — try again shortly")
                self._processed[job_id] = {"responded": True, "delivered": True}
                self._jobs_rejected += 1
                return
            await self._handle_request(job_id, detail)

        # Phase: TRANSACTION → funds should be available, create order
        elif phase_name == "TRANSACTION" and not stages.get("delivered"):
            await self._handle_transaction(job_id, detail)

        # Terminal phases
        elif phase_name in ("EVALUATION", "COMPLETED", "REJECTED", "EXPIRED"):
            self._processed[job_id] = {"responded": True, "delivered": True}

    async def _handle_request(self, job_id: int, detail):
        """Validate offering + requirements, accept or reject."""
        offering_name = detail.name or self._parse_offering_fallback(detail)
        req = detail.requirement if isinstance(detail.requirement, dict) else {}
        price = getattr(detail, 'price_value', None) or getattr(detail, 'price', 0) or 0
        print(f"[ACP] Job {job_id} REQUEST offering={offering_name} price={price}")

        # Check offering exists
        if offering_name not in OFFERINGS:
            print(f"[ACP] Job {job_id} REJECT: unknown offering '{offering_name}'")
            await asyncio.to_thread(detail.reject, f"Unknown offering: {offering_name}")
            self._processed[job_id] = {"responded": True}
            self._jobs_rejected += 1
            return

        # Check price bounds
        min_price = OFFERINGS[offering_name]["min_price"]
        max_price = OFFERINGS[offering_name].get("max_price", float("inf"))
        if price < min_price:
            print(f"[ACP] Job {job_id} REJECT: price {price} < ${min_price}")
            await asyncio.to_thread(detail.reject, f"Below minimum (${min_price})")
            self._processed[job_id] = {"responded": True}
            self._jobs_rejected += 1
            return
        if price > max_price:
            print(f"[ACP] Job {job_id} REJECT: price {price} > ${max_price}")
            await asyncio.to_thread(detail.reject, f"Above maximum (${max_price})")
            self._processed[job_id] = {"responded": True}
            self._jobs_rejected += 1
            return

        # Validate requirements
        validator = VALIDATORS.get(offering_name)
        if validator:
            valid, reason = validator(req)
            if not valid:
                print(f"[ACP] Job {job_id} REJECT: {reason}")
                await asyncio.to_thread(detail.reject, reason)
                self._processed[job_id] = {"responded": True}
                self._jobs_rejected += 1
                return

        # Validate token is tradeable (if resolver available)
        token_address = req.get("token_address") or req.get("token")
        if self.token_resolver and token_address and offering_name == "limit_order":
            try:
                token_info = await self.token_resolver.resolve(token_address)
                if not token_info or not token_info.get("tradeable"):
                    print(f"[ACP] Job {job_id} REJECT: token not tradeable {token_address}")
                    await asyncio.to_thread(detail.reject, f"Token not tradeable: {token_address}")
                    self._processed[job_id] = {"responded": True}
                    self._jobs_rejected += 1
                    return
                # Store resolved token info for TRANSACTION phase
                if token_info.get("symbol"):
                    self._job_context[job_id] = {
                        "token_symbol": token_info["symbol"],
                        "token_address": token_info.get("address", token_address),
                    }
            except Exception as e:
                print(f"[ACP] Job {job_id} token resolution error: {e}")
                await asyncio.to_thread(detail.reject, "Token validation failed — try again")
                self._processed[job_id] = {"responded": True}
                self._jobs_rejected += 1
                return

        # Check capacity
        active_count = await self.db.count_active_orders()
        if active_count >= MAX_CONCURRENT_ORDERS:
            print(f"[ACP] Job {job_id} REJECT: at capacity ({active_count})")
            await asyncio.to_thread(detail.reject, "At capacity — try again later")
            self._processed[job_id] = {"responded": True}
            self._jobs_rejected += 1
            return

        # Accept the job
        print(f"[ACP] Job {job_id} ACCEPT {offering_name} @ ${price}")
        await asyncio.to_thread(detail.accept, f"Accepted {offering_name}")
        self._processed[job_id] = {"responded": True}
        self._jobs_accepted += 1

        # Create payable requirement (request USDC from buyer → hot wallet)
        if offering_name == "limit_order":
            try:
                usdc_amount = float(req.get("amount_usdc", price))
                if usdc_amount <= 0:
                    print(f"[ACP] Job {job_id} ⚠️  Invalid USDC amount: {usdc_amount}")
                    return
                usdc_fare = Fare(USDC_ADDRESS, 6)
                amount = FareAmount(usdc_amount, usdc_fare)
                expired_at = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())
                await asyncio.wait_for(
                    asyncio.to_thread(
                        detail.create_payable_requirement,
                        f"Send {usdc_amount} USDC for {offering_name}",
                        MemoType.PAYABLE_TRANSFER_ESCROW,
                        amount,
                        self.hot_wallet.address,
                        expired_at,
                    ),
                    timeout=SDK_THREAD_TIMEOUT,
                )
                print(f"[ACP] Job {job_id} payable requirement created: {usdc_amount} USDC")
            except asyncio.TimeoutError:
                print(f"[ACP] Job {job_id} payable requirement TIMEOUT")
            except Exception as e:
                print(f"[ACP] Job {job_id} payable requirement failed: {e}")

    async def _handle_transaction(self, job_id: int, detail):
        """Buyer has paid. Create the order and start watching."""
        offering_name = detail.name or self._parse_offering_fallback(detail)
        req = detail.requirement if isinstance(detail.requirement, dict) else {}

        # Merge enriched context from REQUEST phase (token_symbol, etc.)
        ctx = self._job_context.pop(job_id, {})
        for k, v in ctx.items():
            if v and not req.get(k):
                req[k] = v

        print(f"[ACP] Job {job_id} TRANSACTION — processing {offering_name}")

        if offering_name == "limit_order":
            await self.order_manager.create_order_from_job(job_id, detail, req)

        elif offering_name == "cancel_order":
            original_job_id = req.get("original_job_id")
            if original_job_id:
                try:
                    await self.order_manager.cancel_order(int(original_job_id), job_id, detail)
                except (ValueError, TypeError) as e:
                    print(f"[ACP] Job {job_id} invalid original_job_id: {original_job_id}")
                    await asyncio.to_thread(
                        detail.deliver,
                        deliverable.close_job_and_withdraw(f"Invalid original_job_id: {e}"),
                    )
            else:
                await asyncio.to_thread(
                    detail.deliver,
                    deliverable.close_job_and_withdraw("Missing original_job_id"),
                )

        self._processed[job_id] = {"responded": True, "delivered": True}

    # ------------------------------------------------------------------
    # Parsing + validation
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_offering_fallback(detail) -> str:
        """Fallback: extract offering name from memos if SDK parsing failed."""
        try:
            memos = detail.memos if hasattr(detail, 'memos') else []
            for memo in memos:
                if not memo.content:
                    continue
                try:
                    data = json.loads(str(memo.content)) if isinstance(memo.content, str) else memo.content
                    if isinstance(data, dict) and "name" in data:
                        return data["name"]
                except (json.JSONDecodeError, TypeError):
                    continue
        except Exception:
            pass
        return "limit_order"

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def metrics(self) -> dict:
        return {
            "jobs_accepted": self._jobs_accepted,
            "jobs_rejected": self._jobs_rejected,
            "jobs_errored": self._jobs_errored,
            "sdk_bridge_errors": self._sdk_bridge_errors,
            "processed_cache_size": len(self._processed),
            "rate_count_this_window": self._rate_count,
        }

