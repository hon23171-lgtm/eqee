# -*- coding: utf-8 -*-
"""
MRKT Ultra-Fast & Safe Sniper Bot
Designed by Antigravity for TON Gift Market (mrkt)
"""

import time
import json
import os
import sys
if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass
import threading
import traceback
import re
from datetime import datetime
import urllib.parse

# Safe import of curl_cffi for browser impersonation (TLS fingerprinting)
try:
    from curl_cffi import requests as curl_requests
    HAS_CURL_CFFI = True
except Exception:
    import requests as curl_requests
    HAS_CURL_CFFI = False

# =====================================================================
# CONSTANTS & CONFIGURATION
# =====================================================================
API_BASE_URL = "https://api.tgmrkt.io/api/v1"

# Collections the bot watches for sniping, floor analysis, and auto-offers.
TARGET_COLLECTIONS = [
    "Vice Cream",
    "Chill Flame",
    "Pet Snake",
    "Lol Pop",
    "Mood Pack",
    "Pool Float",
    "Big Year",
]

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__)) if '__file__' in globals() else os.getcwd()
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")
ALERTED_FILE = os.path.join(SCRIPT_DIR, "alerted_ids.json")

# Default headers matching a mobile Telegram App environment
HEADERS_TEMPLATE = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Content-Type": "application/json",
    "Origin": "https://app.tgmrkt.io",
    "Referer": "https://app.tgmrkt.io/",
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
}

# =====================================================================
# GETGEMS (getgems.io) CONFIGURATION
# =====================================================================
# GetGems is a TON NFT marketplace with a GraphQL API at
# `https://api.getgems.io/graphql`. To search a specific collection it needs
# that collection's CONTRACT ADDRESS — these Telegram-gift collections each
# live at their own TON address, which the user supplies via config /
# the /getgems_address command (we cannot know them in advance).
#
# Buying a fix-price sale on GetGems is non-custodial: the buyer's wallet sends
# the sale price (+ gas) to the sale contract address that comes WITH the
# listing data. So auto-buy builds the transaction from the listing itself
# (sale address + price), not from invented values.
#
# IMPORTANT — UNVERIFIED INTEGRATION POINTS (adjust after a real test run):
#   • GETGEMS_SEARCH_QUERY — the GraphQL query / field names for on-sale items.
#   • The response shape parsed in `_getgems_parse_listings`.
#   • GETGEMS_BUY_GAS_TON / the buy message body (sale-contract opcode).
GETGEMS_GRAPHQL_URL = "https://api.getgems.io/graphql"

# Per-collection TON contract addresses. Empty by default — fill via config
# ("getgems_addresses": {"Vice Cream": "EQ..."}) or /getgems_address <name> <addr>.
GETGEMS_COLLECTION_ADDRESSES = {name: "" for name in TARGET_COLLECTIONS}

# GraphQL query returning the cheapest fix-price items in a collection.
GETGEMS_SEARCH_QUERY = (
    "query nftSearch($address: String!, $count: Int!) {"
    "  alphaNftItemSearch(query: {collectionAddress: $address, saleType: \"fix_price\"},"
    "    sort: {byPrice: {direction: asc}}, first: $count) {"
    "    edges { node { name address"
    "      sale { __typename ... on NftSaleFixPrice { fullPrice address } } } } } }"
)

# Extra TON sent with a buy to cover gas/forward fees (excess is returned by the
# sale contract). UNVERIFIED — depends on the sale contract version.
GETGEMS_BUY_GAS_TON = 1.0
# Fee buffer (TON) added on top of the price cap when sanity-checking the total
# value of the transaction, guarding against draining the wallet beyond intent.
GETGEMS_TX_FEE_BUFFER_TON = GETGEMS_BUY_GAS_TON + 0.2

GETGEMS_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Content-Type": "application/json",
    "Origin": "https://getgems.io",
    "Referer": "https://getgems.io/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}

# =====================================================================
# TON BLOCKCHAIN API (toncenter.com) CONFIGURATION
# =====================================================================
# A toncenter API key (64-hex) lets the bot read on-chain data and broadcast
# signed transactions over plain HTTPS — used here for an auto-buy pre-flight
# wallet-balance check and as a transaction broadcast fallback. The key below
# is the one supplied by the operator; override it in config.json
# ("ton_api_key") or via the /ton_apikey Telegram command. Keep it private.
TONCENTER_API_BASE = "https://toncenter.com/api/v2"
DEFAULT_TON_API_KEY = "59c26cb767cce5edbc28a3bde188c798bd44d72efcb4afba470f5d7ffc381b34"

# =====================================================================
# GLOBAL STATE
# =====================================================================
state = {
    "auth_token": "",
    "tg_bot_token": "",
    "tg_chat_id": "",
    "margin": 0.2,        # in TON — used both for sniper profit and offer discount below floor
    "delay": 1.5,         # Fast polling interval in seconds
    "sniper_mode": "buy", # "buy" (auto-buy + alert) or "alert" (only alert)
    "running": True,
    "offers_running": False,  # Auto-offers mode flag
    "offers_delay": 30.0,     # Seconds between offer re-check cycles
    "last_analysis_time": None,
    "consecutive_429": 0,
    # --- GetGems (getgems.io) ---
    "getgems_api_key": "",       # Optional API key for getgems API (sent as Authorization if set)
    "getgems_running": False,    # Whether the GetGems floor watcher / alert loop is active
    "getgems_delay": 5.0,        # Polling interval (seconds) for the GetGems sniper
    "getgems_addresses": {},     # {collection_name: contract_address} overrides from config
    # --- GetGems auto-buy (signs a TON transaction with the wallet below) ---
    "getgems_autobuy": False,        # Master switch for GetGems auto-buy (default OFF)
    "getgems_wallet_mnemonic": "",   # 24-word seed of the buying wallet — SENSITIVE, see warnings
    "getgems_max_buy_price": 0.0,    # Hard safety cap (TON). 0 = auto-buy disabled, must be set > 0 to buy.
    # --- TON blockchain API (toncenter) ---
    "ton_api_key": DEFAULT_TON_API_KEY,  # toncenter API key for balance checks + tx broadcast
}

# Calculated market floor prices
# Calculated as the median of the 2nd, 3rd, and 4th cheapest items to prevent outliers from skewing
stable_floors = {name: None for name in TARGET_COLLECTIONS}

# Calculated GetGems floor prices (same collections as MRKT, computed the same way)
getgems_floors = {name: None for name in TARGET_COLLECTIONS}

# Resolved GetGems collection addresses (constant defaults merged with config overrides)
getgems_addresses = dict(GETGEMS_COLLECTION_ADDRESSES)

# Cache of already placed offers: {collection_name: placed_price_ton}
# Used to skip re-placing an offer if the price hasn't changed significantly
placed_offers = {}

# Statistics
stats = {
    "scans": 0,
    "buys": 0,
    "alerts": 0,
    "offers": 0,
    "errors": 0,
    "start_time": datetime.now()
}

# Prevent race conditions when modifying configuration or alerted set
state_lock = threading.RLock()
alerted_lock = threading.Lock()
alerted_ids = set()

# Helper: Print with timestamp
def log(msg, level="INFO"):
    t = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{t}] [{level}] {msg}", flush=True)

# =====================================================================
# CONFIGURATION PERSISTENCE
# =====================================================================
def load_config():
    global alerted_ids
    # Load settings
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                state["auth_token"] = cfg.get("auth_token", "")
                state["tg_bot_token"] = cfg.get("tg_bot_token", "")
                state["tg_chat_id"] = cfg.get("tg_chat_id", "")
                state["margin"] = float(cfg.get("margin", 0.2))
                state["delay"] = float(cfg.get("delay", 1.5))
                state["offers_delay"] = float(cfg.get("offers_delay", 30.0))
                state["getgems_api_key"] = cfg.get("getgems_api_key", "")
                state["getgems_running"] = bool(cfg.get("getgems_running", False))
                state["getgems_delay"] = float(cfg.get("getgems_delay", 5.0))
                state["getgems_autobuy"] = bool(cfg.get("getgems_autobuy", False))
                state["getgems_wallet_mnemonic"] = cfg.get("getgems_wallet_mnemonic", "")
                state["getgems_max_buy_price"] = float(cfg.get("getgems_max_buy_price", 0.0))
                state["ton_api_key"] = cfg.get("ton_api_key", DEFAULT_TON_API_KEY)
                cfg_addrs = cfg.get("getgems_addresses", {})
                if isinstance(cfg_addrs, dict):
                    state["getgems_addresses"] = cfg_addrs
                    for name, addr in cfg_addrs.items():
                        if name in getgems_addresses:
                            getgems_addresses[name] = addr

                raw_mode = cfg.get("sniper_mode", "buy")
                if "автовыкуп" in str(raw_mode).lower():
                    state["sniper_mode"] = "buy"
                elif "уведомлен" in str(raw_mode).lower() or "бот" in str(raw_mode).lower():
                    state["sniper_mode"] = "alert"
                elif raw_mode in ["buy", "alert"]:
                    state["sniper_mode"] = raw_mode
                else:
                    state["sniper_mode"] = "buy"
                log("Configuration loaded successfully.")
        except Exception as e:
            log(f"Failed to load config: {e}", "ERROR")

    # Load alerted IDs to prevent duplicate actions
    if os.path.exists(ALERTED_FILE):
        try:
            with open(ALERTED_FILE, "r", encoding="utf-8") as f:
                alerted_ids = set(json.load(f))
                log(f"Loaded {len(alerted_ids)} processed item IDs.")
        except Exception:
            pass

