from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from . import analyzer
from .config import Config
from .models import FloorEstimate, Opportunity
from .mrkt_client import MrktClient, MrktError
from .state import State

log = logging.getLogger("mrkt.buyer")


@dataclass
class BuyResult:
    ok: bool
    listing_id: str
    detail: str
    price_ton: float = 0.0


class Buyer:
    def __init__(self, cfg: Config, client: MrktClient, state: State) -> None:
        self.cfg = cfg
        self.client = client
        self.state = state

    async def execute(self, opp: Opportunity, fresh_floor: FloorEstimate) -> BuyResult:
        listing = opp.listing

        # Reserve the listing so two concurrent loops can't both buy it.
        if not self.state.claim_in_flight(listing.id):
            return BuyResult(False, listing.id, "already in flight / purchased")

        try:
            # Re-validate against the freshest floor right before spending money.
            revalidated = analyzer.evaluate(listing, fresh_floor, self.cfg)
            if revalidated is None:
                return BuyResult(False, listing.id, "failed revalidation against fresh floor")

            # Pass a hard price cap so the marketplace rejects any slippage above
            # what we judged safe. This is the last line of defence.
            max_price = min(revalidated.max_safe_price_ton, listing.price_ton)
            try:
                result = await self.client.buy(listing, max_price_ton=max_price)
            except MrktError as exc:
                return BuyResult(False, listing.id, f"buy failed: {exc}")

            self.state.purchased.add(listing.id)
            await self.state.save()
            log.info("bought %s at <= %.4f TON", listing.id, max_price)
            return BuyResult(True, listing.id, str(result)[:200], price_ton=listing.price_ton)
        finally:
            self.state.release_in_flight(listing.id)
