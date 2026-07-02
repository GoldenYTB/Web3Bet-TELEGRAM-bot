"""
persistence.py — Save and load user data to/from Neon PostgreSQL.

This bridges the in-memory Store (fast, used during gameplay) with
the Neon database (persistent, survives restarts).

Strategy:
  - On startup: load all users from DB into Store
  - On deposit confirmed: save balance to DB
  - On withdrawal: save balance to DB  
  - On game result: save balance to DB
  - Every 60 seconds: flush all dirty users to DB (background task)

Tables used:
  users         — telegram_id, username, usd_balance, preferred_coin
  wallets       — telegram_id, coin_symbol, address, encrypted_key
  user_stats    — telegram_id, games_played, games_won, total_wagered, total_won, biggest_win
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from decimal import Decimal
from typing import Optional, Set

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

# Users that need to be saved to DB
_dirty: Set[int] = set()


# ── Schema (created alongside existing tables) ────────────────────────────────

CREATE_USERS_EXT = """
CREATE TABLE IF NOT EXISTS users_ext (
    telegram_id    BIGINT PRIMARY KEY,
    username       TEXT,
    first_name     TEXT,
    usd_balance    NUMERIC(20, 4) DEFAULT 0,
    preferred_coin TEXT DEFAULT '',
    referred_by    BIGINT DEFAULT 0,
    referral_count INT DEFAULT 0,
    registered_at  DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW()),
    updated_at     DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
);
"""

CREATE_WALLETS = """
CREATE TABLE IF NOT EXISTS user_wallets (
    telegram_id     BIGINT NOT NULL,
    coin_symbol     TEXT NOT NULL,
    network         TEXT NOT NULL,
    address         TEXT NOT NULL,
    encrypted_key   TEXT NOT NULL,
    created_at      DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW()),
    PRIMARY KEY (telegram_id, coin_symbol, network)
);
"""

CREATE_STATS = """
CREATE TABLE IF NOT EXISTS user_stats (
    telegram_id    BIGINT PRIMARY KEY,
    games_played   INT DEFAULT 0,
    games_won      INT DEFAULT 0,
    total_wagered  NUMERIC(20, 4) DEFAULT 0,
    total_won      NUMERIC(20, 4) DEFAULT 0,
    biggest_win    NUMERIC(20, 4) DEFAULT 0,
    favourite_game TEXT DEFAULT '',
    coin_holdings  TEXT DEFAULT '{}',
    updated_at     DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
);
"""

CREATE_HOUSE = """
CREATE TABLE IF NOT EXISTS house_state (
    id             INT PRIMARY KEY DEFAULT 1,
    usd_balance    NUMERIC(20, 4) DEFAULT 0,
    total_volume   NUMERIC(20, 4) DEFAULT 0,
    total_rake     NUMERIC(20, 4) DEFAULT 0,
    updated_at     DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
);
INSERT INTO house_state (id, usd_balance) VALUES (1, 0)
ON CONFLICT (id) DO NOTHING;
"""


async def create_persistence_tables(engine: AsyncEngine) -> None:
    """Create persistence tables if they don't exist."""
    async with engine.begin() as conn:
        await conn.execute(text(CREATE_USERS_EXT))
        await conn.execute(text(CREATE_WALLETS))
        await conn.execute(text(CREATE_STATS))
        await conn.execute(text(CREATE_HOUSE))
    logger.info("Persistence tables ready")


async def load_all_users(engine: AsyncEngine, store) -> int:
    """Load all users from DB into the in-memory Store. Returns count."""
    count = 0
    async with engine.connect() as conn:
        # Load users
        rows = await conn.execute(text(
            "SELECT telegram_id, username, first_name, usd_balance, "
            "preferred_coin, referred_by, referral_count, registered_at "
            "FROM users_ext"
        ))
        for row in rows:
            u, created = store.get_or_create_user(
                int(row.telegram_id),
                row.username or "",
                row.first_name or "",
            )
            u.usd_balance      = Decimal(str(row.usd_balance or 0))
            u.preferred_coin   = row.preferred_coin or ""
            u.referred_by      = int(row.referred_by or 0)
            u.referral_count   = int(row.referral_count or 0)
            u.registered_at    = float(row.registered_at or time.time())
            count += 1

        # Load stats
        stat_rows = await conn.execute(text(
            "SELECT telegram_id, games_played, games_won, total_wagered, "
            "total_won, biggest_win, favourite_game, coin_holdings "
            "FROM user_stats"
        ))
        for row in stat_rows:
            u = store.get_user(int(row.telegram_id))
            if u:
                u.games_played   = int(row.games_played or 0)
                u.games_won      = int(row.games_won or 0)
                u.total_wagered  = Decimal(str(row.total_wagered or 0))
                u.total_won      = Decimal(str(row.total_won or 0))
                u.biggest_win    = Decimal(str(row.biggest_win or 0))
                u.favourite_game = row.favourite_game or ""
                try:
                    u.coin_holdings = {
                        k: Decimal(str(v))
                        for k, v in json.loads(row.coin_holdings or "{}").items()
                    }
                except Exception:
                    u.coin_holdings = {}

        # Load wallet addresses
        wallet_rows = await conn.execute(text(
            "SELECT telegram_id, coin_symbol, network, address, encrypted_key "
            "FROM user_wallets"
        ))
        for row in wallet_rows:
            u = store.get_user(int(row.telegram_id))
            if u:
                addr_key = f"{row.coin_symbol}:{row.network}"
                u.deposit_addresses[addr_key] = row.address
                u.deposit_keys[addr_key]      = row.encrypted_key

        # Load house state
        house_row = await conn.execute(text(
            "SELECT usd_balance, total_volume, total_rake FROM house_state WHERE id=1"
        ))
        h = house_row.fetchone()
        if h:
            store.house_balance_usd      = Decimal(str(h.usd_balance or 0))
            store.total_volume_usd       = Decimal(str(h.total_volume or 0))
            store.total_rake_collected   = Decimal(str(h.total_rake or 0))

    logger.info("Loaded %d users from database", count)
    return count