def save_config():
    with state_lock:
        cfg = {
            "auth_token": state["auth_token"],
            "tg_bot_token": state["tg_bot_token"],
            "tg_chat_id": state["tg_chat_id"],
            "margin": state["margin"],
            "delay": state["delay"],
            "offers_delay": state["offers_delay"],
            "sniper_mode": state["sniper_mode"],
            "getgems_api_key": state["getgems_api_key"],
            "getgems_running": state["getgems_running"],
            "getgems_delay": state["getgems_delay"],
            "getgems_autobuy": state["getgems_autobuy"],
            "getgems_wallet_mnemonic": state["getgems_wallet_mnemonic"],
            "getgems_max_buy_price": state["getgems_max_buy_price"],
            "getgems_addresses": state["getgems_addresses"],
            "ton_api_key": state["ton_api_key"],
        }
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=4)
    except Exception as e:
        log(f"Failed to save config: {e}", "ERROR")

def add_alerted_id(item_id):
    with alerted_lock:
        alerted_ids.add(item_id)
        # Periodically save to file
        try:
            with open(ALERTED_FILE, "w", encoding="utf-8") as f:
                json.dump(list(alerted_ids), f)
        except Exception:
            pass

# =====================================================================
# PERSISTENT HTTP SESSION WITH TLS FINGERPRINT
# =====================================================================
class ImpersonatedSession:
    def __init__(self):
        self._local = threading.local()
        # Log session type once on initialization
        if HAS_CURL_CFFI:
            log("Using curl_cffi with Chrome 124 browser impersonation (thread-local).")
        else:
            log("Using default requests library (no TLS impersonation, thread-local).", "WARN")

    @property
    def session(self):
        if not hasattr(self._local, "session"):
            if HAS_CURL_CFFI:
                try:
                    self._local.session = curl_requests.Session(impersonate="chrome124")
                except Exception:
                    self._local.session = curl_requests.Session()
            else:
                self._local.session = curl_requests.Session()
        return self._local.session

    def get_auth_headers(self):
        with state_lock:
            token = state["auth_token"]
        headers = HEADERS_TEMPLATE.copy()
        if token:
            headers["Authorization"] = token
            headers["Cookie"] = f"access_token={token}"
        return headers

    def post(self, url, json_data, timeout=8):
        try:
            return self.session.post(url, json=json_data, headers=self.get_auth_headers(), timeout=timeout)
        except Exception as e:
            log(f"POST request exception to {url}: {e}", "ERROR")
            return None

    def get(self, url, params=None, timeout=8):
        try:
            return self.session.get(url, params=params, headers=self.get_auth_headers(), timeout=timeout)
        except Exception as e:
            log(f"GET request exception to {url}: {e}", "ERROR")
            return None

session = ImpersonatedSession()

# =====================================================================
# GETGEMS (getgems.io) HTTP SESSION + LISTINGS
# =====================================================================
class GetGemsSession:
    """
    Thin client for getgems.io's GraphQL API.

    An optional API key (config `getgems_api_key`) is sent as the Authorization
    header when present. This is a best-effort client for an endpoint whose
    exact query/response shape is UNVERIFIED — see the GETGEMS_* constants.
    """

    def __init__(self):
        self._local = threading.local()

    @property
    def session(self):
        if not hasattr(self._local, "session"):
            if HAS_CURL_CFFI:
                try:
                    self._local.session = curl_requests.Session(impersonate="chrome124")
                except Exception:
                    self._local.session = curl_requests.Session()
            else:
                self._local.session = curl_requests.Session()
        return self._local.session

    def _headers(self):
        headers = GETGEMS_HEADERS.copy()
        with state_lock:
            api_key = state["getgems_api_key"]
        if api_key:
            headers["Authorization"] = api_key
        return headers

    def graphql(self, query, variables, timeout=10):
        payload = {"query": query, "variables": variables}
        try:
            return self.session.post(GETGEMS_GRAPHQL_URL, json=payload, headers=self._headers(), timeout=timeout)
        except Exception as e:
            log(f"[GetGems] GraphQL POST exception: {e}", "ERROR")
            return None


getgems_session = GetGemsSession()


def getgems_get_address(collection_name):
    """Resolve the configured GetGems contract address for a collection, or ''."""
    with state_lock:
        overrides = dict(state["getgems_addresses"])
    return overrides.get(collection_name) or getgems_addresses.get(collection_name) or ""


def _getgems_node_to_item(node):
    """Normalize one GraphQL node into the bot's item shape (price in nanoTON)."""
    if not isinstance(node, dict):
        return None
    sale = node.get("sale") or {}
    full_price = sale.get("fullPrice") if isinstance(sale, dict) else None
    if full_price is None:
        full_price = node.get("price") or node.get("fullPrice")
    if full_price is None:
        return None
    try:
        price_nano = int(full_price)  # GetGems prices are nanoTON strings
    except (TypeError, ValueError):
        try:
            price_nano = int(float(full_price) * 1e9)
        except (TypeError, ValueError):
            return None
    if price_nano <= 0:
        return None
    item_address = node.get("address") or node.get("nftAddress")
    sale_address = sale.get("address") if isinstance(sale, dict) else None
    return {
        "salePrice": price_nano,
        "id": item_address,
        "sale_address": sale_address,
        "name": node.get("name") or node.get("title"),
        "number": node.get("number") or node.get("index"),
        "url": f"https://getgems.io/nft/{item_address}" if item_address else "https://getgems.io",
    }


def _getgems_parse_listings(body):
    """
    Normalize a GetGems GraphQL response into a list of item dicts.
    Defensive against the exact (UNVERIFIED) field path by walking the response
    for an `edges` list of `node` objects, or a plain list of items.
    """
    items = []

    def _collect_edges(edges):
        out = []
        for e in edges:
            node = e.get("node") if isinstance(e, dict) else None
            it = _getgems_node_to_item(node if node is not None else e)
            if it:
                out.append(it)
        return out

    if isinstance(body, dict):
        data = body.get("data", body)
        # Find the first dict with an "edges" list anywhere one level down.
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, dict) and isinstance(v.get("edges"), list):
                    items = _collect_edges(v["edges"])
                    if items:
                        return items
                if isinstance(v, list):
                    cand = _collect_edges(v)
                    if cand:
                        return cand
        if isinstance(data.get("edges"), list):
            return _collect_edges(data["edges"])
    elif isinstance(body, list):
        return _collect_edges(body)
    return items


def getgems_fetch_listings(collection_name, count=10):
    """
    Fetch the cheapest `count` fix-price listings for a collection from GetGems.
    Requires the collection's contract address (set via /getgems_address).
    Returns a normalized list (see _getgems_parse_listings).
    """
    address = getgems_get_address(collection_name)
    if not address:
        raise Exception("GETGEMS_NO_ADDRESS")

    variables = {"address": address, "count": count}
    r = getgems_session.graphql(GETGEMS_SEARCH_QUERY, variables)
    if r is None:
        raise Exception("GETGEMS_CONNECTION_FAILED")
    if r.status_code == 429:
        raise Exception("API_429")
    if r.status_code in (401, 403):
        raise Exception("GETGEMS_AUTH_REQUIRED")
    if r.status_code != 200:
        raise Exception(f"GETGEMS_ERROR_{r.status_code}")
    try:
        body = r.json()
    except Exception:
        raise Exception("GETGEMS_BAD_JSON")
    if isinstance(body, dict) and body.get("errors"):
        raise Exception(f"GETGEMS_GRAPHQL_ERROR: {body['errors']}")
    return _getgems_parse_listings(body)


# =====================================================================
# GETGEMS AUTO-BUY (TON wallet transaction signing)
# =====================================================================
# GetGems is non-custodial: buying a fix-price sale = sending the price (+ gas)
# to the sale CONTRACT address that comes with the listing. So auto-buy builds
# the transaction from the listing itself (sale address + price) — not invented
# values — then signs and broadcasts it with the configured wallet.
#
# SAFETY MODEL:
#   • Disabled unless `getgems_autobuy` is True AND `getgems_max_buy_price` > 0.
#   • Destination = the listing's sale contract address (from the API).
#   • The item price must be within the price cap; total sent (price + gas) is
#     also bounded by (cap + fee buffer) before signing.
#   • The TON SDK (pytoniq) is imported lazily; if missing, auto-buy aborts
#     cleanly and falls back to an alert (no broadcast).
#
# UNVERIFIED: GETGEMS_BUY_GAS_TON and the buy message body (sale-contract op).
# Test with a cheap item and a low cap first.

