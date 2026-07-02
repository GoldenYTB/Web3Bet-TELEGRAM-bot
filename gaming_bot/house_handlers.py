"""
house_handlers.py — Telegram handlers for all house games.

All house games run in private chat (DM with bot).
Each game has its own conversation flow via CallbackQuery buttons.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from decimal import Decimal, InvalidOperation
from typing import Dict, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .house_games import (
    HouseGameSession, HouseGameStatus,
    # CoinFlip
    play_coinflip, coinflip_result_text,
    # RPS
    play_rps, rps_result_text,
    # Roulette
    play_roulette, roulette_result_text,
    # Blackjack
    start_blackjack, blackjack_hit, blackjack_stand, blackjack_status_text,
    # Baccarat
    play_baccarat, baccarat_result_text,
    # Keno
    play_keno, keno_result_text,
    # Crash
    start_crash, crash_cashout, crash_result_text,
    # Plinko
    play_plinko, plinko_result_text,
    # Mines
    start_mines, mines_pick, mines_cashout, mines_status_text,
    # Limbo
    play_limbo, limbo_result_text,
    # Tower
    start_tower, tower_pick, tower_cashout, tower_status_text,
)
from .models import Store, User
from .config import cfg, PRESET_WAGERS

logger = logging.getLogger(__name__)

# Active sessions: user_id → HouseGameSession
_sessions: Dict[int, HouseGameSession] = {}


def _store(ctx): return ctx.application.bot_data["store"]
def _registry(ctx): return ctx.application.bot_data["game_registry"]

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
    except TelegramError as e:
        if "not modified" not in str(e).lower(): raise

async def _answer(update: Update, text="", alert=False):
    if update.callback_query:
        await update.callback_query.answer(text, show_alert=alert)

def _back_btn(label="⬅️ Back", data="house:menu"):
    return InlineKeyboardButton(label, callback_data=data)

def _cancel_btn():
    return InlineKeyboardButton("❌ Cancel", callback_data="house:cancel")


# ── Wager selection ───────────────────────────────────────────────────────────

def wager_kb(game_key: str) -> InlineKeyboardMarkup:
    btns = [InlineKeyboardButton(f"${w}", callback_data=f"house:wager:{game_key}:{w}")
            for w in PRESET_WAGERS]
    rows = [btns[i:i+4] for i in range(0, len(btns), 4)]
    rows.append([InlineKeyboardButton("✏️ Custom", callback_data=f"house:wager_custom:{game_key}")])
    rows.append([_cancel_btn()])
    return InlineKeyboardMarkup(rows)


# ── House game menu ───────────────────────────────────────────────────────────

async def house_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show all enabled house games."""
    await _answer(update)
    reg     = _registry(ctx)
    games   = reg.enabled("house")
    u       = _user(update, ctx)
    bal     = f"${u.usd_balance:.2f}" if u else "$0.00"

    if not games:
        await _edit(update,
            "🏠 *House Games*\n\nNo house games are currently enabled.\nCheck back soon!",
            InlineKeyboardMarkup([[_back_btn("⬅️ Back", "back:main")]]))
        return

    rows = []
    for i in range(0, len(games), 2):
        row = []
        for g in games[i:i+2]:
            row.append(InlineKeyboardButton(
                f"{g.emoji} {g.name}",
                callback_data=f"house:select:{g.key}"
            ))
        rows.append(row)
    rows.append([_back_btn("⬅️ Back", "back:main")])

    await _edit(update,
        f"🏠 *House Games*\n\nBalance: *{bal}*\n\nPick a game:",
        InlineKeyboardMarkup(rows))


async def house_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """User picked a game — show wager selection."""
    await _answer(update)
    game_key = update.callback_query.data.split(":")[2]
    reg      = _registry(ctx)
    game     = reg.get(game_key)
    if not game or not game.enabled:
        await _answer(update, "This game is not available.", alert=True); return

    ctx.user_data["house_game"] = game_key
    await _edit(update,
        f"{game.emoji} *{game.name}*\n\n_{game.description}_\n\nChoose your wager:",
        wager_kb(game_key))


