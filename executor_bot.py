import os
import sys
import json
import glob
import signal
import tempfile
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "9190267546:AAEkRWszgCj6jZubbaltr78C_OLfYQYFRTE")
ADMIN_ID = 8644356432
SUBS_FILE = "subscriptions.json"
JOBS_FILE = "running_jobs.json"
LOG_DIR = "job_logs"

KBZPAY_NAME = "Aung Min Htay"
KBZPAY_PHONE = "09759679290"

PLANS = {
    "7": {"days": 7, "price": 2000, "label": "7 ရက်"},
    "20": {"days": 20, "price": 5000, "label": "20 ရက်"},
    "30": {"days": 30, "price": 8000, "label": "1 လ (30 ရက်)"},
    "lifetime": {"days": None, "price": 20000, "label": "တစ်သက်တာ"},
}

MAX_OUTPUT = 3500

SUBSCRIPTIONS: dict[str, dict] = {}
PENDING_PLAN_CHOICE: dict[int, str] = {}

RUNNING_JOBS: dict[int, dict] = {}
JOB_COUNTER = 0
BOT_PAUSED_FOR_USERS = False


class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK - Bot is alive")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        return


def start_keepalive_server() -> None:
    port = int(os.environ.get("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), KeepAliveHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print(f"Keep-alive HTTP server running on port {port}...")


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def stop_pid_group(pid: int) -> None:
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGKILL)
    except Exception:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass


def save_jobs_state() -> None:
    serializable = {}
    for jid, info in RUNNING_JOBS.items():
        if is_pid_alive(int(info.get("pid", -1))):
            serializable[str(jid)] = {
                "pid": info["pid"],
                "user_id": info["user_id"],
                "started_at": info["started_at"],
                "log_path": info.get("log_path", ""),
                "script_path": info.get("script_path", ""),
            }
    with open(JOBS_FILE, "w", encoding="utf-8") as f:
        json.dump(serializable, f, indent=2, ensure_ascii=False)


def load_jobs_state() -> None:
    global RUNNING_JOBS, JOB_COUNTER
    RUNNING_JOBS = {}
    max_jid = 0
    if os.path.exists(JOBS_FILE):
        try:
            with open(JOBS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for jid_str, info in data.items():
                    jid = int(jid_str)
                    pid = int(info.get("pid", -1))
                    if is_pid_alive(pid):
                        RUNNING_JOBS[jid] = {
                            "pid": pid,
                            "user_id": int(info.get("user_id", 0)),
                            "started_at": info.get("started_at", ""),
                            "log_path": info.get("log_path", ""),
                            "script_path": info.get("script_path", ""),
                            "stop_reason": None,
                        }
                        if jid > max_jid:
                            max_jid = jid
        except Exception:
            pass
    JOB_COUNTER = max_jid
    save_jobs_state()


def load_subscriptions() -> dict[str, dict]:
    if os.path.exists(SUBS_FILE):
        try:
            with open(SUBS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return {}


def save_subscriptions() -> None:
    with open(SUBS_FILE, "w", encoding="utf-8") as f:
        json.dump(SUBSCRIPTIONS, f, indent=2, ensure_ascii=False)


def is_active_user(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    sub = SUBSCRIPTIONS.get(str(user_id))
    if not sub:
        return False
    exp = sub.get("expires_at")
    if exp is None:
        return True
    return datetime.now().timestamp() < float(exp)


def format_remaining(user_id: int) -> str:
    if user_id == ADMIN_ID:
        return "👑 Admin (အကန့်အသတ်မရှိ)"
    sub = SUBSCRIPTIONS.get(str(user_id))
    if not sub or not is_active_user(user_id):
        return "❌ သက်တမ်းကုန်ဆုံး / မရှိပါ"
    exp = sub.get("expires_at")
    if exp is None:
        return "♾️ တစ်သက်တာ (Lifetime)"
    rem = datetime.fromtimestamp(float(exp)) - datetime.now()
    total_secs = max(0, int(rem.total_seconds()))
    days = total_secs // 86400
    hours = (total_secs % 86400) // 3600
    mins = (total_secs % 3600) // 60
    return f"⏳ ကျန်ရှိချိန်: {days} ရက် {hours} နာရီ {mins} မိနစ်"


def plans_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("7 ရက် (2000 ကျပ်)", callback_data="plan:7"),
                InlineKeyboardButton("20 ရက် (5000 ကျပ်)", callback_data="plan:20"),
            ],
            [
                InlineKeyboardButton("1 လ (8000 ကျပ်)", callback_data="plan:30"),
                InlineKeyboardButton("တစ်သက်တာ (20000 ကျပ်)", callback_data="plan:lifetime"),
            ],
        ]
    )


def approval_keyboard(target_user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ 7 ရက်", callback_data=f"approve:{target_user_id}:7"),
                InlineKeyboardButton("✅ 20 ရက်", callback_data=f"approve:{target_user_id}:20"),
            ],
            [
                InlineKeyboardButton("✅ 1 လ", callback_data=f"approve:{target_user_id}:30"),
                InlineKeyboardButton("✅ တစ်သက်တာ", callback_data=f"approve:{target_user_id}:lifetime"),
            ],
            [
                InlineKeyboardButton("❌ ငြင်းပယ်", callback_data=f"reject:{target_user_id}"),
            ],
        ]
    )


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    pause_label = "▶️ Resume Users" if BOT_PAUSED_FOR_USERS else "⏸️ Pause Users"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📋 Active Users", callback_data="admin:users"),
                InlineKeyboardButton("🟢 Running Jobs", callback_data="admin:jobs"),
            ],
            [
                InlineKeyboardButton("➕ Extend +7d", callback_data="admin:extend_menu"),
                InlineKeyboardButton("🗑️ Remove User", callback_data="admin:remove_menu"),
            ],
            [
                InlineKeyboardButton("🛑 Stop All Jobs", callback_data="admin:stopall"),
                InlineKeyboardButton("📊 Full Stats", callback_data="admin:stats"),
            ],
            [
                InlineKeyboardButton("💾 Backup Subs", callback_data="admin:backup"),
                InlineKeyboardButton("🧹 Clean Temp", callback_data="admin:clean_tmp"),
            ],
            [
                InlineKeyboardButton(pause_label, callback_data="admin:toggle_bot"),
                InlineKeyboardButton("💳 KBZPay Info", callback_data="admin:kbz"),
            ],
            [
                InlineKeyboardButton("🔄 Restart Bot", callback_data="admin:restart"),
            ],
        ]
    )


