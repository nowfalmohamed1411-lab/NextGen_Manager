#!/usr/bin/env python3
"""
NextGen Manager - Telegram Group Scheduler Bot (Replit + UptimeRobot keepalive)

This version runs the Telegram bot (polling) and starts a small aiohttp webserver
so UptimeRobot can ping the root URL to keep the Repl alive.

Environment variables required:
- TELEGRAM_TOKEN
- SPREADSHEET_ID
- GOOGLE_SERVICE_ACCOUNT_JSON  (paste full JSON text)
- TZ (optional, e.g., "Asia/Kolkata")
"""

import os
import json
import logging
import re
import uuid
from datetime import datetime, date, timedelta

# timezone handling
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

from aiohttp import web
import gspread
from google.oauth2.service_account import Credentials

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
)

from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ----- CONFIG -----
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_DISPLAY_NAME = "NextGen Manager"

TZ_NAME = os.environ.get("TZ", "Asia/Kolkata")
if ZoneInfo:
    try:
        TZ = ZoneInfo(TZ_NAME)
    except Exception:
        TZ = None
else:
    TZ = None

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")

if not TELEGRAM_TOKEN or not SPREADSHEET_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
    raise Exception("Set TELEGRAM_TOKEN, SPREADSHEET_ID and GOOGLE_SERVICE_ACCOUNT_JSON env vars.")

# Sheets objects (initialized later)
SPREADSHEET = None
SLOTS_SHEET = None
PENDING_SHEET = None
META_SHEET = None

# ----- Sheets helpers -----
def init_sheets():
    global SPREADSHEET, SLOTS_SHEET, PENDING_SHEET, META_SHEET
    sa_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"],
    )
    client = gspread.authorize(creds)
    SPREADSHEET = client.open_by_key(SPREADSHEET_ID)

    # slots (confirmed) sheet
    try:
        SLOTS_SHEET = SPREADSHEET.worksheet("slots")
    except Exception:
        SLOTS_SHEET = SPREADSHEET.add_worksheet(title="slots", rows="2000", cols="20")
        SLOTS_SHEET.append_row(["id","date","start_time","end_time","username","first_name","user_id","details","created_at","reminder_sent"])

    # pending sheet (for tentative slots awaiting confirmation)
    try:
        PENDING_SHEET = SPREADSHEET.worksheet("pending")
    except Exception:
        PENDING_SHEET = SPREADSHEET.add_worksheet(title="pending", rows="500", cols="20")
        PENDING_SHEET.append_row(["id","date","start_time","end_time","username","first_name","user_id","details","created_at"])

    # meta sheet (store team_chat_id etc.)
    try:
        META_SHEET = SPREADSHEET.worksheet("meta")
    except Exception:
        META_SHEET = SPREADSHEET.add_worksheet(title="meta", rows="50", cols="2")
        META_SHEET.append_row(["key","value"])

def get_meta_value(key):
    try:
        rows = META_SHEET.get_all_records()
    except Exception:
        return None
    for r in rows:
        if r.get("key") == key:
            return r.get("value")
    return None

def set_meta_value(key, value):
    rows = META_SHEET.get_all_records()
    for idx, r in enumerate(rows, start=2):
        if r.get("key") == key:
            META_SHEET.update_cell(idx, 2, value)
            return
    META_SHEET.append_row([key, value])

def all_slots_records():
    return SLOTS_SHEET.get_all_records()

def all_pending_records():
    return PENDING_SHEET.get_all_records()

def append_slot_row(row_values):
    SLOTS_SHEET.append_row(row_values)

def append_pending_row(row_values):
    PENDING_SHEET.append_row(row_values)

def find_slot_row(slot_id):
    try:
        c = SLOTS_SHEET.find(slot_id)
        return c.row
    except Exception:
        return None

def find_pending_row(slot_id):
    try:
        c = PENDING_SHEET.find(slot_id)
        return c.row
    except Exception:
        return None

def get_pending_record(slot_id):
    rows = all_pending_records()
    for r in rows:
        if r.get("id") == slot_id:
            return r
    return None

def delete_pending(slot_id):
    row = find_pending_row(slot_id)
    if row:
        PENDING_SHEET.delete_rows(row)
        return True
    return False

def update_reminder_sent(slot_id):
    row = find_slot_row(slot_id)
    if not row:
        return False
    # reminder_sent column is 10 (header position)
    SLOTS_SHEET.update_cell(row, 10, "yes")
    return True

def delete_slot(slot_id):
    row = find_slot_row(slot_id)
    if row:
        SLOTS_SHEET.delete_rows(row)
        return True
    return False

# ----- Utilities -----
def make_dt(date_str, time_str):
    d = date.fromisoformat(date_str)
    hhmm = datetime.strptime(time_str, "%H:%M").time()
    if TZ:
        return datetime.combine(d, hhmm).replace(tzinfo=TZ)
    else:
        return datetime.combine(d, hhmm)