async def house_wager_selected(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Wager picked — route to game-specific flow."""
    await _answer(update)
    parts    = update.callback_query.data.split(":")
    game_key = parts[2]
    amount   = Decimal(parts[3])
    await _start_house_game(update, ctx, game_key, amount)


async def house_wager_custom(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _answer(update)
    game_key = update.callback_query.data.split(":")[2]
    ctx.user_data["house_game"]        = game_key
    ctx.user_data["awaiting_wager"]    = True
    await _edit(update,
        "✏️ Enter your wager amount in USD:",
        InlineKeyboardMarkup([[_cancel_btn()]]))


async def house_wager_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle custom wager typed as text."""
    if not ctx.user_data.get("awaiting_wager"): return
    ctx.user_data.pop("awaiting_wager")
    try:
        amount = Decimal(update.message.text.strip())
    except InvalidOperation:
        await update.message.reply_text("❌ Invalid amount. Try again."); return
    game_key = ctx.user_data.get("house_game", "")
    await _start_house_game(update, ctx, game_key, amount)


async def _start_house_game(update, ctx, game_key: str, amount: Decimal):
    """Deduct wager, create session, show game UI."""
    u = _user(update, ctx)
    if amount < cfg.min_wager or amount > cfg.max_wager:
        msg = f"❌ Wager must be ${cfg.min_wager}–${cfg.max_wager}."
        if update.callback_query:
            await _answer(update, msg, alert=True)
        else:
            await update.message.reply_text(msg)
        return
    if not u.debit_usd(amount):
        msg = f"❌ Insufficient balance. You have ${u.usd_balance:.2f}."
        if update.callback_query:
            await _answer(update, msg, alert=True)
        else:
            await update.message.reply_text(msg)
        return

    session = HouseGameSession(
        session_id=str(uuid.uuid4()),
        user_id=u.telegram_id,
        game=game_key,
        wager_usd=amount,
    )
    _sessions[u.telegram_id] = session

    # Route to game
    game_starters = {
        "coinflip":  _show_coinflip,
        "rps":       _show_rps,
        "roulette":  _show_roulette,
        "blackjack": _show_blackjack,
        "baccarat":  _show_baccarat,
        "keno":      _show_keno,
        "crash":     _show_crash,
        "plinko":    _show_plinko,
        "mines":     _show_mines,
        "limbo":     _show_limbo,
        "tower":     _show_tower,
    }
    starter = game_starters.get(game_key)
    if starter:
        await starter(update, ctx, session)
    else:
        u.credit_usd(amount)  # refund
        await _edit(update, "❌ Game not implemented yet.", None)


def _finish_session(session: HouseGameSession, store) -> None:
    """Update house balance and stats."""
    profit = session.profit_usd
    if profit < 0:
        # House won — add to house fund
        store.add_rake(-profit)
    else:
        # Player won — debit house fund
        store.debit_house_usd(profit)
    # Remove session
    _sessions.pop(session.user_id, None)


# ══════════════════════════════════════════════════════════════════════════════
#  COINFLIP
# ══════════════════════════════════════════════════════════════════════════════

async def _show_coinflip(update, ctx, session: HouseGameSession):
    await _edit(update,
        f"🪙 *CoinFlip* — Wager: ${session.wager_usd}\n\nPick a side:",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🪙 Heads", callback_data="hg:coinflip:heads"),
             InlineKeyboardButton("🔄 Tails", callback_data="hg:coinflip:tails")],
            [_cancel_btn()],
        ]))

async def hg_coinflip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _answer(update)
    choice  = update.callback_query.data.split(":")[2]
    u       = _user(update, ctx)
    session = _sessions.get(u.telegram_id)
    if not session or session.game != "coinflip": return
    session = play_coinflip(session, choice)
    if session.payout_usd > 0: u.credit_usd(session.payout_usd)
    _finish_session(session, _store(ctx))
    await _edit(update, coinflip_result_text(session) + "\n\n" + session.proof(),
        InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Play again", callback_data="house:select:coinflip")],
                               [_back_btn("🏠 House games", "house:menu")]]))


# ══════════════════════════════════════════════════════════════════════════════
#  RPS
# ══════════════════════════════════════════════════════════════════════════════

async def _show_rps(update, ctx, session: HouseGameSession):
    await _edit(update,
        f"✊ *Rock Paper Scissors* — Wager: ${session.wager_usd}\n\nChoose:",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("✊ Rock",     callback_data="hg:rps:rock"),
             InlineKeyboardButton("🖐 Paper",    callback_data="hg:rps:paper"),
             InlineKeyboardButton("✂️ Scissors", callback_data="hg:rps:scissors")],
            [_cancel_btn()],
        ]))

