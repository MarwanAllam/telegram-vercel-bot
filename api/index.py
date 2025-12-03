# api/index.py
import os
import json
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)

# -----------------------------
# ضع التوكن هنا كما طلبت
# -----------------------------
TOKEN = "8246108964:AAGTQI8zQl6rXqhLVG7_8NyFj4YqO35dMVg"

# -----------------------------
# بيانات محلية (ملف JSON)
# ملاحظة: في بيئة Serverless (Vercel) التخزين المحلي مؤقت (ephemeral).
# إذا تحتاج بيانات ثابتة بين الدعوات استخدم DB خارجي (مثلاً Firebase, Supabase, أو ملف على S3).
# -----------------------------
DATA_FILE = "/tmp/data.json"  # /tmp أحيانًا يصلح للاختبار المؤقت في بعض رن타يمز

# حاول تحميل user_channels من الملف (لو موجود)
try:
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        user_channels = json.load(f)
        user_channels = {k: [int(cid) for cid in v] if isinstance(v, list) else v for k, v in user_channels.items()}
except Exception:
    user_channels = {}

def save_data():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(user_channels, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("Warning: could not save data locally:", e)

# -----------------------------
# الحالة العامة (in-memory)
# -----------------------------
queues = {}
awaiting_input = {}

# -----------------------------
# مساعدة للكيبورد والـ permissions
# -----------------------------
def make_main_keyboard(chat_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 انضم / انسحب", callback_data=f"join|{chat_id}")],
        [
            InlineKeyboardButton("🗑️ ريموف", callback_data=f"remove_menu|{chat_id}"),
            InlineKeyboardButton("🔒 إنهاء الدور", callback_data=f"close|{chat_id}")
        ],
        [InlineKeyboardButton("⭐ إدارة المشرفين", callback_data=f"manage_admins|{chat_id}")]
    ])

def is_admin_or_creator(user_id, q):
    return user_id == q["creator"] or user_id in q["admins"]

# -----------------------------
# Handlers (نفس وظيفتك) - أهم حاجة تُبقيها كما هي
# -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = (
        "أهلاً 👋\n"
        "استخدم /startrole في الخاص لبدء دور في قناة مربوطة أو استخدم /link لربط قناة."
    )
    await update.message.reply_text(text)

# الربط، عرض القنوات، بدء الدور... (نسخة مصغرة من اللي بعته)
async def link_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    awaiting_input[user_id] = {"step": "link_channel", "creator_id": update.effective_user.id, "chat_id": update.effective_chat.id}
    await update.message.reply_text("🔗 أرسل الآن اسم القناة (مع @) لربطها:")

async def unlink_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    awaiting_input[user_id] = {"step": "unlink_channel", "creator_id": update.effective_user.id, "chat_id": update.effective_chat.id}
    await update.message.reply_text("🗑️ أرسل الآن اسم القناة (مع @) لفصلها:")

async def my_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id not in user_channels or not user_channels[user_id]:
        await update.message.reply_text("📭 مفيش قنوات مربوطة.")
        return
    text = "📋 قنواتك المربوطة:\n"
    for idx, ch_id in enumerate(user_channels[user_id], start=1):
        try:
            ch = await context.bot.get_chat(ch_id)
            username_display = f" (@{ch.username})" if ch.username else ""
            text += f"{idx}. **{ch.title}**{username_display}\n"
        except Exception:
            text += f"{idx}. قناة غير متاحة (ID: {ch_id})\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def start_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id not in user_channels or not user_channels[user_id]:
        await update.message.reply_text("🚫 مفيش قنوات مربوطة. استخدم /link أول.")
        return
    text = "اختر القناة لبدء الدور:\n"
    keyboard = []
    for ch_id in user_channels[user_id]:
        try:
            ch = await context.bot.get_chat(ch_id)
            keyboard.append([InlineKeyboardButton(ch.title, callback_data=f"select_channel|{ch_id}")])
        except Exception:
            continue
    if not keyboard:
        await update.message.reply_text("⚠️ لم يتم العثور على أي قنوات متاحة.")
        return
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

# جمع المعلومات وبدء الدور (مختصر)
async def prompt_for_role(update: Update, context: ContextTypes.DEFAULT_TYPE, target_chat_id: int):
    if target_chat_id in queues and not queues[target_chat_id].get("closed", True):
        await context.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ فيه دور شغال بالفعل في هذه القناة.")
        return
    awaiting_input[target_chat_id] = {
        "step": "teacher",
        "creator_id": update.effective_user.id,
        "creator_name": update.effective_user.full_name,
        "private_chat_id": update.effective_chat.id
    }
    await context.bot.send_message(chat_id=update.effective_chat.id, text="👩‍🏫 اكتب اسم المعلمة:")

