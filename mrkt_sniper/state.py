from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from typing import Dict, Optional, Set

log = logging.getLogger("mrkt.state")


class State:
    """Persistent runtime state: dedupe sets + live overrides.

    Lets the Telegram operator change mode/thresholds at runtime without a
    restart, and prevents duplicate alerts or double-buys across restarts.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self.alerted: Set[str] = set()
        self.purchased: Set[str] = set()
        self.in_flight: Set[str] = set()
        self.overrides: Dict[str, float] = {}
        self.mode_override: Optional[str] = None
        self.paused: bool = False
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.alerted = set(data.get("alerted", []))
            self.purchased = set(data.get("purchased", []))
            self.overrides = {k: float(v) for k, v in data.get("overrides", {}).items()}
            self.mode_override = data.get("mode_override")
            self.paused = bool(data.get("paused", False))
        except Exception as exc:  # noqa: BLE001 - corrupt state must not crash startup
            log.warning("could not load state %s: %s", self.path, exc)

    async def save(self) -> None:
        async with self._lock:
            data = {
                # Cap dedupe history so the file cannot grow without bound.
                "alerted": list(self.alerted)[-5000:],
                "purchased": list(self.purchased)[-5000:],
                "overrides": self.overrides,
                "mode_override": self.mode_override,
                "paused": self.paused,
            }
            await asyncio.to_thread(self._atomic_write, data)

    def _atomic_write(self, data: dict) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def already_handled(self, listing_id: str) -> bool:
        return listing_id in self.alerted or listing_id in self.purchased or listing_id in self.in_flight

    def claim_in_flight(self, listing_id: str) -> bool:
        """Atomically reserve a listing for purchase. Returns False if taken."""
        if listing_id in self.in_flight or listing_id in self.purchased:
            return False
        self.in_flight.add(listing_id)
        return True

    def release_in_flight(self, listing_id: str) -> None:
        self.in_flight.discard(listing_id)