def overlaps(a_start, a_end, b_start, b_end):
    return (a_start < b_end) and (b_start < a_end)

# ----- Bot commands -----
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        f"Hi — I am *{BOT_DISPLAY_NAME}*.\n\n"
        "This bot is group-first. Add me to your team group and run /setteam in the group.\n\n"
        "Commands (use inside the registered group):\n"
        "/addslot YYYY-MM-DD HH:MM HH:MM Details...  OR  /addslot HH:MM-HH:MM Details... (date defaults to today)\n"
        "/me [YYYY-MM-DD]   — show your slots for the date (posted in group)\n"
        "/team [YYYY-MM-DD] — show team slots for the date (posted in group)\n"
        "/cancel <id>       — cancel the slot you created\n"
        "/setteam           — (run once inside the group) register this group for all alerts\n\n"
        "Behaviour:\n- Overlap prompts and reminders are posted ONLY in the registered group.\n- Only the slot creator can Confirm/Cancel a pending (overlapping) slot.\n- Reminders: 15 minutes before the start, posted to the group.\n"
    )
    await update.message.reply_text(txt, parse_mode='Markdown')

async def setteam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Run /setteam inside the group you want the bot to post team alerts to.")
        return
    set_meta_value("team_chat_id", str(chat.id))
    await update.message.reply_text(f"Registered this group (id={chat.id}) as the team channel for {BOT_DISPLAY_NAME} alerts.")

async def addslot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    team_chat = get_meta_value("team_chat_id")
    if not team_chat:
        await update.message.reply_text("No team group registered yet. Add the bot to your group and run /setteam inside the group first.")
        return

    # Ensure the command is executed inside the registered group
    if str(update.effective_chat.id) != str(team_chat):
        await update.message.reply_text("You must add slots from the registered team group. Switch to your team group and run /addslot there.")
        return

    args = context.args
    if not args:
        await update.message.reply_text("Usage: /addslot YYYY-MM-DD HH:MM HH:MM Details... OR /addslot HH:MM-HH:MM Details...")
        return

    # parse
    date_str, start_str, end_str, details = None, None, None, ""
    if len(args) >= 3 and re.match(r"^\d{4}-\d{2}-\d{2}$", args[0]):
        date_str = args[0]
        start_str = args[1]
        end_str = args[2]
        details = " ".join(args[3:]) or "Busy"
    elif re.match(r"^\d{1,2}:\d{2}-\d{1,2}:\d{2}$", args[0]):
        date_str = date.today().isoformat()
        start_str, end_str = args[0].split("-")
        details = " ".join(args[1:]) or "Busy"
    elif len(args) >= 2 and re.match(r"^\d{1,2}:\d{2}$", args[0]) and re.match(r"^\d{1,2}:\d{2}$", args[1]):
        date_str = date.today().isoformat()
        start_str = args[0]
        end_str = args[1]
        details = " ".join(args[2:]) or "Busy"
    else:
        await update.message.reply_text("Couldn't parse. Use /addslot YYYY-MM-DD HH:MM HH:MM Details... or /addslot HH:MM-HH:MM Details...")
        return

    try:
        datetime.strptime(start_str, "%H:%M")
        datetime.strptime(end_str, "%H:%M")
    except Exception:
        await update.message.reply_text("Times must be HH:MM (24-hour).")
        return

    try:
        start_dt = make_dt(date_str, start_str)
        end_dt = make_dt(date_str, end_str)
    except Exception:
        await update.message.reply_text("Invalid date/time format.")
        return

    if not (start_dt < end_dt):
        await update.message.reply_text("Start must be before end.")
        return

    user = update.effective_user
    slot_id = uuid.uuid4().hex[:10]
    created_at = datetime.now(TZ).isoformat() if TZ else datetime.now().isoformat()

    # check overlaps with confirmed slots for that date
    overlaps_found = []
    for rec in all_slots_records():
        if rec.get("date") != date_str:
            continue
        try:
            other_start = make_dt(rec["date"], rec["start_time"])
            other_end = make_dt(rec["date"], rec["end_time"])
        except Exception:
            continue
        if overlaps(start_dt, end_dt, other_start, other_end):
            overlaps_found.append(rec)

    if not overlaps_found:
        row = [slot_id, date_str, start_str, end_str, user.username or "", user.first_name or "", str(user.id), details, created_at, ""]
        append_slot_row(row)
        # Post to the registered group only
        await context.bot.send_message(chat_id=int(team_chat), text=f"✅ Slot added by {user.first_name or user.username}: {date_str} {start_str}-{end_str} — {details} (ID {slot_id})")
        return

    # overlaps -> create pending and post confirm buttons in group
    pending_row = [slot_id, date_str, start_str, end_str, user.username or "", user.first_name or "", str(user.id), details, created_at]
    append_pending_row(pending_row)

    text = f"⚠️ {user.first_name or user.username} wants to add a slot that overlaps {len(overlaps_found)} existing slot(s):\n"
    for o in overlaps_found:
        text += f"- {o.get('first_name') or o.get('username')}: {o['start_time']}-{o['end_time']} — {o['details']}\n"
    text += "\nCreator, please confirm or cancel."

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Confirm (creator only)", callback_data=f"confirm:{slot_id}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{slot_id}"),
            ]
        ]
    )
    await context.bot.send_message(chat_id=int(team_chat), text=text, reply_markup=keyboard)

