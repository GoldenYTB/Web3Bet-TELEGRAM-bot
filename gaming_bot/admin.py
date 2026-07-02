"""
admin.py — Admin commands and panel.

Commands:
  /admin              — open admin panel
  /toggle_game [game] [on/off]
  /list_games
  /status
"""
from __future__ import annotations

import logging
from decimal import Decimal

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from .config import cfg
from .game_registry import GameRegistry

logger = logging.getLogger(__name__)


def _is_admin(user_id: int) -> bool:
    return cfg.is_admin(user_id)

def _store(ctx): return ctx.application.bot_data["store"]
def _registry(ctx) -> GameRegistry: return ctx.application.bot_data["game_registry"]
def _adm(ctx): return ctx.application.bot_data.setdefault("admin_state", {
    "bot_betting_enabled": False,
    "referral_bonus": Decimal("0.50"),
    "promo_codes": {},
    "tip_min": Decimal("0.01"),
    "tip_max": Decimal("100"),
})


def admin_kb(adm: dict) -> InlineKeyboardMarkup:
    bot_s = "✅ ON" if adm.get("bot_betting_enabled") else "❌ OFF"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎮 Game toggles",          callback_data="admin:games")],
        [InlineKeyboardButton("📊 Stats & house balance", callback_data="admin:stats")],
        [InlineKeyboardButton("💸 Withdraw house funds",  callback_data="admin:house_withdraw")],
        [InlineKeyboardButton(f"🤖 Bot betting: {bot_s}", callback_data="admin:bot_toggle")],
        [InlineKeyboardButton("👥 Referral bonus",        callback_data="admin:referral")],
        [InlineKeyboardButton("🎟 Promo codes",           callback_data="admin:promos")],
        [InlineKeyboardButton("💝 Tip limits",            callback_data="admin:tip_limits")],
        [InlineKeyboardButton("🏦 House addresses",       callback_data="admin:house_addr")],
    ])


async def admin_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only."); return
    adm = _adm(ctx)
    await update.message.reply_text("⚙️ *Admin Panel*",
                                    reply_markup=admin_kb(adm),
                                    parse_mode=ParseMode.MARKDOWN)


async def toggle_game_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/toggle_game [game] [on/off]"""
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only."); return
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /toggle_game [game] [on/off]"); return
    key   = args[0].lower()
    state = args[1].lower() in ("on", "true", "1", "yes")
    reg   = _registry(ctx)
    err   = reg.toggle(key, state)
    if err:
        await update.message.reply_text(f"❌ {err}")
    else:
        status = "✅ enabled" if state else "❌ disabled"
        await update.message.reply_text(f"Game `{key}` is now {status}.",
                                        parse_mode=ParseMode.MARKDOWN)


async def list_games_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/list_games"""
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only."); return
    await update.message.reply_text(_registry(ctx).list_text(),
                                    parse_mode=ParseMode.MARKDOWN)


