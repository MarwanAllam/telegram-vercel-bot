# api/index.py
import json
import os
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)

# -----------------------------
# 🔑 ضع التوكن هنا كما طلبت (مضمن داخل الكود)
# -----------------------------
TOKEN = "8246108964:AAGTQI8zQl6rXqhLVG7_8NyFj4YqO35dMVg"
DATA_FILE = "data.json"  # ملاحظة: في Vercel التخزين محلي مؤقت (ephemeral)

# -----------------------------
# تحميل/حفظ بيانات بسيطة (user_channels)
# -----------------------------
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
# مساعدات
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
# Handlers
# -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "أهلاً 👋\nأنا بوت إدارة القنوات والدور.\n\n"
        "🔗 استخدم /link لربط قناة.\n"
        "🗑️ استخدم /unlink لفصل قناة.\n"
        "📜 استخدم /mychannels لعرض القنوات المربوطة.\n"
        "🎯 بعد ما تربط قناة، استخدم /startrole لتبدأ الدور في أي قناة مربوطة."
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def link_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    awaiting_input[user_id] = {"step": "link_channel", "chat_id": update.effective_chat.id, "creator_id": update.effective_user.id}
    await update.message.reply_text("🔗 أرسل الآن اسم القناة (مع @) لربطها:")

async def unlink_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    awaiting_input[user_id] = {"step": "unlink_channel", "chat_id": update.effective_chat.id, "creator_id": update.effective_user.id}
    await update.message.reply_text("🗑️ أرسل الآن اسم القناة (مع @) لفصلها:")

async def my_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id not in user_channels or not user_channels[user_id]:
        await update.message.reply_text("📭 مفيش قنوات مربوطة.")
        return
    text = "📋 القنوات المربوطة:\n"
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
        await update.message.reply_text("⚠️ لم يتم العثور على أي قنوات متاحة للبدء فيها.")
        return
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def prompt_for_role(update: Update, context: ContextTypes.DEFAULT_TYPE, target_chat_id: int):
    if target_chat_id in queues and not queues[target_chat_id].get("closed", True):
        await context.bot.send_message(chat_id=update.effective_chat.id, text="⚠️ فيه دور شغال بالفعل في هذه القناة، قم بإنهاءه أولاً.")
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

    # processing link/unlink (private)
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
                    await update.message.reply_text(f"✅ تم ربط القناة: **{channel.title}**", parse_mode="Markdown")
                else:
                    await update.message.reply_text("⚠️ القناة مربوطة بالفعل.")
            except Exception:
                await update.message.reply_text("❌ خطأ: تأكد من اسم القناة وأن البوت موجود فيها.")
            return
        elif step == "unlink_channel":
            try:
                channel = await context.bot.get_chat(channel_username)
                if user_id in user_channels and channel.id in user_channels[user_id]:
                    user_channels[user_id].remove(channel.id)
                    save_data()
                    await update.message.reply_text(f"✅ فصلت القناة: **{channel.title}**", parse_mode="Markdown")
                else:
                    await update.message.reply_text("⚠️ القناة مش مربوطة بحسابك.")
            except Exception:
                await update.message.reply_text("❌ خطأ: تأكد من اسم القناة.")
            return

    # processing role creation (waiting keyed by target_chat_id as int)
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