# Callback handler for inline buttons (group messages)
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    user = query.from_user
    team_chat = get_meta_value("team_chat_id")

    if data.startswith("confirm:"):
        slot_id = data.split(":",1)[1]
        pending = get_pending_record(slot_id)
        if not pending:
            await query.edit_message_text("This pending slot was not found or already processed.")
            return
        # only creator can confirm
        if str(user.id) != str(pending.get("user_id")):
            await query.edit_message_text("Only the slot creator can confirm or cancel this pending slot.")
            return

        # move pending -> slots
        row = [
            pending.get("id"),
            pending.get("date"),
            pending.get("start_time"),
            pending.get("end_time"),
            pending.get("username"),
            pending.get("first_name"),
            pending.get("user_id"),
            pending.get("details"),
            pending.get("created_at"),
            ""
        ]
        append_slot_row(row)
        delete_pending(slot_id)

        # find overlapping confirmed slots (to show in message)
        start_dt = make_dt(pending['date'], pending['start_time'])
        end_dt = make_dt(pending['date'], pending['end_time'])
        overlaps_list = []
        for rec in all_slots_records():
            if rec.get('id') == slot_id:
                continue
            try:
                other_start = make_dt(rec['date'], rec['start_time'])
                other_end = make_dt(rec['date'], rec['end_time'])
            except Exception:
                continue
            if rec.get('date') == pending.get('date') and overlaps(start_dt, end_dt, other_start, other_end):
                overlaps_list.append(rec)

        # post confirmation and overlaps to the group only
        reply_text = f"✅ Slot confirmed by {pending.get('first_name') or pending.get('username')}: {pending.get('date')} {pending.get('start_time')}-{pending.get('end_time')} — {pending.get('details')} (ID {pending.get('id')})\\n\\nOverlaps with:\\n"
        if overlaps_list:
            for o in overlaps_list:
                reply_text += f"- {o.get('first_name') or o.get('username')}: {o.get('start_time')}-{o.get('end_time')} — {o.get('details')}\\n"
        else:
            reply_text += "- None\\n"

        if team_chat:
            await context.bot.send_message(chat_id=int(team_chat), text=reply_text)
        await query.edit_message_text("Slot confirmed and posted to group.")
        return

    if data.startswith("cancel:"):
        slot_id = data.split(":",1)[1]
        pending = get_pending_record(slot_id)
        if not pending:
            await query.edit_message_text("This pending slot was not found or already processed.")
            return
        if str(user.id) != str(pending.get("user_id")):
            await query.edit_message_text("Only the slot creator can confirm or cancel this pending slot.")
            return
        delete_pending(slot_id)
        await query.edit_message_text("Cancelled: the pending slot was not added.")
        return

async def me_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    team_chat = get_meta_value("team_chat_id")
    if not team_chat:
        await update.message.reply_text("No team group registered yet. Run /setteam in your group.")
        return

    # Enforce being in group
    if str(update.effective_chat.id) != str(team_chat):
        await update.message.reply_text("Run /me inside the registered group.")
        return

    args = context.args
    target_date = date.today().isoformat()
    if args and re.match(r"^\\d{4}-\\d{2}-\\d{2}$", args[0]):
        target_date = args[0]
    records = all_slots_records()
    user_id = str(update.effective_user.id)
    my_slots = [r for r in records if r.get("user_id") == user_id and r.get("date") == target_date]
    if not my_slots:
        await update.message.reply_text(f"No slots for {target_date}.")
        return
    text = f"Your slots for {target_date}:\\n"
    for r in my_slots:
        text += f"- ID {r['id']}: {r['start_time']}-{r['end_time']} — {r['details']}\\n"
    await update.message.reply_text(text)

