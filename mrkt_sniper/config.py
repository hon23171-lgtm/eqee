from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass


DEFAULT_COLLECTIONS = ["vice", "cream", "chill", "flame"]

# Hard upper bound for the market-analysis interval. The user requires the
# market (floor) to be re-evaluated at least every 6 minutes.
MAX_FLOOR_REFRESH_SEC = 360


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    return float(raw)


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    return int(raw)


def _env_list(name: str, default: List[str]) -> List[str]:
    raw = _env(name)
    if not raw:
        return list(default)
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


@dataclass
class Config:
    # --- credentials ---
    mrkt_token: str = field(default_factory=lambda: _env("MRKT_TOKEN"))
    telegram_bot_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    # Only this chat may control the bot and receive alerts.
    telegram_chat_id: int = field(default_factory=lambda: _env_int("TELEGRAM_CHAT_ID", 0))

    # --- MRKT endpoints (NO public API — these MUST be verified by the user) ---
    mrkt_base_url: str = field(default_factory=lambda: _env("MRKT_BASE_URL", "https://api.mrkt.io"))
    listings_path: str = field(
        default_factory=lambda: _env("MRKT_LISTINGS_PATH", "/v1/collections/{collection}/listings")
    )
    buy_path: str = field(default_factory=lambda: _env("MRKT_BUY_PATH", "/v1/listings/{listing_id}/buy"))
    sales_path: str = field(
        default_factory=lambda: _env("MRKT_SALES_PATH", "/v1/collections/{collection}/sales")
    )

    # --- targeting ---
    collections: List[str] = field(default_factory=lambda: _env_list("COLLECTIONS", DEFAULT_COLLECTIONS))
    # "auto_buy" -> buy + notify ; "notify" -> notify only
    mode: str = field(default_factory=lambda: _env("MODE", "notify").lower())

    # --- profit / safety thresholds ---
    # Minimum absolute profit in TON required to act (e.g. 0.03 or 0.2).
    min_profit_ton: float = field(default_factory=lambda: _env_float("MIN_PROFIT_TON", 0.03))
    # Minimum profit as a fraction of the buy price (0.05 = 5%).
    min_profit_pct: float = field(default_factory=lambda: _env_float("MIN_PROFIT_PCT", 0.05))
    # Required discount below the estimated floor before we even consider a buy.
    safety_margin_pct: float = field(default_factory=lambda: _env_float("SAFETY_MARGIN_PCT", 0.03))
    # Optional absolute cap on how much we will ever spend on a single gift.
    max_buy_price_ton: Optional[float] = field(
        default_factory=lambda: (_env_float("MAX_BUY_PRICE_TON", 0.0) or None)
    )
    # Listings priced below floor * this ratio are treated as scams/wrong items,
    # never bought (too-good-to-be-true guard).
    scam_price_ratio: float = field(default_factory=lambda: _env_float("SCAM_PRICE_RATIO", 0.15))

    # --- floor estimation robustness ---
    # Lowest N active listings considered when estimating the floor.
    floor_sample_size: int = field(default_factory=lambda: _env_int("FLOOR_SAMPLE_SIZE", 15))
    # Drop listings priced below median * this ratio when estimating the floor
    # (troll / mispriced listings must not drag the floor down).
    floor_outlier_ratio: float = field(default_factory=lambda: _env_float("FLOOR_OUTLIER_RATIO", 0.5))

    # --- liquidity gate ---
    min_liquidity_listings: int = field(default_factory=lambda: _env_int("MIN_LIQUIDITY_LISTINGS", 12))
    min_recent_sales: int = field(default_factory=lambda: _env_int("MIN_RECENT_SALES", 3))

    # --- fees (used in the profit calculation) ---
    marketplace_fee_pct: float = field(default_factory=lambda: _env_float("MARKETPLACE_FEE_PCT", 0.05))
    network_fee_ton: float = field(default_factory=lambda: _env_float("NETWORK_FEE_TON", 0.01))

    # --- timing ---
    # Fast loop that scans for fresh underpriced listings (finding speed).
    watch_interval_sec: float = field(default_factory=lambda: _env_float("WATCH_INTERVAL_SEC", 3.0))
    # Market analysis (floor recompute) interval. Clamped to <= 6 minutes.
    floor_refresh_sec: float = field(default_factory=lambda: _env_float("FLOOR_REFRESH_SEC", 120.0))

    # --- rate limiting / anti-429 ---
    request_rate_per_sec: float = field(default_factory=lambda: _env_float("REQUEST_RATE_PER_SEC", 4.0))
    request_burst: int = field(default_factory=lambda: _env_int("REQUEST_BURST", 8))
    max_retries: int = field(default_factory=lambda: _env_int("MAX_RETRIES", 5))
    backoff_base_sec: float = field(default_factory=lambda: _env_float("BACKOFF_BASE_SEC", 0.5))
    backoff_max_sec: float = field(default_factory=lambda: _env_float("BACKOFF_MAX_SEC", 30.0))
    request_timeout_sec: float = field(default_factory=lambda: _env_float("REQUEST_TIMEOUT_SEC", 8.0))

    # --- misc ---
    state_path: str = field(default_factory=lambda: _env("STATE_PATH", "mrkt_sniper_state.json"))
    dry_run: bool = field(default_factory=lambda: _env("DRY_RUN", "false").lower() in {"1", "true", "yes"})

    def validate(self) -> None:
        errors = []
        if self.mode not in {"auto_buy", "notify"}:
            errors.append(f"MODE must be 'auto_buy' or 'notify', got {self.mode!r}")
        if not self.collections:
            errors.append("COLLECTIONS is empty")
        if not self.telegram_bot_token:
            errors.append("TELEGRAM_BOT_TOKEN is required")
        if not self.telegram_chat_id:
            errors.append("TELEGRAM_CHAT_ID is required (the chat allowed to control the bot)")
        if self.mode == "auto_buy" and not self.mrkt_token:
            errors.append("MRKT_TOKEN is required for auto_buy mode")
        if self.min_profit_ton < 0:
            errors.append("MIN_PROFIT_TON must be >= 0")
        if not 0 <= self.safety_margin_pct < 1:
            errors.append("SAFETY_MARGIN_PCT must be in [0, 1)")
        if self.floor_refresh_sec > MAX_FLOOR_REFRESH_SEC:
            # Enforce the "analyze at least every 6 minutes" requirement.
            self.floor_refresh_sec = MAX_FLOOR_REFRESH_SEC
        if errors:
            raise ValueError("Invalid configuration:\n  - " + "\n  - ".join(errors))