def mark_dirty(user_id: int) -> None:
    """Mark a user as needing to be saved."""
    _dirty.add(user_id)


async def save_user(engine: AsyncEngine, user) -> None:
    """Save a single user to DB."""
    async with engine.begin() as conn:
        await conn.execute(text("""
            INSERT INTO users_ext
                (telegram_id, username, first_name, usd_balance,
                 preferred_coin, referred_by, referral_count, registered_at, updated_at)
            VALUES
                (:tid, :username, :first_name, :bal,
                 :pref_coin, :referred_by, :ref_count, :reg_at, :now)
            ON CONFLICT (telegram_id) DO UPDATE SET
                username       = EXCLUDED.username,
                first_name     = EXCLUDED.first_name,
                usd_balance    = EXCLUDED.usd_balance,
                preferred_coin = EXCLUDED.preferred_coin,
                referred_by    = EXCLUDED.referred_by,
                referral_count = EXCLUDED.referral_count,
                updated_at     = EXCLUDED.updated_at
        """), {
            "tid":         user.telegram_id,
            "username":    user.username,
            "first_name":  user.first_name,
            "bal":         str(user.usd_balance),
            "pref_coin":   user.preferred_coin,
            "referred_by": user.referred_by,
            "ref_count":   user.referral_count,
            "reg_at":      user.registered_at,
            "now":         time.time(),
        })

        await conn.execute(text("""
            INSERT INTO user_stats
                (telegram_id, games_played, games_won, total_wagered,
                 total_won, biggest_win, favourite_game, coin_holdings, updated_at)
            VALUES
                (:tid, :gp, :gw, :tw, :twon, :bw, :fg, :ch, :now)
            ON CONFLICT (telegram_id) DO UPDATE SET
                games_played   = EXCLUDED.games_played,
                games_won      = EXCLUDED.games_won,
                total_wagered  = EXCLUDED.total_wagered,
                total_won      = EXCLUDED.total_won,
                biggest_win    = EXCLUDED.biggest_win,
                favourite_game = EXCLUDED.favourite_game,
                coin_holdings  = EXCLUDED.coin_holdings,
                updated_at     = EXCLUDED.updated_at
        """), {
            "tid":  user.telegram_id,
            "gp":   user.games_played,
            "gw":   user.games_won,
            "tw":   str(user.total_wagered),
            "twon": str(user.total_won),
            "bw":   str(user.biggest_win),
            "fg":   user.favourite_game,
            "ch":   json.dumps({k: str(v) for k, v in user.coin_holdings.items()}),
            "now":  time.time(),
        })


async def save_wallet(engine: AsyncEngine, user_id: int,
                      coin_symbol: str, network: str,
                      address: str, encrypted_key: str) -> None:
    """Save a wallet address to DB."""
    async with engine.begin() as conn:
        await conn.execute(text("""
            INSERT INTO user_wallets
                (telegram_id, coin_symbol, network, address, encrypted_key)
            VALUES (:tid, :coin, :net, :addr, :enc_key)
            ON CONFLICT (telegram_id, coin_symbol, network) DO UPDATE SET
                address       = EXCLUDED.address,
                encrypted_key = EXCLUDED.encrypted_key
        """), {
            "tid":     user_id,
            "coin":    coin_symbol,
            "net":     network,
            "addr":    address,
            "enc_key": encrypted_key,
        })


async def save_house(engine: AsyncEngine, store) -> None:
    """Save house balance to DB."""
    async with engine.begin() as conn:
        await conn.execute(text("""
            UPDATE house_state SET
                usd_balance  = :bal,
                total_volume = :vol,
                total_rake   = :rake,
                updated_at   = :now
            WHERE id = 1
        """), {
            "bal":  str(store.house_balance_usd),
            "vol":  str(store.total_volume_usd),
            "rake": str(store.total_rake_collected),
            "now":  time.time(),
        })


async def flush_dirty(engine: AsyncEngine, store) -> None:
    """Save all dirty users to DB. Called every 60 seconds."""
    if not _dirty:
        return
    to_save = list(_dirty)
    _dirty.clear()
    saved = 0
    for user_id in to_save:
        user = store.get_user(user_id)
        if user:
            try:
                await save_user(engine, user)
                saved += 1
            except Exception as exc:
                logger.warning("Failed to save user %d: %s", user_id, exc)
                _dirty.add(user_id)  # retry next flush
    if saved:
        logger.debug("Flushed %d users to DB", saved)
    # Always save house state
    try:
        await save_house(engine, store)
    except Exception as exc:
        logger.warning("Failed to save house state: %s", exc)


async def auto_flush_loop(engine: AsyncEngine, store, interval: int = 60) -> None:
    """Background task — flush dirty users every `interval` seconds."""
    while True:
        await asyncio.sleep(interval)
        try:
            await flush_dirty(engine, store)
        except Exception as exc:
            logger.error("Auto-flush error: %s", exc)