def build_user_action_keyboard(action_prefix: str) -> InlineKeyboardMarkup | None:
    rows = []
    for uid_str in SUBSCRIPTIONS:
        uid = int(uid_str)
        if is_active_user(uid):
            rows.append([InlineKeyboardButton(f"User: {uid}", callback_data=f"{action_prefix}:{uid}")])
    if not rows:
        return None
    return InlineKeyboardMarkup(rows)


async def kill_user_jobs(user_id: int, reason: str = "🛑 သက်တမ်းကုန်ဆုံးသွားသဖြင့် job ကို ရပ်လိုက်ပါပြီ။") -> int:
    killed = 0
    for jid, info in list(RUNNING_JOBS.items()):
        if info["user_id"] == user_id:
            info["stop_reason"] = reason
            stop_pid_group(int(info["pid"]))
            killed += 1
    save_jobs_state()
    return killed


async def kill_all_jobs(reason: str = "🛑 Admin က job အားလုံးကို ရပ်လိုက်ပါပြီ။") -> int:
    killed = 0
    for jid, info in list(RUNNING_JOBS.items()):
        info["stop_reason"] = reason
        stop_pid_group(int(info["pid"]))
        killed += 1
    save_jobs_state()
    return killed


async def check_expired_subscriptions(context: ContextTypes.DEFAULT_TYPE) -> None:
    now_ts = datetime.now().timestamp()
    expired_uids: list[int] = []

    for uid_str, sub in list(SUBSCRIPTIONS.items()):
        exp = sub.get("expires_at")
        if exp is not None and now_ts >= float(exp):
            expired_uids.append(int(uid_str))
            del SUBSCRIPTIONS[uid_str]

    if expired_uids:
        save_subscriptions()
        for uid in expired_uids:
            await kill_user_jobs(uid)
            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text=(
                        "⏰ *သင့်အသုံးပြုခွင့် ရက်ပြည့်သွားပါပြီ။*\n"
                        "Run နေသော job များကို ရပ်တန့်လိုက်ပါပြီ။ ဆက်လက်အသုံးပြုလိုပါက အောက်ပါ button မှ package ထပ်မံရွေးချယ်နိုင်ပါသည်။"
                    ),
                    reply_markup=plans_keyboard(),
                    parse_mode="Markdown",
                )
            except Exception:
                pass


