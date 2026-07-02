"""
telegram.py — All handlers, keyboards, health server, logging.

Unified USD balance system:
- Deposits in any coin → converted to USD at deposit time
- Bets placed in USD ($1, $5, $10 etc)
- Withdrawals: choose coin → ChangeNow swaps USD balance to that coin
- Preferred coin saved on first withdrawal

Group game commands: /dice /bowl /darts
Private commands: /start /wallet /profile /tip /promo /admin /withdraw
"""
from __future__ import annotations

import asyncio
import datetime
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
    BotCommand, InlineKeyboardButton, InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode, DiceEmoji
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters,
)

from .config import (
    cfg, COINS, NETWORKS, MULTI_NETWORK_COINS, GAME_TYPES, GAME_MODES, PRESET_WAGERS,
    DEFAULT_ADMIN_STATE, MIN_WAGER, MAX_WAGER, HOUSE_FEE_PCT, State,
    CB_BACK_MAIN, CB_CANCEL,
    CB_MENU_PROFILE, CB_MENU_WALLET, CB_MENU_HELP, CB_MENU_REFERRAL,
    CB_PROFILE_HISTORY, CB_PROFILE_LEADERBOARD, CB_PROFILE_TRANSFER,
    CB_PROFILE_SETTINGS, CB_PROFILE_REFERRAL,
    CB_WALLET_DEPOSIT, CB_WALLET_WITHDRAW, CB_WALLET_REFRESH,
    CB_WALLET_TIP, CB_WALLET_PROMO,
    CB_COIN_PREFIX, CB_NET_PREFIX,
    CB_GAME_PREFIX, CB_MODE_PREFIX, CB_WAGER_PREFIX, CB_WAGER_CUSTOM,
    CB_GAME_JOIN, CB_GAME_CANCEL,
    CB_ADMIN_BOT_TOGGLE, CB_ADMIN_HOUSE_ADDR, CB_ADMIN_REFERRAL_AMT,
    CB_ADMIN_ADD_PROMO, CB_ADMIN_LIST_PROMOS, CB_ADMIN_TIP_LIMITS,
    CB_ADMIN_STATS, CB_ADMIN_PREFIX,
)
from .games import GroupGame, GameType, GameMode, GameStatus, result_text, lobby_text, active_text
from .models import Store, User
from . import swap as swap_module

logger = logging.getLogger(__name__)
_START_TIME: float = time.time()
_runner = None


# ══════════════════════════════════════════════════════════════════════════════
#  Logging
# ══════════════════════════════════════════════════════════════════════════════

_QUIET = {"httpx","httpcore","telegram","telegram.ext","apscheduler",
          "asyncio","sqlalchemy.engine","aiohttp","web3"}
_COL   = {"DEBUG":"\033[36m","INFO":"\033[32m","WARNING":"\033[33m",
           "ERROR":"\033[31m","CRITICAL":"\033[35m"}

class _CFmt(logging.Formatter):
    def format(self, r):
        r2 = logging.makeLogRecord(r.__dict__)
        if sys.stderr.isatty():
            r2.levelname = f"{_COL.get(r2.levelname,'')}{r2.levelname:8s}\033[0m"
        return logging.Formatter("%(asctime)s [%(levelname)s] %(name)s — %(message)s","%H:%M:%S").format(r2)

def configure_logging(level="INFO", log_file="", max_bytes=10*1024*1024, backup_count=5):
    num = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger(); root.setLevel(num); root.handlers.clear()
    ch = logging.StreamHandler(sys.stderr); ch.setLevel(num); ch.setFormatter(_CFmt())
    root.addHandler(ch)
    if log_file:
        p = Path(log_file); p.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(p, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)-8s] %(name)s — %(message)s"))
        root.addHandler(fh)
    for n in _QUIET:
        logging.getLogger(n).setLevel(max(num, logging.WARNING))


# ══════════════════════════════════════════════════════════════════════════════
#  Health server
# ══════════════════════════════════════════════════════════════════════════════

async def start_health_server(host="0.0.0.0", port=8080):
    global _runner
    async def _health(req):
        from .database import ping, pool_status
        ok, msg = await ping()
        return web.json_response({"status":"ok" if ok else "degraded",
            "uptime_s":int(time.time()-_START_TIME),
            "database":{"ok":ok,"message":msg},
            "pool": await pool_status()}, status=200 if ok else 503)
    app = web.Application()
    app.router.add_get("/health", _health)
    app.router.add_get("/", _health)
    app.router.add_get("/ready", lambda r: web.Response(text="ready"))
    _runner = web.AppRunner(app, access_log=None)
    await _runner.setup()
    await web.TCPSite(_runner, host, port).start()
    logger.info("Health: http://%s:%d/health", host, port)

async def stop_health_server():
    global _runner
    if _runner:
        await _runner.cleanup(); _runner = None


# ══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _store(ctx): return ctx.application.bot_data["store"]
def _adm(ctx):   return ctx.application.bot_data.setdefault("admin_state", dict(DEFAULT_ADMIN_STATE))

def _user(update: Update, ctx) -> Optional[User]:
    tg = update.effective_user
    if not tg: return None
    u, _ = _store(ctx).get_or_create_user(tg.id, tg.username or "", tg.first_name)
    return u

async def _edit(update: Update, text: str, kb=None):
    q = update.callback_query
    try:
        if q and q.message:
            await q.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        else:
            await update.effective_message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    except BadRequest as e:
        if "not modified" not in str(e).lower(): raise

async def _answer(update: Update, text="", alert=False):
    if update.callback_query:
        await update.callback_query.answer(text, show_alert=alert)

def _back(data=CB_BACK_MAIN, label="⬅️ Back"):
    return InlineKeyboardButton(label, callback_data=data)


# ══════════════════════════════════════════════════════════════════════════════
#  Keyboards
# ══════════════════════════════════════════════════════════════════════════════

def main_menu_kb(socials: dict = None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🎮 PvP Games",   callback_data="menu:games"),
         InlineKeyboardButton("🏠 House Games", callback_data="house:menu")],
        [InlineKeyboardButton("👤 Profile",  callback_data=CB_MENU_PROFILE),
         InlineKeyboardButton("💰 Wallet",   callback_data=CB_MENU_WALLET)],
        [InlineKeyboardButton("👥 Referral", callback_data=CB_MENU_REFERRAL),
         InlineKeyboardButton("❓ Help",      callback_data=CB_MENU_HELP)],
    ]
    if socials:
        social_defs = [
            ("channel", "📢 Channel"),
            ("chat",    "💬 Chat"),
            ("twitter", "🐦 Twitter"),
            ("tiktok",  "🎵 TikTok"),
            ("youtube", "📺 YouTube"),
            ("discord", "🎮 Discord"),
        ]
        active = [(label, socials[key]) for key, label in social_defs if socials.get(key)]
        for i in range(0, len(active), 2):
            row = []
            for label, url in active[i:i+2]:
                row.append(InlineKeyboardButton(label, url=url))
            rows.append(row)
    return InlineKeyboardMarkup(rows)

def profile_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Referral system",  callback_data=CB_PROFILE_REFERRAL)],
        [InlineKeyboardButton("📋 Game history",     callback_data=CB_PROFILE_HISTORY)],
        [InlineKeyboardButton("🏆 Top users",        callback_data=CB_PROFILE_LEADERBOARD)],
        [InlineKeyboardButton("⚙️ Settings",         callback_data=CB_PROFILE_SETTINGS)],
        [_back()],
    ])

def wallet_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Deposit",    callback_data=CB_WALLET_DEPOSIT),
         InlineKeyboardButton("📤 Withdraw",   callback_data=CB_WALLET_WITHDRAW)],
        [InlineKeyboardButton("💝 Tip user",   callback_data=CB_WALLET_TIP),
         InlineKeyboardButton("🎟 Promo code", callback_data=CB_WALLET_PROMO)],
        [InlineKeyboardButton("🔄 Refresh",    callback_data=CB_WALLET_REFRESH)],
        [_back()],
    ])

def coin_grid_kb(action: str):
    """Grid of all coins — 2 per row."""
    items = list(COINS.items())
    rows  = []
    for i in range(0, len(items), 2):
        row = []
        for sym, info in items[i:i+2]:
            row.append(InlineKeyboardButton(
                f"{info['emoji']} {sym}",
                callback_data=f"{CB_COIN_PREFIX}{action}:{sym}",
            ))
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data=CB_CANCEL)])
    return InlineKeyboardMarkup(rows)

