from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class Listing:
    id: str
    collection: str
    price_ton: float
    name: str = ""
    seller: str = ""
    url: str = ""
    attributes: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FloorEstimate:
    collection: str
    floor_ton: float
    median_ton: float
    sample_size: int
    active_listings: int
    recent_sales: int
    liquid: bool
    computed_at: float = field(default_factory=time.time)

    def age_sec(self) -> float:
        return time.time() - self.computed_at


@dataclass
class Opportunity:
    listing: Listing
    floor: FloorEstimate
    profit_ton: float
    profit_pct: float
    max_safe_price_ton: float