def toncenter_get_balance_nano(address):
    """
    Read a wallet's balance (in nanoTON) via toncenter's getAddressBalance.
    Returns an int, or None if the API key is missing or the request fails.
    Pure HTTPS — does not require the TON SDK.
    """
    with state_lock:
        api_key = state["ton_api_key"]
    if not api_key:
        return None
    try:
        headers = {"Accept": "application/json", "X-API-Key": api_key}
        resp = curl_requests.get(
            f"{TONCENTER_API_BASE}/getAddressBalance",
            params={"address": address},
            headers=headers,
            timeout=15,
        )
        data = resp.json()
        if not data.get("ok"):
            return None
        return int(data.get("result"))
    except Exception as e:
        log(f"[TON] Balance check failed: {e}", "WARN")
        return None


def _ton_sign_and_send(messages, mnemonic):
    """
    Sign and broadcast the given TON transaction messages with the configured
    wallet. `messages` = list of {"address", "amount_nano", "payload"|None}.
    Uses pytoniq (lazy import). If a toncenter API key is configured, runs a
    pre-flight balance check so we never sign a transfer the wallet can't cover.
    Returns (success: bool, msg: str).
    """
    try:
        from pytoniq import LiteBalancer, WalletV4R2  # type: ignore
    except Exception:
        return False, "TON SDK не установлен. Установите: pip install pytoniq"

    words = mnemonic.strip().split()
    if len(words) not in (12, 24):
        return False, "BAD_MNEMONIC (ожидается 12 или 24 слова)"

    import asyncio

    total_nano = sum(int(m["amount_nano"]) for m in messages)

    async def _run():
        provider = LiteBalancer.from_mainnet_config(trust_level=2)
        await provider.start_up()
        try:
            wallet = await WalletV4R2.from_mnemonic(provider, words)

            # Pre-flight balance guard via toncenter (skipped if no API key).
            try:
                addr_str = wallet.address.to_str(is_user_friendly=True, is_bounceable=False)
            except Exception:
                addr_str = str(wallet.address)
            balance = toncenter_get_balance_nano(addr_str)
            if balance is not None and balance < total_nano:
                return False, (
                    f"INSUFFICIENT_FUNDS: баланс {balance/1e9:.3f} TON < "
                    f"требуется {total_nano/1e9:.3f} TON — покупка отменена."
                )

            for m in messages:
                await wallet.transfer(
                    destination=m["address"],
                    amount=m["amount_nano"],
                    body=m.get("payload"),
                )
            return True, "SUCCESS"
        finally:
            await provider.close_all()

    try:
        return asyncio.run(_run())
    except Exception as e:
        return False, f"TON_SEND_ERROR: {e}"


def getgems_execute_buy(item):
    """
    Full GetGems auto-buy flow with safety guards. Returns (success, message).
    """
    with state_lock:
        autobuy = state["getgems_autobuy"]
        mnemonic = state["getgems_wallet_mnemonic"]
        max_price = state["getgems_max_buy_price"]

    if not autobuy:
        return False, "AUTOBUY_DISABLED"
    if max_price <= 0:
        return False, "NO_PRICE_CAP (установите /getgems_maxprice)"
    if not mnemonic:
        return False, "NO_WALLET (установите /getgems_wallet)"

    price_ton = float(item.get("salePrice", 0)) / 1e9
    if price_ton <= 0:
        return False, "BAD_PRICE"
    if price_ton > max_price:
        return False, f"PRICE_ABOVE_CAP ({price_ton:.3f} > {max_price:.3f} TON)"

    sale_address = item.get("sale_address")
    if not sale_address:
        return False, "NO_SALE_ADDRESS (нет адреса контракта продажи в листинге)"

    # Build the buy transaction: send price + gas to the sale contract.
    total_nano = item.get("salePrice", 0) + int(GETGEMS_BUY_GAS_TON * 1e9)

    # Hard guard: total value must not exceed cap + fee buffer.
    limit_nano = int((max_price + GETGEMS_TX_FEE_BUFFER_TON) * 1e9)
    if total_nano > limit_nano:
        return False, (
            f"TX_VALUE_OVER_LIMIT: транзакция на {total_nano/1e9:.3f} TON превышает "
            f"лимит {limit_nano/1e9:.3f} TON — покупка отменена ради безопасности."
        )

    messages = [{"address": str(sale_address), "amount_nano": total_nano, "payload": None}]
    return _ton_sign_and_send(messages, mnemonic)