async def hg_rps(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _answer(update)
    choice  = update.callback_query.data.split(":")[2]
    u       = _user(update, ctx)
    session = _sessions.get(u.telegram_id)
    if not session or session.game != "rps": return
    session = play_rps(session, choice)
    if session.payout_usd > 0: u.credit_usd(session.payout_usd)
    _finish_session(session, _store(ctx))
    await _edit(update, rps_result_text(session) + "\n\n" + session.proof(),
        InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Play again", callback_data="house:select:rps")],
                               [_back_btn("🏠 House games", "house:menu")]]))


# ══════════════════════════════════════════════════════════════════════════════
#  ROULETTE
# ══════════════════════════════════════════════════════════════════════════════

async def _show_roulette(update, ctx, session: HouseGameSession):
    await _edit(update,
        f"🎡 *Roulette* — Wager: ${session.wager_usd}\n\nChoose bet type:",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🔴 Red",      callback_data="hg:roulette:color:red"),
             InlineKeyboardButton("⚫ Black",    callback_data="hg:roulette:color:black")],
            [InlineKeyboardButton("Odd",         callback_data="hg:roulette:odd:odd"),
             InlineKeyboardButton("Even",        callback_data="hg:roulette:even:even")],
            [InlineKeyboardButton("1–18",        callback_data="hg:roulette:low:low"),
             InlineKeyboardButton("19–36",       callback_data="hg:roulette:high:high")],
            [InlineKeyboardButton("1st 12",      callback_data="hg:roulette:dozen:1"),
             InlineKeyboardButton("2nd 12",      callback_data="hg:roulette:dozen:2"),
             InlineKeyboardButton("3rd 12",      callback_data="hg:roulette:dozen:3")],
            [InlineKeyboardButton("🎯 Number (35x)", callback_data="hg:roulette:number_prompt:0")],
            [_cancel_btn()],
        ]))

async def hg_roulette(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _answer(update)
    parts    = update.callback_query.data.split(":")
    bet_type = parts[2]
    bet_val  = parts[3] if len(parts) > 3 else ""
    u        = _user(update, ctx)
    session  = _sessions.get(u.telegram_id)
    if not session or session.game != "roulette": return

    if bet_type == "number_prompt":
        ctx.user_data["roulette_awaiting_number"] = True
        await _edit(update,
            "Enter a number (0–36):",
            InlineKeyboardMarkup([[_cancel_btn()]]))
        return

    session = play_roulette(session, bet_type, bet_val)
    if session.payout_usd > 0: u.credit_usd(session.payout_usd)
    _finish_session(session, _store(ctx))
    await _edit(update, roulette_result_text(session) + "\n\n" + session.proof(),
        InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Play again", callback_data="house:select:roulette")],
                               [_back_btn("🏠 House games", "house:menu")]]))

async def hg_roulette_number(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.pop("roulette_awaiting_number", False): return
    try: num = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Invalid number."); return
    if not 0 <= num <= 36:
        await update.message.reply_text("❌ Enter 0–36."); return
    u       = _user(update, ctx)
    session = _sessions.get(u.telegram_id)
    if not session: return
    session = play_roulette(session, "number", str(num))
    if session.payout_usd > 0: u.credit_usd(session.payout_usd)
    _finish_session(session, _store(ctx))
    await update.message.reply_text(
        roulette_result_text(session) + "\n\n" + session.proof(),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Play again", callback_data="house:select:roulette")],
            [_back_btn("🏠 House games", "house:menu")],
        ]))


# ══════════════════════════════════════════════════════════════════════════════
#  BLACKJACK
# ══════════════════════════════════════════════════════════════════════════════

async def _show_blackjack(update, ctx, session: HouseGameSession):
    session = start_blackjack(session)
    _sessions[session.user_id] = session
    await _edit(update,
        blackjack_status_text(session),
        InlineKeyboardMarkup([
            [InlineKeyboardButton("👆 Hit",   callback_data="hg:bj:hit"),
             InlineKeyboardButton("✋ Stand", callback_data="hg:bj:stand")],
            [_cancel_btn()],
        ]))