async def collect_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    user_id = str(update.effective_user.id)
    user_input = update.message.text.strip()

    # حالات الربط/فصل القنوات (بالمفتاح user_id)
    if user_id in awaiting_input and awaiting_input[user_id].get("creator_id") == update.effective_user.id:
        state = awaiting_input.pop(user_id)
        step = state["step"]
        channel_username = user_input.split()[0]
        if step == "link_channel":
            try:
                channel = await context.bot.get_chat(channel_username)
                bot_member = await context.bot.get_chat_member(channel.id, context.bot.id)
                if bot_member.status not in ["administrator", "creator"]:
                    await update.message.reply_text("❌ البوت لازم يكون أدمن في القناة قبل الربط.")
                    return
                if user_id not in user_channels:
                    user_channels[user_id] = []
                if channel.id not in user_channels[user_id]:
                    user_channels[user_id].append(channel.id)
                    save_data()
                    await update.message.reply_text(f"✅ تم ربط القناة: {channel.title}")
                else:
                    await update.message.reply_text("⚠️ القناة مربوطة بالفعل.")
            except Exception:
                await update.message.reply_text("❌ خطأ: تأكد من اسم القناة وأن البوت في القناة.")
            return
        elif step == "unlink_channel":
            try:
                channel = await context.bot.get_chat(channel_username)
                if user_id in user_channels and channel.id in user_channels[user_id]:
                    user_channels[user_id].remove(channel.id)
                    save_data()
                    await update.message.reply_text(f"✅ فصلت القناة: {channel.title}")
                else:
                    await update.message.reply_text("⚠️ القناة مش مربوطة.")
            except Exception:
                await update.message.reply_text("❌ خطأ: تأكد من اسم القناة.")
            return

    # حالات بدء الدور بالمفتاح chat_id (int)
    target_chat_id = None
    for chat_id, data in awaiting_input.items():
        if isinstance(chat_id, int) and data.get("creator_id") == update.effective_user.id:
            target_chat_id = chat_id
            break
    if target_chat_id is None:
        return

    step = awaiting_input[target_chat_id]["step"]
    if step == "teacher":
        awaiting_input[target_chat_id]["teacher"] = user_input
        awaiting_input[target_chat_id]["step"] = "class_name"
        await update.message.reply_text("📘 اكتب اسم الحلقة:")
        return
    elif step == "class_name":
        teacher_name = awaiting_input[target_chat_id]["teacher"]
        class_name = user_input
        creator_name = awaiting_input[target_chat_id]["creator_name"]
        queues[target_chat_id] = {
            "creator": update.effective_user.id,
            "creator_name": creator_name,
            "admins": set(),
            "members": [],
            "removed": set(),
            "all_joined": set(),
            "closed": False,
            "usernames": {},
            "teacher_name": teacher_name,
            "class_name": class_name
        }
        del awaiting_input[target_chat_id]
        text = (
            f"👤 *بدأ الدور:* {creator_name}\n"
            f"📚 *اسم المعلمة:* {teacher_name}\n"
            f"🏫 *اسم الحلقة:* {class_name}\n\n"
            f"🎯 *القائمة الحالية:* (فاضية)"
        )
        await context.bot.send_message(chat_id=target_chat_id, text=text, reply_markup=make_main_keyboard(target_chat_id), parse_mode="Markdown")
        await update.message.reply_text("✅ تم إنشاء الدور بنجاح في القناة!")

# معالجة الأزرار (مختصر لأنك محفوظ اللوجيك كاملاً عندك)
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    data = query.data
    parts = data.split("|")
    action = parts[0]
    if action == "select_channel":
        target_chat_id = int(parts[1])
        await query.answer("تم الاختيار، هبدأ جمع البيانات في الخاص")
        await prompt_for_role(update, context, target_chat_id)
        return
    # الباقي منطقك كما هو — اختصرنا هنا للوضوح
    # يمكنك استبدال الدالة هذه بالكاملة لديك إذا رغبت.

# أمر الإغلاق الموزع (مقتبس)
async def force_close_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        # عرض اختيار للقنوات كما في كودك الأصلي
        await update.message.reply_text("استخدم الواجهة الخاصة لإغلاق الدور.")
    else:
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        try:
            member = await context.bot.get_chat_member(chat_id, user_id)
            if member.status not in ["administrator", "creator"]:
                await update.message.reply_text("🚫 لازم تكون مشرف.")
                return
        except Exception:
            await update.message.reply_text("❌ خطأ أثناء التحقق.")
            return
        if chat_id in queues:
            del queues[chat_id]
        if chat_id in awaiting_input:
            del awaiting_input[chat_id]
        await update.message.reply_text("✅ تم إغلاق الدور وإزالته من الذاكرة.")

# -----------------------------
# بناء الـ Application مرة واحدة (لا polling على Vercel)
# -----------------------------
application = ApplicationBuilder().token(TOKEN).build()
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("link", link_prompt))
application.add_handler(CommandHandler("unlink", unlink_prompt))
application.add_handler(CommandHandler("mychannels", my_channels))
application.add_handler(CommandHandler("startrole", start_role))
application.add_handler(CommandHandler("forceclose", force_close_command))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, collect_info))
application.add_handler(CallbackQueryHandler(button))

# -----------------------------
# FastAPI app ليتعامل مع Webhook
# -----------------------------
app = FastAPI()

@app.post("/api")
async def telegram_webhook(request: Request):
    """يتلقى تحديثات Telegram في شكل webhooks ويعالجها عبر python-telegram-bot application."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"status":"error","message":"Invalid JSON"})
    try:
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
        return {"status":"ok"}
    except Exception as e:
        print("Error processing update:", e)
        return JSONResponse(status_code=500, content={"status":"error","message":str(e)})

@app.get("/api")
async def root():
    return {"message":"Telegram Bot is ready to receive webhooks!"}