# =====================================================================
# TELEGRAM BOT INTEGRATION (LONG POLLING)
# =====================================================================
class TelegramBot:
    def __init__(self):
        self.offset = 0

    def send_message(self, text, parse_mode="HTML"):
        with state_lock:
            token = state["tg_bot_token"]
            chat_id = state["tg_chat_id"]
        if not token or not chat_id:
            return
        
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode
        }
        try:
            r = session.session.post(url, json=payload, timeout=8)
            if r.status_code != 200:
                log(f"Telegram send_message failed: {r.status_code} {r.text}", "WARN")
        except Exception as e:
            log(f"Telegram bot communication error: {e}", "ERROR")

    def send_photo(self, photo_url, caption, parse_mode="HTML"):
        with state_lock:
            token = state["tg_bot_token"]
            chat_id = state["tg_chat_id"]
        if not token or not chat_id:
            return
        
        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        payload = {
            "chat_id": chat_id,
            "photo": photo_url,
            "caption": caption,
            "parse_mode": parse_mode
        }
        try:
            r = session.session.post(url, json=payload, timeout=8)
            if r.status_code != 200:
                # Fallback to simple sendMessage if sendPhoto fails
                self.send_message(caption, parse_mode)
        except Exception:
            self.send_message(caption, parse_mode)

    def handle_command(self, cmd, args, chat_id):
        # Update chat_id in state if it's new
        with state_lock:
            if not state["tg_chat_id"] or state["tg_chat_id"] != str(chat_id):
                state["tg_chat_id"] = str(chat_id)
                threading.Thread(target=save_config, daemon=True).start()

        cmd = cmd.lower()
        if cmd == "/start" or cmd == "/help":
            collections_str = ", ".join(f"<b>{c}</b>" for c in TARGET_COLLECTIONS)
            help_text = (
                "🤖 <b>MRKT Sniper Bot</b>\n\n"
                f"Снайпер запущен и готов к работе. Отслеживаемые коллекции: {collections_str}.\n\n"
                "<b>Доступные команды:</b>\n"
                "📊 /status - Проверить текущий статус и флор-цены\n"
                "💰 /margin &lt;число&gt; - Установить мин. профит / скидку оффера в TON (например: <code>/margin 0.2</code>)\n"
                "⏱ /delay &lt;число&gt; - Интервал сканирования снайпера в сек\n"
                "⏱ /offers_delay &lt;число&gt; - Интервал обновления офферов в сек (по умолч. 30)\n"
                "⚡ /mode_buy - Включить АВТОВЫКУП + уведомления\n"
                "🔔 /mode_alert - Включить ТОЛЬКО УВЕДОМЛЕНИЯ\n"
                "🔑 /token &lt;токен&gt; - Установить новый авторизационный токен MRKT\n"
                "🚀 /start_sniper - Запустить снайпер\n"
                "🛑 /stop_sniper - Остановить снайпер\n"
                "✉️ /offers_on - Включить авто-офферы (ниже флора на margin TON)\n"
                "❌ /offers_off - Выключить авто-офферы\n"
                "\n<b>💎 GetGems (getgems.io):</b>\n"
                "🔑 /getgems_apikey &lt;ключ&gt; - (Опц.) API-ключ getgems.io\n"
                "🏷 /getgems_address &lt;коллекция&gt; &lt;адрес&gt; - Задать контракт коллекции\n"
                "💎 /getgems_on - Включить мониторинг GetGems (флор + алерты)\n"
                "💎 /getgems_off - Выключить мониторинг GetGems\n"
                "👛 /getgems_wallet &lt;сид-фраза&gt; - Кошелёк для автовыкупа (12/24 слова)\n"
                "🎯 /getgems_maxprice &lt;TON&gt; - Лимит цены автовыкупа (0 = выкл)\n"
                "⚡ /getgems_autobuy_on - Включить автовыкуп GetGems\n"
                "🛑 /getgems_autobuy_off - Выключить автовыкуп GetGems\n"
                "🔗 /ton_apikey &lt;ключ&gt; - API-ключ toncenter (баланс перед автовыкупом)\n\n"
                "🧪 /test - Запустить тестовый запрос и вывести флор прямо сейчас"
            )
            self.send_message(help_text)

        elif cmd == "/status":
            uptime = datetime.now() - stats["start_time"]
            hours, remainder = divmod(uptime.seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            uptime_str = f"{uptime.days}д {hours}ч {minutes}м {seconds}с"

            with state_lock:
                running_status = "🟢 АКТИВЕН" if state["running"] else "🔴 ОСТАНОВЛЕН"
                offers_status = "🟢 ВКЛЮЧЕНЫ" if state["offers_running"] else "🔴 ВЫКЛЮЧЕНЫ"
                getgems_status = "🟢 ВКЛЮЧЕН" if state["getgems_running"] else "🔴 ВЫКЛЮЧЕН"
                getgems_autobuy_status = "🟢 ВКЛЮЧЕН" if state["getgems_autobuy"] else "🔴 ВЫКЛЮЧЕН"
                getgems_cap = state["getgems_max_buy_price"]
                getgems_wallet_set = "Задан" if state["getgems_wallet_mnemonic"] else "Отсутствует"
                mode_str = "Выкуп + Уведомление" if state["sniper_mode"] == "buy" else "Только Уведомление"
                margin_val = state["margin"]
                delay_val = state["delay"]
                offers_delay_val = state["offers_delay"]
                token_preview = f"{state['auth_token'][:6]}...{state['auth_token'][-6:]}" if state["auth_token"] else "Отсутствует"
                getgems_auth = "Задан" if state["getgems_api_key"] else "Отсутствует"
                ton_api_set = "Задан" if state["ton_api_key"] else "Отсутствует"

            floor_lines = []
            offer_lines = []
            getgems_floor_lines = []
            for c in TARGET_COLLECTIONS:
                floor_val = stable_floors.get(c)
                floor_str = f"{floor_val:.2f} TON" if floor_val else "Не определен"
                floor_lines.append(f"• {c}: <b>{floor_str}</b>")

                placed_val = placed_offers.get(c)
                placed_str = f"{placed_val:.2f} TON" if placed_val else "—"
                offer_lines.append(f"• {c}: <code>{placed_str}</code>")

                gg_val = getgems_floors.get(c)
                gg_str = f"{gg_val:.2f} TON" if gg_val else "Не определен"
                getgems_floor_lines.append(f"• {c}: <b>{gg_str}</b>")

            floors_block = "\n".join(floor_lines)
            offers_block = "\n".join(offer_lines)
            getgems_floors_block = "\n".join(getgems_floor_lines)

            status_text = (
                f"📊 <b>Текущий статус снайпера:</b>\n"
                f"• Режим работы: <b>{running_status}</b>\n"
                f"• Тип снайпера: <code>{mode_str}</code>\n"
                f"• Авто-офферы: <b>{offers_status}</b>\n"
                f"• GetGems-мониторинг: <b>{getgems_status}</b>\n"
                f"• GetGems-автовыкуп: <b>{getgems_autobuy_status}</b> (лимит {getgems_cap} TON, кошелёк: {getgems_wallet_set})\n"
                f"• Мин. профит / скидка оффера: <code>{margin_val} TON</code>\n"
                f"• Интервал опроса: <code>{delay_val} сек</code>\n"
                f"• Интервал офферов: <code>{offers_delay_val} сек</code>\n"
                f"• Токен MRKT: <code>{token_preview}</code>\n"
                f"• API-ключ GetGems: <code>{getgems_auth}</code>\n"
                f"• TON API (toncenter): <code>{ton_api_set}</code>\n"
                f"• Время работы: <code>{uptime_str}</code>\n\n"
                f"📈 <b>Рыночный флор (MRKT):</b>\n"
                f"{floors_block}\n\n"
                f"💎 <b>Флор GetGems:</b>\n"
                f"{getgems_floors_block}\n\n"
                f"✉️ <b>Последние выставленные офферы:</b>\n"
                f"{offers_block}\n\n"
                f"⚙️ <b>Статистика:</b>\n"
                f"• Проверено лотов: <code>{stats['scans']}</code>\n"
                f"• Успешных покупок: <code>{stats['buys']}</code>\n"
                f"• Выставлено офферов: <code>{stats['offers']}</code>\n"
                f"• Отправлено алертов: <code>{stats['alerts']}</code>\n"
                f"• Ошибок сети: <code>{stats['errors']}</code>"
            )
            self.send_message(status_text)

        elif cmd == "/margin":
            if not args:
                self.send_message("❌ Укажите значение маржи. Пример: <code>/margin 0.15</code>")
                return
            try:
                val = float(args[0])
                with state_lock:
                    state["margin"] = val
                save_config()
                self.send_message(f"✅ Минимальный профит успешно изменен на <b>{val} TON</b>.")
            except ValueError:
                self.send_message("❌ Неверный формат числа.")

        elif cmd == "/delay":
            if not args:
                self.send_message("❌ Укажите значение задержки. Пример: <code>/delay 1.5</code>")
                return
            try:
                val = float(args[0])
                if val < 0.1:
                    self.send_message("❌ Задержка не может быть меньше 0.1 сек.")
                    return
                with state_lock:
                    state["delay"] = val
                save_config()
                self.send_message(f"✅ Интервал опроса изменен на <b>{val} сек</b>.")
            except ValueError:
                self.send_message("❌ Неверный формат числа.")

        elif cmd == "/mode_buy":
            with state_lock:
                state["sniper_mode"] = "buy"
            save_config()
            self.send_message("✅ Режим снайпера изменен на: <b>Автовыкуп + Уведомление</b>.")

        elif cmd == "/mode_alert":
            with state_lock:
                state["sniper_mode"] = "alert"
            save_config()
            self.send_message("✅ Режим снайпера изменен на: <b>Только Уведомления</b>.")

        elif cmd == "/token":
            if not args:
                self.send_message("❌ Укажите новый токен авторизации. Пример: <code>/token xxxxxxx</code>")
                return
            new_tok = args[0].strip()
            with state_lock:
                state["auth_token"] = new_tok
            save_config()
            self.send_message("✅ Токен авторизации MRKT успешно обновлен.")

        elif cmd == "/start_sniper":
            with state_lock:
                if state["running"]:
                    self.send_message("⚠️ Снайпер уже запущен!")
                    return
                state["running"] = True
            self.send_message("🚀 <b>Снайпер успешно ЗАПУЩЕН!</b> Начинаю проверку рынка.")
            log("Sniper started by Telegram command.")

        elif cmd == "/stop_sniper":
            with state_lock:
                if not state["running"]:
                    self.send_message("⚠️ Снайпер уже остановлен!")
                    return
                state["running"] = False
            self.send_message("🛑 <b>Снайпер УСПЕШНО ОСТАНОВЛЕН.</b>")
            log("Sniper stopped by Telegram command.")

        elif cmd == "/offers_delay":
            if not args:
                self.send_message("❌ Укажите интервал. Пример: <code>/offers_delay 30</code>")
                return
            try:
                val = float(args[0])
                if val < 5.0:
                    self.send_message("❌ Интервал не может быть меньше 5 сек.")
                    return
                with state_lock:
                    state["offers_delay"] = val
                save_config()
                self.send_message(f"✅ Интервал обновления офферов изменен на <b>{val} сек</b>.")
            except ValueError:
                self.send_message("❌ Неверный формат числа.")

        elif cmd == "/offers_on":
            with state_lock:
                tok = state["auth_token"]
            if not tok:
                self.send_message("❌ Сначала укажите токен MRKT командой <code>/token &lt;токен&gt;</code>.")
                return
            with state_lock:
                state["offers_running"] = True
            save_config()
            with state_lock:
                margin_v = state["margin"]
            collections_str = ", ".join(f"<b>{c}</b>" for c in TARGET_COLLECTIONS)
            self.send_message(
                f"✅ <b>Авто-офферы включены!</b>\n"
                f"Бот будет выставлять офферы на {collections_str}\n"
                f"ниже рыночного флора на <b>{margin_v} TON</b>.\n"
                f"Защита от скама активна — оффер всегда ставится относительно реального флора, "
                f"а не цены отдельного листинга."
            )
            log("Auto-offers mode ENABLED via Telegram command.")

        elif cmd == "/offers_off":
            with state_lock:
                state["offers_running"] = False
            save_config()
            self.send_message("🛑 <b>Авто-офферы выключены.</b>")
            log("Auto-offers mode DISABLED via Telegram command.")

        elif cmd == "/getgems_apikey":
            if not args:
                self.send_message(
                    "❌ Укажите API-ключ getgems.io. Пример:\n"
                    "<code>/getgems_apikey &lt;ключ&gt;</code>"
                )
                return
            new_key = " ".join(args).strip()
            with state_lock:
                state["getgems_api_key"] = new_key
            save_config()
            self.send_message("✅ API-ключ GetGems сохранён.")

        elif cmd == "/getgems_address":
            if len(args) < 2:
                self.send_message(
                    "❌ Укажите коллекцию и адрес контракта. Пример:\n"
                    "<code>/getgems_address Vice Cream EQ...</code>\n"
                    f"Доступные коллекции: {', '.join(TARGET_COLLECTIONS)}"
                )
                return
            # Address is the last token; everything before it is the collection name.
            addr = args[-1].strip()
            name_input = " ".join(args[:-1]).strip()
            match = None
            for c in TARGET_COLLECTIONS:
                if c.lower() == name_input.lower():
                    match = c
                    break
            if not match:
                self.send_message(
                    f"❌ Неизвестная коллекция «{name_input}».\n"
                    f"Доступные: {', '.join(TARGET_COLLECTIONS)}"
                )
                return
            with state_lock:
                overrides = dict(state["getgems_addresses"])
                overrides[match] = addr
                state["getgems_addresses"] = overrides
            getgems_addresses[match] = addr
            save_config()
            self.send_message(f"✅ Адрес контракта для <b>{match}</b> сохранён:\n<code>{addr}</code>")

        elif cmd == "/getgems_on":
            missing = [c for c in TARGET_COLLECTIONS if not getgems_get_address(c)]
            if missing:
                self.send_message(
                    "⚠️ Не заданы адреса контрактов для: "
                    + ", ".join(missing)
                    + ".\nЭти коллекции будут пропущены. Задайте их командой "
                    "<code>/getgems_address &lt;коллекция&gt; &lt;адрес&gt;</code>."
                )
            with state_lock:
                state["getgems_running"] = True
            save_config()
            collections_str = ", ".join(f"<b>{c}</b>" for c in TARGET_COLLECTIONS)
            self.send_message(
                f"✅ <b>Мониторинг GetGems включён!</b>\n"
                f"Считаю флор и слежу за рынком на {collections_str}.\n"
                f"⚠️ Автовыкуп на GetGems требует подписи TON-транзакции — "
                f"включите его отдельно через <code>/getgems_autobuy_on</code>."
            )
            log("GetGems monitoring ENABLED via Telegram command.")

        elif cmd == "/getgems_off":
            with state_lock:
                state["getgems_running"] = False
            save_config()
            self.send_message("🛑 <b>Мониторинг GetGems выключен.</b>")
            log("GetGems monitoring DISABLED via Telegram command.")

        elif cmd == "/getgems_wallet":
            if not args:
                self.send_message(
                    "❌ Укажите сид-фразу кошелька (12 или 24 слова) для автовыкупа GetGems.\n"
                    "⚠️ <b>Внимание:</b> фраза хранится в config.json в открытом виде — "
                    "используйте отдельный кошелёк с небольшим балансом."
                )
                return
            words = args
            if len(words) not in (12, 24):
                self.send_message("❌ Сид-фраза должна состоять из 12 или 24 слов.")
                return
            with state_lock:
                state["getgems_wallet_mnemonic"] = " ".join(words)
            save_config()
            self.send_message(
                "✅ Кошелёк GetGems сохранён.\n"
                "⚠️ Рекомендуется удалить это сообщение из чата — оно содержит сид-фразу."
            )

        elif cmd == "/getgems_maxprice":
            if not args:
                self.send_message("❌ Укажите макс. цену покупки в TON. Пример: <code>/getgems_maxprice 5</code>")
                return
            try:
                val = float(args[0])
                if val < 0:
                    self.send_message("❌ Цена не может быть отрицательной.")
                    return
                with state_lock:
                    state["getgems_max_buy_price"] = val
                save_config()
                if val == 0:
                    self.send_message("✅ Лимит сброшен в 0 — автовыкуп GetGems не будет покупать, пока не зададите лимит > 0.")
                else:
                    self.send_message(f"✅ Макс. цена автовыкупа GetGems: <b>{val} TON</b>.")
            except ValueError:
                self.send_message("❌ Неверный формат числа.")

        elif cmd == "/getgems_autobuy_on":
            with state_lock:
                has_wallet = bool(state["getgems_wallet_mnemonic"])
                cap = state["getgems_max_buy_price"]
            if not has_wallet:
                self.send_message("❌ Сначала задайте кошелёк: <code>/getgems_wallet &lt;сид-фраза&gt;</code>.")
                return
            if cap <= 0:
                self.send_message("❌ Сначала задайте лимит цены: <code>/getgems_maxprice &lt;TON&gt;</code>.")
                return
            with state_lock:
                state["getgems_autobuy"] = True
            save_config()
            self.send_message(
                f"⚡ <b>Автовыкуп GetGems ВКЛЮЧЁН.</b>\n"
                f"• Лимит цены: <b>{cap} TON</b>\n"
                f"• Бот подпишет транзакцию покупки вашим кошельком при выгодном лоте.\n"
                f"⚠️ Эндпоинты покупки GetGems не проверены — протестируйте на дешёвом лоте с низким лимитом."
            )
            log("GetGems auto-buy ENABLED via Telegram command.")

        elif cmd == "/getgems_autobuy_off":
            with state_lock:
                state["getgems_autobuy"] = False
            save_config()
            self.send_message("🛑 <b>Автовыкуп GetGems выключен.</b>")
            log("GetGems auto-buy DISABLED via Telegram command.")

        elif cmd == "/ton_apikey":
            if not args:
                self.send_message(
                    "❌ Укажите API-ключ toncenter.io. Пример:\n"
                    "<code>/ton_apikey &lt;ключ&gt;</code>"
                )
                return
            new_key = args[0].strip()
            with state_lock:
                state["ton_api_key"] = new_key
            save_config()
            self.send_message(
                "✅ TON API-ключ (toncenter) сохранён.\n"
                "Используется для проверки баланса кошелька перед автовыкупом."
            )
            log("TON API key updated via Telegram command.")

        elif cmd == "/test":
            self.send_message("⏳ Выполняю тестовый анализ рынка...")
            threading.Thread(target=self._run_market_test, daemon=True).start()

    def _run_market_test(self):
        try:
            blocks = []
            for col in TARGET_COLLECTIONS:
                listings = fetch_listings(col, count=10)
                floor = calculate_stable_floor(listings)
                floor_str = f"<b>{floor:.2f} TON</b>" if floor else "Не найдено лотов"
                cheapest = f"{float(listings[0]['salePrice'])/1e9:.2f} TON" if listings else "Нет"
                blocks.append(
                    f"<b>{col}:</b>\n"
                    f"• Стабильный флор: {floor_str}\n"
                    f"• Самый дешевый лот: <code>{cheapest}</code>"
                )

            res_text = (
                "🧪 <b>Результаты быстрого анализа (MRKT):</b>\n\n"
                + "\n\n".join(blocks)
                + "\n\n🔌 <i>Соединение с MRKT API работает корректно!</i>"
            )
            self.send_message(res_text)
        except Exception as e:
            self.send_message(f"❌ Ошибка тестирования рынка MRKT: <code>{e}</code>")

        # GetGems test (only if enabled / api key set) — isolated so MRKT result is unaffected.
        with state_lock:
            gg_enabled = state["getgems_running"] or bool(state["getgems_api_key"])
        if gg_enabled:
            try:
                gg_blocks = []
                for col in TARGET_COLLECTIONS:
                    if not getgems_get_address(col):
                        gg_blocks.append(f"<b>{col}:</b>\n• Адрес контракта не задан — пропуск")
                        continue
                    listings = getgems_fetch_listings(col, count=10)
                    floor = calculate_stable_floor(listings)
                    floor_str = f"<b>{floor:.2f} TON</b>" if floor else "Не найдено лотов"
                    cheapest = f"{float(listings[0]['salePrice'])/1e9:.2f} TON" if listings else "Нет"
                    gg_blocks.append(
                        f"<b>{col}:</b>\n"
                        f"• Флор: {floor_str}\n"
                        f"• Самый дешевый лот: <code>{cheapest}</code>"
                    )
                    time.sleep(0.3)
                self.send_message(
                    "💎 <b>Результаты анализа GetGems:</b>\n\n"
                    + "\n\n".join(gg_blocks)
                    + "\n\n🔌 <i>Соединение с GetGems работает.</i>"
                )
            except Exception as e:
                self.send_message(f"❌ Ошибка тестирования GetGems: <code>{e}</code>")

    def updates_listener_loop(self):
        log("Telegram command listener thread started.")
        while True:
            with state_lock:
                token = state["tg_bot_token"]
            if not token:
                time.sleep(3.0)
                continue

            url = f"https://api.telegram.org/bot{token}/getUpdates"
            params = {"offset": self.offset, "timeout": 15}
            try:
                log(f"[Telegram] Polling updates (offset={self.offset})...")
                r = session.session.get(url, params=params, timeout=20)
                if r is not None:
                    log(f"[Telegram] Poll returned HTTP {r.status_code}")
                    if r.status_code == 200:
                        data = r.json()
                        if data.get("ok"):
                            updates = data.get("result", [])
                            if updates:
                                log(f"[Telegram] Processing {len(updates)} updates")
                            for update in updates:
                                self.offset = update["update_id"] + 1
                                message = update.get("message")
                                if message and "text" in message:
                                    text = message["text"].strip()
                                    chat_id = message["chat"]["id"]
                                    if text.startswith("/"):
                                        parts = text.split()
                                        cmd = parts[0]
                                        args = parts[1:]
                                        log(f"[Telegram] Executing command: {cmd}")
                                        self.handle_command(cmd, args, chat_id)
                else:
                    log("[Telegram] Poll returned None (connection failed)")
            except Exception as e:
                log(f"[Telegram] Exception during poll: {e}", "ERROR")
            time.sleep(1.0)

tg_bot = TelegramBot()

# =====================================================================
# CORE API FUNCTIONS
# =====================================================================
def fetch_listings(collection_name, count=10):
    url = f"{API_BASE_URL}/gifts/saling"
    payload = {
        "collectionNames": [collection_name],
        "modelNames": [],
        "backdropNames": [],
        "symbolNames": [],
        "ordering": "Price",
        "lowToHigh": True,
        "maxPrice": None,
        "minPrice": None,
        "mintable": None,
        "number": None,
        "count": count,
        "cursor": "",
        "query": None,
        "promotedFirst": False
    }
    r = session.post(url, payload)
    if r is not None:
        if r.status_code == 200:
            return r.json().get("gifts", [])
        elif r.status_code == 429:
            raise Exception("API_429")
        else:
            raise Exception(f"API_ERROR_{r.status_code}")
    raise Exception("API_CONNECTION_FAILED")

def fetch_combined_listings(collections, count=15):
    url = f"{API_BASE_URL}/gifts/saling"
    payload = {
        "collectionNames": collections,
        "modelNames": [],
        "backdropNames": [],
        "symbolNames": [],
        "ordering": "Price",
        "lowToHigh": True,
        "maxPrice": None,
        "minPrice": None,
        "mintable": None,
        "number": None,
        "count": count,
        "cursor": "",
        "query": None,
        "promotedFirst": False
    }
    r = session.post(url, payload)
    if r is not None:
        if r.status_code == 200:
            return r.json().get("gifts", [])
        elif r.status_code == 401:
            raise Exception("API_401_UNAUTHORIZED")
        elif r.status_code == 429:
            raise Exception("API_429")
        else:
            raise Exception(f"API_ERROR_{r.status_code}")
    raise Exception("API_CONNECTION_FAILED")

def execute_buy(gift_id):
    """
    Executes a buy order for a specific gift ID.

    IMPORTANT: TGMRKT API returns HTTP 200 even when the purchase FAILS
    (e.g. insufficient balance, item already sold, session expired).
    We must inspect the JSON body to determine real success.
    """
    url = f"{API_BASE_URL}/gifts/buy"
    payload = {"Ids": [gift_id]}
    r = session.post(url, payload)
    if r is None:
        return False, "CONNECTION_ERROR"

    # Always log raw response for debugging
    try:
        raw_body = r.text[:500]  # truncate to avoid log spam
    except Exception:
        raw_body = "<unreadable>"
    log(f"[BUY] HTTP {r.status_code} | gift_id={gift_id} | body={raw_body}", "INFO")

    if r.status_code not in [200, 201]:
        # Non-200: definitely an error
        try:
            err = r.json().get("message") or raw_body
        except Exception:
            err = f"HTTP_{r.status_code}"
        return False, err

    # HTTP 200/201 — but TGMRKT embeds errors in the body too!
    # Check multiple success indicators in the JSON response.
    try:
        body = r.json()
    except Exception:
        # Can't parse JSON — treat as failure to be safe
        return False, f"JSON_PARSE_ERROR: {raw_body}"

    # If response is a list, that usually means the gifts were returned/bought
    if isinstance(body, list):
        if len(body) > 0:
            return True, "SUCCESS"
        else:
            return False, "EMPTY_RESPONSE_LIST: item may have sold out"

    # If response is a dict, check for known error/success fields
    if isinstance(body, dict):
        # Explicit success flags
        if body.get("success") is True or body.get("ok") is True:
            return True, "SUCCESS"

        # Explicit error message from the server
        err_msg = body.get("message") or body.get("error") or body.get("detail")
        if err_msg:
            return False, str(err_msg)

        # No recognizable fields — assume success if no error key
        if "success" not in body and "error" not in body and "message" not in body:
            return True, "SUCCESS"

        return False, f"UNKNOWN_RESPONSE: {raw_body}"

    # Fallback: non-empty response with 200 — assume success
    if raw_body.strip():
        return True, "SUCCESS"

    return False, "EMPTY_RESPONSE"


def execute_place_offer(collection_name, offer_price_ton):
    """
    Places a collection-wide buy offer (bid) on TGMRKT for the given collection
    at the specified price in TON.
    Returns (success: bool, message: str).
    """
    url = f"{API_BASE_URL}/orders/create"
    offer_price_ton = round(offer_price_ton, 2)
    price_nano = int(offer_price_ton * 1_000_000_000)

    payload = {
        "backdropName": None,
        "collectionName": collection_name,
        "isTransferable": False,
        "modelName": None,
        "priceMaxNanoTONs": price_nano,
        "priceMinNanoTONs": price_nano,
        "quantity": 1,
        "symbolName": None
    }
    r = session.post(url, payload)
    if r is not None:
        if r.status_code in [200, 201]:
            return True, "SUCCESS"
        try:
            err = r.json().get("message") or r.text
        except Exception:
            err = f"HTTP_{r.status_code}"
        return False, err
    return False, "CONNECTION_ERROR"


def fetch_true_floor(collection_name, count=6):
    """
    Fetches the ACTUAL market floor of a collection — the minimum price at which
    a real item is currently listed (1st cheapest item).
    Uses count=6 so we have a comfortable sample even if the cheapest is a 1-of-1 outlier.
    Returns the floor price in TON, or None if no listings found.
    """
    listings = fetch_listings(collection_name, count=count)
    if not listings:
        return None
    prices = sorted([float(g.get("salePrice", 0)) / 1e9 for g in listings if float(g.get("salePrice", 0)) > 0])
    return prices[0] if prices else None

# =====================================================================
# CRASH-PROOF FLOOR CALCULATION ALGORITHM
# =====================================================================
def calculate_stable_floor(listings):
    """
    Calculates a stable floor price ignoring deals or cheap outliers.
    Uses the median of the 2nd, 3rd, and 4th cheapest items.
    """
    if not listings:
        return None
    
    # Extract prices in TON and sort ascending
    prices = []
    for item in listings:
        price_val = item.get("salePrice")
        if price_val:
            prices.append(float(price_val) / 1e9)
    prices = sorted(prices)
    
    if not prices:
        return None
    
    n = len(prices)
    if n == 1:
        return prices[0]
    elif n == 2:
        return prices[1]
    else:
        # Get the range index 1 to 3 (which represents the 2nd, 3rd, and 4th cheapest items)
        sub = prices[1:4]
        return sorted(sub)[len(sub) // 2]

# =====================================================================
# BACKGROUND THREADS
# =====================================================================

def floor_analyzer_loop():
    """
    Refreshes stable floor cache every 60 seconds (was 6 minutes).
    Tight refresh keeps TURBO mode accurate without pre-buy verification.
    """
    log("Floor analyzer started (60s refresh cycle).")
    while True:
        try:
            computed = {}
            for col in TARGET_COLLECTIONS:
                listings = fetch_listings(col, count=10)
                computed[col] = calculate_stable_floor(listings)

            with state_lock:
                for col, floor in computed.items():
                    stable_floors[col] = floor
                state["last_analysis_time"] = datetime.now()

            summary = "  ".join(
                f"{col}={f'{floor:.3f} TON' if floor else 'Нет лотов'}"
                for col, floor in computed.items()
            )
            log(f"Floors updated: {summary}")

        except Exception as e:
            log(f"Floor analyzer error: {e}", "ERROR")
            stats["errors"] += 1

        time.sleep(60)  # refresh every 60s — was 360s


# =====================================================================
# GETGEMS FLOOR ANALYZER + ALERT SNIPER
# =====================================================================
# Mirrors the MRKT sniper but for getgems.io, on the SAME collections.
# Floor is computed with the identical calculate_stable_floor() algorithm.
# Collections without a configured contract address are skipped.

def getgems_floor_analyzer_loop():
    """Refreshes the GetGems floor cache every 60s while GetGems mode is on."""
    log("GetGems floor analyzer started (60s refresh cycle, paused until /getgems_on).")
    while True:
        with state_lock:
            active = state["getgems_running"]
        if not active:
            time.sleep(3.0)
            continue

        try:
            computed = {}
            for col in TARGET_COLLECTIONS:
                if not getgems_get_address(col):
                    continue  # skip collections without a contract address
                listings = getgems_fetch_listings(col, count=10)
                computed[col] = calculate_stable_floor(listings)
                time.sleep(0.5)  # gentle pacing between collections

            with state_lock:
                for col, floor in computed.items():
                    getgems_floors[col] = floor

            if computed:
                summary = "  ".join(
                    f"{col}={f'{floor:.3f} TON' if floor else 'Нет лотов'}"
                    for col, floor in computed.items()
                )
                log(f"[GetGems] Floors updated: {summary}")

        except Exception as e:
            err_str = str(e)
            if "GETGEMS_AUTH_REQUIRED" in err_str:
                log("[GetGems] Auth required — set API key via /getgems_apikey. Pausing GetGems mode.", "ERROR")
                tg_bot.send_message(
                    "❌ <b>GetGems:</b> требуется авторизация. Задайте API-ключ командой "
                    "<code>/getgems_apikey &lt;ключ&gt;</code> и снова включите <code>/getgems_on</code>."
                )
                with state_lock:
                    state["getgems_running"] = False
            else:
                log(f"[GetGems] Floor analyzer error: {e}", "ERROR")
                stats["errors"] += 1

        time.sleep(60)


def _getgems_buy_worker(item, col, name, price_ton, cached_floor, url):
    """Runs GetGems auto-buy in a background thread and reports the result."""
    profit = cached_floor - price_ton
    log(f"[GetGems] 🚀 Auto-buy attempt: {name} @ {price_ton:.3f} TON")
    try:
        success, msg = getgems_execute_buy(item)
    except Exception as e:
        success, msg = False, str(e)

    if success:
        stats["buys"] += 1
        log(f"[GetGems] ✅ BOUGHT: {name} @ {price_ton:.3f} TON")
        tg_bot.send_message(
            f"🎉 <b>GETGEMS: УСПЕШНЫЙ АВТОВЫКУП!</b>\n\n"
            f"• Коллекция: <b>{col}</b>\n"
            f"• NFT: <b>{name}</b>\n"
            f"• Цена: <code>{price_ton:.3f} TON</code>\n"
            f"• Флор GetGems: <code>{cached_floor:.3f} TON</code>\n"
            f"• 💰 Прибыль: <b>~{profit:.3f} TON</b>\n\n"
            f"🔗 <a href='{url}'>Открыть на GetGems</a>"
        )
    else:
        stats["errors"] += 1
        log(f"[GetGems] ❌ Auto-buy failed: {name} | {msg}", "ERROR")
        tg_bot.send_message(
            f"🚨 <b>GETGEMS: автовыкуп не удался</b>\n\n"
            f"• NFT: <b>{name}</b>\n"
            f"• Цена: <code>{price_ton:.3f} TON</code>\n"
            f"• Причина: <code>{msg}</code>\n\n"
            f"🔗 <a href='{url}'>Купить вручную</a>"
        )


def getgems_sniper_loop():
    """
    Fast-polling GetGems watcher. When a listing is priced at least `margin`
    TON below the cached stable floor it sends a Telegram alert, and — if
    GetGems auto-buy is enabled — fires a background buy in parallel.
    """
    log("GetGems sniper loop started (paused until /getgems_on).")
    current_sleep = 5.0

    while True:
        with state_lock:
            active = state["getgems_running"]
            margin_limit = state["margin"]
            poll_delay = state["getgems_delay"]

        if not active:
            time.sleep(3.0)
            continue

        try:
            for col in TARGET_COLLECTIONS:
                if not getgems_get_address(col):
                    continue
                with state_lock:
                    cached_floor = getgems_floors.get(col)
                if cached_floor is None:
                    continue

                listings = getgems_fetch_listings(col, count=5)
                stats["scans"] += len(listings)
                current_sleep = poll_delay

                for item in listings:
                    price_ton = float(item.get("salePrice", 0)) / 1e9
                    if price_ton <= 0:
                        continue
                    real_profit = cached_floor - price_ton
                    if real_profit < margin_limit:
                        continue

                    # Deduplicate by collection+price+number so we don't re-alert the same lot.
                    number = item.get("number") or "?"
                    dedup_key = f"getgems:{col}:{number}:{price_ton:.4f}"
                    with alerted_lock:
                        seen = dedup_key in alerted_ids
                    if seen:
                        continue
                    add_alerted_id(dedup_key)

                    stats["alerts"] += 1
                    name = item.get("name") or f"{col} #{number}"
                    url = item.get("url") or "https://getgems.io"
                    log(f"[GetGems] 🔔 ALERT: {name} @ {price_ton:.3f} TON (floor {cached_floor:.3f}, profit {real_profit:.3f})")
                    tg_bot.send_message(
                        f"💎 <b>GETGEMS: дешёвый лот!</b>\n\n"
                        f"• Коллекция: <b>{col}</b>\n"
                        f"• NFT: <b>{name}</b>\n"
                        f"• Цена: <code>{price_ton:.3f} TON</code>\n"
                        f"• Флор GetGems: <code>{cached_floor:.3f} TON</code>\n"
                        f"• 💸 Выгода: <b>~{real_profit:.3f} TON</b>\n\n"
                        f"🔗 <a href='{url}'>Открыть на GetGems</a>"
                    )

                    with state_lock:
                        autobuy_on = state["getgems_autobuy"]
                    if autobuy_on:
                        threading.Thread(
                            target=_getgems_buy_worker,
                            args=(item, col, name, price_ton, cached_floor, url),
                            daemon=True,
                        ).start()

                time.sleep(0.5)

        except Exception as e:
            err_str = str(e)
            if "429" in err_str:
                stats["errors"] += 1
                current_sleep = min(15.0, poll_delay * 2)
                log(f"[GetGems] 429 rate limit — backing off {current_sleep:.1f}s.", "WARN")
            elif "GETGEMS_AUTH_REQUIRED" in err_str:
                log("[GetGems] Auth required — pausing GetGems mode.", "ERROR")
                with state_lock:
                    state["getgems_running"] = False
            else:
                stats["errors"] += 1
                log(f"[GetGems] Sniper loop error: {e}", "ERROR")

        time.sleep(current_sleep)


# =====================================================================
# TURBO SNIPER — zero-latency buy execution
# =====================================================================

def _turbo_buy_worker(gift_id, gift_name, col_name, price_ton, cached_floor, photo_url, purchase_url):
    """
    Fires immediately in a dedicated thread — NO pre-verification delay.
    Buys first, reports result, never blocks the main scan loop.
    """
    profit = cached_floor - price_ton
    log(f"[TURBO] 🚀 Firing buy: {gift_name} @ {price_ton:.3f} TON (floor={cached_floor:.3f})")

    success, msg = execute_buy(gift_id)

    if success:
        stats["buys"] += 1
        log(f"[TURBO] ✅ BOUGHT: {gift_name} @ {price_ton:.3f} TON | profit ~{profit:.3f} TON")
        caption = (
            f"🎉 <b>УСПЕШНЫЙ АВТОВЫКУП!</b>\n\n"
            f"• Коллекция: <b>{col_name}</b>\n"
            f"• Подарок: <b>{gift_name}</b>\n"
            f"• Цена покупки: <code>{price_ton:.3f} TON</code>\n"
            f"• Рыночный флор: <code>{cached_floor:.3f} TON</code>\n"
            f"• 💰 Прибыль: <b>~{profit:.3f} TON</b>\n\n"
            f"🔗 <a href='{purchase_url}'>Открыть в приложении MRKT</a>"
        )
        if photo_url:
            tg_bot.send_photo(photo_url, caption)
        else:
            tg_bot.send_message(caption)
    else:
        stats["errors"] += 1
        log(f"[TURBO] ❌ FAILED: {gift_name} @ {price_ton:.3f} | reason: {msg}", "ERROR")
        # Suppress spam for "already sold" — normal race condition, expected
        skip_keywords = ("already", "sold", "EMPTY", "not found", "unavailable")
        if not any(kw in msg.lower() for kw in skip_keywords):
            caption = (
                f"🚨 <b>АВТОВЫКУП НЕ УДАЛСЯ</b>\n\n"
                f"• Коллекция: <b>{col_name}</b>\n"
                f"• Подарок: <b>{gift_name}</b>\n"
                f"• Цена: <code>{price_ton:.3f} TON</code>\n"
                f"• Ошибка: <code>{msg}</code>\n\n"
                f"🔗 <a href='{purchase_url}'>Купить вручную</a>"
            )
            if photo_url:
                tg_bot.send_photo(photo_url, caption)
            else:
                tg_bot.send_message(caption)


def sniper_loop():
    """
    TURBO fast-polling sniper.

    Speed optimizations vs old version:
      ✅  Buy fires in a background THREAD — main loop keeps scanning
      ✅  No blocking live-verification round-trip before buy
      ✅  Floor cache refreshed every 60s (was 6min)
      ✅  add_alerted_id() called BEFORE spawning thread → no duplicate buys
      ✅  Minimal sleep between cycles
    """
    log("TURBO sniper loop started.")
    current_sleep = 1.5

    while True:
        with state_lock:
            is_running   = state["running"]
            margin_limit = state["margin"]
            poll_delay   = state["delay"]
            sniper_mode  = state["sniper_mode"]
            token        = state["auth_token"]

        if not is_running:
            time.sleep(2.0)
            continue

        if not token:
            log("Sniper is running but MRKT Auth Token is missing! Pausing sniper.", "WARN")
            tg_bot.send_message("⚠️ <b>Ошибка снайпера!</b> У вас не указан авторизационный токен MRKT. Пожалуйста, введите токен с помощью команды: <code>/token &lt;токен&gt;</code>.")
            with state_lock:
                state["running"] = False
            continue

        try:
            # Combined query (only 1 request to check all target collections)
            gifts = fetch_combined_listings(TARGET_COLLECTIONS, count=30)
            stats["scans"] += len(gifts)
            
            # Reset backoff counters on success
            with state_lock:
                state["consecutive_429"] = 0
            current_sleep = poll_delay

            for gift in gifts:
                gift_id = gift.get("id")
                if not gift_id:
                    continue
                
                # Fast set checks
                with alerted_lock:
                    is_processed = gift_id in alerted_ids
                if is_processed:
                    continue

                # Safety checks (not mine, on sale, not locked)
                if gift.get("isLocked") or gift.get("isLockedForSale") or gift.get("isMine"):
                    continue
                if not gift.get("isOnSale"):
                    continue

                col_name = gift.get("collectionName")
                if col_name not in TARGET_COLLECTIONS:
                    continue

                # Get cached stable floor
                with state_lock:
                    cached_floor = stable_floors.get(col_name)
                if cached_floor is None:
                    continue

                price_ton = float(gift.get("salePrice", 0)) / 1e9
                real_profit = cached_floor - price_ton

                # Final safety check: must be profitable against cached calculated floor
                if real_profit >= margin_limit:
                    # Prevent duplicate buy actions immediately
                    add_alerted_id(gift_id)
                    
                    img_key = gift.get("modelStickerThumbnailKey")
                    photo_url = f"https://cdn.tgmrkt.io/{img_key}" if img_key else None
                    gift_number = gift.get("number", "?")
                    gift_name = gift.get("name") or f"{col_name} #{gift_number}"
                    
                    # Generate Telegram startapp one-click purchase link
                    purchase_url = f"https://t.me/mrkt/app?startapp={gift_id}"
                    
                    if sniper_mode == "buy":
                        log(f"💥 SNIPING DEAL: Spawning TURBO buy thread for {gift_name} for {price_ton:.2f} TON (Cached Floor: {cached_floor:.2f} TON, Profit: {real_profit:.2f} TON)!")
                        # Spawn background thread to buy immediately
                        threading.Thread(
                            target=_turbo_buy_worker,
                            args=(gift_id, gift_name, col_name, price_ton, cached_floor, photo_url, purchase_url),
                            daemon=True
                        ).start()
                    else:
                        # Alert-only mode
                        stats["alerts"] += 1
                        log(f"🔔 ALERT: Found cheap {gift_name} at {price_ton:.2f} TON (Cached Floor: {cached_floor:.2f} TON, Profit: {real_profit:.2f} TON)!")
                        
                        caption = (
                            f"🎁 <b>НАЙДЕН ДЕШЕВЫЙ ПОДАРК НА MRKT!</b>\n\n"
                            f"• Коллекция: <b>{col_name}</b>\n"
                            f"• Подарок: <b>{gift_name}</b>\n"
                            f"• Цена покупки: <code>{price_ton:.2f} TON</code>\n"
                            f"• Рыночный флор: <code>{cached_floor:.2f} TON</code>\n"
                            f"• 💸 Выгода: <b>~{real_profit:.2f} TON</b>\n\n"
                            f"🔗 <a href='{purchase_url}'>КУПИТЬ В 1 КЛИК НА MRKT</a>"
                        )
                        if photo_url:
                            tg_bot.send_photo(photo_url, caption)
                        else:
                            tg_bot.send_message(caption)

        except Exception as e:
            err_str = str(e)
            if "API_429" in err_str:
                stats["errors"] += 1
                with state_lock:
                    state["consecutive_429"] = min(10, state["consecutive_429"] + 1)
                    mult = state["consecutive_429"]
                
                # Apply exponential backoff
                current_sleep = min(12.0, poll_delay * (1.5 ** mult))
                log(f"Encountered API 429 rate limit. Backing off for {current_sleep:.2f} seconds...", "WARN")
            elif "API_401_UNAUTHORIZED" in err_str:
                stats["errors"] += 1
                log("MRKT Auth Token is invalid or expired! Disabling sniper.", "ERROR")
                tg_bot.send_message("❌ <b>Ошибка снайпера!</b> Ваш токен авторизации MRKT невалиден или истек. Снайпер остановлен.\n\nОбновите токен через команду: <code>/token &lt;новый_токен&gt;</code>, а затем запустите снайпер с помощью <code>/start_sniper</code>.")
                with state_lock:
                    state["running"] = False
            else:
                stats["errors"] += 1
                log(f"Sniper loop error: {e}", "ERROR")

        time.sleep(current_sleep)

# =====================================================================
# AUTO-OFFERS LOOP
# =====================================================================
def offers_loop():
    """
    Background loop that automatically places collection-wide buy offers
    (bids) on TGMRKT for target collections.

    Anti-scam logic:
    - The offer price is ALWAYS based on the REAL market floor (cheapest
      actual listing), NOT on the price of an individual item that someone
      may have inflated or listed as a scam (e.g. listed at 10 TON when
      the real floor is 2 TON).
    - The offer price = real_floor - margin, rounded to 2 decimal places.
    - Minimum offer price enforced at 0.2 TON.
    - Offers are skipped if the price hasn't shifted by more than 0.05 TON
      since the last placed offer (to avoid spamming the API).
    """
    log("Auto-offers background loop started (PAUSED until /offers_on).")

    while True:
        with state_lock:
            offers_active = state["offers_running"]
            margin = state["margin"]
            delay = state["offers_delay"]
            token = state["auth_token"]

        if not offers_active:
            time.sleep(3.0)
            continue

        if not token:
            log("[Offers] No MRKT token set — pausing offers.", "WARN")
            time.sleep(10.0)
            continue

        for col in TARGET_COLLECTIONS:
            try:
                # Step 1: Fetch the REAL floor (cheapest listed price from API)
                real_floor = fetch_true_floor(col, count=6)

                if real_floor is None or real_floor <= 0:
                    log(f"[Offers] No active listings for '{col}', skipping.", "WARN")
                    continue

                # Step 2: Compute target offer price = real floor minus margin
                # This is safe against scam listings: even if someone lists at 10 TON
                # when floor is 2 TON, our offer goes to 2 - margin, NOT 10 - margin.
                target_price = round(real_floor - margin, 2)

                # Step 3: Enforce minimum offer price
                if target_price < 0.2:
                    log(f"[Offers] Computed offer for '{col}' is {target_price:.2f} TON — below 0.2 TON minimum. Skipping.")
                    continue

                # Step 4: Skip if price hasn't changed significantly (avoid spam)
                prev_price = placed_offers.get(col)
                if prev_price is not None and abs(prev_price - target_price) < 0.05:
                    log(f"[Offers] Offer for '{col}' unchanged ({target_price:.2f} TON). Skipping re-place.")
                    continue

                # Step 5: Place the offer
                log(f"[Offers] Placing offer on '{col}' at {target_price:.2f} TON (Real floor: {real_floor:.2f} TON, Margin: {margin:.2f} TON)")
                success, msg = execute_place_offer(col, target_price)

                if success:
                    placed_offers[col] = target_price
                    stats["offers"] += 1
                    log(f"[Offers] ✅ Offer placed: '{col}' @ {target_price:.2f} TON")
                    tg_bot.send_message(
                        f"✉️ <b>Оффер выставлен!</b>\n"
                        f"• Коллекция: <b>{col}</b>\n"
                        f"• Рыночный флор: <code>{real_floor:.2f} TON</code>\n"
                        f"• Цена оффера: <b>{target_price:.2f} TON</b> (флор - {margin:.2f} TON)\n"
                        f"• 🛡️ Антискам: оффер ставится по реальному флору, "
                        f"а не по ценам отдельных листингов."
                    )
                else:
                    stats["errors"] += 1
                    log(f"[Offers] ❌ Failed to place offer on '{col}': {msg}", "ERROR")
                    tg_bot.send_message(
                        f"❌ <b>Ошибка выставления оффера</b>\n"
                        f"• Коллекция: <b>{col}</b>\n"
                        f"• Цена: <code>{target_price:.2f} TON</code>\n"
                        f"• Ошибка: <code>{msg}</code>"
                    )

            except Exception as e:
                err_str = str(e)
                if "API_429" in err_str:
                    stats["errors"] += 1
                    log(f"[Offers] 429 rate limit — backing off.", "WARN")
                    time.sleep(15.0)
                else:
                    stats["errors"] += 1
                    log(f"[Offers] Error for '{col}': {e}", "ERROR")

            # Small gap between collections to avoid burst rate limits
            time.sleep(1.5)

        # Wait for next offers cycle
        with state_lock:
            cycle_delay = state["offers_delay"]
        time.sleep(cycle_delay)


# =====================================================================
# MAIN INITIALIZATION ENTRYPOINT
# =====================================================================
def main():
    log("========================================")
    log("Starting MRKT Sniper Bot...")
    log(f"Target Collections: {', '.join(TARGET_COLLECTIONS)}")
    log("========================================")

    # 1. Load config and set initial variables
    load_config()

    # Alert the user on startup if bot token or chat ID is missing
    with state_lock:
        bot_token = state["tg_bot_token"]
        chat_id = state["tg_chat_id"]
        auth_token = state["auth_token"]

    if not bot_token:
        log("No Telegram Bot Token in config.json! Commands and alerts will not work.", "WARN")
    if not chat_id:
        log("No Telegram Chat ID in config.json! Please chat with your bot to register your ID.", "WARN")
    if not auth_token:
        log("No MRKT Authorization Token found. Please register it via Telegram /token.", "WARN")

    # 2. Launch background threads
    # Thread A: Telegram long polling listener
    threading.Thread(target=tg_bot.updates_listener_loop, daemon=True).start()

    # Thread B: 6-minute floor analyzer
    threading.Thread(target=floor_analyzer_loop, daemon=True).start()

    # Thread C: Fast polling sniper
    threading.Thread(target=sniper_loop, daemon=True).start()

    # Thread D: Auto-offers loop
    threading.Thread(target=offers_loop, daemon=True).start()

    # Thread E: GetGems floor analyzer (idle until /getgems_on)
    threading.Thread(target=getgems_floor_analyzer_loop, daemon=True).start()

    # Thread F: GetGems alert sniper (idle until /getgems_on)
    threading.Thread(target=getgems_sniper_loop, daemon=True).start()

    # Send startup message to registered chat ID
    tg_bot.send_message(
        "🤖 <b>MRKT Sniper Bot успешно запущен!</b>\n"
        "• Снайпер работает в фоновом режиме.\n"
        "• Авто-офферы: выключены (включить: /offers_on)\n"
        "• GetGems-мониторинг: выключен (включить: /getgems_on)\n"
        "• Отправьте /status для проверки текущего состояния и цен."
    )

    # 3. Keep main thread alive
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        log("Shutting down sniper bot...")

if __name__ == "__main__":
    main()