async def hg_blackjack(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _answer(update)
    action  = update.callback_query.data.split(":")[2]
    u       = _user(update, ctx)
    session = _sessions.get(u.telegram_id)
    if not session or session.game != "blackjack": return
    if action == "hit":
        session = blackjack_hit(session)
    elif action == "stand":
        session = blackjack_stand(session)
    _sessions[u.telegram_id] = session
    done = session.status == HouseGameStatus.COMPLETED
    if done:
        if session.payout_usd > 0: u.credit_usd(session.payout_usd)
        _finish_session(session, _store(ctx))
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Play again", callback_data="house:select:blackjack")],
            [_back_btn("🏠 House games", "house:menu")],
        ])
        await _edit(update, blackjack_status_text(session, reveal_dealer=True) + "\n\n" + session.proof(), kb)
    else:
        await _edit(update, blackjack_status_text(session),
            InlineKeyboardMarkup([
                [InlineKeyboardButton("👆 Hit",   callback_data="hg:bj:hit"),
                 InlineKeyboardButton("✋ Stand", callback_data="hg:bj:stand")],
            ]))


# ══════════════════════════════════════════════════════════════════════════════
#  BACCARAT
# ══════════════════════════════════════════════════════════════════════════════

async def _show_baccarat(update, ctx, session: HouseGameSession):
    await _edit(update,
        f"💎 *Baccarat* — Wager: ${session.wager_usd}\n\nChoose your bet:",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("👤 Player (2x)",    callback_data="hg:baccarat:player"),
             InlineKeyboardButton("🏦 Banker (1.95x)", callback_data="hg:baccarat:banker")],
            [InlineKeyboardButton("🤝 Tie (8x)",       callback_data="hg:baccarat:tie")],
            [_cancel_btn()],
        ]))

async def hg_baccarat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _answer(update)
    bet     = update.callback_query.data.split(":")[2]
    u       = _user(update, ctx)
    session = _sessions.get(u.telegram_id)
    if not session or session.game != "baccarat": return
    session = play_baccarat(session, bet)
    if session.payout_usd > 0: u.credit_usd(session.payout_usd)
    _finish_session(session, _store(ctx))
    await _edit(update, baccarat_result_text(session) + "\n\n" + session.proof(),
        InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Play again", callback_data="house:select:baccarat")],
                               [_back_btn("🏠 House games", "house:menu")]]))


# ══════════════════════════════════════════════════════════════════════════════
#  KENO
# ══════════════════════════════════════════════════════════════════════════════

async def _show_keno(update, ctx, session: HouseGameSession):
    ctx.user_data["keno_picks"] = []
    await _edit(update,
        f"🎯 *Keno* — Wager: ${session.wager_usd}\n\nPick 1–10 numbers (1–80).\nType them separated by spaces:",
        InlineKeyboardMarkup([[_cancel_btn()]]))
    ctx.user_data["awaiting_keno"] = True

async def hg_keno_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.pop("awaiting_keno", False): return
    try:
        picks = [int(x) for x in update.message.text.strip().split() if x.isdigit()]
        picks = [p for p in picks if 1 <= p <= 80]
        picks = list(dict.fromkeys(picks))[:10]  # unique, max 10
        if not picks: raise ValueError
    except (ValueError, TypeError):
        await update.message.reply_text("❌ Enter numbers 1–80."); return
    u       = _user(update, ctx)
    session = _sessions.get(u.telegram_id)
    if not session: return
    session = play_keno(session, picks)
    if session.payout_usd > 0: u.credit_usd(session.payout_usd)
    _finish_session(session, _store(ctx))
    await update.message.reply_text(
        keno_result_text(session) + "\n\n" + session.proof(),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Play again", callback_data="house:select:keno")],
            [_back_btn("🏠 House games", "house:menu")],
        ]))


# ══════════════════════════════════════════════════════════════════════════════
#  CRASH
# ══════════════════════════════════════════════════════════════════════════════

async def _show_crash(update, ctx, session: HouseGameSession):
    session = start_crash(session)
    _sessions[session.user_id] = session
    # Start the multiplier ticker
    asyncio.create_task(_crash_tick(ctx, session, update.effective_chat.id, update.effective_message.message_id if update.effective_message else 0))
    await _edit(update,
        f"🚀 *Crash* — Wager: ${session.wager_usd}\n\nMultiplier rising... 1.00x\n\nCash out before it crashes!",
        InlineKeyboardMarkup([[InlineKeyboardButton("💸 Cash Out", callback_data="hg:crash:cashout")]]))