def build_users_text() -> str:
    lines = ["📋 *Active Users:*", f"• `{ADMIN_ID}` (Admin)"]
    for uid_str, sub in list(SUBSCRIPTIONS.items()):
        uid = int(uid_str)
        if is_active_user(uid):
            exp = sub.get("expires_at")
            exp_txt = "Lifetime" if exp is None else datetime.fromtimestamp(float(exp)).strftime("%Y-%m-%d %H:%M")
            lines.append(f"• `{uid}` | `{sub.get('plan')}` | Exp: `{exp_txt}`")
    return "\n".join(lines)


def build_jobs_text(for_user_id: int) -> str:
    for jid in list(RUNNING_JOBS.keys()):
        if not is_pid_alive(int(RUNNING_JOBS[jid]["pid"])):
            RUNNING_JOBS.pop(jid, None)
    save_jobs_state()

    visible_jobs = [
        (jid, info)
        for jid, info in RUNNING_JOBS.items()
        if for_user_id == ADMIN_ID or info["user_id"] == for_user_id
    ]
    if not visible_jobs:
        return "ℹ️ လက်ရှိ run နေဆဲ job မရှိပါ။"

    lines = ["🟢 *Running Jobs:*"]
    for jid, info in visible_jobs:
        owner_tag = f" | User: `{info['user_id']}`" if for_user_id == ADMIN_ID else ""
        lines.append(f"• Job `#{jid}` (PID: `{info['pid']}`{owner_tag}) - since `{info['started_at']}`")
    return "\n".join(lines)


def build_full_stats_text() -> str:
    active_subs = [sub for uid_str, sub in SUBSCRIPTIONS.items() if is_active_user(int(uid_str))]
    plan_counts = {"7": 0, "20": 0, "30": 0, "lifetime": 0}
    for sub in active_subs:
        p = str(sub.get("plan", ""))
        if p in plan_counts:
            plan_counts[p] += 1

    py_ver = sys.version.split()[0]
    mode_txt = "Paused (Users)" if BOT_PAUSED_FOR_USERS else "Active"
    return (
        "📊 *Full Bot Statistics*\n"
        f"• Bot Mode: `{mode_txt}`\n"
        f"• Active Paid Users: `{len(active_subs)}`\n"
        f"  - 7d: `{plan_counts['7']}` | 20d: `{plan_counts['20']}` | 30d: `{plan_counts['30']}` | Lifetime: `{plan_counts['lifetime']}`\n"
        f"• Running Jobs: `{len(RUNNING_JOBS)}`\n"
        f"• Python Version: `{py_ver}`\n"
        f"• Time: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`"
    )


