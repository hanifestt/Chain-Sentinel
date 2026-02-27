import os
import asyncio
import json
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from scanner import scan_token
from watchlist import add_to_watchlist, remove_from_watchlist, get_watchlist, check_watchlist_alerts
from invites import generate_invite, use_invite, is_authorized, authorize_user, list_invites

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
BOT_USERNAME = os.environ.get("BOT_USERNAME", "chainsentinel_bot")


def require_auth(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id == ADMIN_ID or is_authorized(user_id):
            return await func(update, context)
        else:
            await update.message.reply_text(
                "🔒 *Access Denied*\n\nChain Sentinel is invite-only.\nYou need a valid invite link to access this bot.",
                parse_mode="Markdown"
            )
    return wrapper


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name or "trader"

    if context.args:
        token = context.args[0]
        if is_authorized(user_id) or user_id == ADMIN_ID:
            await send_welcome(update, user_name)
            return
        success = use_invite(token, user_id)
        if success:
            await update.message.reply_text(
                f"✅ *Invite accepted!* Welcome, {user_name}.\n\nYou now have full access to Chain Sentinel.",
                parse_mode="Markdown"
            )
            await send_welcome(update, user_name)
        else:
            await update.message.reply_text(
                "❌ *Invalid or already used invite link.*\n\nThis link has already been redeemed.\nContact the admin for a new invite.",
                parse_mode="Markdown"
            )
        return

    if is_authorized(user_id) or user_id == ADMIN_ID:
        await send_welcome(update, user_name)
    else:
        await update.message.reply_text(
            "🔒 *Chain Sentinel is invite-only.*\n\nYou need a valid invite link to access this bot.",
            parse_mode="Markdown"
        )


async def send_welcome(update: Update, name: str):
    text = (
        f"👁 *CHAIN SENTINEL* — Welcome, {name}\n\n"
        "Send me any Solana contract address and I'll scan it for:\n"
        "• Wallet clustering & snipers\n"
        "• LP lock status\n"
        "• Supply concentration\n"
        "• MEV bot exposure\n\n"
        "*Commands:*\n"
        "/scan `<CA>` — Scan a token\n"
        "/watch `<CA>` — Add to watchlist\n"
        "/unwatch `<CA>` — Remove from watchlist\n"
        "/watchlist — View your watchlist\n\n"
        "_Paste a contract address to get started ↓_"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def genlink_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Admin only.")
        return
    count = 1
    if context.args and context.args[0].isdigit():
        count = min(int(context.args[0]), 20)
    lines = [f"🔑 *Generated {count} invite link(s):*\n"]
    for _ in range(count):
        token = generate_invite()
        link = f"https://t.me/{BOT_USERNAME}?start={token}"
        lines.append(f"`{link}`")
    lines.append("\n_Each link can only be used once._")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def invites_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Admin only.")
        return
    invites = list_invites()
    if not invites:
        await update.message.reply_text("No invites yet. Use /genlink to create one.")
        return
    used = sum(1 for v in invites.values() if v["used"])
    unused = len(invites) - used
    lines = [f"📋 *Invite Summary:*", f"Total: {len(invites)} | Used: {used} | Available: {unused}\n"]
    for token, meta in list(invites.items())[-10:]:
        status = "✅ Used" if meta["used"] else "⏳ Available"
        lines.append(f"`...{token[-8:]}` — {status}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def adduser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Admin only.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /adduser <telegram_user_id>")
        return
    authorize_user(int(context.args[0]))
    await update.message.reply_text(f"✅ User `{context.args[0]}` authorized.", parse_mode="Markdown")


@require_auth
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_welcome(update, update.effective_user.first_name or "trader")


@require_auth
async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /scan <contract_address>")
        return
    await run_scan(update, context, context.args[0].strip())


@require_auth
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if 32 <= len(text) <= 44 and text.isalnum():
        await run_scan(update, context, text)
    else:
        await update.message.reply_text("Send me a Solana contract address to scan, or use /help.")


async def run_scan(update: Update, context: ContextTypes.DEFAULT_TYPE, ca: str):
    msg = await update.message.reply_text(
        f"🔍 Scanning `{ca[:8]}...{ca[-4:]}`\nThis takes ~10 seconds...",
        parse_mode="Markdown"
    )
    try:
        result = await scan_token(ca)
        text = format_report(ca, result)
        keyboard = [[
            InlineKeyboardButton("👁 Watch Token", callback_data=f"watch:{ca}"),
            InlineKeyboardButton("🔗 Pump.fun", url=f"https://pump.fun/coin/{ca}")
        ]]
        await msg.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        logger.error(f"Scan error: {e}")
        await msg.edit_text(f"❌ Scan failed for `{ca[:8]}...`\n\nMake sure it's a valid Solana CA.", parse_mode="Markdown")


def format_report(ca: str, r: dict) -> str:
    score = r.get("risk_score", 0)
    if score <= 30:   verdict = "🟢 LOW RISK"
    elif score <= 60: verdict = "🟡 MEDIUM RISK"
    elif score <= 80: verdict = "🟠 HIGH RISK"
    else:             verdict = "🔴 CRITICAL RISK"
    return "\n".join([
        f"👁 *CHAIN SENTINEL REPORT*", f"`{ca}`", f"",
        f"*Risk Score: {score}/100 — {verdict}*", f"",
        f"━━━ 💼 WALLET ANALYSIS ━━━",
        f"• Unique wallets: `{r.get('wallet_count', 'N/A')}`",
        f"• Clustered/coordinated: `{r.get('cluster_pct', 'N/A')}%`",
        f"• Fresh wallets (<24h): `{r.get('fresh_wallet_pct', 'N/A')}%`",
        f"• Wallet risk: `{r.get('wallet_risk', 'N/A')}`", f"",
        f"━━━ 💧 LIQUIDITY (LP) ━━━",
        f"• LP status: `{r.get('lp_locked', 'Unknown')}`",
        f"• LP risk: `{r.get('lp_risk', 'N/A')}`", f"",
        f"━━━ 📊 SUPPLY CONCENTRATION ━━━",
        f"• Holder count: `{r.get('holder_count', 'N/A')}`",
        f"• Top holder: `{r.get('top1_pct', 'N/A')}%`",
        f"• Top 10 holders: `{r.get('top10_pct', 'N/A')}%`",
        f"• Gini coefficient: `{r.get('gini', 'N/A')}`",
        f"• Supply risk: `{r.get('supply_risk', 'N/A')}`", f"",
        f"━━━ ⚡ MEV EXPOSURE ━━━",
        f"• Suspected bot wallets: `{r.get('mev_bots', 'N/A')}`",
        f"• Sandwich patterns: `{r.get('sandwich_count', 'N/A')}`",
        f"• MEV risk: `{r.get('mev_risk', 'N/A')}`", f"",
        f"━━━ 🤖 ANALYSIS ━━━",
        f"{r.get('ai_summary', 'No summary available.')}", f"",
        f"_Powered by Chain Sentinel • $CS_"
    ])


@require_auth
async def watch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /watch <contract_address>")
        return
    ca = context.args[0].strip()
    add_to_watchlist(str(update.effective_user.id), ca)
    await update.message.reply_text(f"✅ Added `{ca[:8]}...{ca[-4:]}` to your watchlist.", parse_mode="Markdown")


@require_auth
async def unwatch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /unwatch <contract_address>")
        return
    removed = remove_from_watchlist(str(update.effective_user.id), context.args[0].strip())
    if removed:
        await update.message.reply_text(f"🗑 Removed from watchlist.", parse_mode="Markdown")
    else:
        await update.message.reply_text("That address wasn't in your watchlist.")


@require_auth
async def watchlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wl = get_watchlist(str(update.effective_user.id))
    if not wl:
        await update.message.reply_text("Your watchlist is empty. Use /watch <CA> to add tokens.")
        return
    lines = ["👁 *Your Watchlist:*\n"] + [f"{i}. `{ca}`" for i, ca in enumerate(wl, 1)]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data.startswith("watch:"):
        ca = query.data.split("watch:")[1]
        add_to_watchlist(str(query.from_user.id), ca)
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Watching", callback_data="noop"),
            InlineKeyboardButton("🔗 Pump.fun", url=f"https://pump.fun/coin/{ca}")
        ]]))


async def watchlist_job(context: ContextTypes.DEFAULT_TYPE):
    alerts = await check_watchlist_alerts()
    for user_id, ca, message in alerts:
        try:
            await context.bot.send_message(chat_id=user_id, text=f"🚨 *WATCHLIST ALERT*\n\n`{ca}`\n\n{message}", parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Alert error: {e}")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("watch", watch_cmd))
    app.add_handler(CommandHandler("unwatch", unwatch_cmd))
    app.add_handler(CommandHandler("watchlist", watchlist_cmd))
    app.add_handler(CommandHandler("genlink", genlink_cmd))
    app.add_handler(CommandHandler("invites", invites_cmd))
    app.add_handler(CommandHandler("adduser", adduser_cmd))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    app.job_queue.run_repeating(watchlist_job, interval=1800, first=60)
    logger.info("Chain Sentinel bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