async def _crash_tick(ctx, session: HouseGameSession, chat_id: int, msg_id: int):
    """Simulate crash multiplier rising."""
    import time
    mult      = Decimal("1.00")
    crash_pt  = Decimal(session.state["crash_point"])
    step      = Decimal("0.01")
    interval  = 0.5  # seconds per tick
    start     = time.time()
    while mult < crash_pt:
        await asyncio.sleep(interval)
        elapsed = time.time() - start
        # Exponential growth
        mult = (Decimal("1") + Decimal(str(elapsed * 0.1))).quantize(Decimal("0.01"))
        if mult >= crash_pt:
            break
        session.state["current_mult"] = str(mult)
        # Update message
        try:
            await ctx.bot.edit_message_text(
                f"🚀 *Crash* — Wager: ${session.wager_usd}\n\n🔥 **{mult}x** and climbing...\n\nCash out before it crashes!",
                chat_id=chat_id, message_id=msg_id,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💸 Cash Out", callback_data="hg:crash:cashout")]]))
        except TelegramError: break
    # Crashed
    if _sessions.get(session.user_id) and _sessions[session.user_id].status == HouseGameStatus.ACTIVE:
        session.payout_usd = Decimal("0")
        session.profit_usd = -session.wager_usd
        session.status     = HouseGameStatus.BUSTED
        sto = ctx.application.bot_data["store"]
        sto.add_rake(session.wager_usd)
        _sessions.pop(session.user_id, None)
        try:
            await ctx.bot.edit_message_text(
                crash_result_text(session) + "\n\n" + session.proof(),
                chat_id=chat_id, message_id=msg_id,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Play again", callback_data="house:select:crash")],
                    [InlineKeyboardButton("🏠 House games", callback_data="house:menu")],
                ]))
        except TelegramError: pass

async def hg_crash(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _answer(update)
    u       = _user(update, ctx)
    session = _sessions.get(u.telegram_id)
    if not session or session.game != "crash" or session.status != HouseGameStatus.ACTIVE: return
    current = Decimal(session.state.get("current_mult","1.00"))
    session = crash_cashout(session, current)
    if session.status == HouseGameStatus.CASHED_OUT:
        u.credit_usd(session.payout_usd)
    _finish_session(session, _store(ctx))
    await _edit(update, crash_result_text(session) + "\n\n" + session.proof(),
        InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Play again", callback_data="house:select:crash")],
                               [_back_btn("🏠 House games", "house:menu")]]))


# ══════════════════════════════════════════════════════════════════════════════
#  PLINKO
# ══════════════════════════════════════════════════════════════════════════════

async def _show_plinko(update, ctx, session: HouseGameSession):
    await _edit(update,
        f"⚡ *Plinko* — Wager: ${session.wager_usd}\n\nChoose risk level:",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🟢 Low",    callback_data="hg:plinko:low"),
             InlineKeyboardButton("🟡 Medium", callback_data="hg:plinko:medium"),
             InlineKeyboardButton("🔴 High",   callback_data="hg:plinko:high")],
            [_cancel_btn()],
        ]))

async def hg_plinko(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _answer(update)
    risk    = update.callback_query.data.split(":")[2]
    u       = _user(update, ctx)
    session = _sessions.get(u.telegram_id)
    if not session or session.game != "plinko": return
    session = play_plinko(session, risk)
    if session.payout_usd > 0: u.credit_usd(session.payout_usd)
    _finish_session(session, _store(ctx))
    await _edit(update, plinko_result_text(session) + "\n\n" + session.proof(),
        InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Play again", callback_data="house:select:plinko")],
                               [_back_btn("🏠 House games", "house:menu")]]))


# ══════════════════════════════════════════════════════════════════════════════
#  MINES
# ══════════════════════════════════════════════════════════════════════════════

