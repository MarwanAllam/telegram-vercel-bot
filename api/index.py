import os
import json
import asyncio
import time
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from telegram.error import BadRequest

# -----------------------------
# 🔑 التوكن والإعدادات الخاصة بـ Vercel
# -----------------------------
# يستخدم os.environ للحصول على التوكن من متغيرات البيئة (الأكثر أمانًا)
# يجب عليك وضع التوكن في إعدادات Vercel كمتغير بيئة باسم TOKEN
TOKEN = os.environ.get("TOKEN", "8246108964:AAGTQI8zQl6rXqhLVG7_8NyFj4YqO35dMVg")

DATA_FILE = "data.json"  # تنبيه: هذا التخزين مؤقت (Ephemeral) على Vercel ولن يدوم!

# -----------------------------
# الحالة العامة (in-memory) + أدوات التزامن
# -----------------------------
queues = {}            
awaiting_input = {}    

# أدوات منع السباق (Race Condition)
locks = {}             # chat_id -> asyncio.Lock()
last_action = {}       # chat_id -> timestamp of last edit
COOLDOWN = 0.6         # (تم زيادة القيمة من 0.35 إلى 0.6 للثبات على Vercel)

# -----------------------------
# تحميل/حفظ بيانات بسيطة (user_channels)
# -----------------------------
# ملاحظة: هذا التخزين لن يدوم على Vercel!
try:
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        user_channels = json.load(f)
        user_channels = {k: [int(cid) for cid in v] if isinstance(v, list) else v for k, v in user_channels.items()}
except (FileNotFoundError, json.JSONDecodeError):
    user_channels = {}