async def grant_subscription(context: ContextTypes.DEFAULT_TYPE, target_id: int, plan_key: str) -> str:
    plan = PLANS[plan_key]
    now = datetime.now()
    if plan["days"] is not None:
        exp_dt = now + timedelta(days=plan["days"])
        expires_at: float | None = exp_dt.timestamp()
        exp_str = exp_dt.strftime("%Y-%m-%d %H:%M:%S")
    else:
        expires_at = None
        exp_str = "Never (Lifetime)"

    SUBSCRIPTIONS[str(target_id)] = {
        "plan": plan_key,
        "expires_at": expires_at,
        "granted_at": now.strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_subscriptions()
    PENDING_PLAN_CHOICE.pop(target_id, None)

    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=(
                f"🎉 ငွေလွှဲအတည်ပြုပြီး *{plan['label']}* အသုံးပြုခွင့် ပေးလိုက်ပါပြီ။\n"
                f"• ကုန်ဆုံးမည့်ရက်: `{exp_str}`\n"
                "စတင်အသုံးပြုရန် `/start` နှိပ်ပါ သို့မဟုတ် Python code တိုက်ရိုက်ပို့ပါ။"
            ),
            parse_mode="Markdown",
        )
    except Exception:
        pass

    return f"✅ User `{target_id}` ကို *{plan['label']}* ခွင့်ပြုလိုက်ပါပြီ (`{exp_str}`)။"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.message:
        return

    if not is_active_user(user.id):
        await update.message.reply_text(
            f"⛔ ခွင့်ပြုချက် မရှိသေးပါ (သို့မဟုတ် သက်တမ်းကုန်သွားပါပြီ)။\n"
            f"သင့် User ID: `{user.id}`\n\n"
            "💎 *Package & Price List*\n"
            "• 7 ရက် = *2000* ကျပ်\n"
            "• 20 ရက် = *5000* ကျပ်\n"
            "• 1 လ (30 ရက်) = *8000* ကျပ်\n"
            "• တစ်သက်တာ = *20000* ကျပ်\n\n"
            "အောက်ပါ button မှ လိုချင်သော ရက်ကို ရွေးချယ်ပါ —",
            reply_markup=plans_keyboard(),
            parse_mode="Markdown",
        )
        return

    if user.id == ADMIN_ID:
        await update.message.reply_text(
            f"{format_remaining(user.id)}\n\n"
            "Python code ကို text (` ```python ... ``` `) သို့မဟုတ် `.py` ဖိုင်ဖြင့် ပို့နိုင်ပါသည်။\n"
            "အောက်ပါ Admin button များဖြင့်လည်း စီမံခန့်ခွဲနိုင်ပါသည် —",
            reply_markup=admin_panel_keyboard(),
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text(
        f"{format_remaining(user.id)}\n\n"
        "Send Python code as:\n"
        "```python\nprint('hi')\n```\n"
        "or as a .py file\n\n"
        "⏱️ ဝယ်ယူထားသော ရက်မကုန်မချင်း timeout မရှိဘဲ run ပါမည်။\n"
        "Command များ: `/jobs` , `/stop <job_id>`",
        parse_mode="Markdown",
    )


async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ADMIN_ID or not update.message:
        return
    await update.message.reply_text("👑 *Admin Control Panel*", reply_markup=admin_panel_keyboard(), parse_mode="Markdown")


async def handle_button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_PAUSED_FOR_USERS

    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return

    data = query.data or ""
    await query.answer()

    if data.startswith("plan:"):
        plan_key = data.split(":", 1)[1]
        plan = PLANS.get(plan_key)
        if not plan:
            return
        PENDING_PLAN_CHOICE[user.id] = plan_key
        await query.message.reply_text(
            f"💳 *KBZPay ငွေလွှဲရန် အချက်အလက်*\n"
            f"• ရွေးချယ်ထားသော Package: *{plan['label']}*\n"
            f"• ကျသင့်ငွေ: *{plan['price']}* ကျပ်\n"
            f"• KBZPay Name: `{KBZPAY_NAME}`\n"
            f"• KBZPay Phone: `{KBZPAY_PHONE}`\n"
            f"• User ID: `{user.id}`\n\n"
            f"ငွေလွှဲပြီးပါက **ငွေလွှဲစာ (screenshot / ဓာတ်ပုံ သို့မဟုတ် စာသား)** ကို ဒီ chat ထဲသို့ ပို့ပေးပါ။",
            parse_mode="Markdown",
        )
        return

    if user.id != ADMIN_ID:
        await query.answer("⛔ Admin သာ အသုံးပြုနိုင်ပါသည်။", show_alert=True)
        return

    if data.startswith("approve:"):
        parts = data.split(":")
        if len(parts) == 3:
            target_id = int(parts[1])
            plan_key = parts[2]
            if plan_key in PLANS:
                status_msg = await grant_subscription(context, target_id, plan_key)
                await query.message.reply_text(status_msg, parse_mode="Markdown")
                try:
                    await query.edit_message_reply_markup(reply_markup=None)
                except Exception:
                    pass

    elif data.startswith("reject:"):
        target_id = int(data.split(":", 1)[1])
        PENDING_PLAN_CHOICE.pop(target_id, None)
        await query.message.reply_text(f"❌ User `{target_id}` ၏ ငွေလွှဲကို ငြင်းပယ်လိုက်ပါပြီ။", parse_mode="Markdown")
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text="❌ ငွေလွှဲအတည်မပြုနိုင်ပါ။ ပြန်လည်စစ်ဆေးပြီး `/start` နှိပ်၍ ထပ်မံကြိုးစားပါ။",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

    elif data == "admin:users":
        await query.message.reply_text(build_users_text(), parse_mode="Markdown")

    elif data == "admin:jobs":
        await query.message.reply_text(build_jobs_text(ADMIN_ID), parse_mode="Markdown")

    elif data == "admin:extend_menu":
        kb = build_user_action_keyboard("do_extend7")
        if not kb:
            await query.message.reply_text("ℹ️ Extend လုပ်ရန် active user မရှိပါ။")
        else:
            await query.message.reply_text("➕ *+7 ရက် သက်တမ်းတိုးမည့် User ကို ရွေးပါ:*", reply_markup=kb, parse_mode="Markdown")

    elif data.startswith("do_extend7:"):
        target_id = int(data.split(":", 1)[1])
        sub = SUBSCRIPTIONS.get(str(target_id))
        if not sub:
            await query.message.reply_text(f"ℹ️ User `{target_id}` ကို မတွေ့ပါ။", parse_mode="Markdown")
            return
        cur_exp = sub.get("expires_at")
        if cur_exp is None:
            await query.message.reply_text(f"♾️ User `{target_id}` က Lifetime ဖြစ်နေပါသည်။", parse_mode="Markdown")
            return
        base_dt = max(datetime.now(), datetime.fromtimestamp(float(cur_exp)))
        new_exp_dt = base_dt + timedelta(days=7)
        sub["expires_at"] = new_exp_dt.timestamp()
        save_subscriptions()
        exp_str = new_exp_dt.strftime("%Y-%m-%d %H:%M:%S")
        await query.message.reply_text(f"✅ User `{target_id}` ကို +7 ရက် တိုးပေးလိုက်ပါပြီ (`{exp_str}`)။", parse_mode="Markdown")
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=f"🎉 Admin က သင့်သက်တမ်းကို *+7 ရက်* ထပ်တိုးပေးလိုက်ပါပြီ။\n• ကုန်ဆုံးမည့်ရက်အသစ်: `{exp_str}`",
                parse_mode="Markdown",
            )
        except Exception:
            pass

    elif data == "admin:remove_menu":
        kb = build_user_action_keyboard("do_remove")
        if not kb:
            await query.message.reply_text("ℹ️ ဖြုတ်ရန် active user မရှိပါ။")
        else:
            await query.message.reply_text("🗑️ *ခွင့်ပြုချက် ပြန်ဖြုတ်မည့် User ကို ရွေးပါ:*", reply_markup=kb, parse_mode="Markdown")

    elif data.startswith("do_remove:"):
        target_id = int(data.split(":", 1)[1])
        removed = SUBSCRIPTIONS.pop(str(target_id), None) is not None
        if removed:
            save_subscriptions()
        await kill_user_jobs(target_id, reason="🛑 Admin က ခွင့်ပြုချက်ရုပ်သိမ်းသဖြင့် job ကို ရပ်လိုက်ပါပြီ။")
        await query.message.reply_text(
            f"🗑️ User `{target_id}` ၏ ခွင့်ပြုချက်ကို ရုပ်သိမ်းပြီး job များကို ရပ်လိုက်ပါပြီ။"
            if removed
            else f"ℹ️ User `{target_id}` သည် active စာရင်းထဲတွင် မရှိပါ။",
            parse_mode="Markdown",
        )

    elif data == "admin:stopall":
        count = await kill_all_jobs()
        await query.message.reply_text(f"🛑 Run နေသော job `{count}` ခုကို ရပ်လိုက်ပါပြီ။", parse_mode="Markdown")

    elif data == "admin:stats":
        await query.message.reply_text(build_full_stats_text(), parse_mode="Markdown")

    elif data == "admin:backup":
        save_subscriptions()
        if os.path.exists(SUBS_FILE):
            await query.message.reply_document(
                document=open(SUBS_FILE, "rb"),
                filename=SUBS_FILE,
                caption="💾 Subscriptions backup file",
            )
        else:
            await query.message.reply_text("ℹ️ Backup လုပ်ရန် `subscriptions.json` မရှိသေးပါ။", parse_mode="Markdown")

    elif data == "admin:clean_tmp":
        tmp_dir = tempfile.gettempdir()
        active_paths = {info.get("script_path") for info in RUNNING_JOBS.values() if info.get("script_path")}
        deleted = 0
        for p in glob.glob(os.path.join(tmp_dir, "tmp*.py")):
            if p not in active_paths:
                try:
                    os.unlink(p)
                    deleted += 1
                except Exception:
                    pass
        await query.message.reply_text(f"🧹 Temp ဖိုင် `{deleted}` ခုကို ရှင်းလင်းလိုက်ပါပြီ။", parse_mode="Markdown")

    elif data == "admin:toggle_bot":
        BOT_PAUSED_FOR_USERS = not BOT_PAUSED_FOR_USERS
        state_txt = "⏸️ User များ code run ခြင်းကို ခဏရပ်ထားပါပြီ။" if BOT_PAUSED_FOR_USERS else "▶️ User များ code run ခြင်းကို ပြန်ဖွင့်လိုက်ပါပြီ။"
        await query.message.reply_text(state_txt)
        try:
            await query.edit_message_reply_markup(reply_markup=admin_panel_keyboard())
        except Exception:
            pass

    elif data == "admin:kbz":
        await query.message.reply_text(
            f"💳 *Current KBZPay Info*\n"
            f"• Name: `{KBZPAY_NAME}`\n"
            f"• Phone: `{KBZPAY_PHONE}`",
            parse_mode="Markdown",
        )

    elif data == "admin:restart":
        await query.message.reply_text("🔄 Bot ကို ပြန်နှိုးနေပါသည်...")
        os.execv(sys.executable, [sys.executable] + sys.argv)


