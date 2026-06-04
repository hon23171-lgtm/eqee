from __future__ import annotations

import logging
import statistics
from typing import List, Optional

from .config import Config
from .models import FloorEstimate, Listing, Opportunity

log = logging.getLogger("mrkt.analyzer")


def estimate_floor(
    collection: str,
    listings: List[Listing],
    recent_sales: int,
    cfg: Config,
) -> FloorEstimate:
    """Robust floor estimate that ignores troll / mispriced listings.

    A naive `min(price)` is dangerous: a single fat-finger or scam listing
    can crater the apparent floor and trick the bot into a bad buy. We take
    the cheapest sample, drop anything far below the sample median, then use
    the lowest *surviving* price as the floor.
    """
    prices = sorted(l.price_ton for l in listings if l.price_ton > 0)
    active = len(prices)
    if active == 0:
        return FloorEstimate(collection, 0.0, 0.0, 0, 0, recent_sales, liquid=False)

    sample = prices[: cfg.floor_sample_size]
    median = statistics.median(sample)
    cleaned = [p for p in sample if p >= median * cfg.floor_outlier_ratio]
    if not cleaned:
        cleaned = sample

    floor = min(cleaned)
    liquid = active >= cfg.min_liquidity_listings and recent_sales >= cfg.min_recent_sales

    estimate = FloorEstimate(
        collection=collection,
        floor_ton=floor,
        median_ton=median,
        sample_size=len(cleaned),
        active_listings=active,
        recent_sales=recent_sales,
        liquid=liquid,
    )
    log.debug(
        "floor[%s]=%.4f median=%.4f active=%d sales=%d liquid=%s",
        collection, floor, median, active, recent_sales, liquid,
    )
    return estimate


def evaluate(listing: Listing, floor: FloorEstimate, cfg: Config) -> Optional[Opportunity]:
    """Return an Opportunity only if every safety gate passes.

    Gates (all must hold):
      1. Collection must be liquid (enough listings + recent sales).
      2. Price must not be a too-good-to-be-true scam (below floor*scam_ratio).
      3. Price must be at least `safety_margin_pct` below the floor.
      4. Net profit after fees must clear both the absolute and percentage mins.
      5. Price must respect the optional absolute spend cap.
    """
    if floor.floor_ton <= 0 or not floor.liquid:
        return None

    price = listing.price_ton

    # Guard: suspiciously cheap -> almost certainly a scam or wrong item.
    if price < floor.floor_ton * cfg.scam_price_ratio:
        log.info("skip %s: price %.4f below scam floor", listing.id, price)
        return None

    # Hard rule: never pay at/above the floor; require the configured discount.
    max_safe_price = floor.floor_ton * (1.0 - cfg.safety_margin_pct)
    if price > max_safe_price:
        return None

    if cfg.max_buy_price_ton is not None and price > cfg.max_buy_price_ton:
        return None

    # Profit = what we could net reselling at floor, minus fees, minus our cost.
    expected_proceeds = floor.floor_ton * (1.0 - cfg.marketplace_fee_pct) - cfg.network_fee_ton
    profit_ton = expected_proceeds - price
    if profit_ton < cfg.min_profit_ton:
        return None

    profit_pct = profit_ton / price if price > 0 else 0.0
    if profit_pct < cfg.min_profit_pct:
        return None

    return Opportunity(
        listing=listing,
        floor=floor,
        profit_ton=profit_ton,
        profit_pct=profit_pct,
        max_safe_price_ton=max_safe_price,
    )