async def _show_mines(update, ctx, session: HouseGameSession):
    await _edit(update,
        f"💣 *Mines* — Wager: ${session.wager_usd}\n\nHow many mines? (1–24)",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("1", callback_data="hg:mines_start:1"),
             InlineKeyboardButton("3", callback_data="hg:mines_start:3"),
             InlineKeyboardButton("5", callback_data="hg:mines_start:5"),
             InlineKeyboardButton("10", callback_data="hg:mines_start:10")],
            [InlineKeyboardButton("15", callback_data="hg:mines_start:15"),
             InlineKeyboardButton("20", callback_data="hg:mines_start:20"),
             InlineKeyboardButton("24", callback_data="hg:mines_start:24")],
            [_cancel_btn()],
        ]))

async def hg_mines_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _answer(update)
    num_mines = int(update.callback_query.data.split(":")[2])
    u         = _user(update, ctx)
    session   = _sessions.get(u.telegram_id)
    if not session or session.game != "mines": return
    session = start_mines(session, num_mines)
    _sessions[u.telegram_id] = session
    await _show_mines_grid(update, ctx, session)

async def _show_mines_grid(update, ctx, session: HouseGameSession):
    rows = []
    rev  = {r["cell"]: r["mine"] for r in session.state["revealed"]}
    for row in range(5):
        btn_row = []
        for col in range(5):
            cell = row * 5 + col
            if cell in rev:
                label = "💣" if rev[cell] else "💎"
                data  = "hg:mines_noop"
            else:
                label = "🟦"
                data  = f"hg:mines_pick:{cell}"
            btn_row.append(InlineKeyboardButton(label, callback_data=data))
        rows.append(btn_row)
    if session.status == HouseGameStatus.ACTIVE:
        rows.append([InlineKeyboardButton(f"💸 Cash Out ({session.multiplier}x)", callback_data="hg:mines_cashout")])
    await _edit(update, mines_status_text(session), InlineKeyboardMarkup(rows))

async def hg_mines_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _answer(update)
    data = update.callback_query.data
    if data == "hg:mines_noop": return
    cell    = int(data.split(":")[2])
    u       = _user(update, ctx)
    session = _sessions.get(u.telegram_id)
    if not session or session.game != "mines" or session.status != HouseGameStatus.ACTIVE: return
    session = mines_pick(session, cell)
    _sessions[u.telegram_id] = session
    if session.status == HouseGameStatus.BUSTED:
        _finish_session(session, _store(ctx))
        await _edit(update, mines_status_text(session) + "\n\n" + session.proof(),
            InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Play again", callback_data="house:select:mines")],
                                   [_back_btn("🏠 House games", "house:menu")]]))
    elif session.status == HouseGameStatus.CASHED_OUT:
        u.credit_usd(session.payout_usd)
        _finish_session(session, _store(ctx))
        await _edit(update, mines_status_text(session) + "\n\n" + session.proof(),
            InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Play again", callback_data="house:select:mines")],
                                   [_back_btn("🏠 House games", "house:menu")]]))
    else:
        await _show_mines_grid(update, ctx, session)

async def hg_mines_cashout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _answer(update)
    u       = _user(update, ctx)
    session = _sessions.get(u.telegram_id)
    if not session or session.game != "mines" or session.status != HouseGameStatus.ACTIVE: return
    if session.state["gems"] == 0:
        await _answer(update, "Reveal at least one gem first!", alert=True); return
    session = mines_cashout(session)
    u.credit_usd(session.payout_usd)
    _finish_session(session, _store(ctx))
    await _show_mines_grid(update, ctx, session)


# ══════════════════════════════════════════════════════════════════════════════
#  LIMBO
# ══════════════════════════════════════════════════════════════════════════════

async def _show_limbo(update, ctx, session: HouseGameSession):
    await _edit(update,
        f"🌊 *Limbo* — Wager: ${session.wager_usd}\n\nEnter target multiplier (min 1.01x):",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("1.5x",  callback_data="hg:limbo:1.5"),
             InlineKeyboardButton("2x",    callback_data="hg:limbo:2"),
             InlineKeyboardButton("5x",    callback_data="hg:limbo:5")],
            [InlineKeyboardButton("10x",   callback_data="hg:limbo:10"),
             InlineKeyboardButton("100x",  callback_data="hg:limbo:100"),
             InlineKeyboardButton("1000x", callback_data="hg:limbo:1000")],
            [InlineKeyboardButton("✏️ Custom", callback_data="hg:limbo:custom")],
            [_cancel_btn()],
        ]))