async def admin_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    if not _is_admin(update.effective_user.id):
        await update.callback_query.answer("⛔ Admin only.", show_alert=True); return

    data = update.callback_query.data
    adm  = _adm(ctx)
    sto  = _store(ctx)
    reg  = _registry(ctx)

    async def edit(text, kb=None):
        try:
            await update.callback_query.message.edit_text(
                text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        except Exception: pass

    if data == "admin:stats":
        from . import swap as swap_module
        pf    = swap_module.price_feed
        lines = []
        total = Decimal("0")
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
        holdings = "\n".join(lines) if lines else "  None"
        await edit(
            f"📊 *Stats & House Balance*\n\n"
            f"*House fund:* ${sto.house_balance_usd:.2f} USD\n\n"
            f"*Coin holdings:*\n{holdings}\n"
            f"*Coin total:* ≈${total:.2f}\n\n"
            f"*All time:*\n"
            f"  Users: {len(sto.users)}\n"
            f"  Volume: ${sto.total_volume_usd:.2f}\n"
            f"  Rake: ${sto.total_rake_collected:.2f}\n"
            f"  Active games: {len(sto.active_games)}",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin:back")]]))

    elif data == "admin:games":
        games      = reg.all()
        toggleable = [g for g in games if g.toggleable]
        locked     = [g for g in games if not g.toggleable]
        rows       = []
        for i in range(0, len(toggleable), 2):
            row = []
            for g in toggleable[i:i+2]:
                status = "✅" if g.enabled else "❌"
                toggle = f"admin:toggle:{'off' if g.enabled else 'on'}:{g.key}"
                row.append(InlineKeyboardButton(
                    f"{status} {g.emoji} {g.name}", callback_data=toggle))
            rows.append(row)
        for g in locked:
            rows.append([InlineKeyboardButton(
                f"🔒 {g.emoji} {g.name} (Phase 2)", callback_data="admin:noop")])
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="admin:back")])
        on_count  = sum(1 for g in games if g.enabled)
        off_count = len(games) - on_count
        await edit(
            f"🎮 *Game Toggles*\n\n"
            f"✅ {on_count} on  ❌ {off_count} off\n\n"
            f"Tap any game to toggle on/off:",
            InlineKeyboardMarkup(rows))

    elif data.startswith("admin:toggle:"):
        parts = data.split(":")
        state = parts[2] == "on"
        key   = parts[3]
        err   = reg.toggle(key, state)
        if err:
            await update.callback_query.answer(err, show_alert=True)
        else:
            await update.callback_query.answer(f"{'✅ Enabled' if state else '❌ Disabled'}")
            # Refresh games list
            fake_data = "admin:games"
            update.callback_query.data = fake_data
            await admin_cb(update, ctx)

    elif data == "admin:noop":
        await update.callback_query.answer("This game cannot be toggled (Phase 2)")

    elif data == "admin:bot_toggle":
        adm["bot_betting_enabled"] = not adm.get("bot_betting_enabled", False)
        s = "✅ Enabled" if adm["bot_betting_enabled"] else "❌ Disabled"
        await update.callback_query.answer(f"Bot betting: {s}")
        await edit("⚙️ *Admin Panel*", admin_kb(adm))

    elif data == "admin:house_withdraw":
        await edit(
            f"💸 *Withdraw House Funds*\n\n"
            f"House balance: *${sto.house_balance_usd:.2f}*\n\n"
            f"Reply:\n`amount coin your_address`\n\nExample:\n`500 USDT 0xYourAddress`",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin:back")]]))
        ctx.user_data["admin_action"] = "house_withdraw"

    elif data == "admin:referral":
        await edit(
            f"👥 *Referral Bonus*\n\nCurrent: *${adm.get('referral_bonus', 0):.2f}*\n\nReply with new amount:",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin:back")]]))
        ctx.user_data["admin_action"] = "set_referral"

    elif data == "admin:promos":
        codes = adm.get("promo_codes", {})
        text  = "🎟 *Promos*\n\n" + ("\n".join(
            f"• `{c}` — ${v['bonus']:.2f} × {v['uses_left']} uses" for c,v in codes.items()
        ) if codes else "_None_")
        await edit(text, InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add promo", callback_data="admin:add_promo")],
            [InlineKeyboardButton("⬅️ Back",     callback_data="admin:back")],
        ]))

    elif data == "admin:add_promo":
        await edit("Reply: `CODE amount uses`\nExample: `SUMMER10 10.00 50`",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin:back")]]))
        ctx.user_data["admin_action"] = "add_promo"

    elif data == "admin:tip_limits":
        await edit(
            f"💝 *Tip Limits*\n\nMin: *${adm.get('tip_min',0):.2f}*  Max: *${adm.get('tip_max',0):.2f}*\n\nReply: `min max`",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin:back")]]))
        ctx.user_data["admin_action"] = "set_tip_limits"

    elif data == "admin:house_addr":
        addrs = adm.get("house_addresses", {})
        lines = [f"• {n}: `{a or 'not set'}`" for n,a in addrs.items()]
        await edit(
            f"🏦 *House Addresses*\n\n" + ("\n".join(lines) or "_None set_") +
            "\n\nReply: `network address`",
            InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin:back")]]))
        ctx.user_data["admin_action"] = "set_house_addr"

    elif data == "admin:back":
        await edit("⚙️ *Admin Panel*", admin_kb(adm))


async def admin_text_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id): return
    action = ctx.user_data.pop("admin_action", None)
    if not action: return
    adm  = _adm(ctx)
    sto  = _store(ctx)
    text = update.message.text.strip()

    if action == "house_withdraw":
        parts = text.split()
        if len(parts) < 3:
            await update.message.reply_text("❌ Format: amount coin address"); return
        try:
            amt  = Decimal(parts[0]); coin = parts[1].upper(); addr = parts[2]
        except Exception:
            await update.message.reply_text("❌ Invalid."); return
        if not sto.debit_house_usd(amt):
            await update.message.reply_text(f"❌ House balance ${sto.house_balance_usd:.2f} insufficient."); return
        await update.message.reply_text(
            f"✅ House withdrawal\n${amt:.2f} → {coin}\nTo: `{addr}`",
            parse_mode=ParseMode.MARKDOWN)

    elif action == "set_referral":
        try:
            adm["referral_bonus"] = Decimal(text)
            await update.message.reply_text(f"✅ Referral bonus: ${adm['referral_bonus']:.2f}",
                                            parse_mode=ParseMode.MARKDOWN)
        except Exception: await update.message.reply_text("❌ Invalid.")

    elif action == "add_promo":
        parts = text.split()
        if len(parts) < 3: await update.message.reply_text("❌ Format: CODE amount uses"); return
        try:
            adm.setdefault("promo_codes", {})[parts[0].upper()] = {
                "bonus": Decimal(parts[1]), "uses_left": int(parts[2])}
            await update.message.reply_text(f"✅ Promo `{parts[0].upper()}` added",
                                            parse_mode=ParseMode.MARKDOWN)
        except Exception: await update.message.reply_text("❌ Invalid.")

    elif action == "set_tip_limits":
        parts = text.split()
        if len(parts) < 2: await update.message.reply_text("❌ Format: min max"); return
        try:
            adm["tip_min"] = Decimal(parts[0]); adm["tip_max"] = Decimal(parts[1])
            await update.message.reply_text(f"✅ Tips: ${adm['tip_min']} – ${adm['tip_max']}",
                                            parse_mode=ParseMode.MARKDOWN)
        except Exception: await update.message.reply_text("❌ Invalid.")

    elif action == "set_house_addr":
        parts = text.split(maxsplit=1)
        if len(parts) < 2: await update.message.reply_text("❌ Format: network address"); return
        adm.setdefault("house_addresses", {})[parts[0].lower()] = parts[1]
        await update.message.reply_text(f"✅ {parts[0]} → `{parts[1]}`",
                                        parse_mode=ParseMode.MARKDOWN)


def register_admin_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("admin",       admin_command))
    app.add_handler(CommandHandler("toggle_game", toggle_game_command))
    app.add_handler(CommandHandler("list_games",  list_games_command))
    app.add_handler(CallbackQueryHandler(admin_cb, pattern=r"^admin:"))
    from telegram.ext import MessageHandler, filters
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
        admin_text_handler), group=4)
