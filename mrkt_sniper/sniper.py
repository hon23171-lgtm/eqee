from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Dict, Optional

from . import analyzer
from .buyer import Buyer
from .config import Config
from .models import FloorEstimate, Opportunity
from .mrkt_client import MrktClient, MrktError
from .state import State

log = logging.getLogger("mrkt.sniper")

Notifier = Callable[[str], Awaitable[None]]


class Sniper:
    def __init__(self, cfg: Config, client: MrktClient, state: State, buyer: Buyer) -> None:
        self.cfg = cfg
        self.client = client
        self.state = state
        self.buyer = buyer
        self.floors: Dict[str, FloorEstimate] = {}
        self.notify: Notifier = self._noop
        self._stop = asyncio.Event()

    async def _noop(self, _text: str) -> None:
        return None

    def set_notifier(self, notifier: Notifier) -> None:
        self.notify = notifier

    def effective_mode(self) -> str:
        return self.state.mode_override or self.cfg.mode

    def get_override(self, key: str, default: float) -> float:
        return self.state.overrides.get(key, default)

    async def run(self) -> None:
        # Compute an initial floor for every collection before we start sniping,
        # so we never act on an empty / stale floor.
        await self._refresh_all_floors()
        tasks = [asyncio.create_task(self._floor_loop())]
        tasks += [asyncio.create_task(self._watch_loop(c)) for c in self.cfg.collections]
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()

    def stop(self) -> None:
        self._stop.set()

    # ---- floor (market analysis) --------------------------------------

    async def _floor_loop(self) -> None:
        while not self._stop.is_set():
            await _sleep_or_stop(self._stop, self.cfg.floor_refresh_sec)
            if self._stop.is_set():
                break
            await self._refresh_all_floors()

    async def _refresh_all_floors(self) -> None:
        results = await asyncio.gather(
            *(self._refresh_floor(c) for c in self.cfg.collections),
            return_exceptions=True,
        )
        for collection, res in zip(self.cfg.collections, results):
            if isinstance(res, Exception):
                log.warning("floor refresh failed for %s: %s", collection, res)

    async def _refresh_floor(self, collection: str) -> None:
        listings = await self.client.fetch_listings(collection)
        sales = await self.client.fetch_recent_sales_count(collection)
        self.floors[collection] = analyzer.estimate_floor(collection, listings, sales, self.cfg)

    # ---- fast watch (finding speed) -----------------------------------

    async def _watch_loop(self, collection: str) -> None:
        while not self._stop.is_set():
            try:
                if not self.state.paused:
                    await self._scan(collection)
            except MrktError as exc:
                log.warning("scan error %s: %s", collection, exc)
            except Exception as exc:  # noqa: BLE001 - one bad cycle must not kill the loop
                log.exception("unexpected scan error %s: %s", collection, exc)
            await _sleep_or_stop(self._stop, self.cfg.watch_interval_sec)

    async def _scan(self, collection: str) -> None:
        floor = self.floors.get(collection)
        if floor is None or not floor.liquid:
            return
        listings = await self.client.fetch_listings(collection)
        for listing in listings:
            if self.state.already_handled(listing.id):
                continue
            opp = analyzer.evaluate(listing, floor, self.cfg)
            if opp is None:
                continue
            await self._handle_opportunity(opp)

    async def _handle_opportunity(self, opp: Opportunity) -> None:
        listing = opp.listing
        self.state.alerted.add(listing.id)
        await self.notify(_format_alert(opp, self.effective_mode()))

        if self.effective_mode() == "auto_buy":
            fresh_floor = self.floors.get(listing.collection, opp.floor)
            result = await self.buyer.execute(opp, fresh_floor)
            if result.ok:
                await self.notify(
                    f"✅ Куплено {listing.collection} `{listing.id}` за ~{listing.price_ton:.3f} TON"
                )
            else:
                await self.notify(
                    f"⚠️ Покупка не прошла {listing.collection} `{listing.id}`: {result.detail}"
                )
        await self.state.save()


def _format_alert(opp: Opportunity, mode: str) -> str:
    l = opp.listing
    tag = "🤖 авто-покупка" if mode == "auto_buy" else "🔔 только уведомление"
    return (
        f"🎯 Снайп [{l.collection}] {tag}\n"
        f"Цена: {l.price_ton:.3f} TON\n"
        f"Флор: {opp.floor.floor_ton:.3f} TON (медиана {opp.floor.median_ton:.3f}, "
        f"листингов {opp.floor.active_listings})\n"
        f"Профит: ~{opp.profit_ton:.3f} TON ({opp.profit_pct * 100:.1f}%)\n"
        f"ID: {l.id}" + (f"\n{l.url}" if l.url else "")
    )


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