async def hg_limbo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _answer(update)
    val     = update.callback_query.data.split(":")[2]
    u       = _user(update, ctx)
    session = _sessions.get(u.telegram_id)
    if not session or session.game != "limbo": return
    if val == "custom":
        ctx.user_data["awaiting_limbo"] = True
        await _edit(update, "Enter target multiplier:", InlineKeyboardMarkup([[_cancel_btn()]]))
        return
    session = play_limbo(session, Decimal(val))
    if session.payout_usd > 0: u.credit_usd(session.payout_usd)
    _finish_session(session, _store(ctx))
    await _edit(update, limbo_result_text(session) + "\n\n" + session.proof(),
        InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Play again", callback_data="house:select:limbo")],
                               [_back_btn("🏠 House games", "house:menu")]]))

async def hg_limbo_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.pop("awaiting_limbo", False): return
    try: target = Decimal(update.message.text.strip())
    except InvalidOperation:
        await update.message.reply_text("❌ Invalid multiplier."); return
    u       = _user(update, ctx)
    session = _sessions.get(u.telegram_id)
    if not session: return
    session = play_limbo(session, target)
    if session.payout_usd > 0: u.credit_usd(session.payout_usd)
    _finish_session(session, _store(ctx))
    await update.message.reply_text(
        limbo_result_text(session) + "\n\n" + session.proof(),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Play again", callback_data="house:select:limbo")],
            [_back_btn("🏠 House games", "house:menu")],
        ]))


# ══════════════════════════════════════════════════════════════════════════════
#  TOWER
# ══════════════════════════════════════════════════════════════════════════════

async def _show_tower(update, ctx, session: HouseGameSession):
    await _edit(update,
        f"🏗️ *Tower* — Wager: ${session.wager_usd}\n\nChoose difficulty:",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🟢 Easy",   callback_data="hg:tower_start:easy"),
             InlineKeyboardButton("🟡 Medium", callback_data="hg:tower_start:medium"),
             InlineKeyboardButton("🔴 Hard",   callback_data="hg:tower_start:hard")],
            [_cancel_btn()],
        ]))

async def hg_tower_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _answer(update)
    diff    = update.callback_query.data.split(":")[2]
    u       = _user(update, ctx)
    session = _sessions.get(u.telegram_id)
    if not session or session.game != "tower": return
    session = start_tower(session, diff)
    _sessions[u.telegram_id] = session
    await _show_tower_grid(update, ctx, session)

async def _show_tower_grid(update, ctx, session: HouseGameSession):
    s     = session.state
    floor = s["current_floor"]
    tiles = s["tiles"]
    rows  = []
    if session.status == HouseGameStatus.ACTIVE:
        btn_row = []
        for t in range(tiles):
            btn_row.append(InlineKeyboardButton(f"🟦 {t+1}", callback_data=f"hg:tower_pick:{t}"))
        rows.append(btn_row)
        if floor > 0:
            rows.append([InlineKeyboardButton(
                f"💸 Cash Out ({session.multiplier}x = ${(session.wager_usd * session.multiplier).quantize(Decimal('0.01'))})",
                callback_data="hg:tower_cashout")])
    await _edit(update, tower_status_text(session), InlineKeyboardMarkup(rows) if rows else None)

async def hg_tower_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _answer(update)
    tile    = int(update.callback_query.data.split(":")[2])
    u       = _user(update, ctx)
    session = _sessions.get(u.telegram_id)
    if not session or session.game != "tower" or session.status != HouseGameStatus.ACTIVE: return
    session = tower_pick(session, tile)
    _sessions[u.telegram_id] = session
    if session.status in (HouseGameStatus.BUSTED, HouseGameStatus.CASHED_OUT):
        if session.status == HouseGameStatus.CASHED_OUT:
            u.credit_usd(session.payout_usd)
        _finish_session(session, _store(ctx))
        await _edit(update, tower_status_text(session) + "\n\n" + session.proof(),
            InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Play again", callback_data="house:select:tower")],
                                   [_back_btn("🏠 House games", "house:menu")]]))
    else:
        await _show_tower_grid(update, ctx, session)