def wager_kb():
    btns = [InlineKeyboardButton(f"${w}", callback_data=f"{CB_WAGER_PREFIX}{w}") for w in PRESET_WAGERS]
    rows = [btns[i:i+4] for i in range(0, len(btns), 4)]
    rows.append([InlineKeyboardButton("✏️ Custom", callback_data=CB_WAGER_CUSTOM)])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data=CB_CANCEL)])
    return InlineKeyboardMarkup(rows)

def game_type_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎲 Dice",    callback_data=f"{CB_GAME_PREFIX}dice"),
         InlineKeyboardButton("🎳 Bowling", callback_data=f"{CB_GAME_PREFIX}bowling")],
        [InlineKeyboardButton("🎯 Darts",   callback_data=f"{CB_GAME_PREFIX}darts")],
        [InlineKeyboardButton("❌ Cancel",  callback_data=CB_CANCEL)],
    ])

def game_mode_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Normal",       callback_data=f"{CB_MODE_PREFIX}normal"),
         InlineKeyboardButton("Crazy 🤪",     callback_data=f"{CB_MODE_PREFIX}crazy")],
        [InlineKeyboardButton("Double ×2",    callback_data=f"{CB_MODE_PREFIX}double"),
         InlineKeyboardButton("Double Crazy", callback_data=f"{CB_MODE_PREFIX}double_crazy")],
        [InlineKeyboardButton("❌ Cancel",    callback_data=CB_CANCEL)],
    ])

def join_game_kb(game_id: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚔️ Join Game", callback_data=f"{CB_GAME_JOIN}{game_id}")],
        [InlineKeyboardButton("❌ Cancel",    callback_data=f"{CB_GAME_CANCEL}:{game_id}")],
    ])

def admin_kb(adm: dict):
    bot_s = "✅ ON" if adm.get("bot_betting_enabled") else "❌ OFF"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🤖 Bot betting: {bot_s}", callback_data=CB_ADMIN_BOT_TOGGLE)],
        [InlineKeyboardButton("📊 Stats & house balance",  callback_data=CB_ADMIN_STATS)],
        [InlineKeyboardButton("🏦 House addresses",        callback_data=CB_ADMIN_HOUSE_ADDR)],
        [InlineKeyboardButton("💸 Withdraw house funds",   callback_data="admin:house_withdraw")],
        [InlineKeyboardButton("👥 Referral bonus",         callback_data=CB_ADMIN_REFERRAL_AMT)],
        [InlineKeyboardButton("🎟 Add promo code",         callback_data=CB_ADMIN_ADD_PROMO)],
        [InlineKeyboardButton("📋 List promos",            callback_data=CB_ADMIN_LIST_PROMOS)],
        [InlineKeyboardButton("💝 Tip limits",             callback_data=CB_ADMIN_TIP_LIMITS)],
        [InlineKeyboardButton("🔗 Social links",           callback_data="admin:socials")],
    ])

def preferred_coin_kb():
    """Ask user which coin they want for withdrawals."""
    items = list(COINS.items())
    rows  = []
    for i in range(0, len(items), 2):
        row = []
        for sym, info in items[i:i+2]:
            row.append(InlineKeyboardButton(
                f"{info['emoji']} {sym}",
                callback_data=f"pref_coin:{sym}",
            ))
        rows.append(row)
    return InlineKeyboardMarkup(rows)


# ══════════════════════════════════════════════════════════════════════════════
#  /start
# ══════════════════════════════════════════════════════════════════════════════

def _main_menu_with_socials(ctx) -> InlineKeyboardMarkup:
    """Build main menu keyboard with current social links."""
    socials = _adm(ctx).get("socials", {})
    return main_menu_kb(socials)


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg  = update.effective_user
    sto = _store(ctx)
    u, created = sto.get_or_create_user(tg.id, tg.username or "", tg.first_name)

    # Handle referral
    args = ctx.args
    if created and args:
        try:
            ref_id = int(args[0].replace("ref_",""))
            if ref_id != tg.id:
                referrer = sto.get_user(ref_id)
                if referrer:
                    u.referred_by = ref_id
                    referrer.referral_count += 1
                    bonus = _adm(ctx).get("referral_bonus", Decimal("0.50"))
                    if bonus > 0:
                        referrer.credit_usd(bonus)
                        try:
                            await ctx.bot.send_message(ref_id,
                                f"👥 *Referral bonus!*\n{u.display_name()} joined via your link.\n"
                                f"You earned *${bonus:.2f}*!",
                                parse_mode=ParseMode.MARKDOWN)
                        except TelegramError: pass
        except (ValueError, AttributeError): pass

    if created:
        text = (
            f"🎮 *Welcome to Web3Bet, {tg.first_name}!*\n\n"
            f"Play provably fair games in group chats.\n"
            f"Bet in USD, pay out in any crypto!\n\n"
            f"*How to play:*\n"
            f"1. Deposit any crypto — balance shown in USD\n"
            f"2. Add bot to a group chat\n"
            f"3. /dice /bowl or /darts — set a USD wager\n"
            f"4. Another player joins and you both roll\n"
            f"5. Winner withdraws in any coin via swap!"
        )
    else:
        text = f"👋 Welcome back, *{tg.first_name}*!\n\nBalance: *${u.usd_balance:.2f}*"

    await update.message.reply_text(text, reply_markup=_main_menu_with_socials(ctx), parse_mode=ParseMode.MARKDOWN)


# ══════════════════════════════════════════════════════════════════════════════
#  Profile
# ══════════════════════════════════════════════════════════════════════════════