def save_data():
    """يحفظ بيانات القنوات المربوطة (ملاحظة: لن يعمل بشكل دائم على Vercel)."""
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(user_channels, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("Warning: could not save data locally:", e)

# -----------------------------
# مساعدات (Functions)
# -----------------------------

def make_main_keyboard(chat_id):
    """ينشئ لوحة المفاتيح الرئيسية للدور."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 انضم / انسحب", callback_data=f"join|{chat_id}")],
        [
            InlineKeyboardButton("🗑️ ريموف", callback_data=f"remove_menu|{chat_id}"),
            InlineKeyboardButton("🔒 إنهاء الدور", callback_data=f"close|{chat_id}")
        ],
        [InlineKeyboardButton("⭐ إدارة المشرفين", callback_data=f"manage_admins|{chat_id}")]
    ])

def is_admin_or_creator(user_id, q):
    """يتحقق إن كان المستخدم هو المنشئ أو مشرف في الدور."""
    return user_id == q["creator"] or user_id in q["admins"]


# ----------------------------------------
#        1. أوامر الربط والإدارة
# ----------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "أهلاً 👋\nأنا بوت إدارة القنوات والدور.\n\n"
        "🔗 استخدم **/link** لربط قناة.\n"
        "🗑️ استخدم **/unlink** لفصل قناة.\n"
        "📜 استخدم **/mychannels** لعرض القنوات المربوطة.\n"
        "🎯 بعد ما تربط قناة، استخدم **/startrole** لتبدأ الدور في أي قناة مربوطة."
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def link_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    awaiting_input[user_id] = {"step": "link_channel", "chat_id": update.effective_chat.id, "creator_id": update.effective_user.id} 
    await update.message.reply_text("🔗 **أرسل الآن اسم القناة** (مع @) التي تود ربطها:")

async def unlink_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    awaiting_input[user_id] = {"step": "unlink_channel", "chat_id": update.effective_chat.id, "creator_id": update.effective_user.id}
    await update.message.reply_text("🗑️ **أرسل الآن اسم القناة** (مع @) التي تود فصلها:")

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
        await update.message.reply_text("🚫 مفيش قنوات مربوطة. استخدم **/link** أول.")
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

# ----------------------------------------
#        2. منطق بدء الدور وجمع المعلومات / الربط والفصل
# ----------------------------------------

async def prompt_for_role(update: Update, context: ContextTypes.DEFAULT_TYPE, target_chat_id: int):
    
    if target_chat_id in queues and not queues[target_chat_id].get("closed", True):
        await context.bot.send_message(
            chat_id=update.effective_chat.id, 
            text="⚠️ فيه دور شغال بالفعل في هذه القناة، قم بإنهاءه أولاً."
        )
        return

    awaiting_input[target_chat_id] = { 
        "step": "teacher",
        "creator_id": update.effective_user.id,
        "creator_name": update.effective_user.full_name,
        "private_chat_id": update.effective_chat.id 
    }
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="👩‍🏫 **اكتب اسم المعلمة:** (الرد هيكون في الدردشة الخاصة هنا)"
    )


async def collect_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    if not update.message or not update.message.text:
        return

    user_id = str(update.effective_user.id)
    user_input = update.message.text.strip()

    # 1. عمليات الربط/الفصل
    if user_id in awaiting_input and awaiting_input[user_id].get("creator_id") == update.effective_user.id:
        state = awaiting_input.pop(user_id) 
        step = state["step"]
        channel_username = user_input.split()[0]

        if step == "link_channel":
            try:
                channel = await context.bot.get_chat(channel_username)
                bot_member = await context.bot.get_chat_member(channel.id, context.bot.id)
                
                if bot_member.status not in ["administrator", "creator"]:
                    await update.message.reply_text("❌ البوت لازم يكون **أدمن** في القناة قبل الربط.")
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
                await update.message.reply_text(f"❌ حصل خطأ. تأكد من إرسال اسم قناة صحيح (مع @) ومن كون البوت في القناة.")
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
                await update.message.reply_text(f"❌ حصل خطأ. تأكد من إرسال اسم قناة صحيح (مع @).")
            return

    # 2. عملية بدء الدور
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
        await update.message.reply_text("📘 **اكتب اسم الحلقة:**")
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
        await context.bot.send_message(
            chat_id=target_chat_id,
            text=text,
            reply_markup=make_main_keyboard(target_chat_id),
            parse_mode="Markdown"
        )
        await update.message.reply_text("✅ تم إنشاء الدور بنجاح في القناة!")


# ----------------------------------------
#        3. معالجة الأزرار (مع تثبيت التزامن)
# ----------------------------------------

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    # 1. الرد السريع لمنع خطأ "Query is too old"
    try:
        await query.answer() 
    except Exception:
        pass

    data = query.data or ""
    user = query.from_user
    parts = data.split("|")
    action = parts[0] if parts else ""

    # مسارات لا تحتاج قفل (select_channel, forceclose_channel)
    if action == "select_channel":
        try:
            target_chat_id = int(parts[1])
        except Exception:
            return
        await prompt_for_role(update, context, target_chat_id)
        return

    if action == "forceclose_channel":
        try:
            target_chat_id = int(parts[1])
        except Exception:
            return
        # تنفيذ منطق force_close_channel...
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
        await query.edit_message_text(
            f"🔒 **إغلاق إجباري مكتمل:**\nتم مسح بيانات الدور من الذاكرة لـ **{title}**.\n{closed_queue_message}",
            parse_mode="Markdown"
        )
        return
    # نهاية المسارات التي لا تحتاج قفل

    if len(parts) < 2:
        return
    
    try:
        chat_id = int(parts[1])
    except Exception:
        return

    q = queues.get(chat_id)
    if not q:
        return # الرسالة أصبحت قديمة، أو لا يوجد دور

    # 2. Debounce: منع الضغطة السريعة المتكررة (COOLDOWN)
    now = time.time()
    last = last_action.get(chat_id, 0)
    if now - last < COOLDOWN:
        return # تجاهل الطلب

    last_action[chat_id] = now

    # 3. Lock: منع التعديل المتزامن (Race Condition)
    lock = locks.setdefault(chat_id, asyncio.Lock())
    async with lock:
        
        # ------------------------------------
        # منطق الانضمام (مع إصلاح التكرار)
        # ------------------------------------
        if action == "join":
            if q["closed"]:
                return

            q["usernames"][user.id] = user.full_name

            if user.id in q["removed"]:
                return

            # تحديث الحالة
            if user.id in q["members"]:
                q["members"].remove(user.id)
                if user.id in q["all_joined"]:
                    q["all_joined"].remove(user.id)
            else:
                q["members"].append(user.id)
                q["all_joined"].add(user.id)

            # بناء النص
            members_text = "\n".join([f"{i+1}. {q['usernames'].get(uid, 'مجهول')}" for i, uid in enumerate(q["members"])]) or "(فاضية)"
            text = (
                f"👤 *بدأ الدور:* {q['creator_name']}\n"
                f"📚 *اسم المعلمة:* {q['teacher_name']}\n"
                f"🏫 *اسم الحلقة:* {q['class_name']}\n\n"
                f"🎯 *القائمة الحالية:*\n{members_text}"
            )
            
            # 🛑 الإصلاح: نحاول تعديل الرسالة، ونلغي أي Fallback لإرسال رسالة جديدة.
            try:
                await query.edit_message_text(text, reply_markup=make_main_keyboard(chat_id), parse_mode="Markdown")
            except BadRequest as e:
                # هذا الخطأ شائع عند محاولة تعديل رسالة تم تعديلها بالفعل أو لم تتغير.
                # هذا هو بالضبط ما نحتاجه: نتجاهل ونمنع التكرار.
                print(f"Warning: could not edit message after join (likely concurrency or no change): {e}")
            except Exception as e:
                print(f"CRITICAL ERROR: Failed to edit message after join (General Exception): {e}")
            return
        
        # ------------------------------------
        # منطق الإدارة (نفس المنطق السابق، نعتمد على edit_message_text)
        # ------------------------------------

        elif action == "remove_menu":
            if not is_admin_or_creator(user.id, q): return
            if not q["members"]: return
            keyboard = []
            for i, uid in enumerate(q["members"]):
                name = q["usernames"].get(uid, "مجهول")
                keyboard.append([InlineKeyboardButton(f"❌ {name}", callback_data=f"remove_member|{chat_id}|{i}")])
            keyboard.append([InlineKeyboardButton("🔙 إلغاء", callback_data=f"cancel_remove|{chat_id}")])
            text = "🗑️ *اختر الاسم اللي عايز تمسحه:*"
            try:
                await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
            except Exception:
                pass
            return

        elif action == "remove_member":
            if not is_admin_or_creator(user.id, q): return
            try:
                index = int(parts[2])
            except Exception:
                return
            if 0 <= index < len(q["members"]):
                target = q["members"].pop(index)
                q["removed"].add(target)

            members_text = "\n".join([f"{i+1}. {q['usernames'].get(uid, 'مجهول')}" for i, uid in enumerate(q["members"])]) or "(فاضية)"
            text = (
                f"👤 *بدأ الدور:* {q['creator_name']}\n"
                f"📚 *اسم المعلمة:* {q['teacher_name']}\n"
                f"🏫 *اسم الحلقة:* {q['class_name']}\n\n"
                f"🎯 *القائمة الحالية:*\n{members_text}"
            )
            try:
                await query.edit_message_text(text, reply_markup=make_main_keyboard(chat_id), parse_mode="Markdown")
            except Exception:
                pass
            return

        elif action == "cancel_remove":
            members_text = "\n".join([f"{i+1}. {q['usernames'].get(uid, 'مجهول')}" for i, uid in enumerate(q["members"])]) or "(فاضية)"
            text = (
                f"👤 *بدأ الدور:* {q['creator_name']}\n"
                f"📚 *اسم المعلمة:* {q['teacher_name']}\n"
                f"🏫 *اسم الحلقة:* {q['class_name']}\n\n"
                f"🎯 *القائمة الحالية:*\n{members_text}"
            )
            try:
                await query.edit_message_text(text, reply_markup=make_main_keyboard(chat_id), parse_mode="Markdown")
            except Exception:
                pass
            return

        elif action == "close":
            if not is_admin_or_creator(user.id, q): return
            q["closed"] = True
            
            # بناء رسالة التلخيص النهائية
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
                print(f"Warning: could not finalize or delete message on close: {e}")

            if chat_id in queues:
                del queues[chat_id]
            return

        elif action == "manage_admins":
            if user.id != q["creator"]: return
            members_to_manage = [uid for uid in q["all_joined"] if uid != q["creator"]]
            if not members_to_manage: return
            keyboard = []
            for uid in members_to_manage:
                name = q["usernames"].get(uid, "مجهول")
                label = f"⭐ أزل {name} من المشرفين" if uid in q["admins"] else f"⭐ عيّن {name} مشرف"
                keyboard.append([InlineKeyboardButton(label, callback_data=f"toggle_admin|{chat_id}|{uid}")])
            keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"cancel_remove|{chat_id}")])
            try:
                await query.edit_message_text("👮 *إدارة المشرفين:*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
            except Exception:
                pass
            return

        elif action == "toggle_admin":
            if user.id != q["creator"]: return
            try:
                target_id = int(parts[2])
            except Exception:
                return
            if target_id in q["admins"]:
                q["admins"].remove(target_id)
            else:
                q["admins"].add(target_id)

            members_to_manage = [uid for uid in q["all_joined"] if uid != q["creator"]]
            keyboard = []
            for uid in members_to_manage:
                name = q["usernames"].get(uid, "مجهول")
                label = f"⭐ أزل {name} من المشرفين" if uid in q["admins"] else f"⭐ عيّن {name} مشرف"
                keyboard.append([InlineKeyboardButton(label, callback_data=f"toggle_admin|{chat_id}|{uid}")])
            keyboard.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"cancel_remove|{chat_id}")])
            try:
                await query.edit_message_text("👮 *إدارة المشرفين:*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
            except Exception:
                pass
            return

# ----------------------------------------
#        4. أمر الإغلاق الإجباري
# ----------------------------------------

async def force_close_in_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    user_name = update.effective_user.full_name
    
    # تحقق من صلاحيات المشرف
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status not in ["administrator", "creator"]:
            await update.message.reply_text("🚫 يجب أن تكون مشرفًا في هذه القناة لاستخدام أمر `/forceclose`.")
            return
    except Exception:
        await update.message.reply_text("❌ حدث خطأ أثناء التحقق من صلاحياتك.")
        return

    # تنظيف الحالة
    if chat_id in queues: del queues[chat_id]
    if chat_id in awaiting_input: del awaiting_input[chat_id]
    user_id_str = str(user_id)
    if user_id_str in awaiting_input: del awaiting_input[user_id_str]
        
    closed_queue_message = f"🚨 تم حذف الدور العالق بنجاح بواسطة **{user_name}** ✅\nالآن يمكنك بدء دور جديد."
    await update.message.reply_text(closed_queue_message, parse_mode="Markdown")

async def force_close_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id not in user_channels or not user_channels[user_id]:
        await update.message.reply_text("🚫 مفيش قنوات مربوطة بحسابك عشان تختار منها. استخدم **/link** أولاً.")
        return

    text = "🔒 **اختر القناة التي تريد إغلاق الدور العالق فيها إجباريًا:**"
    keyboard = []
    active_queues_for_user = [] 
    
    for ch_id in user_channels[user_id]:
        if ch_id in queues: 
            try:
                ch = await context.bot.get_chat(ch_id)
                active_queues_for_user.append((ch_id, ch.title))
                keyboard.append([
                    InlineKeyboardButton(
                        f"✅ {ch.title} (المعلمة: {queues[ch_id]['teacher_name']})", 
                        callback_data=f"forceclose_channel|{ch_id}"
                    )
                ])
            except Exception:
                continue
    
    if not active_queues_for_user:
        await update.message.reply_text("🎉 **لا توجد أدوار فعالة حاليًا** في أي من قنواتك المربوطة.")
        return

    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def force_close_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await force_close_prompt(update, context)
    else:
        if update.effective_chat.type in ["channel", "supergroup", "group"]:
            await force_close_in_group(update, context)

# ----------------------------------------
#        5. إعداد Webhook و FastAPI
# ----------------------------------------
application = ApplicationBuilder().token(TOKEN).build()

# تسجيل المعالجات (Handlers)
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("link", link_prompt))
application.add_handler(CommandHandler("unlink", unlink_prompt))
application.add_handler(CommandHandler("mychannels", my_channels))
application.add_handler(CommandHandler("startrole", start_role))
application.add_handler(CommandHandler("forceclose", force_close_command))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, collect_info))
application.add_handler(CallbackQueryHandler(button))

# تطبيق FastAPI (الذي سيستقبل الـ Webhook)
app = FastAPI()

@app.on_event("startup")
async def on_startup():
    """تهيئة التطبيق عند بدء تشغيل نسخة Vercel"""
    try:
        await application.initialize()
    except Exception as e:
        print("Error during application.initialize():", e)

@app.post("/api") # المسار الذي يستدعيه تليجرام
async def telegram_webhook(request: Request):
    """معالج طلبات Webhook"""
    if not TOKEN:
        return JSONResponse(status_code=500, content={"status":"error","message":"TOKEN is not set"})
    
    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        # تشغيل معالجات python-telegram-bot
        await application.process_update(update) 
        return {"status":"ok"}
    except Exception as e:
        print(f"Error processing update: {e}")
        return JSONResponse(status_code=500, content={"status":"error","message":str(e)})

@app.get("/api")
async def root():
    """مسار اختبار بسيط"""
    return {"message":"Telegram Bot is ready to receive webhooks!"}
