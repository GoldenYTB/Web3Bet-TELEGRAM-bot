"""
telegram.py — Everything Telegram-facing.

Merges handlers.py + keyboards.py + health.py + logging_setup.py into one module.

Public API
----------
  configure_logging(...)    — Set up rotating logs with colour console output
  start_health_server(...)  — Launch aiohttp health HTTP server
  stop_health_server()      — Shut down health HTTP server
  status_command(...)       — /status Telegram command handler
  register_all_handlers(app)— Wire all PTB handlers onto an Application
"""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
import sys
import time
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

from aiohttp import web
from telegram import (
    BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters,
)

from .config import (
    CB_BACK_MAIN, CB_CANCEL,
    CB_LOBBY_CANCEL, CB_LOBBY_CONFIRM, CB_LOBBY_CUSTOM,
    CB_LOBBY_GAME, CB_LOBBY_NETWORK, CB_LOBBY_TOKEN, CB_LOBBY_WAGER,
    CB_MATCH_CANCEL,
    CB_MENU_HELP, CB_MENU_LEADERBOARD, CB_MENU_PLAY, CB_MENU_WALLET,
    CB_WALLET_DEPOSIT, CB_WALLET_REFRESH, CB_WALLET_WITHDRAW,
    GAME_TYPES, MATCHMAKING_TTL, MAX_WAGER, MIN_WAGER,
    NETWORKS, PRESET_WAGERS, TOKENS_BY_NETWORK, State,
)
from .games import game_result_text, resolve_game
from .models import ActiveGame, GameStatus, GameType, PendingGame, Store, User

logger = logging.getLogger(__name__)
_START_TIME: float = time.time()


# ══════════════════════════════════════════════════════════════════════════════
#  Logging setup
# ══════════════════════════════════════════════════════════════════════════════

_THIRD_PARTY_QUIET = {
    "httpx", "httpcore", "telegram", "telegram.ext",
    "apscheduler", "asyncio", "sqlalchemy.engine",
    "sqlalchemy.pool", "aiohttp", "web3",
}
_COLOURS = {
    "DEBUG": "\033[36m", "INFO": "\033[32m", "WARNING": "\033[33m",
    "ERROR": "\033[31m", "CRITICAL": "\033[35m",
}
_RESET = "\033[0m"


class _ColourFmt(logging.Formatter):
    _FMT  = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
    _DATE = "%H:%M:%S"

    def format(self, record: logging.LogRecord) -> str:
        r = logging.makeLogRecord(record.__dict__)
        if sys.stderr.isatty():
            c = _COLOURS.get(r.levelname, "")
            r.levelname = f"{c}{r.levelname:8s}{_RESET}"
        return logging.Formatter(self._FMT, datefmt=self._DATE).format(r)


class _PlainFmt(logging.Formatter):
    _FMT  = "%(asctime)s [%(levelname)-8s] %(name)s — %(message)s"
    _DATE = "%Y-%m-%d %H:%M:%S"
    def __init__(self): super().__init__(fmt=self._FMT, datefmt=self._DATE)


