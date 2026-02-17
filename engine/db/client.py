"""
Supabase DB client wrapper — all CRUD for orders, fills, balance_snapshots.
Includes retry on transient errors, operation timeouts, and metrics.
"""

import asyncio
import time
from datetime import datetime, timezone
from typing import Optional, List

from supabase import create_client, Client


DB_OPERATION_TIMEOUT = 10.0    # seconds per DB operation
DB_RETRY_ATTEMPTS = 3          # retries on transient errors
DB_RETRY_BASE_DELAY = 0.5     # seconds — exponential backoff base

# Transient error substrings that trigger retry
_TRANSIENT_ERRORS = (
    "timeout", "connection", "unavailable", "502", "503", "504",
    "broken pipe", "reset by peer", "socket", "network",
    "too many requests", "rate limit",
)


def init_supabase(url: str, key: str) -> Client:
    """Initialize and return a Supabase client."""
    return create_client(url, key)


async def health_check(client: Client) -> bool:
    """Health check — actually queries Supabase to verify connectivity."""
    try:
        if client is None or not hasattr(client, "table"):
            return False
        result = await asyncio.to_thread(
            lambda: client.table("orders").select("id", count="exact").limit(0).execute()
        )
        return result is not None
    except Exception:
        return False


def _is_transient(e: Exception) -> bool:
    """Check if an exception is transient and worth retrying."""
    msg = str(e).lower()
    return any(kw in msg for kw in _TRANSIENT_ERRORS)


TERMINAL_STATUSES = ("completed", "cancelled", "expired", "failed")


