from __future__ import annotations

import asyncio
import logging

from .buyer import Buyer
from .config import Config
from .mrkt_client import MrktClient
from .rate_limiter import RateLimiter
from .sniper import Sniper
from .state import State
from .telegram_bot import TelegramController


def _apply_overrides(cfg: Config, state: State) -> None:
    for key, value in state.overrides.items():
        if hasattr(cfg, key):
            setattr(cfg, key, value)
    if state.mode_override:
        cfg.mode = state.mode_override


async def _run() -> None:
    cfg = Config()
    state = State(cfg.state_path)
    _apply_overrides(cfg, state)
    cfg.validate()

    limiter = RateLimiter(cfg.request_rate_per_sec, cfg.request_burst)
    client = MrktClient(cfg, limiter)
    buyer = Buyer(cfg, client, state)
    sniper = Sniper(cfg, client, state, buyer)
    telegram = TelegramController(cfg, state, sniper)
    sniper.set_notifier(telegram.notify)

    sniper_task = asyncio.create_task(sniper.run(), name="sniper")
    telegram_task = asyncio.create_task(telegram.run(), name="telegram")
    try:
        await asyncio.gather(sniper_task, telegram_task)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        sniper.stop()
        sniper_task.cancel()
        telegram_task.cancel()
        await client.aclose()
        await telegram.bot.session.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