def configure_logging(
    level: str = "INFO", log_file: str = "",
    max_bytes: int = 10*1024*1024, backup_count: int = 5,
) -> None:
    """Configure root logger with colour console + optional rotating file."""
    num  = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(num)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(num)
    console.setFormatter(_ColourFmt())
    root.addHandler(console)

    if log_file:
        p = Path(log_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            p, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        fh.setLevel(num); fh.setFormatter(_PlainFmt())
        root.addHandler(fh)

    for name in _THIRD_PARTY_QUIET:
        logging.getLogger(name).setLevel(max(num, logging.WARNING))

    logging.getLogger(__name__).info("Logging: level=%s file=%s", level, log_file or "stdout")


# ══════════════════════════════════════════════════════════════════════════════
#  Health check HTTP server
# ══════════════════════════════════════════════════════════════════════════════

_runner: Optional[web.AppRunner] = None


async def _health_handler(request: web.Request) -> web.Response:
    from .database import ping, pool_status
    db_ok, db_msg = await ping()
    body = {
        "status":   "ok" if db_ok else "degraded",
        "uptime_s": int(time.time() - _START_TIME),
        "database": {"ok": db_ok, "message": db_msg},
        "pool":     await pool_status(),
    }
    return web.json_response(body, status=200 if db_ok else 503)


async def start_health_server(host: str = "0.0.0.0", port: int = 8080) -> None:
    global _runner
    app = web.Application()
    app.router.add_get("/health", _health_handler)
    app.router.add_get("/ready",  lambda r: web.Response(text="ready"))
    app.router.add_get("/",       _health_handler)
    _runner = web.AppRunner(app, access_log=None)
    await _runner.setup()
    await web.TCPSite(_runner, host, port).start()
    logger.info("Health server: http://%s:%d/health", host, port)


async def stop_health_server() -> None:
    global _runner
    if _runner:
        await _runner.cleanup()
        _runner = None
        logger.info("Health server stopped.")


# ── /status command ───────────────────────────────────────────────────────────

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/status — Admin-only system dashboard."""
    from .database import ping, pool_status

    user     = update.effective_user
    settings = context.application.bot_data.get("settings")
    if settings and settings.admin_ids and (user is None or user.id not in settings.admin_ids):
        await update.message.reply_text("⛔ Admin only.")
        return

    await update.message.reply_text("⏳ Gathering stats…")

    uptime_s   = int(time.time() - _START_TIME)
    h, rem     = divmod(uptime_s, 3600)
    m, s       = divmod(rem, 60)
    uptime_str = f"{h}h {m}m {s}s"

    try:
        bi       = await context.bot.get_me()
        bot_line = f"🤖 *{bi.first_name}* (@{bi.username})\n`ID: {bi.id}`"
    except Exception as exc:
        bot_line = f"⚠️ Bot info error: {exc}"

    db_ok, db_msg = await ping()
    db_emoji      = "✅" if db_ok else "❌"
    try:
        pool = await pool_status()
        pool_line = (
            f"{db_emoji} DB: {db_msg}\n"
            f"  Pool: {pool.get('checked_out',0)} active / "
            f"{pool.get('checked_in',0)} idle / "
            f"{pool.get('size',0)+pool.get('overflow',0)} total"
        )
    except Exception as exc:
        pool_line = f"{db_emoji} DB error: {exc}"

    store: Optional[Store] = context.application.bot_data.get("store")
    store_line = (
        f"👥 Users: {len(store.users)}\n"
        f"🎮 Pending: {len(store.pending_games)}\n"
        f"🏁 Completed: {len(store.active_games)}"
    ) if store else "⚠️ Store not initialised"

    tasks     = asyncio.all_tasks()
    run_names = [t.get_name() for t in tasks if not t.done()]
    bot_tasks = [n for n in run_names if any(kw in n.lower() for kw in ("monitor","health","mm_"))]
    task_line = f"⚙️ Tasks: {len(run_names)} running ({len(bot_tasks)} bot-specific)"

    try:
        import psutil, os
        mem_mb   = psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
        mem_line = f"🧠 Memory: {mem_mb:.1f} MB"
    except ImportError:
        mem_line = "🧠 Memory: install psutil for stats"

    text = (
        f"📊 *System Status*\n━━━━━━━━━━━━━━━━\n\n"
        f"{bot_line}\n⏱️ Uptime: {uptime_str}\n\n"
        f"*Database*\n{pool_line}\n\n"
        f"*Game Store*\n{store_line}\n\n"
        f"{task_line}\n{mem_line}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ══════════════════════════════════════════════════════════════════════════════
#  Keyboards
# ══════════════════════════════════════════════════════════════════════════════

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Wallet", callback_data=CB_MENU_WALLET),
         InlineKeyboardButton("🎮 Play",   callback_data=CB_MENU_PLAY)],
        [InlineKeyboardButton("🏆 Leaderboard", callback_data=CB_MENU_LEADERBOARD),
         InlineKeyboardButton("❓ Help",         callback_data=CB_MENU_HELP)],
    ])

def back_to_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data=CB_BACK_MAIN)]])

def wallet_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Deposit",  callback_data=CB_WALLET_DEPOSIT),
         InlineKeyboardButton("📤 Withdraw", callback_data=CB_WALLET_WITHDRAW)],
        [InlineKeyboardButton("🔄 Refresh",  callback_data=CB_WALLET_REFRESH)],
        [InlineKeyboardButton("⬅️ Back",     callback_data=CB_BACK_MAIN)],
    ])

def deposit_network_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(net["label"], callback_data=f"{CB_LOBBY_NETWORK}{key}")]
            for key, net in NETWORKS.items()]
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data=CB_CANCEL)])
    return InlineKeyboardMarkup(rows)

def withdraw_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm", callback_data="withdraw:confirm"),
        InlineKeyboardButton("❌ Cancel",  callback_data=CB_CANCEL),
    ]])

def game_type_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(info["label"], callback_data=f"{CB_LOBBY_GAME}{key}")]
            for key, info in GAME_TYPES.items()]
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data=CB_BACK_MAIN)])
    return InlineKeyboardMarkup(rows)

def network_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(info["label"], callback_data=f"{CB_LOBBY_NETWORK}{key}")]
            for key, info in NETWORKS.items()]
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data=CB_LOBBY_CANCEL)])
    return InlineKeyboardMarkup(rows)

def token_kb(network: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"{t['symbol']} — {t['name']}",
                                  callback_data=f"{CB_LOBBY_TOKEN}{t['symbol']}")]
            for t in TOKENS_BY_NETWORK.get(network, [])]
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data=CB_LOBBY_CANCEL)])
    return InlineKeyboardMarkup(rows)

def wager_kb() -> InlineKeyboardMarkup:
    btns = [InlineKeyboardButton(f"${w}", callback_data=f"{CB_LOBBY_WAGER}{w}") for w in PRESET_WAGERS]
    rows = [btns[i:i+3] for i in range(0, len(btns), 3)]
    rows.append([InlineKeyboardButton("✏️ Custom amount", callback_data=CB_LOBBY_CUSTOM)])
    rows.append([InlineKeyboardButton("❌ Cancel",        callback_data=CB_LOBBY_CANCEL)])
    return InlineKeyboardMarkup(rows)

def lobby_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Find Match", callback_data=CB_LOBBY_CONFIRM),
        InlineKeyboardButton("❌ Cancel",     callback_data=CB_LOBBY_CANCEL),
    ]])

def matchmaking_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Search", callback_data=CB_MATCH_CANCEL)]])


# ══════════════════════════════════════════════════════════════════════════════
#  Handler helpers
# ══════════════════════════════════════════════════════════════════════════════

def _store(context: ContextTypes.DEFAULT_TYPE) -> Store:
    return context.application.bot_data["store"]

def _user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[User]:
    tg = update.effective_user
    if tg is None:
        return None
    u, _ = _store(context).get_or_create_user(tg.id, tg.username or "", tg.first_name)
    return u

def _lobby(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.setdefault("lobby", {})

def _clear_lobby(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("lobby", None)

async def _edit(update: Update, text: str, kb=None, **kw) -> None:
    q = update.callback_query
    try:
        if q and q.message:
            await q.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN, **kw)
        else:
            await update.effective_message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN, **kw)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise

async def _answer(update: Update, text: str = "", alert: bool = False) -> None:
    if update.callback_query:
        await update.callback_query.answer(text, show_alert=alert)


# ══════════════════════════════════════════════════════════════════════════════
#  Command handlers
# ══════════════════════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg = update.effective_user
    _, created = _store(context).get_or_create_user(tg.id, tg.username or "", tg.first_name)
    if created:
        text = (
            f"👋 Welcome, *{tg.first_name}*!\n\n"
            "You've been registered with a starter balance:\n"
            "• 100 USDT on BNB Chain\n• 1 SOL on Solana\n\n"
            "Use the menu below to play or manage your wallet."
        )
        logger.info("New user: %d @%s", tg.id, tg.username)
    else:
        text = f"👋 Welcome back, *{tg.first_name}*!"
    await update.message.reply_text(text, reply_markup=main_menu_kb(), parse_mode=ParseMode.MARKDOWN)

async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u    = _user(update, context)
    text = f"🏠 *Main Menu*\nHello, {u.display_name() if u else 'there'}!"
    if update.message:
        await update.message.reply_text(text, reply_markup=main_menu_kb(), parse_mode=ParseMode.MARKDOWN)
    else:
        await _edit(update, text, main_menu_kb())

async def wallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = _user(update, context)
    if not u:
        await update.message.reply_text("Please /start first.")
        return
    lines = [f"• `{k.split(':')[0]}` — {v.amount:.4f} {v.token_symbol}" for k, v in u.balances.items()]
    text  = "💰 *Wallet*\n\n" + ("\n".join(lines) if lines else "_No balances yet._")
    await update.message.reply_text(text, reply_markup=wallet_menu_kb(), parse_mode=ParseMode.MARKDOWN)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Use the menu to navigate:", reply_markup=main_menu_kb())
    await _show_help(update, context)

async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _answer(update)
    _clear_lobby(context)
    await _edit(update, "❌ Cancelled.", main_menu_kb())
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
#  Menu callbacks
# ══════════════════════════════════════════════════════════════════════════════

async def menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _answer(update)
    data = update.callback_query.data
    if data == CB_MENU_WALLET:
        await _show_wallet(update, context)
    elif data == CB_MENU_PLAY:
        await _edit(update, "🎮 *Game Lobby*\n\nChoose a game:", game_type_kb())
    elif data == CB_MENU_LEADERBOARD:
        await _show_leaderboard(update, context)
    elif data == CB_MENU_HELP:
        await _show_help(update, context)
    elif data == CB_BACK_MAIN:
        u = _user(update, context)
        await _edit(update, f"🏠 *Main Menu*\nHello, {u.display_name() if u else 'there'}!", main_menu_kb())


# ══════════════════════════════════════════════════════════════════════════════
#  Wallet conversation
# ══════════════════════════════════════════════════════════════════════════════

async def _show_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = _user(update, context)
    if not u: return
    lines = [f"  • `{k.split(':')[0]}` — {v.amount:.4f} {v.token_symbol}" for k, v in u.balances.items()]
    bal_text = "\n".join(lines) if lines else "  _No balances yet — make a deposit!_"
    text = (
        f"💰 *Your Wallet*\n\n*Balances:*\n{bal_text}\n\n"
        f"*Stats:*\n  Games played: {u.games_played}\n"
        f"  Wins: {u.games_won} ({u.win_rate:.1f}%)\n"
        f"  Total wagered: {u.total_wagered:.2f}\n"
        f"  Total won: {u.total_won:.2f}"
    )
    await _edit(update, text, wallet_menu_kb())

async def wallet_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _answer(update)
    data = update.callback_query.data
    if data == CB_WALLET_REFRESH:
        await _show_wallet(update, context); return ConversationHandler.END
    if data == CB_WALLET_DEPOSIT:
        await _edit(update, "📥 *Deposit*\n\nChoose a network:", deposit_network_kb())
        return State.DEPOSIT_SELECT_NET
    if data == CB_WALLET_WITHDRAW:
        u = _user(update, context)
        if not u or not u.balances:
            await _answer(update, "❌ No balances to withdraw.", alert=True)
            return ConversationHandler.END
        rows = [[InlineKeyboardButton(
            f"{k.split(':')[0]} — {v.amount:.4f} {v.token_symbol}",
            callback_data=f"withdraw:select:{k}",
        )] for k, v in u.balances.items() if v.amount > 0]
        rows.append([InlineKeyboardButton("❌ Cancel", callback_data=CB_CANCEL)])
        await _edit(update, "📤 *Withdraw*\n\nSelect balance:", InlineKeyboardMarkup(rows))
        return State.WITHDRAW_ADDRESS
    return ConversationHandler.END

async def deposit_net_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _answer(update)
    net_key  = update.callback_query.data.removeprefix("lobby:net:")
    net_info = NETWORKS.get(net_key)
    if not net_info:
        await _answer(update, "Unknown network.", alert=True)
        return State.DEPOSIT_SELECT_NET
    demo = "0xDEMO_ADDRESS_REPLACE_WITH_WALLET_MANAGER"
    if net_key == "SOLANA":
        demo = "DEMO_SOL_ADDRESS_REPLACE_WITH_WALLET_MANAGER"
    await _edit(
        update,
        f"📥 *Deposit on {net_info['label']}*\n\nSend to:\n`{demo}`\n\n"
        "_Deposits credited after network confirmation._\n"
        "⚠️ *Demo:* Wire `wallet.py` for real addresses.",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back", callback_data=CB_WALLET_WITHDRAW)],
            [InlineKeyboardButton("🏠 Menu", callback_data=CB_BACK_MAIN)],
        ]),
    )
    return ConversationHandler.END

async def withdraw_tok_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _answer(update)
    data = update.callback_query.data
    if data == CB_CANCEL:
        await _show_wallet(update, context); return ConversationHandler.END
    bk  = data.removeprefix("withdraw:select:")
    net, tok = bk.split(":", 1)
    context.user_data["withdraw_key"] = bk
    u   = _user(update, context)
    bal = u.get_balance(net, tok) if u else Decimal("0")
    await _edit(update, f"📤 *Withdraw {tok} from {net}*\n\nAvailable: *{bal:.4f} {tok}*\n\nEnter destination address:",
                InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=CB_CANCEL)]]))
    return State.WITHDRAW_ADDRESS

async def withdraw_addr_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    addr = update.message.text.strip()
    if len(addr) < 20:
        await update.message.reply_text("❌ Invalid address. Try again:")
        return State.WITHDRAW_ADDRESS
    context.user_data["withdraw_address"] = addr
    bk  = context.user_data.get("withdraw_key", "")
    net, tok = bk.split(":", 1) if ":" in bk else ("?", "?")
    u   = _user(update, context)
    bal = u.get_balance(net, tok) if u else Decimal("0")
    await update.message.reply_text(
        f"💸 *Withdraw {tok}*\n\nTo: `{addr}`\nAvailable: {bal:.4f} {tok}\n\nEnter amount:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=CB_CANCEL)]]),
        parse_mode=ParseMode.MARKDOWN,
    )
    return State.WITHDRAW_AMOUNT

async def withdraw_amt_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        amount = Decimal(update.message.text.strip())
    except InvalidOperation:
        await update.message.reply_text("❌ Invalid number. Try again:"); return State.WITHDRAW_AMOUNT
    bk  = context.user_data.get("withdraw_key", "")
    net, tok = bk.split(":", 1) if ":" in bk else ("?","?")
    u   = _user(update, context)
    avl = u.get_balance(net, tok) if u else Decimal("0")
    if amount <= 0:
        await update.message.reply_text("❌ Amount must be positive."); return State.WITHDRAW_AMOUNT
    if amount > avl:
        await update.message.reply_text(f"❌ Insufficient. Have {avl:.4f} {tok}."); return State.WITHDRAW_AMOUNT
    context.user_data["withdraw_amount"] = str(amount)
    addr = context.user_data.get("withdraw_address", "")
    await update.message.reply_text(
        f"📋 *Confirm Withdrawal*\n\nNetwork: *{net}*\nToken: *{tok}*\nAmount: *{amount:.4f} {tok}*\nTo: `{addr}`\n\nProceed?",
        reply_markup=withdraw_confirm_kb(), parse_mode=ParseMode.MARKDOWN,
    )
    return State.WITHDRAW_CONFIRM

async def withdraw_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _answer(update)
    if update.callback_query.data == CB_CANCEL:
        await _show_wallet(update, context); return ConversationHandler.END
    u   = _user(update, context)
    bk  = context.user_data.get("withdraw_key", "")
    net, tok = bk.split(":", 1) if ":" in bk else ("?","?")
    amt  = Decimal(context.user_data.get("withdraw_amount", "0"))
    addr = context.user_data.get("withdraw_address", "")
    if u and u.deduct_balance(net, tok, amt):
        logger.info("Withdrawal: user=%d %s %s → %s", u.telegram_id, amt, tok, addr)
        await _edit(update,
            f"✅ *Withdrawal submitted!*\n\n*{amt:.4f} {tok}* from {net}\n→ `{addr}`\n\n"
            "_Connect `wallet.py` to broadcast on-chain._",
            back_to_main_kb())
    else:
        await _edit(update, "❌ Insufficient funds.", back_to_main_kb())
    for k in ("withdraw_key", "withdraw_address", "withdraw_amount"):
        context.user_data.pop(k, None)
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
#  Game lobby conversation
# ══════════════════════════════════════════════════════════════════════════════

async def play_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _answer(update)
    _clear_lobby(context)
    await _edit(update, "🎮 *Game Lobby*\n\nWhat game would you like to play?", game_type_kb())
    return State.LOBBY_GAME_TYPE

async def lobby_game_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _answer(update)
    key = update.callback_query.data.removeprefix(CB_LOBBY_GAME)
    if key not in GAME_TYPES:
        await _answer(update, "Unknown game.", alert=True); return State.LOBBY_GAME_TYPE
    _lobby(context)["game_type"] = key
    info = GAME_TYPES[key]
    await _edit(update, f"{info['label']}\n_{info['description']}_\n\nChoose a network:", network_kb())
    return State.LOBBY_NETWORK

async def lobby_network(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _answer(update)
    net = update.callback_query.data.removeprefix(CB_LOBBY_NETWORK)
    if net not in NETWORKS:
        await _answer(update, "Unknown network.", alert=True); return State.LOBBY_NETWORK
    _lobby(context)["network"] = net
    await _edit(update, f"Network: *{NETWORKS[net]['label']}*\n\nChoose a token:", token_kb(net))
    return State.LOBBY_TOKEN

async def lobby_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _answer(update)
    sym = update.callback_query.data.removeprefix(CB_LOBBY_TOKEN)
    net = _lobby(context).get("network", "")
    if sym not in [t["symbol"] for t in TOKENS_BY_NETWORK.get(net, [])]:
        await _answer(update, "Invalid token.", alert=True); return State.LOBBY_TOKEN
    _lobby(context)["token"] = sym
    await _edit(update, f"Token: *{sym}* on *{NETWORKS[net]['label']}*\n\nSelect your wager:", wager_kb())
    return State.LOBBY_WAGER

async def lobby_wager(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _answer(update)
    data = update.callback_query.data
    if data == CB_LOBBY_CUSTOM:
        await _edit(update, f"✏️ Enter wager amount (min {MIN_WAGER}, max {MAX_WAGER}):",
                    InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=CB_LOBBY_CANCEL)]]))
        return State.LOBBY_WAGER
    return await _set_wager(update, context, data.removeprefix(CB_LOBBY_WAGER))

async def lobby_wager_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _set_wager(update, context, update.message.text.strip(), from_msg=True)

async def _set_wager(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    amount_str: str, from_msg: bool = False,
) -> int:
    try:
        amount = Decimal(amount_str)
    except InvalidOperation:
        if from_msg: await update.message.reply_text("❌ Invalid number:")
        return State.LOBBY_WAGER
    if amount < MIN_WAGER or amount > MAX_WAGER:
        msg = f"❌ Wager must be {MIN_WAGER}–{MAX_WAGER}."
        if from_msg: await update.message.reply_text(msg)
        else: await _answer(update, msg, alert=True)
        return State.LOBBY_WAGER
    u   = _user(update, context)
    lob = _lobby(context)
    net = lob.get("network",""); tok = lob.get("token","")
    if u and u.get_balance(net, tok) < amount:
        msg = f"❌ Insufficient {tok}. Have {u.get_balance(net,tok):.4f}."
        if from_msg: await update.message.reply_text(msg)
        else: await _answer(update, msg, alert=True)
        return State.LOBBY_WAGER
    lob["wager"] = str(amount)
    game_info = GAME_TYPES.get(lob.get("game_type",""), {})
    summary = (
        f"📋 *Game Summary*\n\n"
        f"Game:    *{game_info.get('label','')}*\n"
        f"Network: *{NETWORKS.get(net,{}).get('label',net)}*\n"
        f"Token:   *{tok}*\nWager:   *{amount} {tok}*\n"
        f"To win:  *~{amount * 2 * Decimal('0.95'):.4f} {tok}* _(5% fee)_\n\nFind a match?"
    )
    if from_msg: await update.message.reply_text(summary, reply_markup=lobby_confirm_kb(), parse_mode=ParseMode.MARKDOWN)
    else: await _edit(update, summary, lobby_confirm_kb())
    return State.LOBBY_CONFIRM

async def lobby_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _answer(update)
    if update.callback_query.data == CB_LOBBY_CANCEL:
        await _edit(update, "❌ Game cancelled.", main_menu_kb())
        _clear_lobby(context); return ConversationHandler.END
    u   = _user(update, context)
    lob = _lobby(context)
    sto = _store(context)
    if sto.get_pending_for_player(u.telegram_id):
        await _answer(update, "⚠️ Already in queue!", alert=True); return ConversationHandler.END
    net = lob["network"]; tok = lob["token"]; amt = Decimal(lob["wager"])
    if not u.deduct_balance(net, tok, amt):
        await _answer(update, "❌ Insufficient balance.", alert=True); return ConversationHandler.END
    pending = PendingGame(
        player_id=u.telegram_id, game_type=GameType(lob["game_type"]),
        network=net, token_symbol=tok, wager_amount=amt,
    )
    context.user_data["pending_game_id"] = pending.game_id
    matched = sto.enqueue(pending)
    if matched:
        await _edit(update, "⚡ *Opponent found!* Resolving…", None)
        await _run_game(update, context, pending, matched)
    else:
        await _edit(update,
            f"🔍 *Searching for opponent…*\n\nGame: *{GAME_TYPES[lob['game_type']]['label']}*\n"
            f"Wager: *{amt} {tok}* on *{net}*\n\n_Queue: 1 — you'll be notified!_",
            matchmaking_kb())
        context.job_queue.run_once(
            _matchmaking_timeout, when=MATCHMAKING_TTL,
            data={"game_id": pending.game_id, "user_id": u.telegram_id,
                  "network": net, "token": tok, "amount": str(amt)},
            name=f"mm_{pending.game_id}", chat_id=update.effective_chat.id, user_id=u.telegram_id,
        )
    _clear_lobby(context); return ConversationHandler.END

async def match_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _answer(update)
    u   = _user(update, context)
    sto = _store(context)
    gid = context.user_data.pop("pending_game_id", None)
    if not gid:
        await _answer(update, "No active search.", alert=True); return
    pending = sto.dequeue(gid)
    if pending:
        u.add_balance(pending.network, pending.token_symbol, pending.wager_amount)
        await _edit(update, f"✅ Cancelled.\n*{pending.wager_amount} {pending.token_symbol}* refunded.", main_menu_kb())
        logger.info("Matchmaking cancelled: user=%d game=%s", u.telegram_id, gid[:8])
    else:
        await _edit(update, "⚠️ Queue entry not found.", main_menu_kb())


# ══════════════════════════════════════════════════════════════════════════════
#  Game resolution
# ══════════════════════════════════════════════════════════════════════════════

async def _run_game(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    p1: PendingGame, p2: PendingGame,
) -> None:
    sto  = _store(context)
    game = ActiveGame(
        game_id=str(uuid.uuid4()), game_type=p1.game_type,
        network=p1.network, token_symbol=p1.token_symbol, wager_amount=p1.wager_amount,
        player1_id=p1.player_id, player2_id=p2.player_id,
    )
    game = resolve_game(game)
    sto.active_games[game.game_id] = game

    u1 = sto.get_user(p1.player_id)
    u2 = sto.get_user(p2.player_id)
    net = game.network; tok = game.token_symbol

    if game.winner_id is None:
        if u1: u1.add_balance(net, tok, game.winner_payout)
        if u2: u2.add_balance(net, tok, game.winner_payout)
    elif game.winner_id == p1.player_id:
        if u1: u1.add_balance(net, tok, game.winner_payout)
    else:
        if u2: u2.add_balance(net, tok, game.winner_payout)

    for pid, u in ((p1.player_id, u1), (p2.player_id, u2)):
        if u:
            u.games_played += 1; u.total_wagered += game.wager_amount
            if game.winner_id == pid:
                u.games_won += 1; u.total_won += game.winner_payout
            elif game.winner_id is None:
                u.total_won += game.winner_payout

    logger.info("Game resolved: %s p1=%d(%d) p2=%d(%d) winner=%s",
                game.game_id[:8], p1.player_id, game.player1_score,
                p2.player_id, game.player2_score, game.winner_id)

    await _edit(update, game_result_text(game, p1.player_id), main_menu_kb())
    try:
        await context.bot.send_message(
            chat_id=p2.player_id,
            text=f"⚡ *Match found!*\n\n{game_result_text(game, p2.player_id)}",
            reply_markup=main_menu_kb(), parse_mode=ParseMode.MARKDOWN,
        )
    except (Forbidden, TelegramError) as exc:
        logger.warning("Could not notify player %d: %s", p2.player_id, exc)


async def _matchmaking_timeout(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    gid  = data["game_id"]; uid = data["user_id"]
    sto  = context.application.bot_data["store"]
    pending = sto.dequeue(gid)
    if not pending: return
    u = sto.get_user(uid)
    if u: u.add_balance(data["network"], data["token"], Decimal(data["amount"]))
    try:
        await context.bot.send_message(
            chat_id=uid,
            text=(f"⏰ *Matchmaking timed out.*\n\nNo opponent after {MATCHMAKING_TTL//60}m.\n"
                  f"*{data['amount']} {data['token']}* refunded."),
            reply_markup=main_menu_kb(), parse_mode=ParseMode.MARKDOWN,
        )
    except TelegramError as exc:
        logger.warning("Timeout notify failed user=%d: %s", uid, exc)
    logger.info("Matchmaking timeout: user=%d game=%s", uid, gid[:8])


# ══════════════════════════════════════════════════════════════════════════════
#  Leaderboard / Help / Error handler
# ══════════════════════════════════════════════════════════════════════════════

async def _show_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    top = _store(context).leaderboard(10)
    if not top:
        text = "🏆 *Leaderboard*\n\n_No games yet. Be the first!_"
    else:
        medals = ["🥇","🥈","🥉"] + ["4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
        rows   = [f"{medals[i] if i < len(medals) else str(i+1)+'.'} "
                  f"{'@'+u.username if u.username else u.first_name} — "
                  f"{u.games_won}W/{u.games_played}G ({u.win_rate:.0f}%)"
                  for i, u in enumerate(top)]
        text = "🏆 *Leaderboard — Top Players*\n\n" + "\n".join(rows)
    await _edit(update, text, back_to_main_kb())

async def _show_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _edit(update, (
        "❓ *Help & How to Play*\n\n"
        "*Games*\n🎲 *Dice* — Roll 1–6. Highest wins.\n"
        "🎳 *Bowling* — Score 0–300. Highest wins.\n\n"
        "*Matchmaking*\n• Set wager → wait for opponent → instant resolve.\n"
        "• 5-min timeout if no match — wager refunded.\n\n"
        "*Fees*\n• 5% rake · Winner gets 95% of pool.\n"
        "• Tie: each gets 47.5% back.\n\n"
        "*Commands*\n/start /menu /wallet /help /cancel"
    ), back_to_main_kb())

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Handler exception:", exc_info=context.error)
    if isinstance(context.error, Forbidden):
        return
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ *Something went wrong.*\nPlease try again or use /menu.",
                parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_kb(),
            )
        except TelegramError:
            pass


# ══════════════════════════════════════════════════════════════════════════════
#  Handler registration
# ══════════════════════════════════════════════════════════════════════════════

def register_all_handlers(app: Application) -> None:
    # Commands
    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("menu",   menu_cmd))
    app.add_handler(CommandHandler("wallet", wallet_cmd))
    app.add_handler(CommandHandler("help",   help_cmd))
    app.add_handler(CommandHandler("status", status_command))

    # Wallet conversation
    app.add_handler(ConversationHandler(
        entry_points=[
            CallbackQueryHandler(wallet_cb, pattern=f"^{CB_WALLET_DEPOSIT}$"),
            CallbackQueryHandler(wallet_cb, pattern=f"^{CB_WALLET_WITHDRAW}$"),
            CallbackQueryHandler(wallet_cb, pattern=f"^{CB_WALLET_REFRESH}$"),
        ],
        states={
            State.DEPOSIT_SELECT_NET: [
                CallbackQueryHandler(deposit_net_selected, pattern=r"^lobby:net:"),
                CallbackQueryHandler(cancel_conv, pattern=f"^{CB_CANCEL}$"),
            ],
            State.WITHDRAW_ADDRESS: [
                CallbackQueryHandler(withdraw_tok_selected, pattern=r"^withdraw:select:"),
                CallbackQueryHandler(cancel_conv, pattern=f"^{CB_CANCEL}$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_addr_received),
            ],
            State.WITHDRAW_AMOUNT: [
                CallbackQueryHandler(cancel_conv, pattern=f"^{CB_CANCEL}$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amt_received),
            ],
            State.WITHDRAW_CONFIRM: [
                CallbackQueryHandler(withdraw_confirmed, pattern=r"^withdraw:confirm$"),
                CallbackQueryHandler(cancel_conv, pattern=f"^{CB_CANCEL}$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(cancel_conv, pattern=f"^{CB_CANCEL}$"),
            CommandHandler("cancel", cancel_conv),
        ],
        per_message=False, allow_reentry=True,
    ))

    # Game lobby conversation
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(play_cb, pattern=f"^{CB_MENU_PLAY}$")],
        states={
            State.LOBBY_GAME_TYPE: [
                CallbackQueryHandler(lobby_game_type, pattern=r"^lobby:game:"),
                CallbackQueryHandler(cancel_conv, pattern=f"^{CB_LOBBY_CANCEL}$"),
                CallbackQueryHandler(menu_cb, pattern=f"^{CB_BACK_MAIN}$"),
            ],
            State.LOBBY_NETWORK: [
                CallbackQueryHandler(lobby_network, pattern=r"^lobby:net:"),
                CallbackQueryHandler(cancel_conv, pattern=f"^{CB_LOBBY_CANCEL}$"),
            ],
            State.LOBBY_TOKEN: [
                CallbackQueryHandler(lobby_token, pattern=r"^lobby:token:"),
                CallbackQueryHandler(cancel_conv, pattern=f"^{CB_LOBBY_CANCEL}$"),
            ],
            State.LOBBY_WAGER: [
                CallbackQueryHandler(lobby_wager, pattern=r"^lobby:wager:"),
                CallbackQueryHandler(cancel_conv, pattern=f"^{CB_LOBBY_CANCEL}$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, lobby_wager_text),
            ],
            State.LOBBY_CONFIRM: [
                CallbackQueryHandler(lobby_confirm, pattern=f"^{CB_LOBBY_CONFIRM}$"),
                CallbackQueryHandler(cancel_conv, pattern=f"^{CB_LOBBY_CANCEL}$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(cancel_conv, pattern=f"^{CB_LOBBY_CANCEL}$"),
            CommandHandler("cancel", cancel_conv),
        ],
        per_message=False, allow_reentry=True,
    ))

    # Main menu and matchmaking callbacks
    app.add_handler(CallbackQueryHandler(menu_cb,      pattern=r"^menu:|^back:main$"))
    app.add_handler(CallbackQueryHandler(match_cancel, pattern=f"^{CB_MATCH_CANCEL}$"))

    app.add_error_handler(error_handler)