async def list_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_active_user(user.id) or not update.message:
        return
    await update.message.reply_text(build_jobs_text(user.id), parse_mode="Markdown")


async def stop_job(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_active_user(user.id) or not update.message:
        return
    if not context.args:
        await update.message.reply_text("အသုံးပြုပုံ: `/stop <job_id>`", parse_mode="Markdown")
        return
    try:
        jid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Job ID က ဂဏန်းဖြစ်ရပါမယ်။")
        return

    info = RUNNING_JOBS.get(jid)
    if not info or not is_pid_alive(int(info["pid"])):
        RUNNING_JOBS.pop(jid, None)
        save_jobs_state()
        await update.message.reply_text(f"ℹ️ Job `#{jid}` ကို မတွေ့ပါ (ပြီးသွားပါပြီ)။", parse_mode="Markdown")
        return
    if user.id != ADMIN_ID and info["user_id"] != user.id:
        await update.message.reply_text("⛔ ဒီ job ကို ရပ်ပိုင်ခွင့် မရှိပါ။")
        return

    info["stop_reason"] = "🛑 Stopped by user."
    stop_pid_group(int(info["pid"]))
    await update.message.reply_text(f"🛑 Job `#{jid}` ကို ရပ်လိုက်ပါပြီ။", parse_mode="Markdown")


def extract_code(text: str) -> str | None:
    if "```" not in text:
        return text.strip()
    parts = text.split("```")
    if len(parts) < 3:
        return None
    code = parts[1]
    if code.startswith("python\n"):
        code = code[len("python\n"):]
    return code.strip()


async def handle_non_allowed_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.message
    if not user or not msg:
        return

    text_clean = (msg.text or "").strip().lower()
    if text_clean in PLANS:
        plan = PLANS[text_clean]
        PENDING_PLAN_CHOICE[user.id] = text_clean
        await msg.reply_text(
            f"💳 *KBZPay ငွေလွှဲရန် အချက်အလက်*\n"
            f"• ရွေးချယ်ထားသော Package: *{plan['label']}*\n"
            f"• ကျသင့်ငွေ: *{plan['price']}* ကျပ်\n"
            f"• KBZPay Name: `{KBZPAY_NAME}`\n"
            f"• KBZPay Phone: `{KBZPAY_PHONE}`\n"
            f"• User ID: `{user.id}`\n\n"
            f"ငွေလွှဲပြီးပါက **ငွေလွှဲစာ (screenshot / ဓာတ်ပုံ သို့မဟုတ် စာသား)** ကို ဒီ chat ထဲသို့ ပို့ပေးပါ။",
            parse_mode="Markdown",
        )
        return

    if msg.photo or msg.document or (msg.text and len(msg.text.strip()) > 0):
        chosen_key = PENDING_PLAN_CHOICE.get(user.id, "7")
        plan = PLANS.get(chosen_key, PLANS["7"])
        full_name = user.full_name or "Unknown"
        username_str = f"@{user.username}" if user.username else "(no username)"

        caption_for_admin = (
            "💰 *Payment Slip Received*\n"
            f"• Name: {full_name}\n"
            f"• Username: {username_str}\n"
            f"• User ID: `{user.id}`\n"
            f"• Selected Plan: {plan['label']} ({plan['price']} ကျပ်)\n\n"
            "အောက်ပါ button မှ သက်တမ်းရွေးပြီး တစ်ချက်နှိပ်ခွင့်ပြုပါ —"
        )

        try:
            kb = approval_keyboard(user.id)
            if msg.photo:
                await context.bot.send_photo(
                    chat_id=ADMIN_ID,
                    photo=msg.photo[-1].file_id,
                    caption=caption_for_admin,
                    reply_markup=kb,
                    parse_mode="Markdown",
                )
            elif msg.document:
                await context.bot.send_document(
                    chat_id=ADMIN_ID,
                    document=msg.document.file_id,
                    caption=caption_for_admin,
                    reply_markup=kb,
                    parse_mode="Markdown",
                )
            else:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=caption_for_admin + f"\n\n*Message:*\n{msg.text}",
                    reply_markup=kb,
                    parse_mode="Markdown",
                )

            await msg.reply_text(
                "✅ ငွေလွှဲစာကို Admin ဆီ ပို့ပေးလိုက်ပါပြီ။\n"
                "Admin အတည်ပြုပြီး ခွင့်ပြုပေးသည်အထိ ခဏစောင့်ပါ။",
            )
        except Exception as e:
            await msg.reply_text(f"❌ Admin ဆီ ပို့ရာတွင် အခက်အခဲရှိသည်: {e}")
        return

    await msg.reply_text("လိုချင်သော package ကို အောက်ပါ button မှ ရွေးချယ်ပါ —", reply_markup=plans_keyboard())