async def hg_tower_cashout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _answer(update)
    u       = _user(update, ctx)
    session = _sessions.get(u.telegram_id)
    if not session or session.game != "tower" or session.status != HouseGameStatus.ACTIVE: return
    if session.state["current_floor"] == 0:
        await _answer(update, "Clear at least one floor first!", alert=True); return
    session = tower_cashout(session)
    u.credit_usd(session.payout_usd)
    _finish_session(session, _store(ctx))
    await _edit(update, tower_status_text(session) + "\n\n" + session.proof(),
        InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Play again", callback_data="house:select:tower")],
                               [_back_btn("🏠 House games", "house:menu")]]))


# ══════════════════════════════════════════════════════════════════════════════
#  Cancel active session
# ══════════════════════════════════════════════════════════════════════════════

async def house_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _answer(update)
    u       = _user(update, ctx)
    session = _sessions.pop(u.telegram_id, None)
    if session:
        u.credit_usd(session.wager_usd)  # refund
    await _edit(update, "❌ Game cancelled. Wager refunded.",
        InlineKeyboardMarkup([[_back_btn("🏠 House games", "house:menu")]]))


# ══════════════════════════════════════════════════════════════════════════════
#  Register all handlers
# ══════════════════════════════════════════════════════════════════════════════

def register_house_handlers(app) -> None:
    from telegram.ext import CallbackQueryHandler, MessageHandler, filters

    # House menu
    app.add_handler(CallbackQueryHandler(house_menu,           pattern=r"^house:menu$"))
    app.add_handler(CallbackQueryHandler(house_select,         pattern=r"^house:select:"))
    app.add_handler(CallbackQueryHandler(house_wager_selected, pattern=r"^house:wager:"))
    app.add_handler(CallbackQueryHandler(house_wager_custom,   pattern=r"^house:wager_custom:"))
    app.add_handler(CallbackQueryHandler(house_cancel,         pattern=r"^house:cancel$"))

    # CoinFlip
    app.add_handler(CallbackQueryHandler(hg_coinflip, pattern=r"^hg:coinflip:"))
    # RPS
    app.add_handler(CallbackQueryHandler(hg_rps,      pattern=r"^hg:rps:"))
    # Roulette
    app.add_handler(CallbackQueryHandler(hg_roulette, pattern=r"^hg:roulette:"))
    # Blackjack
    app.add_handler(CallbackQueryHandler(hg_blackjack, pattern=r"^hg:bj:"))
    # Baccarat
    app.add_handler(CallbackQueryHandler(hg_baccarat,  pattern=r"^hg:baccarat:"))
    # Crash
    app.add_handler(CallbackQueryHandler(hg_crash,     pattern=r"^hg:crash:"))
    # Plinko
    app.add_handler(CallbackQueryHandler(hg_plinko,    pattern=r"^hg:plinko:"))
    # Mines
    app.add_handler(CallbackQueryHandler(hg_mines_start,   pattern=r"^hg:mines_start:"))
    app.add_handler(CallbackQueryHandler(hg_mines_pick,    pattern=r"^hg:mines_pick:"))
    app.add_handler(CallbackQueryHandler(hg_mines_pick,    pattern=r"^hg:mines_noop$"))
    app.add_handler(CallbackQueryHandler(hg_mines_cashout, pattern=r"^hg:mines_cashout$"))
    # Limbo
    app.add_handler(CallbackQueryHandler(hg_limbo,      pattern=r"^hg:limbo:"))
    # Tower
    app.add_handler(CallbackQueryHandler(hg_tower_start,   pattern=r"^hg:tower_start:"))
    app.add_handler(CallbackQueryHandler(hg_tower_pick,    pattern=r"^hg:tower_pick:"))
    app.add_handler(CallbackQueryHandler(hg_tower_cashout, pattern=r"^hg:tower_cashout$"))

    # Text handlers for games that need number input
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
        _house_text_router), group=5)


async def _house_text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Route text messages to the right game handler."""
    if ctx.user_data.get("awaiting_wager"):
        await house_wager_text(update, ctx)
    elif ctx.user_data.get("awaiting_keno"):
        await hg_keno_text(update, ctx)
    elif ctx.user_data.get("awaiting_limbo"):
        await hg_limbo_text(update, ctx)
    elif ctx.user_data.get("roulette_awaiting_number"):
        await hg_roulette_number(update, ctx)