# -----------------------------
# Callback Query handler (full)
# -----------------------------
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    data = query.data or ""
    user = query.from_user
    parts = data.split("|")
    action = parts[0] if parts else ""

    print(f"[callback] action={action} from={user.id} data={data}")

    # select channel from /startrole
    if action == "select_channel":
        try:
            target_chat_id = int(parts[1])
        except Exception:
            await query.answer("❌ خطأ في بيانات القناة.")
            return
        await query.answer("اخترت القناة. سيتم بدء إدخال البيانات.")
        await prompt_for_role(update, context, target_chat_id)
        return

    # forceclose from private list
    if action == "forceclose_channel":
        try:
            target_chat_id = int(parts[1])
        except Exception:
            await query.answer("❌ خطأ في البيانات.")
            return

        closed_queue_message = ""
        if target_chat_id in queues:
            del queues[target_chat_id]
            closed_queue_message = "✅ تم مسح الدور العالق من الذاكرة بنجاح."
        else:
            closed_queue_message = "⚠️ لم يكن هناك دور مفتوح في الذاكرة لهذه القناة."

        if target_chat_id in awaiting_input:
            del awaiting_input[target_chat_id]

        try:
            ch = await context.bot.get_chat(target_chat_id)
            title = ch.title
        except Exception:
            title = "القناة المجهولة"

        await query.answer(closed_queue_message)
        try:
            await query.edit_message_text(
                f"🔒 **إغلاق إجباري مكتمل:**\nتم مسح بيانات الدور من الذاكرة لـ **{title}**.\n{closed_queue_message}",
                parse_mode="Markdown"
            )
        except Exception:
            # fallback: send a new message to the user (private)
            try:
                await context.bot.send_message(chat_id=update.effective_chat.id, text=closed_queue_message)
            except Exception as e:
                print("Warning: couldn't edit or send message on forceclose:", e)
        return

    # require at least chat_id
    if len(parts) < 2:
        await query.answer("❌ خطأ في بيانات الزر.")
        return

    try:
        chat_id = int(parts[1])
    except Exception:
        await query.answer("❌ خطأ في ID الدردشة.")
        return

    q = queues.get(chat_id)
    if not q:
        await query.answer("❌ مفيش دور شغال في هذه القناة.")
        return

    # join / leave
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

        members_text = "\n".join([f"{i+1}. {q['usernames'].get(uid, 'مجهول')}" for i, uid in enumerate(q["members"])]) or "(فاضية)"
        text = (
            f"👤 *بدأ الدور:* {q['creator_name']}\n"
            f"📚 *اسم المعلمة:* {q['teacher_name']}\n"
            f"🏫 *اسم الحلقة:* {q['class_name']}\n\n"
            f"🎯 *القائمة الحالية:*\n{members_text}"
        )
        try:
            await query.edit_message_text(text, reply_markup=make_main_keyboard(chat_id), parse_mode="Markdown")
        except Exception as e:
            print("Warning: could not edit message after join:", e)
            try:
                await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=make_main_keyboard(chat_id), parse_mode="Markdown")
            except Exception as e2:
                print("Also failed to send message to chat:", e2)
        return

    # remove_menu (show remove options)
    if action == "remove_menu":
        if not is_admin_or_creator(user.id, q):
            await query.answer("🚫 مش من صلاحياتك.")
            return
        if not q["members"]:
            await query.answer("📋 مفيش حد في الدور.")
            return

        await query.answer()
        keyboard = []
        for i, uid in enumerate(q["members"]):
            name = q["usernames"].get(uid, "مجهول")
            keyboard.append([InlineKeyboardButton(f"❌ {name}", callback_data=f"remove_member|{chat_id}|{i}")])
        keyboard.append([InlineKeyboardButton("🔙 إلغاء", callback_data=f"cancel_remove|{chat_id}")])

        text = "🗑️ *اختر الاسم اللي عايز تمسحه:*"
        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        except Exception as e:
            print("Warning: could not edit message for remove_menu:", e)
        return

    # remove_member
    if action == "remove_member":
        if not is_admin_or_creator(user.id, q):
            await query.answer("🚫 مش من صلاحياتك.")
            return
        try:
            index = int(parts[2])
        except Exception:
            await query.answer("❌ خطأ في الفهرس.")
            return
        if 0 <= index < len(q["members"]):
            target = q["members"].pop(index)
            q["removed"].add(target)
        await query.answer("✅ تم حذف العضو.")

        members_text = "\n".join([f"{i+1}. {q['usernames'].get(uid, 'مجهول')}" for i, uid in enumerate(q["members"])]) or "(فاضية)"
        text = (
            f"👤 *بدأ الدور:* {q['creator_name']}\n"
            f"📚 *اسم المعلمة:* {q['teacher_name']}\n"
            f"🏫 *اسم الحلقة:* {q['class_name']}\n\n"
            f"🎯 *القائمة الحالية:*\n{members_text}"
        )
        try:
            await query.edit_message_text(text, reply_markup=make_main_keyboard(chat_id), parse_mode="Markdown")
        except Exception as e:
            print("Warning: could not edit message after remove_member:", e)
        return

    # cancel_remove
    if action == "cancel_remove":
        await query.answer("تم الإلغاء ✅")
        members_text = "\n".join([f"{i+1}. {q['usernames'].get(uid, 'مجهول')}" for i, uid in enumerate(q["members"])]) or "(فاضية)"
        text = (
            f"👤 *بدأ الدور:* {q['creator_name']}\n"
            f"📚 *اسم المعلمة:* {q['teacher_name']}\n"
            f"🏫 *اسم الحلقة:* {q['class_name']}\n\n"
            f"🎯 *القائمة الحالية:*\n{members_text}"
        )
        try:
            await query.edit_message_text(text, reply_markup=make_main_keyboard(chat_id), parse_mode="Markdown")
        except Exception as e:
            print("Warning: could not edit message after cancel_remove:", e)
        return

    # close
    if action == "close":
        if not is_admin_or_creator(user.id, q):
            await query.answer("🚫 مش من صلاحياتك.")
            return
        q["closed"] = True
        await query.answer("🔒 تم إنهاء الدور.")

        all_joined = list(q["all_joined"])
        removed = list(q["removed"])
        remaining = [uid for uid in q["members"] if uid not in removed]

        full_list_text = "\n".join([f"{i+1}. {q['usernames'].get(uid, 'مجهول')}" for i, uid in enumerate(all_joined)]) or "(فاضية)"
        removed_text = "\n".join([f"{i+1}. {q['usernames'].get(uid, 'مجهول')}" for i, uid in enumerate(removed)]) or "(مفيش)"
        remaining_text = "\n".join([f"{i+1}. {q['usernames'].get(uid, 'مجهول')}" for i, uid in enumerate(remaining)]) or "(مفيش)"

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
            f"{remaining_text}\n\n"
            "🛑 *تم إنهاء الدور.*"
        )
        try:
            await query.message.reply_text(final_text, parse_mode="Markdown")
            await query.delete_message()
        except Exception as e:
            print("Warning: could not reply/delete original message on close:", e)
        if chat_id in queues:
            del queues[chat_id]
        return

    # manage_admins
    if action == "manage_admins":
        if user.id != q["creator"]:
            await query.answer("🚫 بس اللي بدأ الدور يقدر يدير المشرفين.")
            return

        members_to_manage = [uid for uid in q["all_joined"] if uid != q["creator"]]
        if not members_to_manage:
            await query.answer("📋 مفيش حد يمكن تعيينه مشرفًا غيرك.")
            return

        await query.answer()
        keyboard = []
        for uid in members_to_manage:
            name = q["usernames"].get(uid, "مجهول")
            label = f"⭐ أزل {name} من المشرفين" if uid in q["admins"] else f"⭐ عيّن {name} مشرف"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"toggle_admin|{chat_id}|{uid}")])
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"cancel_remove|{chat_id}")])

        try:
            await query.edit_message_text("👮 *إدارة المشرفين:*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        except Exception as e:
            print("Warning: could not edit message for manage_admins:", e)
        return

    # toggle_admin
    if action == "toggle_admin":
        if user.id != q["creator"]:
            await query.answer("🚫 بس اللي بدأ الدور يقدر يعمل كده.")
            return
        try:
            target_id = int(parts[2])
        except Exception:
            await query.answer("❌ خطأ في بيانات العضو.")
            return

        if target_id in q["admins"]:
            q["admins"].remove(target_id)
            await query.answer("❌ تم إزالة الإشراف.")
        else:
            q["admins"].add(target_id)
            await query.answer("⭐ تم تعيينه مشرفًا.")

        members_to_manage = [uid for uid in q["all_joined"] if uid != q["creator"]]
        keyboard = []
        for uid in members_to_manage:
            name = q["usernames"].get(uid, "مجهول")
            label = f"⭐ أزل {name} من المشرفين" if uid in q["admins"] else f"⭐ عيّن {name} مشرف"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"toggle_admin|{chat_id}|{uid}")])
        keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"cancel_remove|{chat_id}")])

        try:
            await query.edit_message_text("👮 *إدارة المشرفين:*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        except Exception as e:
            print("Warning: could not edit message after toggle_admin:", e)
        return

    # unknown action
    await query.answer("❌ فعل غير معروف.")
    return

# -----------------------------
# force close in group / private prompts
# -----------------------------
async def force_close_in_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    user_name = update.effective_user.full_name
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status not in ["administrator", "creator"]:
            await update.message.reply_text("🚫 يجب أن تكون مشرفًا في هذه القناة لاستخدام أمر /forceclose.")
            return
    except Exception:
        await update.message.reply_text("❌ حدث خطأ أثناء التحقق من صلاحياتك.")
        return
    if chat_id in queues:
        del queues[chat_id]
        closed_queue_message = f"🚨 تم حذف الدور العالق بنجاح بواسطة **{user_name}** ✅\nالآن يمكنك بدء دور جديد."
    else:
        closed_queue_message = f"⚠️ مفيش دور مفتوح حاليًا في هذه الدردشة ليتم حذفه."
    if chat_id in awaiting_input:
        del awaiting_input[chat_id]
    user_id_str = str(user_id)
    if user_id_str in awaiting_input:
        del awaiting_input[user_id_str]
    await update.message.reply_text(closed_queue_message, parse_mode="Markdown")

async def force_close_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id not in user_channels or not user_channels[user_id]:
        await update.message.reply_text("🚫 مفيش قنوات مربوطة بحسابك عشان تختار منها. استخدم /link أولاً.")
        return
    text = "🔒 اختر القناة التي تريد إغلاق الدور العالق فيها إجباريًا:"
    keyboard = []
    for ch_id in user_channels[user_id]:
        if ch_id in queues:
            try:
                ch = await context.bot.get_chat(ch_id)
                keyboard.append([InlineKeyboardButton(f"✅ {ch.title} (المعلمة: {queues[ch_id]['teacher_name']})", callback_data=f"forceclose_channel|{ch_id}")])
            except Exception:
                continue
    if not keyboard:
        await update.message.reply_text("🎉 لا توجد أدوار فعالة حالياً في قنواتك المربوطة.")
        return
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def force_close_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await force_close_prompt(update, context)
    else:
        if update.effective_chat.type in ["channel", "supergroup", "group"]:
            await force_close_in_group(update, context)

# -----------------------------
# إعداد التطبيق (Application + FastAPI integration)
# -----------------------------
application = ApplicationBuilder().token(TOKEN).build()

# register handlers
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("link", link_prompt))
application.add_handler(CommandHandler("unlink", unlink_prompt))
application.add_handler(CommandHandler("mychannels", my_channels))
application.add_handler(CommandHandler("startrole", start_role))
application.add_handler(CommandHandler("forceclose", force_close_command))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, collect_info))
application.add_handler(CallbackQueryHandler(button))

# FastAPI app
app = FastAPI()

@app.on_event("startup")
async def on_startup():
    try:
        await application.initialize()
        print("Application initialized successfully.")
    except Exception as e:
        print("Error during application.initialize():", e)

@app.on_event("shutdown")
async def on_shutdown():
    try:
        await application.shutdown()
        print("Application shutdown completed.")
    except Exception as e:
        print("Error during application.shutdown():", e)

@app.post("/api")
async def telegram_webhook(request: Request):
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