async def games_menu_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show all available games with descriptions."""
    await _answer(update)
    msg = (
        "\U0001f3ae *Games*\n\n"
        "All games are played in group chats.\n"
        "Add the bot to a group first!\n\n"
        "\U0001f3b2 *Dice* \u2014 Roll 1\u20136, highest wins\n"
        "\U0001f3b3 *Bowling* \u2014 Bowl the pins, highest wins\n"
        "\U0001f3af *Darts* \u2014 3 throws each, highest total wins\n\n"
        "*Game modes:*\n"
        "\u2022 Normal \u2014 highest score wins\n"
        "\u2022 Crazy \U0001f92a \u2014 lowest score wins\n"
        "\u2022 Double \xd72 \u2014 2 rolls, scores added\n"
        "\u2022 Double Crazy \u2014 2 rolls, lowest total wins\n\n"
        "*How to start:*\n"
        "Go to your group and type /dice, /bowl or /darts"
    )
    await _edit(update, msg,
        InlineKeyboardMarkup([
            [InlineKeyboardButton("\U0001f3b2 Dice",    callback_data="game_info:dice"),
             InlineKeyboardButton("\U0001f3b3 Bowling", callback_data="game_info:bowling")],
            [InlineKeyboardButton("\U0001f3af Darts",   callback_data="game_info:darts")],
            [_back()],
        ]))


async def _show_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u   = _user(update, ctx)
    tg  = update.effective_user
    reg = datetime.datetime.fromtimestamp(u.registered_at).strftime("%d.%m.%Y")
    fav = GAME_TYPES.get(u.favourite_game,{}).get("label","—") if u.favourite_game else "—"
    pref = u.preferred_coin if u.preferred_coin else "Not set"

    text = (
        f"👤 *Profile*\n\n"
        f"ℹ️ User: {u.display_name()} `({tg.id})`\n"
        f"🎖 Rank: {u.rank}\n"
        f"💵 Balance: *${u.usd_balance:.2f}*\n\n"
        f"⚡ Total games: *{u.games_played}*\n"
        f"💸 Total wagered: *${u.total_wagered:.2f}*\n"
        f"🏆 Total won: *${u.total_won:.2f}*\n\n"
        f"🎲 Favourite: {fav}\n"
        f"🎉 Biggest win: *${u.biggest_win:.2f}*\n"
        f"💳 Payout coin: *{pref}*\n\n"
        f"📅 Registered: {reg}"
    )
    await _edit(update, text, profile_kb())


async def profile_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _answer(update)
    data = update.callback_query.data

    if data == CB_MENU_PROFILE:
        await _show_profile(update, ctx)

    elif data == CB_PROFILE_REFERRAL:
        u   = _user(update, ctx)
        adm = _adm(ctx)
        bot = await ctx.bot.get_me()
        link  = f"https://t.me/{bot.username}?start=ref_{u.telegram_id}"
        bonus = adm.get("referral_bonus", Decimal("0.50"))
        await _edit(update,
            f"👥 *Referral System*\n\nEarn *${bonus:.2f}* for every friend who joins!\n\n"
            f"Your link:\n`{link}`\n\nReferrals: *{u.referral_count}*",
            InlineKeyboardMarkup([[_back(CB_MENU_PROFILE,"⬅️ Back")]]))

    elif data == CB_PROFILE_HISTORY:
        u     = _user(update, ctx)
        games = _store(ctx).get_games_for_user(u.telegram_id)
        if not games:
            text = "📋 *Game History*\n\n_No games yet._"
        else:
            lines = []
            for g in games:
                result = "🏆 Win" if g.winner_id==u.telegram_id else ("🤝 Tie" if g.winner_id is None else "💀 Loss")
                score  = g.p1_score if g.creator_id==u.telegram_id else g.p2_score
                lines.append(f"{GAME_TYPES[g.game_type.value]['emoji']} {result} — score {score} — ${g.wager_usd:.2f}")
            text = "📋 *Game History (last 5)*\n\n" + "\n".join(lines)
        await _edit(update, text, InlineKeyboardMarkup([[_back(CB_MENU_PROFILE,"⬅️ Back")]]))

    elif data == CB_PROFILE_LEADERBOARD:
        top = _store(ctx).leaderboard(10)
        if not top:
            text = "🏆 *Leaderboard*\n\n_No games yet!_"
        else:
            medals = ["🥇","🥈","🥉"]+[f"{i}." for i in range(4,11)]
            rows   = [f"{medals[i]} {u.display_name()} — {u.games_won}W / {u.games_played}G ({u.win_rate:.0f}%)"
                      for i,u in enumerate(top)]
            text = "🏆 *Top Players*\n\n" + "\n".join(rows)
        await _edit(update, text, InlineKeyboardMarkup([[_back(CB_MENU_PROFILE,"⬅️ Back")]]))

    elif data == CB_PROFILE_SETTINGS:
        u = _user(update, ctx)
        await _edit(update,
            f"⚙️ *Settings*\n\nPayout coin: *{u.preferred_coin or 'Not set'}*\n\nChange your preferred withdrawal coin:",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Change payout coin", callback_data="settings:change_coin")],
                [_back(CB_MENU_PROFILE,"⬅️ Back")],
            ]))

    elif data == "settings:change_coin":
        await _edit(update,
            "💳 *Choose your preferred payout coin*\n\n"
            "When you withdraw, your USD balance will be swapped to this coin via ChangeNow:",
            preferred_coin_kb())


# ══════════════════════════════════════════════════════════════════════════════
#  Preferred coin selection
# ══════════════════════════════════════════════════════════════════════════════

async def pref_coin_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _answer(update)
    coin = update.callback_query.data.removeprefix("pref_coin:")
    u    = _user(update, ctx)
    if coin in COINS:
        u.preferred_coin = coin
        # If this was from the withdrawal flow, continue there
        if ctx.user_data.get("withdraw_pending"):
            ctx.user_data.pop("withdraw_pending")
            await _start_withdrawal(update, ctx, u)
            return
        await _edit(update,
            f"✅ Payout coin set to *{COINS[coin]['emoji']} {coin}*\n\n"
            f"From now on, withdrawals will be swapped to {coin}.",
            InlineKeyboardMarkup([[_back()]]))


# ══════════════════════════════════════════════════════════════════════════════
#  Wallet
# ══════════════════════════════════════════════════════════════════════════════

async def _show_wallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = _user(update, ctx)
    # Show coin holdings if any
    holdings = ""
    if u.coin_holdings:
        lines = [f"  • {coin}: {amt:.6f}" for coin, amt in u.coin_holdings.items() if amt > 0]
        if lines:
            holdings = "\n\n*Deposited coins:*\n" + "\n".join(lines)
    pref = u.preferred_coin if u.preferred_coin else "Not set (asked on first withdrawal)"
    await _edit(update,
        f"💰 *Your Wallet*\n\n"
        f"💵 *Balance: ${u.usd_balance:.2f} USD*{holdings}\n\n"
        f"💳 Payout coin: *{pref}*\n\n"
        f"📊 {u.games_played} games · {u.games_won} wins",
        wallet_kb())


async def wallet_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _answer(update)
    data = update.callback_query.data
    if data in (CB_MENU_WALLET, CB_WALLET_REFRESH):
        await _show_wallet(update, ctx)
    elif data == CB_WALLET_DEPOSIT:
        await _edit(update, "📥 *Deposit*\n\nChoose a coin to deposit:", coin_grid_kb("deposit"))
    elif data == CB_WALLET_WITHDRAW:
        await _handle_withdraw_start(update, ctx)
    elif data == CB_WALLET_TIP:
        await _edit(update,
            "💝 *Tip a User*\n\n`/tip @username amount`\n\nExample: `/tip @john 5`\n_(amount in USD)_",
            InlineKeyboardMarkup([[_back(CB_MENU_WALLET,"⬅️ Back")]]))
    elif data == CB_WALLET_PROMO:
        await _edit(update,
            "🎟 *Promo Code*\n\n`/promo YOURCODE`",
            InlineKeyboardMarkup([[_back(CB_MENU_WALLET,"⬅️ Back")]]))


# ══════════════════════════════════════════════════════════════════════════════
#  Deposit — real wallet generation
# ══════════════════════════════════════════════════════════════════════════════

async def coin_selected(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """User picked a coin from the deposit grid."""
    await _answer(update)
    data  = update.callback_query.data  # coin:deposit:ETH  or  coin:withdraw:ETH
    parts = data.split(":")
    if len(parts) < 3: return
    action, sym = parts[1], parts[2]
    if sym not in COINS:
        await _answer(update, "Unknown coin.", alert=True); return

    if action == "deposit":
        # Multi-network coins → show network picker first
        if sym in MULTI_NETWORK_COINS:
            nets = MULTI_NETWORK_COINS[sym]
            coin = COINS[sym]
            rows = []
            for n in nets:
                rows.append([InlineKeyboardButton(
                    n["label"],
                    callback_data=f"deposit_net:{sym}:{n['network']}"
                )])
            rows.append([_back(CB_WALLET_DEPOSIT, "⬅️ Back")])
            await _edit(update,
                f"\U0001f4e5 *Deposit {coin['emoji']} {sym}*\n\nSelect network:",
                InlineKeyboardMarkup(rows))
        else:
            # Single-network coin → generate address directly
            await _do_deposit(update, ctx, sym, COINS[sym]["network"])

    elif action == "withdraw":
        u   = _user(update, ctx)
        coin = COINS[sym]
        net  = coin["network"]
        await _handle_withdraw_start(update, ctx)


async def deposit_network_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """User picked a network from the USDT/USDC/DAI network picker."""
    await _answer(update)
    # data = deposit_net:USDT:bsc
    parts = update.callback_query.data.split(":")
    if len(parts) < 3: return
    sym, net = parts[1], parts[2]
    await _do_deposit(update, ctx, sym, net)


async def _do_deposit(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE,
    sym: str, network: str
) -> None:
    """Generate (or retrieve) a deposit address and show it."""
    u    = _user(update, ctx)
    coin = COINS.get(sym, {})
    wm   = ctx.application.bot_data.get("wallet_manager")

    # Address key includes network so USDT-ETH and USDT-BSC are separate
    addr_key = f"{sym}:{network}"

    if addr_key not in u.deposit_addresses:
        if not wm:
            await _edit(update,
                "⚠️ *Wallet manager not connected.*\nTry again in a moment.",
                InlineKeyboardMarkup([[_back(CB_WALLET_DEPOSIT, "⬅️ Back")]]))
            return
        try:
            # ERC-20 tokens on EVM chains share the same 0x address as the
            # native coin of that network — so reuse if already generated
            evm_nets = {"ethereum", "bsc", "polygon"}
            if network in evm_nets:
                # Check if we already have an EVM wallet for this network
                native_key = f"NATIVE:{network}"
                if native_key in u.deposit_addresses:
                    address     = u.deposit_addresses[native_key]
                    enc_key     = u.deposit_keys[native_key]
                else:
                    info        = await wm.generate_wallet(network)
                    address     = info.address
                    enc_key     = info.encrypted_private_key
                    u.deposit_addresses[native_key] = address
                    u.deposit_keys[native_key]      = enc_key
            else:
                info    = await wm.generate_wallet(network)
                address = info.address
                enc_key = info.encrypted_private_key

            u.deposit_addresses[addr_key] = address
            u.deposit_keys[addr_key]      = enc_key

            # Save wallet address to Neon immediately
            engine = ctx.application.bot_data.get("db_engine")
            if engine:
                try:
                    from .persistence import save_wallet, mark_dirty
                    await save_wallet(engine, u.telegram_id, sym, network, address, enc_key)
                    mark_dirty(u.telegram_id)
                except Exception as exc:
                    logger.warning("Could not save wallet to DB: %s", exc)

            # Register with blockchain monitor for automatic deposit detection
            monitor = ctx.application.bot_data.get("monitor")
            if monitor:
                from .blockchain import WatchedAddress
                monitor.add_address(WatchedAddress(
                    user_id=u.telegram_id,
                    address=address,
                    network=network,
                    coin_symbol=sym,
                ))

        except Exception as exc:
            logger.error("Wallet gen %s/%s failed: %s", sym, network, exc)
            await _edit(update,
                f"❌ Could not generate {sym} address. Try again later.",
                InlineKeyboardMarkup([[_back(CB_WALLET_DEPOSIT, "⬅️ Back")]]))
            return

    address   = u.deposit_addresses[addr_key]
    net_label = NETWORKS.get(network, {}).get("label", network)
    emoji     = coin.get("emoji", "")

    deposit_text = (
        f"\U0001f4e5 *Deposit {emoji} {sym}*\n\n"
        f"Network: *{net_label}*\n\n"
        f"\u21d4 *Send to:*\n`{address}`\n\n"
        f"Balance updates in USD after confirmation.\n"
        f"Only send {sym} on {net_label.split()[0]} network."
    )
    await _edit(update, deposit_text,
        InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Copy address", switch_inline_query=address)],
            [_back(CB_WALLET_DEPOSIT, "⬅️ Back")],
        ]))


# ══════════════════════════════════════════════════════════════════════════════
#  Withdrawal — USD → chosen coin via ChangeNow swap
# ══════════════════════════════════════════════════════════════════════════════

async def _handle_withdraw_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = _user(update, ctx)
    if u.usd_balance <= 0:
        await _answer(update, "❌ No balance to withdraw.", alert=True); return

    if not u.preferred_coin:
        # First time — ask preferred coin
        ctx.user_data["withdraw_pending"] = True
        await _edit(update,
            f"💳 *First Withdrawal*\n\n"
            f"Choose your preferred payout coin.\n"
            f"Your *${u.usd_balance:.2f}* will be swapped to this coin via ChangeNow.\n\n"
            f"You can change this anytime in Settings:",
            preferred_coin_kb())
    else:
        await _start_withdrawal(update, ctx, u)


async def _start_withdrawal(update: Update, ctx: ContextTypes.DEFAULT_TYPE, u: User):
    coin = u.preferred_coin
    await _edit(update,
        f"📤 *Withdraw*\n\n"
        f"Balance: *${u.usd_balance:.2f}*\n"
        f"Payout coin: *{COINS[coin]['emoji']} {coin}*\n\n"
        f"How much USD do you want to withdraw?\n"
        f"_(or type 'all' to withdraw everything)_",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("💯 Withdraw all", callback_data="withdraw:all")],
            [InlineKeyboardButton("🔄 Change coin",  callback_data="withdraw:change_coin")],
            [InlineKeyboardButton("❌ Cancel",        callback_data=CB_CANCEL)],
        ]))
    ctx.user_data["withdraw_step"] = "amount"


async def withdraw_amount_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _answer(update)
    data = update.callback_query.data
    u    = _user(update, ctx)

    if data == "withdraw:all":
        ctx.user_data["withdraw_usd"] = str(u.usd_balance)
        await _ask_withdraw_address(update, ctx, u)
    elif data == "withdraw:change_coin":
        await _edit(update,
            "💳 *Change payout coin:*",
            preferred_coin_kb())
        ctx.user_data["withdraw_pending"] = True


async def withdraw_amount_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if ctx.user_data.get("withdraw_step") != "amount":
        return ConversationHandler.END
    try:
        amount = Decimal(update.message.text.strip())
    except InvalidOperation:
        await update.message.reply_text("❌ Invalid amount. Enter a number or 'all':"); return State.WITHDRAW_AMOUNT
    u = _user(update, ctx)
    if amount <= 0 or amount > u.usd_balance:
        await update.message.reply_text(f"❌ Must be between $0.01 and ${u.usd_balance:.2f}."); return State.WITHDRAW_AMOUNT
    ctx.user_data["withdraw_usd"] = str(amount)
    await _ask_withdraw_address(update, ctx, u, from_message=True)
    return State.WITHDRAW_ADDRESS


async def _ask_withdraw_address(update, ctx, u: User, from_message=False):
    coin   = u.preferred_coin
    amount = Decimal(ctx.user_data.get("withdraw_usd","0"))

    # Get swap estimate
    estimate_text = "_(fetching estimate…)_"
    pf = swap_module.price_feed
    cn = swap_module.changenow
    if pf and cn:
        try:
            price = await pf.get_price(coin)
            if price:
                raw_coin_amount = (amount / price).quantize(Decimal("0.00000001"))
                est = await cn.estimate("USDT", coin, raw_coin_amount)
                if est:
                    estimate_text = f"≈ *{est.to_amount:.6f} {coin}*"
                else:
                    estimate_text = f"≈ *{raw_coin_amount:.6f} {coin}* _(direct estimate)_"
        except Exception as exc:
            logger.warning("Estimate error: %s", exc)

    ctx.user_data["withdraw_step"] = "address"
    text = (
        f"📤 *Withdraw ${amount:.2f}*\n\n"
        f"Coin: *{COINS[coin]['emoji']} {coin}*\n"
        f"You receive: {estimate_text}\n\n"
        f"Enter your *{coin}* wallet address:"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=CB_CANCEL)]])
    if from_message:
        await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    else:
        await _edit(update, text, kb)


async def withdraw_address_received(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if ctx.user_data.get("withdraw_step") != "address":
        return ConversationHandler.END
    addr = update.message.text.strip()
    if len(addr) < 20:
        await update.message.reply_text("❌ Invalid address. Try again:"); return State.WITHDRAW_ADDRESS

    u      = _user(update, ctx)
    coin   = u.preferred_coin
    amount = Decimal(ctx.user_data.get("withdraw_usd","0"))

    ctx.user_data["withdraw_address"] = addr
    ctx.user_data["withdraw_step"]    = "confirm"

    # Final estimate
    estimate_text = "calculating…"
    pf = swap_module.price_feed
    cn = swap_module.changenow
    if pf and cn:
        try:
            price = await pf.get_price(coin)
            if price:
                raw = (amount / price).quantize(Decimal("0.00000001"))
                est = await cn.estimate("USDT", coin, raw)
                if est: estimate_text = f"{est.to_amount:.6f} {coin}"
                else:   estimate_text = f"~{raw:.6f} {coin}"
        except Exception: pass

    await update.message.reply_text(
        f"📋 *Confirm Withdrawal*\n\n"
        f"Amount: *${amount:.2f} USD*\n"
        f"Receive: *{estimate_text}*\n"
        f"To: `{addr}`\n"
        f"Via: ChangeNow swap\n\n"
        f"⚠️ _Swap rate is live — final amount may vary slightly_",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm", callback_data="withdraw:confirm"),
             InlineKeyboardButton("❌ Cancel",  callback_data=CB_CANCEL)],
        ]),
        parse_mode=ParseMode.MARKDOWN)
    return State.WITHDRAW_CONFIRM


async def withdraw_confirmed(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await _answer(update)
    if update.callback_query.data == CB_CANCEL:
        await _show_wallet(update, ctx); return ConversationHandler.END

    u      = _user(update, ctx)
    coin   = u.preferred_coin
    amount = Decimal(ctx.user_data.get("withdraw_usd","0"))
    addr   = ctx.user_data.get("withdraw_address","")

    if not u.debit_usd(amount):
        await _edit(update,"❌ Insufficient balance.",wallet_kb()); return ConversationHandler.END

    # Save balance immediately after deduction
    engine = ctx.application.bot_data.get("db_engine")
    if engine:
        from .persistence import save_user, mark_dirty
        mark_dirty(u.telegram_id)
        try:
            await save_user(engine, u)
        except Exception as exc:
            logger.warning("Could not save user after withdrawal deduction: %s", exc)

    await _edit(update,
        f"⏳ *Processing withdrawal…*\n\n"
        f"Swapping ${amount:.2f} → {coin}\n"
        f"To: `{addr}`\n\n"
        f"_You'll be notified when complete._",
        None)

    # Run swap in background
    asyncio.create_task(_execute_swap(ctx, u, coin, amount, addr))

    for k in ("withdraw_usd","withdraw_address","withdraw_step"):
        ctx.user_data.pop(k, None)
    return ConversationHandler.END


async def _execute_swap(ctx, u: User, coin: str, usd_amount: Decimal, to_addr: str):
    """Background task: execute ChangeNow swap and send to user."""
    cn = swap_module.changenow
    pf = swap_module.price_feed

    if not cn or not pf:
        await ctx.bot.send_message(u.telegram_id,
            f"❌ Swap service unavailable. Your ${usd_amount:.2f} has been refunded.",
            parse_mode=ParseMode.MARKDOWN)
        u.credit_usd(usd_amount)
        return

    try:
        # Convert USD to USDT amount for swap input
        usdt_price = await pf.get_price("USDT")
        from_amount = (usd_amount / (usdt_price or Decimal("1"))).quantize(Decimal("0.01"))

        # Create ChangeNow exchange: USDT → coin
        # House USDT address is the refund address
        house_addr = _adm_state_global.get("house_addresses",{}).get("bsc","") if _adm_state_global else ""
        tx = await cn.create_exchange("USDT", coin, from_amount, to_addr, refund_addr=house_addr or None)

        if not tx:
            await ctx.bot.send_message(u.telegram_id,
                f"❌ Swap failed. Your ${usd_amount:.2f} has been refunded.")
            u.credit_usd(usd_amount)
            return

        await ctx.bot.send_message(u.telegram_id,
            f"✅ *Swap created!*\n\n"
            f"Exchange ID: `{tx.exchange_id}`\n"
            f"Send: {tx.deposit_amount} USDT → {tx.deposit_address}\n"
            f"You receive: ~{tx.to_amount:.6f} {coin}\n"
            f"To: `{to_addr}`\n\n"
            f"_ChangeNow is processing your swap. This usually takes 5-30 minutes._",
            parse_mode=ParseMode.MARKDOWN)

        # Poll for completion
        for _ in range(60):
            await asyncio.sleep(30)
            status = await cn.get_status(tx.exchange_id)
            if status == "finished":
                await ctx.bot.send_message(u.telegram_id,
                    f"🎉 *Withdrawal complete!*\n\n"
                    f"{tx.to_amount:.6f} {coin} sent to `{to_addr}`",
                    parse_mode=ParseMode.MARKDOWN)
                return
            if status == "failed":
                u.credit_usd(usd_amount)
                await ctx.bot.send_message(u.telegram_id,
                    f"❌ Swap failed. Your ${usd_amount:.2f} has been refunded.")
                return

    except Exception as exc:
        logger.error("Swap execution error: %s", exc)
        u.credit_usd(usd_amount)
        await ctx.bot.send_message(u.telegram_id,
            f"❌ Error processing swap. Your ${usd_amount:.2f} has been refunded.")

_adm_state_global: dict = {}  # set in post_init


# ══════════════════════════════════════════════════════════════════════════════
#  Tip & Promo
# ══════════════════════════════════════════════════════════════════════════════

async def tip_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if not args or len(args) < 2:
        await update.message.reply_text("Usage: `/tip @username amount`\nExample: `/tip @john 5`",
                                        parse_mode=ParseMode.MARKDOWN); return
    name = args[0].lstrip("@")
    try: amount = Decimal(args[1])
    except InvalidOperation:
        await update.message.reply_text("❌ Invalid amount."); return

    adm = _adm(ctx)
    if amount < adm.get("tip_min", Decimal("0.01")) or amount > adm.get("tip_max", Decimal("100")):
        await update.message.reply_text(f"❌ Tip must be ${adm['tip_min']} – ${adm['tip_max']}."); return

    sender = _user(update, ctx)
    sto    = _store(ctx)
    target = next((u for u in sto.users.values() if u.username.lower()==name.lower()), None)
    if not target:
        await update.message.reply_text(f"❌ @{name} not found."); return
    if target.telegram_id == sender.telegram_id:
        await update.message.reply_text("❌ Can't tip yourself."); return

    if not sender.debit_usd(amount):
        await update.message.reply_text(f"❌ Insufficient balance. You have ${sender.usd_balance:.2f}."); return

    target.credit_usd(amount)

    # Persist both users after tip
    engine = ctx.application.bot_data.get("db_engine")
    if engine:
        from .persistence import save_user, mark_dirty
        for tu in (sender, target):
            mark_dirty(tu.telegram_id)
            try:
                await save_user(engine, tu)
            except Exception as exc:
                logger.warning("Could not save user after tip: %s", exc)

    await update.message.reply_text(f"💝 *Tipped ${amount:.2f} to @{target.username}!*",
                                    parse_mode=ParseMode.MARKDOWN)
    try:
        await ctx.bot.send_message(target.telegram_id,
            f"💝 *You received a tip!*\n\n${amount:.2f} from {sender.display_name()}",
            parse_mode=ParseMode.MARKDOWN)
    except TelegramError: pass


async def promo_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if not args:
        await update.message.reply_text("Usage: `/promo YOURCODE`", parse_mode=ParseMode.MARKDOWN); return
    code = args[0].upper()
    u    = _user(update, ctx)
    adm  = _adm(ctx)
    codes = adm.get("promo_codes", {})
    if code in u.used_promos:
        await update.message.reply_text("❌ Already used this code."); return
    if code not in codes or codes[code].get("uses_left",0) <= 0:
        await update.message.reply_text("❌ Invalid or expired code."); return
    bonus = Decimal(str(codes[code]["bonus"]))
    u.credit_usd(bonus)
    u.used_promos.append(code)
    codes[code]["uses_left"] -= 1
    await update.message.reply_text(f"🎟 *${bonus:.2f} added to your balance!*",
                                    parse_mode=ParseMode.MARKDOWN)


async def referral_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _answer(update)
    u   = _user(update, ctx)
    adm = _adm(ctx)
    bot = await ctx.bot.get_me()
    link  = f"https://t.me/{bot.username}?start=ref_{u.telegram_id}"
    bonus = adm.get("referral_bonus", Decimal("0.50"))
    await _edit(update,
        f"👥 *Referral System*\n\nEarn *${bonus:.2f}* per friend!\n\n`{link}`\n\nReferrals: *{u.referral_count}*",
        InlineKeyboardMarkup([[_back()]]))


# ══════════════════════════════════════════════════════════════════════════════
#  Group games
# ══════════════════════════════════════════════════════════════════════════════

async def _start_game_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE, game_type: str):
    if update.effective_chat.type == "private":
        await update.message.reply_text("🎮 Add this bot to a group to play!"); return
    if _store(ctx).get_game_for_chat(update.effective_chat.id):
        await update.message.reply_text("⚠️ A game is already running here!"); return
    ctx.user_data["new_game_type"] = game_type
    await update.message.reply_text(
        f"{GAME_TYPES[game_type]['emoji']} *{GAME_TYPES[game_type]['label']}*\n\nChoose mode:",
        reply_markup=game_mode_kb(), parse_mode=ParseMode.MARKDOWN)

async def dice_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _start_game_cmd(update, ctx, "dice")
async def bowl_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _start_game_cmd(update, ctx, "bowling")
async def darts_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _start_game_cmd(update, ctx, "darts")


async def game_mode_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _answer(update)
    mode = update.callback_query.data.removeprefix(CB_MODE_PREFIX)
    ctx.user_data["new_game_mode"] = mode
    await _edit(update,
        f"Mode: *{GAME_MODES[mode]['label']}*\n\nSet your USD wager:",
        wager_kb())


async def game_wager_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _answer(update)
    data = update.callback_query.data
    if data == CB_WAGER_CUSTOM:
        await _edit(update, "✏️ Enter wager in USD (e.g. `2.50`):",
                    InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=CB_CANCEL)]]))
        ctx.user_data["awaiting_wager"] = True
        return
    amount_str = data.removeprefix(CB_WAGER_PREFIX)
    await _create_lobby(update, ctx, amount_str)


async def game_wager_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("awaiting_wager"): return
    ctx.user_data.pop("awaiting_wager")
    await _create_lobby(update, ctx, update.message.text.strip(), True)


async def _create_lobby(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                         amount_str: str, from_msg=False):
    try: amount = Decimal(amount_str)
    except InvalidOperation:
        msg = "❌ Invalid amount."
        if from_msg: await update.message.reply_text(msg)
        else: await _answer(update, msg, alert=True)
        return

    if amount < MIN_WAGER or amount > MAX_WAGER:
        msg = f"❌ Must be ${MIN_WAGER}–${MAX_WAGER}."
        if from_msg: await update.message.reply_text(msg)
        else: await _answer(update, msg, alert=True)
        return

    tg  = update.effective_user
    u   = _user(update, ctx)
    if not u.debit_usd(amount):
        msg = f"❌ Insufficient. Balance: ${u.usd_balance:.2f}"
        if from_msg: await update.message.reply_text(msg)
        else: await _answer(update, msg, alert=True)
        return

    game = GroupGame(
        game_id    = str(uuid.uuid4()),
        chat_id    = update.effective_chat.id,
        message_id = 0,
        game_type  = GameType(ctx.user_data.get("new_game_type","dice")),
        game_mode  = GameMode(ctx.user_data.get("new_game_mode","normal")),
        wager_usd  = amount,
        creator_id  = tg.id,
        creator_name = u.display_name(),
    )
    _store(ctx).add_game(game)

    if from_msg:
        msg = await update.message.reply_text(
            lobby_text(game), reply_markup=join_game_kb(game.game_id),
            parse_mode=ParseMode.MARKDOWN)
    else:
        msg = await ctx.bot.send_message(
            update.effective_chat.id, lobby_text(game),
            reply_markup=join_game_kb(game.game_id),
            parse_mode=ParseMode.MARKDOWN)
    game.message_id = msg.message_id

    ctx.job_queue.run_once(
        _join_timeout,
        when=cfg.game_join_timeout,
        data={"game_id":game.game_id,"chat_id":game.chat_id,
              "creator_id":game.creator_id,"amount":str(amount),
              "message_id":msg.message_id},
        name=f"join_{game.game_id}", chat_id=game.chat_id)

    for k in ("new_game_type","new_game_mode"):
        ctx.user_data.pop(k,None)


async def join_game_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _answer(update)
    game_id = update.callback_query.data.removeprefix(CB_GAME_JOIN)
    sto     = _store(ctx)
    game    = sto.get_game(game_id)
    if not game or game.status != GameStatus.WAITING:
        await _answer(update,"❌ Game not found or started.",alert=True); return
    tg = update.effective_user
    if tg.id == game.creator_id:
        await _answer(update,"❌ Can't join your own game!",alert=True); return
    u = _user(update, ctx)
    if not u.debit_usd(game.wager_usd):
        await _answer(update,f"❌ Need ${game.wager_usd:.2f}. You have ${u.usd_balance:.2f}.",alert=True); return

    game.joiner_id   = tg.id
    game.joiner_name = u.display_name()
    game.status      = GameStatus.ACTIVE

    for job in ctx.job_queue.get_jobs_by_name(f"join_{game_id}"):
        job.schedule_removal()

    try:
        await ctx.bot.edit_message_text(active_text(game),
            chat_id=game.chat_id, message_id=game.message_id,
            parse_mode=ParseMode.MARKDOWN)
    except TelegramError: pass

    await ctx.bot.send_message(game.chat_id,
        f"⚔️ *{game.creator_name}* vs *{game.joiner_name}*\n\n"
        f"Send {game.emoji} to roll! Each needs *{game.rolls_per_player}* roll(s).",
        parse_mode=ParseMode.MARKDOWN)


async def cancel_game_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _answer(update)
    game_id = update.callback_query.data.split(":")[-1]
    sto     = _store(ctx)
    game    = sto.get_game(game_id)
    if not game: return
    if update.effective_user.id != game.creator_id:
        await _answer(update,"Only creator can cancel.",alert=True); return
    if game.status != GameStatus.WAITING: return
    u = _user(update, ctx)
    u.credit_usd(game.wager_usd)
    game.status = GameStatus.CANCELLED
    sto.remove_game(game_id)
    for job in ctx.job_queue.get_jobs_by_name(f"join_{game_id}"):
        job.schedule_removal()
    try:
        await ctx.bot.edit_message_text("❌ Game cancelled. Wager refunded.",
            chat_id=game.chat_id, message_id=game.message_id)
    except TelegramError: pass


async def _join_timeout(ctx: ContextTypes.DEFAULT_TYPE):
    d    = ctx.job.data
    sto  = ctx.application.bot_data["store"]
    game = sto.get_game(d["game_id"])
    if not game or game.status != GameStatus.WAITING: return
    creator = sto.get_user(d["creator_id"])
    if creator: creator.credit_usd(Decimal(d["amount"]))
    game.status = GameStatus.EXPIRED
    sto.remove_game(d["game_id"])
    try:
        await ctx.bot.edit_message_text("⏰ Game expired — no one joined. Refunded.",
            chat_id=d["chat_id"], message_id=d["message_id"])
    except TelegramError: pass


# ══════════════════════════════════════════════════════════════════════════════
#  Dice emoji handler
# ══════════════════════════════════════════════════════════════════════════════

async def dice_message_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg  = update.message
    if not msg or not msg.dice: return
    tg   = update.effective_user
    sto  = _store(ctx)
    game = sto.get_game_for_chat(update.effective_chat.id)
    if not game or game.status != GameStatus.ACTIVE: return

    emoji_to_type = {DiceEmoji.DICE:"dice", DiceEmoji.BOWLING:"bowling", DiceEmoji.DARTS:"darts"}
    if emoji_to_type.get(msg.dice.emoji) != game.game_type.value: return
    if tg.id not in (game.creator_id, game.joiner_id): return

    game.add_roll(tg.id, msg.dice.value)

    try:
        await ctx.bot.edit_message_text(active_text(game),
            chat_id=game.chat_id, message_id=game.message_id,
            parse_mode=ParseMode.MARKDOWN)
    except TelegramError: pass

    if game.both_done():
        game.resolve()
        p1 = sto.get_user(game.creator_id)
        p2 = sto.get_user(game.joiner_id)

        if game.winner_id is None:
            if p1: p1.credit_usd(game.winner_payout_usd)
            if p2: p2.credit_usd(game.winner_payout_usd)
        elif game.winner_id == game.creator_id:
            if p1: p1.credit_usd(game.winner_payout_usd)
        else:
            if p2: p2.credit_usd(game.winner_payout_usd)

        # Rake goes to house
        sto.add_rake(game.house_fee_usd)
        sto.total_volume_usd += game.wager_usd * 2

        # Update stats
        for pid, user in ((game.creator_id,p1),(game.joiner_id,p2)):
            if user:
                user.games_played += 1
                user.total_wagered += game.wager_usd
                user.favourite_game = game.game_type.value
                if game.winner_id == pid:
                    user.games_won += 1
                    user.total_won += game.winner_payout_usd
                    if game.winner_payout_usd > user.biggest_win:
                        user.biggest_win = game.winner_payout_usd
                elif game.winner_id is None:
                    user.total_won += game.winner_payout_usd

        sto.remove_game(game.game_id)
        await ctx.bot.send_message(game.chat_id, result_text(game),
                                   parse_mode=ParseMode.MARKDOWN)

        # Persist balances after game
        engine = ctx.application.bot_data.get("db_engine")
        if engine:
            from .persistence import save_user, save_house, mark_dirty
            for user_obj in (p1, p2):
                if user_obj:
                    mark_dirty(user_obj.telegram_id)
                    try:
                        await save_user(engine, user_obj)
                    except Exception as exc:
                        logger.warning("Could not save user after game: %s", exc)
            try:
                await save_house(engine, sto)
            except Exception as exc:
                logger.warning("Could not save house after game: %s", exc)


# ══════════════════════════════════════════════════════════════════════════════
#  Admin panel
# ══════════════════════════════════════════════════════════════════════════════

async def admin_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not cfg.is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only."); return
    adm = _adm(ctx)
    global _adm_state_global
    _adm_state_global = adm
    await update.message.reply_text("⚙️ *Admin Panel*",
                                    reply_markup=admin_kb(adm),
                                    parse_mode=ParseMode.MARKDOWN)


async def admin_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _answer(update)
    if not cfg.is_admin(update.effective_user.id):
        await _answer(update,"⛔ Admin only.",alert=True); return

    data = update.callback_query.data
    adm  = _adm(ctx)
    sto  = _store(ctx)

    if data == CB_ADMIN_BOT_TOGGLE:
        adm["bot_betting_enabled"] = not adm.get("bot_betting_enabled",False)
        s = "✅ Enabled" if adm["bot_betting_enabled"] else "❌ Disabled"
        await _edit(update, f"⚙️ *Admin Panel*\nBot betting: {s}", admin_kb(adm))

    elif data == CB_ADMIN_STATS:
        # Get live prices for house coin values
        pf     = swap_module.price_feed
        lines  = []
        total  = Decimal("0")
        if pf:
            try:
                prices = await pf.get_usd_prices()
                for coin, amt in sto.house_coin_holdings.items():
                    if amt > 0:
                        price = prices.get(coin, Decimal("0"))
                        usd   = (amt * price).quantize(Decimal("0.01"))
                        total += usd
                        lines.append(f"  • {coin}: {amt:.6f} (≈${usd:.2f})")
            except Exception: pass

        holdings_text = "\n".join(lines) if lines else "  None"
        await _edit(update,
            f"📊 *Stats & House Balance*\n\n"
            f"*House fund:* ${sto.house_balance_usd:.2f} USD\n\n"
            f"*House coin holdings:*\n{holdings_text}\n"
            f"*Coin holdings total:* ≈${total:.2f}\n\n"
            f"*All time:*\n"
            f"  Users: {len(sto.users)}\n"
            f"  Volume: ${sto.total_volume_usd:.2f}\n"
            f"  Rake collected: ${sto.total_rake_collected:.2f}\n"
            f"  Active games: {len(sto.active_games)}",
            InlineKeyboardMarkup([[_back(CB_ADMIN_PREFIX,"⬅️ Back")]]))

    elif data == CB_ADMIN_HOUSE_ADDR:
        addrs = adm.get("house_addresses",{})
        lines = [f"  *{n}:*\n  `{a or 'not set'}`" for n,a in addrs.items()]
        await _edit(update,
            f"🏦 *House Addresses*\n\n" + "\n\n".join(lines) +
            "\n\nReply: `network address`\nExample: `bsc 0xYourAddress`",
            InlineKeyboardMarkup([[_back(CB_ADMIN_PREFIX,"⬅️ Back")]]))
        ctx.user_data["admin_action"] = "set_house_addr"

    elif data == "admin:house_withdraw":
        await _edit(update,
            f"💸 *Withdraw House Funds*\n\n"
            f"House balance: *${sto.house_balance_usd:.2f}*\n\n"
            f"Reply with:\n`amount coin your_address`\n\n"
            f"Example:\n`500 USDT 0xYourAddress`",
            InlineKeyboardMarkup([[_back(CB_ADMIN_PREFIX,"⬅️ Back")]]))
        ctx.user_data["admin_action"] = "house_withdraw"

    elif data == CB_ADMIN_REFERRAL_AMT:
        await _edit(update,
            f"👥 *Referral Bonus*\n\nCurrent: *${adm.get('referral_bonus',0):.2f}*\n\nReply with new USD amount:",
            InlineKeyboardMarkup([[_back(CB_ADMIN_PREFIX,"⬅️ Back")]]))
        ctx.user_data["admin_action"] = "set_referral"

    elif data == CB_ADMIN_ADD_PROMO:
        await _edit(update,
            "🎟 *Add Promo*\n\nReply: `CODE usd_bonus uses`\nExample: `SUMMER10 10.00 50`",
            InlineKeyboardMarkup([[_back(CB_ADMIN_PREFIX,"⬅️ Back")]]))
        ctx.user_data["admin_action"] = "add_promo"

    elif data == CB_ADMIN_LIST_PROMOS:
        codes = adm.get("promo_codes",{})
        text  = "🎟 *Promos*\n\n" + ("\n".join(
            f"• `{c}` — ${v['bonus']:.2f} × {v['uses_left']} uses" for c,v in codes.items()
        ) if codes else "_None_")
        await _edit(update, text, InlineKeyboardMarkup([[_back(CB_ADMIN_PREFIX,"⬅️ Back")]]))

    elif data == CB_ADMIN_TIP_LIMITS:
        await _edit(update,
            f"💝 *Tip Limits*\n\nMin: *${adm.get('tip_min',0):.2f}*  Max: *${adm.get('tip_max',0):.2f}*\n\nReply: `min max`",
            InlineKeyboardMarkup([[_back(CB_ADMIN_PREFIX,"⬅️ Back")]]))
        ctx.user_data["admin_action"] = "set_tip_limits"

    elif data == "admin:socials":
        socials = adm.get("socials", {})
        lines   = []
        defs = [("channel","📢 Channel"),("chat","💬 Chat"),("twitter","🐦 Twitter"),
                ("tiktok","🎵 TikTok"),("youtube","📺 YouTube"),("discord","🎮 Discord")]
        for key, label in defs:
            val = socials.get(key, "")
            lines.append(f"{label}: {('`' + val + '`') if val else '_not set_'}")
        social_text = (
            "\U0001f517 *Social Links*\n\n"
            + "\n".join(lines)
            + "\n\nReply: `platform url`"
            + "\nExample: `twitter https://twitter.com/web3bet`"
            + "\nPlatforms: channel, chat, twitter, tiktok, youtube, discord"
        )
        await _edit(update, social_text,
            InlineKeyboardMarkup([[_back(CB_ADMIN_PREFIX,"\u2b05\ufe0f Back")]]))
        ctx.user_data["admin_action"] = "set_social"

    elif data == CB_ADMIN_PREFIX:
        await _edit(update,"⚙️ *Admin Panel*", admin_kb(adm))


async def admin_text_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not cfg.is_admin(update.effective_user.id): return
    action = ctx.user_data.pop("admin_action",None)
    if not action: return
    adm  = _adm(ctx)
    sto  = _store(ctx)
    text = update.message.text.strip()

    if action == "set_house_addr":
        parts = text.split(maxsplit=1)
        if len(parts)<2: await update.message.reply_text("❌ Format: network address"); return
        adm.setdefault("house_addresses",{})[parts[0].lower()] = parts[1]
        await update.message.reply_text(f"✅ *{parts[0]}* address set to `{parts[1]}`",
                                        parse_mode=ParseMode.MARKDOWN)

    elif action == "set_social":
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await update.message.reply_text("❌ Format: platform url\nExample: twitter https://twitter.com/web3bet"); return
        platform = parts[0].lower()
        url      = parts[1].strip()
        valid    = {"channel","chat","twitter","tiktok","youtube","discord"}
        if platform not in valid:
            await update.message.reply_text(f"❌ Platform must be one of:\n{', '.join(sorted(valid))}"); return
        adm.setdefault("socials",{})[platform] = url
        await update.message.reply_text(
            f"✅ *{platform.title()}* link saved!\n\n"
            f"URL: {url}\n\n"
            f"Users will now see a *{platform.title()}* button in the main menu.",
            parse_mode=ParseMode.MARKDOWN)

    elif action == "house_withdraw":
        parts = text.split()
        if len(parts)<3: await update.message.reply_text("❌ Format: amount coin address"); return
        try:
            amt  = Decimal(parts[0])
            coin = parts[1].upper()
            addr = parts[2]
        except Exception:
            await update.message.reply_text("❌ Invalid format."); return
        if not sto.debit_house_usd(amt):
            await update.message.reply_text(f"❌ House balance ${sto.house_balance_usd:.2f} insufficient."); return
        await update.message.reply_text(
            f"✅ House withdrawal initiated\n${amt:.2f} → {coin}\nTo: `{addr}`\n\n"
            f"_Send manually from house wallet or via ChangeNow._",
            parse_mode=ParseMode.MARKDOWN)

    elif action == "set_referral":
        try:
            adm["referral_bonus"] = Decimal(text)
            await update.message.reply_text(f"✅ Referral bonus: *${adm['referral_bonus']:.2f}*",
                                            parse_mode=ParseMode.MARKDOWN)
        except Exception: await update.message.reply_text("❌ Invalid amount.")

    elif action == "add_promo":
        parts = text.split()
        if len(parts)<3: await update.message.reply_text("❌ Format: CODE amount uses"); return
        try:
            adm.setdefault("promo_codes",{})[parts[0].upper()] = {
                "bonus": Decimal(parts[1]), "uses_left": int(parts[2])}
            await update.message.reply_text(f"✅ Promo `{parts[0].upper()}` added",
                                            parse_mode=ParseMode.MARKDOWN)
        except Exception: await update.message.reply_text("❌ Invalid format.")

    elif action == "set_tip_limits":
        parts = text.split()
        if len(parts)<2: await update.message.reply_text("❌ Format: min max"); return
        try:
            adm["tip_min"] = Decimal(parts[0]); adm["tip_max"] = Decimal(parts[1])
            await update.message.reply_text(f"✅ Tips: ${adm['tip_min']} – ${adm['tip_max']}",
                                            parse_mode=ParseMode.MARKDOWN)
        except Exception: await update.message.reply_text("❌ Invalid.")


# ══════════════════════════════════════════════════════════════════════════════
#  Help / Back / Status / Cancel
# ══════════════════════════════════════════════════════════════════════════════

async def help_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _answer(update)
    await _edit(update, (
        "❓ *Help*\n\n"
        "*Group commands:*\n"
        "/dice — Dice game 🎲\n/bowl — Bowling 🎳\n/darts — Darts 🎯\n\n"
        "*Modes:* Normal · Crazy (lowest wins) · Double (2 rolls) · Double Crazy\n\n"
        "*Balance system:*\n"
        "• Deposit any crypto → converted to USD\n"
        "• Bet in USD ($1, $5, $10…)\n"
        "• Withdraw in any coin via ChangeNow swap\n\n"
        "*Darts scoring:* Bullseye=50 · Outer=25 · Triple=15 · Double=10 · Single=5 · Miss=0\n\n"
        "*Fee:* 5% rake on games"
    ), InlineKeyboardMarkup([[_back()]]))


async def back_main_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _answer(update)
    u = _user(update, ctx)
    await _edit(update, f"🏠 *Main Menu*\nBalance: *${u.usd_balance:.2f}*" if u else "🏠 *Main Menu*",
                main_menu_kb())


async def cancel_conv(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await _answer(update)
    await back_main_cb(update, ctx)
    return ConversationHandler.END


async def status_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not cfg.is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only."); return
    from .database import ping, pool_status
    ok, msg = await ping(); pool = await pool_status()
    sto = _store(ctx)
    up  = int(time.time()-_START_TIME); h,r=divmod(up,3600); m,s=divmod(r,60)
    try:
        import psutil, os
        mem = f"{psutil.Process(os.getpid()).memory_info().rss/1024/1024:.1f} MB"
    except ImportError: mem = "N/A"
    await update.message.reply_text(
        f"📊 *Status*\n\nUptime: {h}h {m}m {s}s\n"
        f"DB: {'✅' if ok else '❌'} {msg}\n"
        f"Pool: {pool.get('checked_out',0)} active\n"
        f"Users: {len(sto.users)}\nActive games: {len(sto.active_games)}\n"
        f"House: ${sto.house_balance_usd:.2f}\nMemory: {mem}",
        parse_mode=ParseMode.MARKDOWN)


async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception:", exc_info=ctx.error)
    if isinstance(ctx.error, Forbidden): return
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ Something went wrong. Try again.",
                                                       reply_markup=main_menu_kb())
        except TelegramError: pass


# ══════════════════════════════════════════════════════════════════════════════
#  Handler registration
# ══════════════════════════════════════════════════════════════════════════════

async def admin_help_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not cfg.is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only."); return
    lines = [
        "⚙️ *Admin Command Reference*",
        "",
        "*Balance & Users*",
        "`/resetbalance all` — reset everyone to $0",
        "`/resetbalance @user` — reset one user",
        "`/givebalance @user amount` — give USD to user",
        "",
        "*Games*",
        "`/toggle_game [game] on/off` — toggle game",
        "`/list_games` — see all games + status",
        "",
        "*Game names:* dice, bowling, darts,",
        "coinflip, rps, roulette, blackjack,",
        "baccarat, keno, crash, plinko,",
        "mines, limbo, tower",
        "",
        "*House Fund*",
        "`/admin` → 📊 Stats — view house balance",
        "`/admin` → 💸 Withdraw house funds",
        "",
        "*Socials*",
        "`/admin` → 🔗 Social links",
        "Reply: `twitter https://...`",
        "Platforms: channel, chat, twitter,",
        "tiktok, youtube, discord",
        "",
        "*Promo Codes*",
        "`/admin` → 🎟 Add promo",
        "Reply: `CODE amount uses`",
        "Example: `SUMMER10 10.00 50`",
        "",
        "*Referral Bonus*",
        "`/admin` → 👥 Referral bonus → reply amount",
        "",
        "*Tip Limits*",
        "`/admin` → 💝 Tip limits → reply: `min max`",
        "",
        "*Bot Betting*",
        "`/admin` → 🤖 Bot betting — toggle on/off",
        "",
        "`/adminhelp` — show this list",
    ]
    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown")


async def give_balance_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/givebalance @username amount"""
    if not cfg.is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only."); return
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /givebalance @username amount"); return
    target_name = args[0].lstrip("@").lower()
    try:
        amount = Decimal(args[1])
    except Exception:
        await update.message.reply_text("❌ Invalid amount."); return
    sto  = _store(ctx)
    user = next((u for u in sto.users.values()
                 if u.username.lower() == target_name or str(u.telegram_id) == target_name), None)
    if not user:
        await update.message.reply_text(f"❌ User @{target_name} not found."); return
    user.credit_usd(amount)
    engine = ctx.application.bot_data.get("db_engine")
    if engine:
        from .persistence import save_user, mark_dirty
        mark_dirty(user.telegram_id)
        try:
            await save_user(engine, user)
        except Exception: pass
    msg = (
        "\u2705 Gave *$" + f"{amount:.2f}" + "* to " + user.display_name() + "\n"
        "New balance: *$" + f"{user.usd_balance:.2f}" + "*"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def reset_balance_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/resetbalance [username|all] — admin only."""
    if not cfg.is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only."); return

    args = ctx.args
    sto  = _store(ctx)

    if not args:
        await update.message.reply_text(
            "Usage:\n"
            "/resetbalance all — reset everyone\n"
            "/resetbalance @username — reset one user"
        ); return

    target = args[0].lstrip("@").lower()

    if target == "all":
        count = 0
        for u in sto.users.values():
            u.usd_balance     = Decimal("0")
            u.coin_holdings   = {}
            count += 1
        await update.message.reply_text(f"✅ Reset {count} user balances to $0.00")

    else:
        user = next((u for u in sto.users.values()
                     if u.username.lower() == target or str(u.telegram_id) == target), None)
        if not user:
            await update.message.reply_text(f"❌ User @{target} not found."); return
        old_bal          = user.usd_balance
        user.usd_balance = Decimal("0")
        user.coin_holdings = {}
        await update.message.reply_text(
            f"✅ Reset {user.display_name()} balance\n"
            f"Was: ${old_bal:.2f} → Now: $0.00",
            parse_mode="Markdown")


def register_all_handlers(app: Application) -> None:
    # Commands
    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("admin",  admin_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("tip",          tip_command))
    app.add_handler(CommandHandler("resetbalance", reset_balance_command))
    app.add_handler(CommandHandler("givebalance",  give_balance_command))
    app.add_handler(CommandHandler("adminhelp",    admin_help_command))
    app.add_handler(CommandHandler("promo",  promo_command))
    app.add_handler(CommandHandler("dice",   dice_command))
    app.add_handler(CommandHandler("bowl",   bowl_command))
    app.add_handler(CommandHandler("darts",  darts_command))

    # Dice emoji in groups
    app.add_handler(MessageHandler(
        (filters.Dice(emoji=DiceEmoji.DICE) | filters.Dice(emoji=DiceEmoji.BOWLING) | filters.Dice(emoji=DiceEmoji.DARTS)) & filters.ChatType.GROUPS,
        dice_message_handler))

    # Withdrawal conversation
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(wallet_cb, pattern=f"^{CB_WALLET_WITHDRAW}$")],
        states={
            State.WITHDRAW_AMOUNT:  [
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, withdraw_amount_text),
                CallbackQueryHandler(withdraw_amount_cb, pattern=r"^withdraw:"),
                CallbackQueryHandler(cancel_conv, pattern=f"^{CB_CANCEL}$"),
            ],
            State.WITHDRAW_ADDRESS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, withdraw_address_received),
                CallbackQueryHandler(cancel_conv, pattern=f"^{CB_CANCEL}$"),
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

    # Admin text replies
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
        admin_text_handler), group=1)

    # Game wager text
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS,
        game_wager_text), group=2)

    # Callbacks
    app.add_handler(CallbackQueryHandler(join_game_cb,    pattern=f"^{CB_GAME_JOIN}"))
    app.add_handler(CallbackQueryHandler(cancel_game_cb,  pattern=f"^{CB_GAME_CANCEL}"))
    app.add_handler(CallbackQueryHandler(game_mode_cb,    pattern=f"^{CB_MODE_PREFIX}"))
    app.add_handler(CallbackQueryHandler(game_wager_cb,   pattern=f"^{CB_WAGER_PREFIX}|^{CB_WAGER_CUSTOM}$"))
    app.add_handler(CallbackQueryHandler(deposit_network_cb, pattern=r"^deposit_net:"))
    app.add_handler(CallbackQueryHandler(coin_selected,   pattern=f"^{CB_COIN_PREFIX}"))
    app.add_handler(CallbackQueryHandler(pref_coin_cb,    pattern=r"^pref_coin:"))
    app.add_handler(CallbackQueryHandler(withdraw_amount_cb, pattern=r"^withdraw:"))
    app.add_handler(CallbackQueryHandler(profile_cb,      pattern=r"^menu:profile$|^profile:|^settings:"))
    app.add_handler(CallbackQueryHandler(wallet_cb,       pattern=r"^menu:wallet$|^wallet:"))
    app.add_handler(CallbackQueryHandler(referral_cb,     pattern=r"^menu:referral$"))
    app.add_handler(CallbackQueryHandler(help_cb,         pattern=r"^menu:help$"))
    app.add_handler(CallbackQueryHandler(admin_cb,        pattern=r"^admin:"))
    app.add_handler(CallbackQueryHandler(games_menu_cb,   pattern=r"^menu:games$"))
    app.add_handler(CallbackQueryHandler(back_main_cb,    pattern=f"^{CB_BACK_MAIN}$"))
    app.add_handler(CallbackQueryHandler(cancel_conv,     pattern=f"^{CB_CANCEL}$"))

    app.add_error_handler(error_handler)
