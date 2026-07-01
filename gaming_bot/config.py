"""
config.py — Single source of truth for every setting and constant.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Dict, List


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
        here = Path(__file__).parent
        for candidate in (here / ".env", here.parent / ".env"):
            if candidate.exists():
                load_dotenv(candidate, override=False)
                return
    except ImportError:
        pass


_load_dotenv()


def _require(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        print(f"\n[FATAL] Required env var '{key}' is not set.\n", file=sys.stderr)
        sys.exit(1)
    return val

def _str(key: str, default: str) -> str:
    return os.environ.get(key, default).strip()

def _int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)).strip())
    except ValueError:
        return default

def _bool(key: str, default: bool = False) -> bool:
    return os.environ.get(key, str(default)).strip().lower() in ("1", "true", "yes")

def _decimal(key: str, default: str) -> Decimal:
    try:
        return Decimal(os.environ.get(key, default).strip())
    except Exception:
        return Decimal(default)

def _int_list(key: str) -> list[int]:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return []
    try:
        return [int(x.strip()) for x in raw.split(",") if x.strip()]
    except ValueError:
        return []


@dataclass(frozen=True)
class Settings:
    """Immutable validated configuration."""

    # Telegram
    bot_token:            str
    admin_ids:            list[int]
    drop_pending_updates: bool

    # Database
    database_url:    str
    db_pool_size:    int
    db_max_overflow: int
    db_pool_timeout: int
    db_echo:         bool

    # Security
    wallet_master_key: str

    # RPC
    eth_rpc_url:     str
    bsc_rpc_url:     str
    polygon_rpc_url: str
    solana_rpc_url:  str

    # Logging
    log_level:        str
    log_file:         str
    log_max_bytes:    int
    log_backup_count: int

    # Health
    health_host: str
    health_port: int

    # Game rules
    game_join_timeout: int    # seconds to wait for 2nd player to join
    house_fee_pct:     Decimal
    min_wager:         Decimal
    max_wager:         Decimal

    # Withdrawal fees
    withdrawal_fixed_fee: Decimal
    withdrawal_pct_fee:   Decimal

    # House wallet addresses (where fees get sent on-chain)
    house_address_eth:     str
    house_address_bsc:     str
    house_address_polygon: str
    house_address_solana:  str

    # ChangeNow swap API
    changenow_api_key:     str
    # Deposit monitoring APIs
    blockcypher_api_key:   str
    trongrid_api_key:      str
    toncenter_api_key:     str

    @property
    def rpc_urls(self) -> dict[str, str]:
        return {
            "ethereum": self.eth_rpc_url,
            "bsc":      self.bsc_rpc_url,
            "polygon":  self.polygon_rpc_url,
            "solana":   self.solana_rpc_url,
        }

    def house_address(self, network: str) -> str:
        return {
            "ethereum": self.house_address_eth,
            "bsc":      self.house_address_bsc,
            "polygon":  self.house_address_polygon,
            "solana":   self.house_address_solana,
        }.get(network, "")

    def is_admin(self, user_id: int) -> bool:
        return bool(self.admin_ids) and user_id in self.admin_ids

    def summary(self) -> str:
        hint = (self.bot_token[:8] + "…") if self.bot_token else "NOT SET"
        db   = self.database_url.split("@")[-1] if "@" in self.database_url else "N/A"
        return (f"BOT_TOKEN={hint} | DB=…@{db} pool={self.db_pool_size}+{self.db_max_overflow} | "
                f"WALLET_KEY={'SET' if self.wallet_master_key else 'NOT SET'} | LOG={self.log_level}")

    def validate(self) -> None:
        errors: list[str] = []
        if not self.bot_token or ":" not in self.bot_token:
            errors.append("BOT_TOKEN is missing or malformed")
        if self.wallet_master_key:
            try:
                from cryptography.fernet import Fernet
                Fernet(self.wallet_master_key.encode())
            except Exception as exc:
                errors.append(f"WALLET_MASTER_KEY invalid: {exc}")
        if not self.database_url.startswith("postgresql"):
            errors.append(f"DATABASE_URL must start with 'postgresql'")
        if self.min_wager >= self.max_wager:
            errors.append(f"MIN_WAGER must be < MAX_WAGER")
        if not (0 < self.house_fee_pct < 1):
            errors.append(f"HOUSE_FEE_PCT must be between 0 and 1")
        if errors:
            raise ValueError("Config errors:\n" + "\n".join(f"  • {e}" for e in errors))


def _load_settings() -> Settings:
    s = Settings(
        bot_token             = _require("BOT_TOKEN"),
        admin_ids             = _int_list("ADMIN_IDS"),
        drop_pending_updates  = _bool("DROP_PENDING_UPDATES", True),

        database_url    = _str("DATABASE_URL", "postgresql+asyncpg://botuser:changeme@localhost:5432/gaming_bot"),
        db_pool_size    = _int("DB_POOL_SIZE",    10),
        db_max_overflow = _int("DB_MAX_OVERFLOW", 20),
        db_pool_timeout = _int("DB_POOL_TIMEOUT", 30),
        db_echo         = _bool("DB_ECHO", False),

        wallet_master_key = _str("WALLET_MASTER_KEY", ""),

        eth_rpc_url     = _str("ETH_RPC_URL",     "https://rpc.ankr.com/eth"),
        bsc_rpc_url     = _str("BSC_RPC_URL",     "https://bsc-dataseed.binance.org/"),
        polygon_rpc_url = _str("POLYGON_RPC_URL", "https://polygon-rpc.com"),
        solana_rpc_url  = _str("SOLANA_RPC_URL",  "https://api.mainnet-beta.solana.com"),

        log_level        = _str("LOG_LEVEL", "INFO").upper(),
        log_file         = _str("LOG_FILE",  ""),
        log_max_bytes    = _int("LOG_MAX_BYTES", 10 * 1024 * 1024),
        log_backup_count = _int("LOG_BACKUP_COUNT", 5),

        health_host = _str("HEALTH_HOST", "0.0.0.0"),
        health_port = _int("PORT", _int("HEALTH_PORT", 8080)),

        game_join_timeout = _int("GAME_JOIN_TIMEOUT", 120),
        house_fee_pct     = _decimal("HOUSE_FEE_PCT", "0.05"),
        min_wager         = _decimal("MIN_WAGER",     "0.01"),
        max_wager         = _decimal("MAX_WAGER",     "10000"),

        withdrawal_fixed_fee = _decimal("WITHDRAWAL_FIXED_FEE", "0.10"),
        withdrawal_pct_fee   = _decimal("WITHDRAWAL_PCT_FEE",   "0.03"),

        house_address_eth     = _str("HOUSE_ADDRESS_ETH",     ""),
        house_address_bsc     = _str("HOUSE_ADDRESS_BSC",     ""),
        house_address_polygon = _str("HOUSE_ADDRESS_POLYGON", ""),
        house_address_solana  = _str("HOUSE_ADDRESS_SOLANA",  ""),

        changenow_api_key     = _str("CHANGENOW_API_KEY", ""),
        blockcypher_api_key   = _str("BLOCKCYPHER_API_KEY", ""),
        trongrid_api_key      = _str("TRONGRID_API_KEY", ""),
        toncenter_api_key     = _str("TONCENTER_API_KEY", ""),
    )
    try:
        s.validate()
    except ValueError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        sys.exit(1)
    return s


cfg: Settings = _load_settings()


# ── Supported coins (display grid) ───────────────────────────────────────────
# Each coin: emoji, display name, which network to generate a wallet on,
# the token symbol used internally, and ERC-20 contract address (None = native)
COINS: dict[str, dict] = {
    # ── Row 1: Big chains ──────────────────────────────────────────────────────
    "BTC":   {"emoji": "₿",  "name": "Bitcoin",       "network": "bitcoin",   "token": "BTC",  "address": None},
    "ETH":   {"emoji": "🔷", "name": "Ethereum",      "network": "ethereum",  "token": "ETH",  "address": None},
    # ── Row 2: Stablecoins ─────────────────────────────────────────────────────
    "USDT":  {"emoji": "💚", "name": "USDT (BSC)",    "network": "bsc",       "token": "USDT",
              "address": "0x55d398326f99059fF775485246999027B3197955"},
    "USDC":  {"emoji": "🔵", "name": "USDC (ETH)",    "network": "ethereum",  "token": "USDC",
              "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"},
    # ── Row 3 ──────────────────────────────────────────────────────────────────
    "LTC":   {"emoji": "🩶", "name": "Litecoin",      "network": "litecoin",  "token": "LTC",  "address": None},
    "SOL":   {"emoji": "🟢", "name": "Solana",        "network": "solana",    "token": "SOL",  "address": None},
    # ── Row 4 ──────────────────────────────────────────────────────────────────
    "BNB":   {"emoji": "🟡", "name": "BNB",           "network": "bsc",       "token": "BNB",  "address": None},
    "TRX":   {"emoji": "🔴", "name": "Tron",          "network": "tron",      "token": "TRX",  "address": None},
    # ── Row 5 ──────────────────────────────────────────────────────────────────
    "XMR":   {"emoji": "🧡", "name": "Monero",        "network": "monero",    "token": "XMR",  "address": None},
    "DAI":   {"emoji": "💛", "name": "DAI (ETH)",     "network": "ethereum",  "token": "DAI",
              "address": "0x6B175474E89094C44Da98b954EedeAC495271d0F"},
    # ── Row 6 ──────────────────────────────────────────────────────────────────
    "DOGE":  {"emoji": "🐕", "name": "Dogecoin",      "network": "dogecoin",  "token": "DOGE", "address": None},
    "SHIB":  {"emoji": "🔥", "name": "Shiba Inu",     "network": "ethereum",  "token": "SHIB",
              "address": "0x95aD61b0a150d79219dCF64E1E6Cc01f0B64C4cE"},
    # ── Row 7 ──────────────────────────────────────────────────────────────────
    "BCH":   {"emoji": "💚", "name": "Bitcoin Cash",  "network": "bitcoincash","token": "BCH", "address": None},
    "MATIC": {"emoji": "🟣", "name": "Polygon",       "network": "polygon",   "token": "MATIC","address": None},
    # ── Row 8 ──────────────────────────────────────────────────────────────────
    "TON":   {"emoji": "💎", "name": "Toncoin",       "network": "ton",       "token": "TON",  "address": None},
}

# ── Multi-network coins — show network picker on deposit ─────────────────────
# coin_symbol → list of {network, label, token_address}
MULTI_NETWORK_COINS: dict[str, list[dict]] = {
    "USDT": [
        {"network": "ethereum",   "label": "USDT Ethereum (ERC-20)", "address": "0xdAC17F958D2ee523a2206206994597C13D831ec7"},
        {"network": "bsc",        "label": "USDT BSC (BEP-20)",      "address": "0x55d398326f99059fF775485246999027B3197955"},
        {"network": "tron",       "label": "USDT Tron (TRC-20)",     "address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"},
        {"network": "solana",     "label": "USDT Solana (SPL)",      "address": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"},
        {"network": "polygon",    "label": "USDT Polygon",           "address": "0xc2132D05D31c914a87C6611C10748AEb04B58e8F"},
    ],
    "USDC": [
        {"network": "ethereum",   "label": "USDC Ethereum (ERC-20)", "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"},
        {"network": "bsc",        "label": "USDC BSC (BEP-20)",      "address": "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d"},
        {"network": "solana",     "label": "USDC Solana (SPL)",      "address": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"},
        {"network": "polygon",    "label": "USDC Polygon",           "address": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"},
    ],
    "DAI": [
        {"network": "ethereum",   "label": "DAI Ethereum (ERC-20)",  "address": "0x6B175474E89094C44Da98b954EedeAC495271d0F"},
        {"network": "polygon",    "label": "DAI Polygon",            "address": "0x8f3Cf7ad23Cd3CaDbD9735AFf958023239c6A063"},
    ],
    "BNB": [
        {"network": "bsc",        "label": "BNB Chain (native)",     "address": None},
        {"network": "ethereum",   "label": "BNB Ethereum (ERC-20)",  "address": "0xB8c77482e45F1F44dE1745F52C74426C631bDD52"},
    ],
}

# ── Networks (for wallet generation) ─────────────────────────────────────────
NETWORKS: dict[str, dict] = {
    # EVM
    "ethereum":   {"label": "Ethereum 🔷",    "native": "ETH",   "rpc": cfg.eth_rpc_url},
    "bsc":        {"label": "BNB Chain 🟡",   "native": "BNB",   "rpc": cfg.bsc_rpc_url},
    "polygon":    {"label": "Polygon 🟣",     "native": "MATIC", "rpc": cfg.polygon_rpc_url},
    # Solana
    "solana":     {"label": "Solana 🟢",      "native": "SOL",   "rpc": cfg.solana_rpc_url},
    # UTXO (no RPC needed for address generation)
    "bitcoin":    {"label": "Bitcoin ₿",      "native": "BTC",   "rpc": ""},
    "litecoin":   {"label": "Litecoin 🩶",    "native": "LTC",   "rpc": ""},
    "dogecoin":   {"label": "Dogecoin 🐕",    "native": "DOGE",  "rpc": ""},
    "bitcoincash":{"label": "Bitcoin Cash 💚", "native": "BCH",   "rpc": ""},
    "tron":       {"label": "Tron 🔴",        "native": "TRX",   "rpc": ""},
    # Other
    "monero":     {"label": "Monero 🧡",      "native": "XMR",   "rpc": ""},
    "ton":        {"label": "Toncoin 💎",     "native": "TON",   "rpc": ""},
}

# ── Game types ────────────────────────────────────────────────────────────────
GAME_TYPES: dict[str, dict] = {
    "dice":    {"label": "🎲 Dice",    "emoji": "🎲", "description": "Roll the dice — highest wins"},
    "bowling": {"label": "🎳 Bowling", "emoji": "🎳", "description": "Bowl a strike — highest pins wins"},
    "darts":   {"label": "🎯 Darts",   "emoji": "🎯", "description": "3 throws each — highest total wins"},
}

GAME_MODES: dict[str, dict] = {
    "normal":       {"label": "Normal",       "description": "Highest score wins"},
    "crazy":        {"label": "Crazy 🤪",     "description": "Lowest score wins"},
    "double":       {"label": "Double ×2",    "description": "2 rolls each, scores added"},
    "double_crazy": {"label": "Double Crazy", "description": "2 rolls each, lowest total wins"},
}

# USD wager presets — all bets are in USD, paid out in chosen coin
PRESET_WAGERS: list[str] = ["0.50", "1", "2", "5", "10", "25", "50", "100"]

# ── ConversationHandler states ────────────────────────────────────────────────
class State(int, Enum):
    # Wallet
    DEPOSIT_COIN      = 10
    WITHDRAW_COIN     = 11
    WITHDRAW_ADDRESS  = 12
    WITHDRAW_AMOUNT   = 13
    WITHDRAW_CONFIRM  = 14
    # Profile
    PROFILE_MENU      = 20
    # Tip
    TIP_USER          = 30
    TIP_AMOUNT        = 31
    TIP_CONFIRM       = 32
    # Promo
    PROMO_ENTER       = 40
    # Admin
    ADMIN_MENU        = 50
    ADMIN_SET_HOUSE   = 51
    ADMIN_SET_REFERRAL= 52
    ADMIN_SET_PROMO   = 53
    ADMIN_SET_TIP     = 54

# ── Callback prefixes ─────────────────────────────────────────────────────────
CB_BACK_MAIN     = "back:main"
CB_CANCEL        = "cancel"

CB_MENU_PROFILE  = "menu:profile"
CB_MENU_WALLET   = "menu:wallet"
CB_MENU_HELP     = "menu:help"
CB_MENU_REFERRAL = "menu:referral"

CB_PROFILE_HISTORY    = "profile:history"
CB_PROFILE_LEADERBOARD= "profile:leaderboard"
CB_PROFILE_TRANSFER   = "profile:transfer"
CB_PROFILE_SETTINGS   = "profile:settings"
CB_PROFILE_REFERRAL   = "profile:referral"

CB_WALLET_DEPOSIT  = "wallet:deposit"
CB_WALLET_WITHDRAW = "wallet:withdraw"
CB_WALLET_REFRESH  = "wallet:refresh"
CB_WALLET_TIP      = "wallet:tip"
CB_WALLET_PROMO    = "wallet:promo"

CB_COIN_PREFIX   = "coin:"      # coin:ETH, coin:SOL, etc.
CB_NET_PREFIX    = "net:"       # net:ethereum, etc.

CB_GAME_PREFIX   = "game:"      # game:dice
CB_MODE_PREFIX   = "mode:"      # mode:normal
CB_WAGER_PREFIX  = "wager:"     # wager:1.00
CB_WAGER_CUSTOM  = "wager:custom"
CB_GAME_JOIN     = "game:join:"  # game:join:<game_id>
CB_GAME_CANCEL   = "game:cancel"

CB_ADMIN_PREFIX       = "admin:"
CB_ADMIN_BOT_TOGGLE   = "admin:bot_toggle"
CB_ADMIN_HOUSE_ADDR   = "admin:house_addr"
CB_ADMIN_REFERRAL_AMT = "admin:referral_amt"
CB_ADMIN_ADD_PROMO    = "admin:add_promo"
CB_ADMIN_LIST_PROMOS  = "admin:list_promos"
CB_ADMIN_TIP_LIMITS   = "admin:tip_limits"
CB_ADMIN_STATS        = "admin:stats"

# ── Runtime admin state (mutable, lives in bot_data) ─────────────────────────
# Default values — admin can change all of these live via /admin panel
DEFAULT_ADMIN_STATE: dict = {
    "bot_betting_enabled": False,    # admin toggles this
    "referral_bonus":      Decimal("0.50"),   # paid to referrer per new user
    "promo_codes":         {},        # code → {"bonus": Decimal, "uses_left": int}
    "tip_min":             Decimal("0.01"),
    "tip_max":             Decimal("100"),
    "house_addresses": {
        "ethereum": cfg.house_address_eth,
        "bsc":      cfg.house_address_bsc,
        "polygon":  cfg.house_address_polygon,
        "solana":   cfg.house_address_solana,
    },
}

# Convenience aliases
HOUSE_FEE_PCT = cfg.house_fee_pct
MIN_WAGER     = cfg.min_wager
MAX_WAGER     = cfg.max_wager