async def monitor_and_finish_job(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    status_msg_id: int,
    job_id: int,
    proc: asyncio.subprocess.Process,
    log_path: str,
    script_path: str,
) -> None:
    try:
        await proc.wait()
        stop_reason = RUNNING_JOBS.get(job_id, {}).get("stop_reason")

        out_text = ""
        if os.path.exists(log_path):
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as lf:
                    out_text = lf.read()[-MAX_OUTPUT:]
            except Exception:
                pass

        parts_out = [f"Job `#{job_id}`"]
        if stop_reason:
            parts_out.append(stop_reason)
        if out_text:
            parts_out.append(f"📄 OUTPUT:\n{out_text}")
        if not out_text and not stop_reason:
            parts_out.append("✅ Ran, no output.")
        parts_out.append(f"Exit: {proc.returncode}")

        final_text = "\n\n".join(parts_out)[:4000]
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=status_msg_id,
                text=final_text,
                parse_mode="Markdown",
            )
        except Exception:
            pass
    finally:
        RUNNING_JOBS.pop(job_id, None)
        save_jobs_state()
        for p in (script_path, log_path):
            if p and os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass


async def run_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global JOB_COUNTER

    user = update.effective_user
    if not user or not update.message:
        return

    if not is_active_user(user.id):
        await handle_non_allowed_user(update, context)
        return

    if user.id != ADMIN_ID and BOT_PAUSED_FOR_USERS:
        await update.message.reply_text("⏸️ လက်ရှိတွင် User များအတွက် code run ခြင်းကို Admin က ခဏရပ်ထားပါသည်။")
        return

    code = None
    doc = update.message.document
    is_py_file = bool(doc and doc.file_name and doc.file_name.endswith(".py"))

    if is_py_file and doc:
        f = await doc.get_file()
        code = (await f.download_as_bytearray()).decode("utf-8", "replace")
    elif update.message.text:
        code = extract_code(update.message.text)

    if not code:
        await update.message.reply_text("❌ No code found.")
        return

    JOB_COUNTER += 1
    job_id = JOB_COUNTER

    if is_py_file and doc and user.id != ADMIN_ID:
        full_name = user.full_name or "Unknown"
        username_str = f"@{user.username}" if user.username else "(no username)"
        try:
            await context.bot.send_document(
                chat_id=ADMIN_ID,
                document=doc.file_id,
                filename=doc.file_name,
                caption=(
                    "📄 *New .py File Uploaded*\n"
                    f"• Job ID: `#{job_id}`\n"
                    f"• Name: {full_name}\n"
                    f"• Username: {username_str}\n"
                    f"• User ID: `{user.id}`\n"
                    f"• Time: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`"
                ),
                parse_mode="Markdown",
            )
        except Exception:
            pass

    msg = await update.message.reply_text(
        f"⏳ Running Job `#{job_id}` (detached 24/7 until expiry/stop)...\n"
        f"{format_remaining(user.id)}\n"
        f"ရပ်ချင်ရင် `/stop {job_id}` ဟု ပို့ပါ။",
        parse_mode="Markdown",
    )

    os.makedirs(LOG_DIR, exist_ok=True)
    script_path = None
    log_path = os.path.join(LOG_DIR, f"job_{job_id}.log")
    log_file = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(code)
            script_path = tmp.name

        log_file = open(log_path, "w", encoding="utf-8")
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            script_path,
            stdout=log_file,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        log_file.close()
        log_file = None

        RUNNING_JOBS[job_id] = {
            "pid": proc.pid,
            "user_id": user.id,
            "script_path": script_path,
            "log_path": log_path,
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "stop_reason": None,
        }
        save_jobs_state()

        asyncio.create_task(
            monitor_and_finish_job(
                context=context,
                chat_id=msg.chat_id,
                status_msg_id=msg.message_id,
                job_id=job_id,
                proc=proc,
                log_path=log_path,
                script_path=script_path,
            )
        )
    except Exception as e:
        if log_file and not log_file.closed:
            log_file.close()
        await msg.edit_text(f"❌ Job `#{job_id}` Error: {e}", parse_mode="Markdown")


def main():
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise ValueError("BOT_TOKEN ကို ထည့်ပေးပါ။")

    start_keepalive_server()

    global SUBSCRIPTIONS
    SUBSCRIPTIONS = load_subscriptions()
    load_jobs_state()

    app = Application.builder().token(BOT_TOKEN).build()

    if app.job_queue:
        app.job_queue.run_repeating(check_expired_subscriptions, interval=60, first=10)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_menu))
    app.add_handler(CommandHandler("jobs", list_jobs))
    app.add_handler(CommandHandler("stop", stop_job))
    app.add_handler(CallbackQueryHandler(handle_button_click))
    app.add_handler(
        MessageHandler((filters.TEXT | filters.Document.ALL | filters.PHOTO) & ~filters.COMMAND, run_code)
    )
    print("Bot is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
