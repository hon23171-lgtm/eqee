from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Dict, List, Optional

import httpx

from .config import Config
from .models import Listing
from .rate_limiter import RateLimiter

log = logging.getLogger("mrkt.client")


class MrktError(Exception):
    pass


class MrktClient:
    """Async client for the MRKT marketplace.

    NOTE: MRKT exposes no documented public API. The request paths and the
    response field names below are best-effort placeholders and MUST be
    verified against real traffic. Override paths via the MRKT_*_PATH env
    vars and adjust `_parse_listing` to match the real payload.
    """

    def __init__(self, config: Config, limiter: RateLimiter) -> None:
        self.cfg = config
        self.limiter = limiter
        headers = {
            "Accept": "application/json",
            "User-Agent": "mrkt-sniper/0.1",
        }
        if config.mrkt_token:
            headers["Authorization"] = f"Bearer {config.mrkt_token}"
        self._client = httpx.AsyncClient(
            base_url=config.mrkt_base_url,
            headers=headers,
            http2=True,
            timeout=config.request_timeout_sec,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=20),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        attempt = 0
        while True:
            attempt += 1
            await self.limiter.acquire()
            try:
                resp = await self._client.request(method, path, **kwargs)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt > self.cfg.max_retries:
                    raise MrktError(f"transport error after {attempt} attempts: {exc}") from exc
                await self._sleep_backoff(attempt)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt > self.cfg.max_retries:
                    raise MrktError(f"{resp.status_code} after {attempt} attempts for {path}")
                await self._sleep_backoff(attempt, resp.headers.get("Retry-After"))
                continue

            if resp.status_code >= 400:
                raise MrktError(f"{resp.status_code} {resp.text[:200]} for {path}")

            if not resp.content:
                return None
            try:
                return resp.json()
            except ValueError as exc:
                raise MrktError(f"non-JSON response for {path}: {exc}") from exc

    async def _sleep_backoff(self, attempt: int, retry_after: Optional[str] = None) -> None:
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = self.cfg.backoff_base_sec * (2 ** (attempt - 1))
        else:
            delay = self.cfg.backoff_base_sec * (2 ** (attempt - 1))
        delay = min(delay, self.cfg.backoff_max_sec)
        delay += random.uniform(0, delay * 0.25)  # jitter to de-sync retries
        log.warning("backoff %.2fs (attempt %d)", delay, attempt)
        await asyncio.sleep(delay)

    # ---- domain calls -------------------------------------------------

    async def fetch_listings(self, collection: str) -> List[Listing]:
        path = self.cfg.listings_path.format(collection=collection)
        # Sorting by price ascending so the cheapest (snipe candidates) come first.
        params = {"sort": "price_asc", "limit": 50, "status": "active"}
        data = await self._request("GET", path, params=params)
        items = _extract_items(data)
        out: List[Listing] = []
        for item in items:
            parsed = self._parse_listing(collection, item)
            if parsed is not None:
                out.append(parsed)
        return out

    async def fetch_recent_sales_count(self, collection: str) -> int:
        path = self.cfg.sales_path.format(collection=collection)
        try:
            data = await self._request("GET", path, params={"limit": 50})
        except MrktError:
            return 0
        return len(_extract_items(data))

    async def buy(self, listing: Listing, max_price_ton: float) -> Dict[str, Any]:
        """Execute a purchase. `max_price_ton` is a hard slippage cap that the
        marketplace must reject if the real price exceeds it."""
        path = self.cfg.buy_path.format(listing_id=listing.id)
        payload = {
            "listing_id": listing.id,
            "max_price": max_price_ton,
            "expected_price": listing.price_ton,
        }
        if self.cfg.dry_run:
            log.info("[DRY_RUN] would POST %s payload=%s", path, payload)
            return {"dry_run": True, "listing_id": listing.id}
        result = await self._request("POST", path, json=payload)
        return result or {}

    def _parse_listing(self, collection: str, item: Dict[str, Any]) -> Optional[Listing]:
        # TODO(verify): map these keys to the real MRKT payload.
        listing_id = _first(item, "id", "listing_id", "address", "nft_address")
        price_raw = _first(item, "price", "price_ton", "amount", "ton_price")
        if listing_id is None or price_raw is None:
            return None
        try:
            price_ton = _to_ton(price_raw)
        except (TypeError, ValueError):
            return None
        if price_ton <= 0:
            return None
        return Listing(
            id=str(listing_id),
            collection=collection,
            price_ton=price_ton,
            name=str(_first(item, "name", "title", "gift_name") or ""),
            seller=str(_first(item, "seller", "owner", "seller_address") or ""),
            url=str(_first(item, "url", "link") or ""),
            attributes=item.get("attributes") or {},
            raw=item,
        )


def _extract_items(data: Any) -> List[Dict[str, Any]]:
    if data is None:
        return []
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("items", "results", "listings", "data", "sales"):
            value = data.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


def _first(item: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in item and item[key] is not None:
            return item[key]
    return None


def _to_ton(value: Any) -> float:
    """MRKT amounts may arrive as TON floats or as nanoton integer strings."""
    if isinstance(value, str):
        value = value.strip()
        if value.isdigit() and len(value) >= 9:  # nanoton integer
            return int(value) / 1e9
        return float(value)
    num = float(value)
    if num > 1e6:  # heuristic: looks like nanoton
        return num / 1e9
    return num
