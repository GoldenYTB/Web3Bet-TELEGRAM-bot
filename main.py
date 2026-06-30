"""
main.py — Application entry point.
====================================
Run from the project ROOT (one level above gaming_bot/):

    python main.py

Layout
------
    main.py            ← this file (at root)
    gaming_bot/
        __init__.py
        config.py      ← settings, constants, env-var validation
        models.py      ← SQLAlchemy ORM + in-memory runtime dataclasses
        database.py    ← async DB pool + TransactionManager (SELECT FOR UPDATE)
        wallet.py      ← key encryption, wallet generation, withdrawal pipeline
        blockchain.py  ← deposit monitoring, confirmation tracking, price feed
        games.py       ← provably-fair game engine (dice + bowling)
        telegram.py    ← all PTB handlers, keyboards, health check, logging

Startup sequence
----------------
 1. Load .env → validate all settings (exits immediately on missing required vars)
 2. Configure structured logging (console + optional rotating file)
 3. Create SQLAlchemy async connection pool
 4. Auto-create DB tables (dev) or run Alembic migrations (prod)
 5. Instantiate WalletManager and BlockchainMonitor
 6. Build the PTB Application with post_init / post_shutdown lifecycle hooks
 7. Register all Telegram handlers (commands, callbacks, conversations, errors)
 8. Start health-check HTTP server (GET /health returns JSON)
 9. Run polling — PTB owns the event loop and installs SIGINT/SIGTERM handlers
10. Graceful shutdown: stop monitoring, drain DB pool, close RPC connections

Dependencies (install with pip install -r requirements.txt)
-----------------------------------------------------------
    python-telegram-bot[job-queue]>=22.0
    sqlalchemy[asyncio]>=2.0
    asyncpg>=0.29
    python-dotenv>=1.0
    web3>=7.0  eth-account>=0.11  cryptography>=42
    solana>=0.34  solders>=0.21  base58>=2.1
    aiohttp>=3.9  psutil>=5.9  alembic>=1.13
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

# ── Path setup ────────────────────────────────────────────────────────────────
# Ensure the directory that CONTAINS gaming_bot/ is on sys.path.
# This lets `from gaming_bot.X import Y` work when main.py is run directly.
_ROOT = Path(__file__).parent.resolve()
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ── PTB must be imported before any relative-path manipulation can shadow it ──
# (Our gaming_bot/telegram.py uses relative imports so there's no shadowing
# issue, but we import PTB here first anyway for clarity.)
from telegram import BotCommand, Update               # python-telegram-bot
from telegram.error import TelegramError
from telegram.ext import Application, ApplicationBuilder, CommandHandler

# ── Project modules (all via gaming_bot package) ──────────────────────────────
from gaming_bot.config import cfg
from gaming_bot.database import (
    TransactionManager,
    close_pool,
    create_pool,
    ping,
    run_migrations,
)
from gaming_bot.models import Store
from gaming_bot.telegram import (
    configure_logging,
    register_all_handlers,
    start_health_server,
    status_command,
    stop_health_server,
)
from gaming_bot.wallet import WalletManager
from gaming_bot.blockchain import BlockchainMonitor

logger = logging.getLogger("main")


# ── PTB lifecycle hooks ───────────────────────────────────────────────────────

async def _post_init(app: Application) -> None:
    """
    Called by PTB after Application.initialize(), before polling starts.
    Sets up all async resources and injects them into bot_data so every
    handler can reach them via context.application.bot_data["key"].
    """
    log = logging.getLogger("startup")

    # ── Database ──────────────────────────────────────────────────────────────
    log.info("Connecting to database…")
    engine = create_pool(
        database_url  = cfg.database_url,
        pool_size     = cfg.db_pool_size,
        max_overflow  = cfg.db_max_overflow,
        pool_timeout  = cfg.db_pool_timeout,
        echo          = cfg.db_echo,
    )
    app.bot_data["db_engine"] = engine
    app.bot_data["tm"]        = TransactionManager(engine)

    db_ok, db_msg = await ping()
    if db_ok:
        log.info("Database: %s", db_msg)
        try:
            await run_migrations(engine)
        except Exception as exc:
            log.warning("Migration step skipped (use Alembic in prod): %s", exc)
    else:
        log.warning("Database unreachable: %s — in-memory store only", db_msg)

    # ── In-memory game store ──────────────────────────────────────────────────
    app.bot_data["store"]    = Store()
    app.bot_data["settings"] = cfg

    # ── Wallet manager (EVM + Solana RPC connections) ─────────────────────────
    log.info("Connecting wallet manager…")
    wm = WalletManager(master_key=cfg.wallet_master_key)
    try:
        await wm.init()
        app.bot_data["wallet_manager"] = wm
        log.info("WalletManager ready for networks: %s", list(cfg.rpc_urls))
    except Exception as exc:
        log.warning("WalletManager init failed (wallet features may be limited): %s", exc)
        app.bot_data["wallet_manager"] = None

    # ── Blockchain monitor (deposit detection) ─────────────────────────────────
    monitor = BlockchainMonitor(callback=_on_deposit(app))
    try:
        await monitor.start()
        app.bot_data["monitor"] = monitor
        log.info("BlockchainMonitor started.")
    except Exception as exc:
        log.warning("BlockchainMonitor start failed: %s", exc)
        app.bot_data["monitor"] = None

    # ── Health HTTP server ────────────────────────────────────────────────────
    health_task = asyncio.create_task(
        start_health_server(cfg.health_host, cfg.health_port),
        name="health-server",
    )
    app.bot_data["health_task"] = health_task

    # ── Bot command list (shown in Telegram '/' menu) ─────────────────────────
    await _register_commands(app)

    # ── Ready log ─────────────────────────────────────────────────────────────
    bot_info = await app.bot.get_me()
    log.info("Bot ready: @%s (id=%d) | %s", bot_info.username, bot_info.id, cfg.summary())
    _print_banner(bot_info.username)


async def _post_shutdown(app: Application) -> None:
    """
    Called by PTB after polling stops and Application.shutdown() completes.
    Closes all async resources in reverse order.
    """
    log = logging.getLogger("shutdown")
    log.info("Shutdown: closing resources…")

    # Stop health HTTP server
    await stop_health_server()
    task: Optional[asyncio.Task] = app.bot_data.pop("health_task", None)
    if task and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    # Stop blockchain monitor
    monitor: Optional[BlockchainMonitor] = app.bot_data.pop("monitor", None)
    if monitor:
        await monitor.stop()
        log.info("BlockchainMonitor stopped.")

    # Close wallet manager RPC connections
    wm: Optional[WalletManager] = app.bot_data.pop("wallet_manager", None)
    if wm:
        await wm.close()
        log.info("WalletManager connections closed.")

    # Close DB pool
    await close_pool()

    log.info("Shutdown complete.")


# ── Deposit callback ──────────────────────────────────────────────────────────

def _on_deposit(app: Application):
    """
    Returns an async callback invoked by BlockchainMonitor on every deposit event.
    On confirmation, credits the user's DB balance via TransactionManager and
    sends a Telegram notification.
    """
    async def _handler(event) -> None:
        if not event.confirmed:
            return   # only act on final confirmation

        log = logging.getLogger("deposit")
        log.info(
            "CONFIRMED deposit: %s %s → %s (label=%s) tx=%s",
            event.amount, event.token_symbol,
            event.address, event.label, event.tx_hash[:16],
        )

        # Credit balance in database
        tm: Optional[TransactionManager] = app.bot_data.get("tm")
        if tm and event.label:
            try:
                # label format: "user:<telegram_id>"
                user_id = int(event.label.split(":")[-1])
                async with tm.transaction() as session:
                    await tm.credit_deposit(
                        session,
                        user_id      = user_id,
                        network      = event.network,
                        token_symbol = event.token_symbol,
                        amount       = event.amount,
                        tx_hash      = event.tx_hash,
                        note         = f"Confirmed after {event.confirmations} blocks",
                    )

                # Also update in-memory store for immediate display
                store: Optional[Store] = app.bot_data.get("store")
                if store:
                    user = store.get_user(user_id)
                    if user:
                        user.add_balance(
                            event.network.upper(),
                            event.token_symbol,
                            event.amount,
                        )

                # Notify user in Telegram
                usd_str = f" (~${event.usd_value:.2f})" if event.usd_value else ""
                await app.bot.send_message(
                    chat_id   = user_id,
                    text      = (
                        f"✅ *Deposit confirmed!*\n\n"
                        f"Amount : {event.amount} {event.token_symbol}{usd_str}\n"
                        f"Network: {event.network}\n"
                        f"Tx     : `{event.tx_hash[:20]}…`\n"
                        f"Blocks : {event.confirmations}"
                    ),
                    parse_mode = "Markdown",
                )
            except Exception as exc:
                log.error("Failed to process confirmed deposit: %s", exc, exc_info=True)

    return _handler


# ── Bot command registration ──────────────────────────────────────────────────

async def _register_commands(app: Application) -> None:
    commands = [
        BotCommand("start",   "Register / open main menu"),
        BotCommand("menu",    "Open main menu"),
        BotCommand("wallet",  "View wallet balances"),
        BotCommand("help",    "How to play & FAQs"),
        BotCommand("status",  "System health (admins only)"),
        BotCommand("cancel",  "Cancel current operation"),
    ]
    try:
        await app.bot.set_my_commands(commands)
    except TelegramError as exc:
        logging.getLogger("startup").warning("Could not set bot commands: %s", exc)


# ── Application factory ───────────────────────────────────────────────────────

def build_application() -> Application:
    """
    Construct the fully configured PTB Application.

    All handlers from gaming_bot/telegram.py are registered here.
    The cfg singleton (loaded at import time from .env) is pre-seeded
    into bot_data so handlers can read it without re-importing.
    """
    app = (
        ApplicationBuilder()
        .token(cfg.bot_token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .connect_timeout(10)
        .read_timeout(10)
        .write_timeout(10)
        .pool_timeout(10)
        .get_updates_read_timeout(42)   # long-poll server timeout
        .build()
    )

    # Pre-seed bot_data with cfg so handlers can access it before post_init runs
    app.bot_data["settings"] = cfg

    # Register all handlers from gaming_bot/telegram.py
    register_all_handlers(app)

    return app


# ── Startup banner ────────────────────────────────────────────────────────────

def _print_banner(username: str) -> None:
    print(
        f"\n{'═' * 60}\n"
        f"  🎮  Gaming Bot started\n"
        f"  🤖  @{username}\n"
        f"  🌐  Health: http://{cfg.health_host}:{cfg.health_port}/health\n"
        f"  📋  Log level: {cfg.log_level}\n"
        f"{'═' * 60}\n"
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    """
    Configure logging → build Application → run polling.

    PTB's run_polling() owns the asyncio event loop for the entire session.
    It installs signal handlers for SIGINT / SIGTERM / SIGABRT and calls
    post_init / post_shutdown automatically, so we don't manage the loop here.
    """
    # 1. Configure logging before anything else so startup messages are captured
    configure_logging(
        level        = cfg.log_level,
        log_file     = cfg.log_file,
        max_bytes    = cfg.log_max_bytes,
        backup_count = cfg.log_backup_count,
    )

    logger.info("main.py starting — %s", cfg.summary())

    # 2. Build and run
    app = build_application()

    logger.info("Entering run_polling loop…")
    app.run_polling(
        poll_interval        = 1.0,
        timeout              = 30,
        bootstrap_retries    = -1,          # retry forever on startup network errors
        drop_pending_updates = cfg.drop_pending_updates,
        allowed_updates      = Update.ALL_TYPES,
        close_loop           = True,
    )

    # run_polling() returns here after a clean SIGINT/SIGTERM shutdown
    logger.info("run_polling() returned — process exiting cleanly.")


if __name__ == "__main__":
    main()
