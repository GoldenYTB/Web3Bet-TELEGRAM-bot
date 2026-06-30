"""
config.py — Single source of truth for every setting and constant.

All other modules import from here.  Nothing else reads os.environ directly.

Exports
-------
  cfg           — validated Settings singleton (loaded once at import time)
  NETWORKS      — supported blockchain networks dict
  TOKENS_BY_NETWORK — token list per network
  GAME_TYPES    — game type metadata
  PRESET_WAGERS — quick-pick amounts for the wager keyboard
  State         — ConversationHandler integer states
  CB_*          — callback_data prefix constants
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path


# ── .env loader ───────────────────────────────────────────────────────────────

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


# ── Env helpers ───────────────────────────────────────────────────────────────

def _require(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        print(
            f"\n[FATAL] Required env var '{key}' is not set.\n"
            "        Copy .env.example → .env and fill in all required values.\n",
            file=sys.stderr,
        )
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


# ── Settings dataclass ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Settings:
    """Immutable, validated configuration loaded from environment variables."""

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

    # RPC endpoints
    eth_rpc_url:     str
    bsc_rpc_url:     str
    polygon_rpc_url: str
    solana_rpc_url:  str

    # Logging
    log_level:        str
    log_file:         str
    log_max_bytes:    int
    log_backup_count: int

    # Health check
    health_host: str
    health_port: int

    # Game rules
    matchmaking_ttl:  int
    house_fee_pct:    Decimal
    min_wager:        Decimal
    max_wager:        Decimal

    # Withdrawal fees
    withdrawal_fixed_fee: Decimal
    withdrawal_pct_fee:   Decimal

    # ── Derived ───────────────────────────────────────────────────────────────

    @property
    def rpc_urls(self) -> dict[str, str]:
        return {
            "ethereum": self.eth_rpc_url,
            "bsc":      self.bsc_rpc_url,
            "polygon":  self.polygon_rpc_url,
            "solana":   self.solana_rpc_url,
        }

    def is_admin(self, user_id: int) -> bool:
        return not self.admin_ids or user_id in self.admin_ids

    def summary(self) -> str:
        token_hint = (self.bot_token[:8] + "…") if self.bot_token else "NOT SET"
        db_hint    = self.database_url.split("@")[-1] if "@" in self.database_url else "N/A"
        key_hint   = "SET ✓" if self.wallet_master_key else "NOT SET ✗"
        return (
            f"BOT_TOKEN={token_hint} | DB=…@{db_hint} "
            f"pool={self.db_pool_size}+{self.db_max_overflow} | "
            f"WALLET_KEY={key_hint} | LOG={self.log_level} | "
            f"HEALTH={self.health_host}:{self.health_port}"
        )

    def validate(self) -> None:
        errors: list[str] = []
        if not self.bot_token or ":" not in self.bot_token:
            errors.append("BOT_TOKEN is missing or malformed (expected: 123456:ABC…)")
        if self.wallet_master_key:
            try:
                from cryptography.fernet import Fernet
                Fernet(self.wallet_master_key.encode())
            except Exception as exc:
                errors.append(f"WALLET_MASTER_KEY is not a valid Fernet key: {exc}")
        if not self.database_url.startswith("postgresql"):
            errors.append(
                f"DATABASE_URL must start with 'postgresql' (got: {self.database_url[:30]}…)"
            )
        if self.min_wager >= self.max_wager:
            errors.append(f"MIN_WAGER ({self.min_wager}) must be < MAX_WAGER ({self.max_wager})")
        if not (0 < self.house_fee_pct < 1):
            errors.append(f"HOUSE_FEE_PCT must be between 0 and 1 (got {self.house_fee_pct})")
        if errors:
            raise ValueError("Config errors:\n" + "\n".join(f"  • {e}" for e in errors))


def _load_settings() -> Settings:
    s = Settings(
        bot_token             = _require("BOT_TOKEN"),
        admin_ids             = _int_list("ADMIN_IDS"),
        drop_pending_updates  = _bool("DROP_PENDING_UPDATES", True),

        database_url    = _str("DATABASE_URL",
                               "postgresql+asyncpg://botuser:changeme@localhost:5432/gaming_bot"),
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
        # Render injects PORT automatically — fall back to HEALTH_PORT for local dev
        health_port = _int("PORT", _int("HEALTH_PORT", 8080)),

        matchmaking_ttl  = _int("MATCHMAKING_TTL", 300),
        house_fee_pct    = _decimal("HOUSE_FEE_PCT", "0.05"),
        min_wager        = _decimal("MIN_WAGER",     "1"),
        max_wager        = _decimal("MAX_WAGER",     "10000"),

        withdrawal_fixed_fee = _decimal("WITHDRAWAL_FIXED_FEE", "0.10"),
        withdrawal_pct_fee   = _decimal("WITHDRAWAL_PCT_FEE",   "0.03"),
    )
    try:
        s.validate()
    except ValueError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        sys.exit(1)
    return s


# ── Singleton — loaded once at import time ────────────────────────────────────
cfg: Settings = _load_settings()


# ── Network / token constants (populated from cfg so RPC URLs are live) ───────

NETWORKS: dict[str, dict] = {
    "ETH":     {"label": "Ethereum 🔷",  "native": "ETH",   "rpc": cfg.eth_rpc_url},
    "BSC":     {"label": "BNB Chain 🟡", "native": "BNB",   "rpc": cfg.bsc_rpc_url},
    "POLYGON": {"label": "Polygon 🟣",   "native": "MATIC", "rpc": cfg.polygon_rpc_url},
    "SOLANA":  {"label": "Solana 🟢",    "native": "SOL",   "rpc": cfg.solana_rpc_url},
}

TOKENS_BY_NETWORK: dict[str, list[dict]] = {
    "ETH": [
        {"symbol": "ETH",  "name": "Ethereum",  "decimals": 18, "address": None},
        {"symbol": "USDT", "name": "Tether USD", "decimals": 6,
         "address": "0xdAC17F958D2ee523a2206206994597C13D831ec7"},
    ],
    "BSC": [
        {"symbol": "BNB",  "name": "BNB",        "decimals": 18, "address": None},
        {"symbol": "USDT", "name": "Tether USD", "decimals": 18,
         "address": "0x55d398326f99059fF775485246999027B3197955"},
    ],
    "POLYGON": [
        {"symbol": "MATIC", "name": "Polygon",   "decimals": 18, "address": None},
        {"symbol": "USDT",  "name": "Tether USD","decimals": 6,
         "address": "0xc2132D05D31c914a87C6611C10748AEb04B58e8F"},
    ],
    "SOLANA": [
        {"symbol": "SOL",  "name": "Solana",     "decimals": 9,  "address": None},
        {"symbol": "USDT", "name": "Tether USD", "decimals": 6,
         "address": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"},
    ],
}

PRESET_WAGERS: list[str] = ["1", "5", "10", "50", "100", "500"]

GAME_TYPES: dict[str, dict] = {
    "dice":    {"label": "🎲 Dice",    "description": "Roll 1–6, highest wins"},
    "bowling": {"label": "🎳 Bowling", "description": "Score 0–300, highest wins"},
}

# ── ConversationHandler states ────────────────────────────────────────────────

class State(int, Enum):
    WALLET_MENU        = 10
    DEPOSIT_SELECT_NET = 11
    WITHDRAW_ADDRESS   = 12
    WITHDRAW_AMOUNT    = 13
    WITHDRAW_CONFIRM   = 14
    LOBBY_GAME_TYPE    = 20
    LOBBY_NETWORK      = 21
    LOBBY_TOKEN        = 22
    LOBBY_WAGER        = 23
    LOBBY_CONFIRM      = 24

# ── Callback data prefixes ────────────────────────────────────────────────────

CB_MENU_WALLET      = "menu:wallet"
CB_MENU_PLAY        = "menu:play"
CB_MENU_LEADERBOARD = "menu:leaderboard"
CB_MENU_HELP        = "menu:help"
CB_BACK_MAIN        = "back:main"
CB_CANCEL           = "cancel"

CB_WALLET_DEPOSIT   = "wallet:deposit"
CB_WALLET_WITHDRAW  = "wallet:withdraw"
CB_WALLET_REFRESH   = "wallet:refresh"

CB_LOBBY_GAME    = "lobby:game:"
CB_LOBBY_NETWORK = "lobby:net:"
CB_LOBBY_TOKEN   = "lobby:token:"
CB_LOBBY_WAGER   = "lobby:wager:"
CB_LOBBY_CUSTOM  = "lobby:wager:custom"
CB_LOBBY_CONFIRM = "lobby:confirm"
CB_LOBBY_CANCEL  = "lobby:cancel"

CB_MATCH_CANCEL  = "match:cancel"

# ── Convenience aliases used by handlers ──────────────────────────────────────
HOUSE_FEE_PCT   = cfg.house_fee_pct
MIN_WAGER       = cfg.min_wager
MAX_WAGER       = cfg.max_wager
MATCHMAKING_TTL = cfg.matchmaking_ttl