class Database:
    """Async wrapper around Supabase client for SENTINEL DB operations.

    All operations retry on transient errors with exponential backoff
    and have a per-operation timeout.
    """

    def __init__(self, client: Client):
        self.client = client
        # Metrics
        self._op_count = 0
        self._op_errors = 0
        self._op_retries = 0
        self._total_latency_ms = 0.0

    async def _exec(self, fn, label: str = "db_op"):
        """Execute a Supabase operation with retry, timeout, and metrics.

        Args:
            fn: callable returning a Supabase execute() result
            label: operation name for logging
        """
        last_err = None
        for attempt in range(DB_RETRY_ATTEMPTS):
            t0 = time.monotonic()
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(fn),
                    timeout=DB_OPERATION_TIMEOUT,
                )
                latency = (time.monotonic() - t0) * 1000
                self._op_count += 1
                self._total_latency_ms += latency
                return result
            except asyncio.TimeoutError:
                self._op_errors += 1
                last_err = RuntimeError(f"DB operation '{label}' timed out after {DB_OPERATION_TIMEOUT}s")
                self._op_retries += 1
            except Exception as e:
                self._op_errors += 1
                last_err = e
                if _is_transient(e) and attempt < DB_RETRY_ATTEMPTS - 1:
                    delay = DB_RETRY_BASE_DELAY * (2 ** attempt)
                    print(f"[DB] {label} transient error (attempt {attempt+1}): {e}. "
                          f"Retry in {delay:.1f}s")
                    self._op_retries += 1
                    await asyncio.sleep(delay)
                    continue
                raise  # non-transient or last attempt

        raise last_err

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    async def insert_order(self, data: dict) -> str:
        """Insert a new order, return its ID."""
        result = await self._exec(
            lambda: self.client.table("orders").insert(data).execute(),
            "insert_order",
        )
        return result.data[0]["id"]

    async def update_order(self, order_id: str, updates: dict):
        """Update arbitrary fields on an order."""
        await self._exec(
            lambda: self.client.table("orders").update(updates).eq("id", order_id).execute(),
            f"update_order({order_id[:8]})",
        )

    async def update_order_status(self, order_id: str, status: str):
        """Update just the status field."""
        await self.update_order(order_id, {"status": status})

    async def get_order(self, order_id: str) -> Optional[dict]:
        """Get a single order by ID."""
        result = await self._exec(
            lambda: self.client.table("orders").select("*").eq("id", order_id).limit(1).execute(),
            f"get_order({order_id[:8]})",
        )
        return result.data[0] if result.data else None

    async def get_order_by_job_id(self, job_id: int) -> Optional[dict]:
        """Get order by ACP job ID."""
        result = await self._exec(
            lambda: self.client.table("orders").select("*").eq("job_id", job_id).limit(1).execute(),
            f"get_order_by_job({job_id})",
        )
        return result.data[0] if result.data else None

    async def get_orders_by_status(self, status: str) -> List[dict]:
        """Get all orders with a given status."""
        result = await self._exec(
            lambda: self.client.table("orders").select("*").eq("status", status).execute(),
            f"get_orders_by_status({status})",
        )
        return result.data or []

    async def get_active_orders(self) -> List[dict]:
        """Get all non-terminal orders."""
        result = await self._exec(
            lambda: self.client.table("orders").select("*").not_.in_("status", list(TERMINAL_STATUSES)).execute(),
            "get_active_orders",
        )
        return result.data or []

    async def count_active_orders(self) -> int:
        """Count non-terminal orders efficiently (head-only query)."""
        result = await self._exec(
            lambda: self.client.table("orders")
                .select("id", count="exact")
                .not_.in_("status", list(TERMINAL_STATUSES))
                .limit(0)
                .execute(),
            "count_active_orders",
        )
        return result.count if hasattr(result, 'count') and result.count is not None else 0

    async def get_orders_for_buyer(self, buyer_address: str) -> List[dict]:
        """Get all orders for a buyer address."""
        result = await self._exec(
            lambda: self.client.table("orders").select("*").eq("buyer_address", buyer_address.lower()).execute(),
            f"get_orders_for_buyer({buyer_address[:10]})",
        )
        return result.data or []

    # ------------------------------------------------------------------
    # Fills
    # ------------------------------------------------------------------

    async def record_fill(self, order_id: str, fill_data: dict):
        """Record a fill (entry or exit) for an order."""
        fill_data["order_id"] = order_id
        await self._exec(
            lambda: self.client.table("fills").insert(fill_data).execute(),
            f"record_fill({order_id[:8]})",
        )

    async def get_fills_for_order(self, order_id: str) -> List[dict]:
        """Get all fills for an order."""
        result = await self._exec(
            lambda: self.client.table("fills").select("*").eq("order_id", order_id).execute(),
            f"get_fills({order_id[:8]})",
        )
        return result.data or []

    # ------------------------------------------------------------------
    # Balance snapshots (Section 32.3 Rule 2)
    # ------------------------------------------------------------------

    async def record_snapshot(self, snapshot: dict):
        """Record a balance snapshot before/after a swap."""
        await self._exec(
            lambda: self.client.table("balance_snapshots").insert(snapshot).execute(),
            "record_snapshot",
        )

    # ------------------------------------------------------------------
    # Price events (Section 33.5 — trigger records)
    # ------------------------------------------------------------------

    async def insert_price_event(self, job_id: int, order_id, route_key: str,
                                 price_usd: float, trigger_type: str):
        """Record a price trigger event."""
        await self._exec(
            lambda: self.client.table("price_events").insert({
                "job_id": job_id,
                "order_id": order_id,
                "route_key": route_key,
                "price_usd": price_usd,
                "trigger_type": trigger_type,
            }).execute(),
            f"insert_price_event(job={job_id})",
        )

    # ------------------------------------------------------------------
    # Price snapshots (Section 33.7 — downsampled every 30s)
    # ------------------------------------------------------------------

    async def insert_price_snapshot(self, route_key: str, price_usd: float,
                                    block_number: int):
        """Record a downsampled price snapshot for a route."""
        await self._exec(
            lambda: self.client.table("price_snapshots").insert({
                "route_key": route_key,
                "price_usd": price_usd,
                "block_number": block_number,
            }).execute(),
            "insert_price_snapshot",
        )

    # ------------------------------------------------------------------
    # Tracked routes (Section 33.8)
    # ------------------------------------------------------------------

    async def upsert_tracked_route(self, route_key: str, in_token: str,
                                   out_token: str, pools_json: list,
                                   subscriber_count: int, last_price_usd: float):
        """Insert or update a tracked route."""
        await self._exec(
            lambda: self.client.table("tracked_routes").upsert({
                "route_key": route_key,
                "in_token": in_token,
                "out_token": out_token,
                "pools_json": pools_json,
                "subscriber_count": subscriber_count,
                "last_price_usd": last_price_usd,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).execute(),
            f"upsert_tracked_route({route_key[:20]})",
        )

    # ------------------------------------------------------------------
    # Token registry (Section 25)
    # ------------------------------------------------------------------

    async def get_token_registry(self, address: str) -> Optional[dict]:
        """Get a token registry entry by address."""
        addr = address.lower()
        result = await self._exec(
            lambda: self.client.table("token_registry").select("*").eq("address", addr).limit(1).execute(),
            f"get_token_registry({addr[:8]})",
        )
        return result.data[0] if result.data else None

    async def upsert_token_registry(self, token_info: dict):
        """Insert or update a token registry entry."""
        addr = str(token_info.get("address", "")).lower()
        if not addr:
            return
        payload = {
            "address": addr,
            "symbol": token_info.get("symbol", ""),
            "name": token_info.get("name"),
            "decimals": token_info.get("decimals"),
            "pair_address": token_info.get("pair_address"),
            "tradeable": token_info.get("tradeable", True),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        await self._exec(
            lambda: self.client.table("token_registry").upsert(payload).execute(),
            f"upsert_token_registry({addr[:8]})",
        )

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def metrics(self) -> dict:
        avg_lat = (self._total_latency_ms / max(self._op_count, 1))
        return {
            "operations": self._op_count,
            "errors": self._op_errors,
            "retries": self._op_retries,
            "avg_latency_ms": round(avg_lat, 1),
        }