async def team_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    team_chat = get_meta_value("team_chat_id")
    if not team_chat:
        await update.message.reply_text("No team group registered yet. Run /setteam in your group.")
        return
    if str(update.effective_chat.id) != str(team_chat):
        await update.message.reply_text("Run /team inside the registered group.")
        return

    args = context.args
    target_date = date.today().isoformat()
    if args and re.match(r"^\\d{4}-\\d{2}-\\d{2}$", args[0]):
        target_date = args[0]
    records = all_slots_records()
    day_slots = [r for r in records if r.get("date") == target_date]
    if not day_slots:
        await update.message.reply_text(f"No slots for {target_date}.")
        return
    text = f"Team slots for {target_date}:\\n"
    for r in day_slots:
        text += f"- {r.get('first_name') or r.get('username')}: {r['start_time']}-{r['end_time']} — {r['details']} (ID {r['id']})\\n"

    # detect overlaps
    overlaps_text = ""
    for i in range(len(day_slots)):
        a = day_slots[i]
        a_start = make_dt(a['date'], a['start_time'])
        a_end = make_dt(a['date'], a['end_time'])
        for j in range(i+1, len(day_slots)):
            b = day_slots[j]
            b_start = make_dt(b['date'], b['start_time'])
            b_end = make_dt(b['date'], b['end_time'])
            if overlaps(a_start, a_end, b_start, b_end):
                overlaps_text += f"- {a.get('first_name') or a.get('username')} ({a['start_time']}-{a['end_time']}) overlaps with {b.get('first_name') or b.get('username')} ({b['start_time']}-{b['end_time']})\\n"

    if overlaps_text:
        text += "\\n⚠️ Overlaps:\\n" + overlaps_text
    await update.message.reply_text(text)

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    team_chat = get_meta_value("team_chat_id")
    if not team_chat:
        await update.message.reply_text("No team group registered yet. Run /setteam in your group.")
        return
    if str(update.effective_chat.id) != str(team_chat):
        await update.message.reply_text("Run /cancel inside the registered group.")
        return

    args = context.args
    if not args:
        await update.message.reply_text("Usage: /cancel <slot_id>")
        return
    slot_id = args[0]
    row = find_slot_row(slot_id)
    if row:
        owner_user_id = SLOTS_SHEET.cell(row, 7).value  # user_id column
        if str(owner_user_id) != str(update.effective_user.id):
            await update.message.reply_text("You can only cancel slots you created.")
            return
        SLOTS_SHEET.delete_rows(row)
        await context.bot.send_message(chat_id=int(team_chat), text=f"🗑️ Slot {slot_id} removed by {update.effective_user.first_name or update.effective_user.username}.")
        return
    prow = find_pending_row(slot_id)
    if prow:
        owner_user_id = PENDING_SHEET.cell(prow, 7).value
        if str(owner_user_id) != str(update.effective_user.id):
            await update.message.reply_text("You can only cancel pending slots you created.")
            return
        PENDING_SHEET.delete_rows(prow)
        await context.bot.send_message(chat_id=int(team_chat), text=f"Cancelled pending slot {slot_id} by {update.effective_user.first_name or update.effective_user.username}.")
        return
    await update.message.reply_text("Slot ID not found.")

# ----- Reminder job -----
async def check_reminders(application):
    team_chat = get_meta_value("team_chat_id")
    if not team_chat:
        return
    now = datetime.now(TZ) if TZ else datetime.now()
    window_end = now + timedelta(minutes=15)
    for rec in all_slots_records():
        if str(rec.get("reminder_sent")).strip().lower() == "yes":
            continue
        try:
            start_dt = make_dt(rec['date'], rec['start_time'])
        except Exception:
            continue
        if now < start_dt <= window_end:
            text = f"🔔 Reminder: {rec.get('first_name') or rec.get('username')}'s \"{rec.get('details')}\" starts at {rec.get('start_time')} (in <=15 minutes).\\n(ID {rec.get('id')})"
            try:
                await application.bot.send_message(chat_id=int(team_chat), text=text)
            except Exception:
                logger.exception("Failed to send reminder to team chat %s", team_chat)
            update_reminder_sent(rec.get('id'))

# ----- Minimal webserver for keepalive -----
async def start_webserver():
    async def handle_root(request):
        return web.Response(text="NextGen Manager is alive.")
    app = web.Application()
    app.router.add_get("/", handle_root)
    port = int(os.environ.get("PORT", "8080"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Webserver started on port {port}")

# ----- Startup -----
async def main():
    init_sheets()
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("setteam", setteam))
    application.add_handler(CommandHandler("addslot", addslot))
    application.add_handler(CommandHandler("me", me_cmd))
    application.add_handler(CommandHandler("team", team_cmd))
    application.add_handler(CommandHandler("cancel", cancel_cmd))
    application.add_handler(CallbackQueryHandler(handle_callback))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_reminders, "interval", seconds=60, args=[application])
    scheduler.start()

    # start webserver in background (so UptimeRobot can ping)
    import asyncio
    asyncio.create_task(start_webserver())

    logger.info("NextGen Manager bot starting polling...")
    await application.run_polling()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
