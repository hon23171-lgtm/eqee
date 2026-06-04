from __future__ import annotations

import logging
from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from .config import Config
from .sniper import Sniper
from .state import State

log = logging.getLogger("mrkt.telegram")

# Runtime-tunable numeric settings, mapped to Config attributes.
TUNABLE_KEYS = {
    "min_profit_ton",
    "min_profit_pct",
    "safety_margin_pct",
    "max_buy_price_ton",
    "min_liquidity_listings",
    "min_recent_sales",
    "watch_interval_sec",
    "floor_refresh_sec",
}


class TelegramController:
    def __init__(self, cfg: Config, state: State, sniper: Sniper) -> None:
        self.cfg = cfg
        self.state = state
        self.sniper = sniper
        self.bot = Bot(token=cfg.telegram_bot_token)
        self.dp = Dispatcher()
        self._register()

    def _authorized(self, message: Message) -> bool:
        return message.chat.id == self.cfg.telegram_chat_id

    def _register(self) -> None:
        self.dp.message.register(self._cmd_start, Command("start"))
        self.dp.message.register(self._cmd_status, Command("status"))
        self.dp.message.register(self._cmd_mode, Command("mode"))
        self.dp.message.register(self._cmd_pause, Command("pause"))
        self.dp.message.register(self._cmd_resume, Command("resume"))
        self.dp.message.register(self._cmd_set, Command("set"))

    async def notify(self, text: str) -> None:
        try:
            await self.bot.send_message(self.cfg.telegram_chat_id, text)
        except Exception as exc:  # noqa: BLE001 - notification must never crash the loops
            log.warning("notify failed: %s", exc)

    async def run(self) -> None:
        await self.notify("🚀 MRKT снайпер запущен. /status для деталей.")
        await self.dp.start_polling(self.bot, handle_signals=False)

    # ---- handlers -----------------------------------------------------

    async def _cmd_start(self, message: Message) -> None:
        if not self._authorized(message):
            return
        await message.answer(
            "MRKT снайпер.\n"
            "/status — состояние\n"
            "/mode auto_buy|notify — режим (авто-покупка+уведомление / только уведомление)\n"
            "/pause /resume — пауза/возобновление\n"
            "/set <ключ> <значение> — настройки (напр. /set min_profit_ton 0.2)"
        )

    async def _cmd_status(self, message: Message) -> None:
        if not self._authorized(message):
            return
        lines = [
            f"Режим: {self.sniper.effective_mode()}",
            f"Пауза: {'да' if self.state.paused else 'нет'}",
            f"Коллекции: {', '.join(self.cfg.collections)}",
            f"min_profit_ton={self.cfg.min_profit_ton} min_profit_pct={self.cfg.min_profit_pct}",
            f"safety_margin_pct={self.cfg.safety_margin_pct}",
            f"watch={self.cfg.watch_interval_sec}s floor_refresh={self.cfg.floor_refresh_sec}s",
            "",
            "Флоры:",
        ]
        for c in self.cfg.collections:
            fl = self.sniper.floors.get(c)
            if fl is None:
                lines.append(f"  {c}: нет данных")
            else:
                lines.append(
                    f"  {c}: {fl.floor_ton:.3f} TON, листингов {fl.active_listings}, "
                    f"ликвид {'да' if fl.liquid else 'нет'}"
                )
        await message.answer("\n".join(lines))

    async def _cmd_mode(self, message: Message, command: CommandObject) -> None:
        if not self._authorized(message):
            return
        arg = (command.args or "").strip().lower()
        if arg not in {"auto_buy", "notify"}:
            await message.answer("Использование: /mode auto_buy | notify")
            return
        if arg == "auto_buy" and not self.cfg.mrkt_token:
            await message.answer("Нельзя включить auto_buy: не задан MRKT_TOKEN.")
            return
        self.state.mode_override = arg
        await self.state.save()
        await message.answer(f"Режим: {arg}")

    async def _cmd_pause(self, message: Message) -> None:
        if not self._authorized(message):
            return
        self.state.paused = True
        await self.state.save()
        await message.answer("⏸ Пауза. Снайпер не покупает и не алертит.")

    async def _cmd_resume(self, message: Message) -> None:
        if not self._authorized(message):
            return
        self.state.paused = False
        await self.state.save()
        await message.answer("▶️ Возобновлено.")

    async def _cmd_set(self, message: Message, command: CommandObject) -> None:
        if not self._authorized(message):
            return
        parts = (command.args or "").split()
        if len(parts) != 2:
            await message.answer("Использование: /set <ключ> <значение>")
            return
        key, raw = parts[0].lower(), parts[1]
        if key not in TUNABLE_KEYS:
            await message.answer(f"Неизвестный ключ. Доступно: {', '.join(sorted(TUNABLE_KEYS))}")
            return
        try:
            value = float(raw)
        except ValueError:
            await message.answer("Значение должно быть числом.")
            return
        setattr(self.cfg, key, value)
        self.state.overrides[key] = value
        await self.state.save()
        await message.answer(f"{key} = {value}")
