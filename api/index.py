# api/index.py
import os
import json
from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)

# جلب التوكن من متغير بيئي
TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TOKEN:
    raise RuntimeError("Missing TELEGRAM_TOKEN environment variable")

# Global state
queues = {}
awaiting_input = {}

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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in queues and not queues[chat_id].get("closed", True):
        await update.message.reply_text("⚠️ فيه دور شغال بالفعل، اقفله الأول قبل تبدأ جديد.")
        return
    awaiting_input[chat_id] = {"step": "teacher"}
    await update.message.reply_text("👩‍🏫 اكتب اسم المعلمة:")

async def collect_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    chat_id = update.effective_chat.id
    user_input = update.message.text.strip()
    if chat_id not in awaiting_input:
        return
    step = awaiting_input[chat_id]["step"]
    if step == "teacher":
        awaiting_input[chat_id]["teacher"] = user_input
        awaiting_input[chat_id]["step"] = "class_name"
        await update.message.reply_text("📘 اكتب اسم الحلقة:")
        return
    elif step == "class_name":
        teacher_name = awaiting_input[chat_id]["teacher"]
        class_name = user_input
        creator_name = update.effective_user.full_name
        queues[chat_id] = {
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
        del awaiting_input[chat_id]
        text = (
            f"👤 *بدأ الدور:* {creator_name}\n"
            f"📚 *اسم المعلمة:* {teacher_name}\n"
            f"🏫 *اسم الحلقة:* {class_name}\n\n"
            f"🎯 *القائمة الحالية:* (فاضية)"
        )
        await update.message.reply_text(text, reply_markup=make_main_keyboard(chat_id), parse_mode="Markdown")

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    data = query.data
    user = query.from_user
    parts = data.split("|")
    action = parts[0]
    try:
        chat_id = int(parts[1])
    except:
        await query.answer("خطأ في البيانات.")
        return
    q = queues.get(chat_id)

    if not q:
        await query.answer("❌ مفيش دور شغال.")
        return

    if action == "join":
        if q["closed"]:
            await query.answer("🚫 التسجيل مقفول.")
            return
        q["usernames"][user.id] = user.full_name
        if user.id in q["removed"]:
            await query.answer("🚫 تم حذفك من الدور. استنى الدور الجديد.")
            return
        if user.id in q["members"]:
            q["members"].remove(user.id)
            if user.id in q["all_joined"]:
                q["all_joined"].remove(user.id)
            await query.answer("❌ تم انسحابك.")
        else:
            q["members"].append(user.id)
            q["all_joined"].add(user.id)
            await query.answer("✅ تم تسجيلك!")

        members_text = "\n".join(
            [f"{i+1}. {q['usernames'].get(uid, 'مجهول')}" for i, uid in enumerate(q["members"])]
        ) or "(فاضية)"
        text = (
            f"👤 *بدأ الدور:* {q['creator_name']}\n"
            f"📚 *اسم المعلمة:* {q['teacher_name']}\n"
            f"🏫 *اسم الحلقة:* {q['class_name']}\n\n"
            f"🎯 *القائمة الحالية:*\n{members_text}"
        )
        await query.edit_message_text(text, reply_markup=make_main_keyboard(chat_id), parse_mode="Markdown")

    elif action == "remove_menu":
        if not is_admin_or_creator(user.id, q):
            await query.answer("🚫 مش من صلاحياتك.")
            return
        if not q["members"]:
            await query.answer("📋 مفيش حد في الدور.")
            return

        keyboard = []
        for i, uid in enumerate(q["members"]):
            name = q["usernames"].get(uid, "مجهول")
            keyboard.append([InlineKeyboardButton(f"❌ {name}", callback_data=f"remove_member|{chat_id}|{i}")])
        keyboard.append([InlineKeyboardButton("🔙 إلغاء", callback_data=f"cancel_remove|{chat_id}")])

        text = "🗑️ *اختر الاسم اللي عايز تمسحه:*"
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif action == "remove_member":
        if not is_admin_or_creator(user.id, q):
            await query.answer("🚫 مش من صلاحياتك.")
            return
        index = int(parts[2])
        if 0 <= index < len(q["members"]):
            target = q["members"].pop(index)
            q["removed"].add(target)

        members_text = "\n".join(
            [f"{i+1}. {q['usernames'].get(uid, 'مجهول')}" for i, uid in enumerate(q["members"])]
        ) or "(فاضية)"
        text = (
            f"👤 *بدأ الدور:* {q['creator_name']}\n"
            f"📚 *اسم المعلمة:* {q['teacher_name']}\n"
            f"🏫 *اسم الحلقة:* {q['class_name']}\n\n"
            f"🎯 *القائمة الحالية:*\n{members_text}"
        )
        await query.edit_message_text(text, reply_markup=make_main_keyboard(chat_id), parse_mode="Markdown")

    elif action == "cancel_remove":
        members_text = "\n".join(
            [f"{i+1}. {q['usernames'].get(uid, 'مجهول')}" for i, uid in enumerate(q["members"])]
        ) or "(فاضية)"
        text = (
            f"👤 *بدأ الدور:* {q['creator_name']}\n"
            f"📚 *اسم المعلمة:* {q['teacher_name']}\n"
            f"🏫 *اسم الحلقة:* {q['class_name']}\n\n"
            f"🎯 *القائمة الحالية:*\n{members_text}"
        )
        await query.edit_message_text(text, reply_markup=make_main_keyboard(chat_id), parse_mode="Markdown")
        await query.answer("تم الإلغاء ✅")

    elif action == "close":
        if not is_admin_or_creator(user.id, q):
            await query.answer("🚫 مش من صلاحياتك.")
            return
        q["closed"] = True

        all_joined = list(q["all_joined"])
        removed = list(q["removed"])
        remaining = [uid for uid in q["members"] if uid not in removed]

        full_list_text = "\n".join(
            [f"{i+1}. {q['usernames'].get(uid, 'مجهول')}" for i, uid in enumerate(all_joined)]
        ) or "(فاضية)"
        removed_text = "\n".join(
            [f"{i+1}. {q['usernames'].get(uid, 'مجهول')}" for i, uid in enumerate(removed)]
        ) or "(مفيش)"
        remaining_text = "\n".join(
            [f"{i+1}. {q['usernames'].get(uid, 'مجهول')}" for i, uid in enumerate(remaining)]
        ) or "(مفيش)"

        final_text = (
            f"👤 *بدأ الدور:* {q['creator_name']}\n"
            f"📚 *اسم المعلمة:* {q['teacher_name']}\n"
            f"🏫 *اسم الحلقة:* {q['class_name']}\n\n"
            "📋 *القائمة النهائية للدور:*\n\n"
            "👥 *كل اللي شاركوا فعليًا:*\n"
            f"{full_list_text}\n\n"
            "✅ *تمت القراءه:*\n"
            f"{removed_text}\n\n"
            "❌ *لم يقرأ:*\n"
            f"{remaining_text}"
        )

        await query.message.reply_text(final_text, parse_mode="Markdown")
        del queues[chat_id]

    elif action == "manage_admins":
        if user.id != q["creator"]:
            await query.answer("🚫 بس اللي بدأ الدور يقدر يدير المشرفين.")
            return

        if not q["members"]:
            await query.answer("📋 مفيش حد في الدور.")
            return

        keyboard = []
        for uid in q["members"]:
            if uid == q["creator"]:
                continue
            name = q["usernames"].get(uid, "مجهول")
            label = f"⭐ أزل {name} من المشرفين" if uid in q["admins"] else f"⭐ عيّن {name} مشرف"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"toggle_admin|{chat_id}|{uid}")])
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"cancel_remove|{chat_id}")])

        await query.edit_message_text("👮 *إدارة المشرفين:*",
                                      reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif action == "toggle_admin":
        if user.id != q["creator"]:
            await query.answer("🚫 بس اللي بدأ الدور يقدر يعمل كده.")
            return
        target_id = int(parts[2])
        if target_id in q["admins"]:
            q["admins"].remove(target_id)
        else:
            q["admins"].add(target_id)

        keyboard = []
        for uid in q["members"]:
            if uid == q["creator"]:
                continue
            name = q["usernames"].get(uid, "مجهول")
            label = f"⭐ أزل {name} من المشرفين" if uid in q["admins"] else f"⭐ عيّن {name} مشرف"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"toggle_admin|{chat_id}|{uid}")])
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"cancel_remove|{chat_id}")])

        await query.edit_message_text("👮 *إدارة المشرفين:*",
                                      reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def force_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_name = update.effective_user.full_name
    if chat_id in queues:
        del queues[chat_id]
    if chat_id in awaiting_input:
        del awaiting_input[chat_id]
    await update.message.reply_text(
        f"🚨 تم قفل أو حذف أي دور مفتوح بواسطة *{user_name}* ✅",
        parse_mode="Markdown"
    )

# Build Application ONCE
application = ApplicationBuilder().token(TOKEN).build()
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("forceclose", force_close))
application.add_handler(CallbackQueryHandler(button))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, collect_info))

# FastAPI app for Vercel
app = FastAPI()

@app.post("/api")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        return {"status": "error", "message": "Invalid JSON"}, 400
    try:
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
        return {"status": "ok"}
    except Exception as e:
        print("Error processing update:", e)
        return {"status": "error", "message": str(e)}, 500

@app.get("/api")
async def root():
    return {"message": "Telegram Bot is ready to receive webhooks!"}
