import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import requests
import time
import os
import threading
from keep_alive import keep_alive


# ================= الإعدادات الأساسية =================
# سيقوم Render بقراءة التوكن من المتغيرات البيئية
BOT_TOKEN = os.getenv("BOT_TOKEN") 
bot = telebot.TeleBot(BOT_TOKEN)

CHANNEL_USERNAME = "@ZenoX_Tools"
ADMIN_ID = 6043858925

# ================= أزرار ملونة (ميزة Bot API 9.4) =================
# style: "success" (أخضر) / "danger" (أحمر) / "primary" (أزرق) / None (افتراضي بدون لون)
# محمية بـ try/except: لو نسخة المكتبة على الاستضافة أقدم ولا تدعم "style" بعد،
# يتم إنشاء الزر بدون لون تلقائيًا بدل ما يتعطل البوت بالكامل.
def colored_button(text, callback_data=None, url=None, style=None):
    kwargs = {}
    if callback_data is not None:
        kwargs["callback_data"] = callback_data
    if url is not None:
        kwargs["url"] = url
    if style:
        try:
            return InlineKeyboardButton(text, style=style, **kwargs)
        except TypeError:
            print("[colored_button] مكتبة pyTelegramBotAPI الحالية لا تدعم 'style' بعد، تم إنشاء الزر بدون لون.")
    return InlineKeyboardButton(text, **kwargs)

# ================= قاعدة بيانات المستخدمين والإحصائيات =================
import sqlite3
from datetime import datetime, timedelta

DB_PATH = "bot_data.db"


def db_connect():
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_seen TEXT,
            last_active TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS requests_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def track_user(user_id, username=None):
    """يسجل المستخدم إذا كان جديدًا، ويحدّث آخر نشاط له."""
    now = datetime.utcnow().isoformat()
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    exists = cur.fetchone()
    if exists:
        cur.execute("UPDATE users SET last_active = ?, username = ? WHERE user_id = ?", (now, username, user_id))
    else:
        cur.execute(
            "INSERT INTO users (user_id, username, first_seen, last_active) VALUES (?, ?, ?, ?)",
            (user_id, username, now, now),
        )
    conn.commit()
    conn.close()


def log_request(user_id, action):
    """يسجل حدث/طلب معين لأغراض الإحصائيات."""
    now = datetime.utcnow().isoformat()
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO requests_log (user_id, action, created_at) VALUES (?, ?, ?)",
        (user_id, action, now),
    )
    conn.commit()
    conn.close()


def get_stats():
    """يحسب إحصائيات دقيقة: مستخدمين نشطين وطلبات، الآن/اليوم/7 أيام/30 يوم."""
    now = datetime.utcnow()
    today_start = now.strftime("%Y-%m-%d")
    d5min = (now - timedelta(minutes=5)).isoformat()
    d7 = (now - timedelta(days=7)).isoformat()
    d30 = (now - timedelta(days=30)).isoformat()

    conn = db_connect()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM users")
    total_users = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM users WHERE last_active >= ?", (d5min,))
    active_now = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM users WHERE last_active LIKE ?", (f"{today_start}%",))
    active_today = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM users WHERE last_active >= ?", (d7,))
    active_7d = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM users WHERE last_active >= ?", (d30,))
    active_30d = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM requests_log WHERE created_at LIKE ?", (f"{today_start}%",))
    requests_today = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM requests_log WHERE created_at >= ?", (d7,))
    requests_7d = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM requests_log WHERE created_at >= ?", (d30,))
    requests_30d = cur.fetchone()[0]

    cur.execute("""
        SELECT action, COUNT(*) FROM requests_log
        WHERE created_at LIKE ?
        GROUP BY action ORDER BY COUNT(*) DESC
    """, (f"{today_start}%",))
    breakdown_today = cur.fetchall()

    conn.close()
    return {
        "total_users": total_users,
        "active_now": active_now,
        "active_today": active_today,
        "active_7d": active_7d,
        "active_30d": active_30d,
        "requests_today": requests_today,
        "requests_7d": requests_7d,
        "requests_30d": requests_30d,
        "breakdown_today": breakdown_today,
    }


def get_all_user_ids():
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users")
    ids = [row[0] for row in cur.fetchall()]
    conn.close()
    return ids


init_db()

# ملاحظة: خادم Flask الخاص بإبقاء البوت حيًا موجود بالفعل في keep_alive.py
# ويُستدعى عبر keep_alive() في الأسفل، لذا لا حاجة لتكراره هنا.

# ================= طبقة خدمة البريد المؤقت (mail.tm أساسي + 1secmail احتياطي) =================
# 1secmail معروفة بعدم الاستقرار (403 / انقطاع متكرر) خصوصًا من IP استضافات مجانية مشتركة.
# لذلك نعتمد mail.tm كخدمة رئيسية أكثر استقرارًا، ونحتفظ بـ1secmail كخطة بديلة تلقائية.

MAILTM_BASE = "https://api.mail.tm"


def _mailtm_create_account():
    """ينشئ حساب mail.tm جديد ويعيد (email, password, token)."""
    domains_resp = requests.get(f"{MAILTM_BASE}/domains", timeout=10)
    domains_resp.raise_for_status()
    domains = domains_resp.json().get("hydra:member", [])
    if not domains:
        raise RuntimeError("لا توجد دومينات متاحة على mail.tm حاليًا")
    domain = domains[0]["domain"]

    login = generate_random_login()
    password = generate_random_login(14)
    address = f"{login}@{domain}"

    create_resp = requests.post(
        f"{MAILTM_BASE}/accounts",
        json={"address": address, "password": password},
        timeout=10,
    )
    create_resp.raise_for_status()

    token_resp = requests.post(
        f"{MAILTM_BASE}/token",
        json={"address": address, "password": password},
        timeout=10,
    )
    token_resp.raise_for_status()
    token = token_resp.json().get("token")

    return {"provider": "mailtm", "address": address, "password": password, "token": token}


def _mailtm_get_messages(token):
    resp = requests.get(
        f"{MAILTM_BASE}/messages",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("hydra:member", [])


def _mailtm_get_message_detail(token, message_id):
    resp = requests.get(
        f"{MAILTM_BASE}/messages/{message_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def _onesecmail_create_account():
    resp = requests.get(
        "https://www.1secmail.com/api/v1/?action=genRandomMailbox&count=1",
        timeout=10,
    )
    resp.raise_for_status()
    address = resp.json()[0]
    login, domain = address.split("@")
    return {"provider": "1secmail", "address": address, "login": login, "domain": domain}


def _onesecmail_get_messages(login, domain):
    resp = requests.get(
        "https://www.1secmail.com/api/v1/",
        params={"action": "getMessages", "login": login, "domain": domain},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def _onesecmail_get_message_detail(login, domain, message_id):
    resp = requests.get(
        "https://www.1secmail.com/api/v1/",
        params={"action": "readMessage", "login": login, "domain": domain, "id": message_id},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def create_temp_email():
    """يحاول mail.tm أولاً، وإن فشل يتحول تلقائيًا إلى 1secmail."""
    try:
        return _mailtm_create_account()
    except Exception as e:
        print(f"[mail.tm] فشل الإنشاء، التحويل إلى 1secmail: {e}")
        return _onesecmail_create_account()


def get_inbox(mailbox):
    if mailbox["provider"] == "mailtm":
        return _mailtm_get_messages(mailbox["token"])
    return _onesecmail_get_messages(mailbox["login"], mailbox["domain"])


def get_message_detail(mailbox, message_id):
    if mailbox["provider"] == "mailtm":
        return _mailtm_get_message_detail(mailbox["token"], message_id)
    return _onesecmail_get_message_detail(mailbox["login"], mailbox["domain"], message_id)


def generate_random_login(length: int = 10):
    import random
    import string
    chars = string.ascii_lowercase + string.digits
    return "".join(random.choice(chars) for _ in range(length))


def extract_verification_code(subject: str, body: str):
    """يحاول استخراج رمز تحقق (4-8 أرقام) من الموضوع أو محتوى الرسالة."""
    import re
    for text in (subject or "", body or ""):
        match = re.search(r"\b(\d{4,8})\b", text)
        if match:
            return match.group(1)
    return None


def clean_message_body(body: str, max_len: int = 600):
    """ينظف نص الرسالة من الروابط الطويلة المزعجة (صور/تتبع) ويقصّره."""
    import re
    if not body:
        return ""
    # إزالة أي رابط طويل (أكثر من 40 حرف) أينما ظهر داخل النص، وليس فقط في بداية السطر
    text = re.sub(r"https?://\S{40,}", "", body)
    # تنظيف الأسطر الفارغة الناتجة عن الحذف
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    text = "\n".join(lines).strip()
    if not text:
        text = body.strip()
    if len(text) > max_len:
        text = text[:max_len].rstrip() + "…"
    return text


def extract_main_link(body: str):
    """يستخرج أول رابط تحقق/تأكيد فعلي (وليس رابط صورة) من الرسالة."""
    import re
    if not body:
        return None
    candidates = re.findall(r"https?://\S+", body)
    for link in candidates:
        lower = link.lower()
        if any(skip in lower for skip in [".png", ".jpg", ".jpeg", ".gif", ".svg", "cdn"]):
            continue
        return link.rstrip(").,")
    return None


# ================= قواعد البيانات المؤقتة =================
user_emails = {} # لتخزين البريد النشط لكل مستخدم
user_last_action = {} # للحد من الطلبات (Rate Limiting)
user_seen_messages = {} # لتتبع الرسائل التي تم عرضها مسبقًا لكل مستخدم (تفادي التكرار)
admin_waiting_broadcast = {"active": False} # حالة انتظار رسالة الإذاعة من الأدمن

# ================= دوال مساعدة =================

# 1. التحقق من الاشتراك الإجباري
def check_subscription(user_id):
    if user_id == ADMIN_ID: # تجاوز الأدمن من الفحص
        return True
    try:
        member = bot.get_chat_member(CHANNEL_USERNAME, user_id)
        status = member.status
        if status in ['member', 'administrator', 'creator']:
            return True
        print(f"[check_subscription] المستخدم {user_id} حالته '{status}' (غير مشترك)")
        return False
    except Exception as e:
        # نطبع الخطأ الحقيقي في اللوجز بدل إخفائه بصمت (كان السبب الرئيسي لصعوبة تشخيص المشكلة)
        print(f"[check_subscription] فشل التحقق للمستخدم {user_id}: {type(e).__name__}: {e}")
        # في حال فشل الفحص لسبب تقني (وليس لأن المستخدم فعلاً غير مشترك)،
        # نسمح له مؤقتًا بالدخول بدل حجبه بالخطأ - أفضل من تعطيل البوت بالكامل لكل المستخدمين
        return True

# 2. نظام الحد من الطلبات (Rate Limit)
def is_rate_limited(user_id):
    current_time = time.time()
    if user_id in user_last_action:
        # السماح بطلب واحد كل 3 ثوانٍ لحماية السيرفر
        if current_time - user_last_action[user_id] < 3:
            return True
    user_last_action[user_id] = current_time
    return False

# ================= أوامر البوت =================

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    track_user(user_id, message.from_user.username)
    log_request(user_id, "start")
    
    # فحص الاشتراك
    if not check_subscription(user_id):
        markup = InlineKeyboardMarkup()
        markup.add(colored_button("📢 انضم إلى قناة البوت", url=f"https://t.me/{CHANNEL_USERNAME[1:]}", style="primary"))
        markup.add(colored_button("✅ زر التحقق", callback_data="check_sub", style="success"))
        first_name = message.from_user.first_name or "صديقي"
        sub_text = (
            f"مرحبًا بك يا {first_name} في بوت GMAIL الذكي 👋\n\n"
            "طريقة الاستخدام:\n\n"
            "1️⃣ انضم إلى قناة البوت أولًا\n"
            "2️⃣ اضغط على زر التحقق\n"
            "3️⃣ أرسل أمر /start للبدء"
        )
        bot.send_message(user_id, sub_text, reply_markup=markup, parse_mode="Markdown")
        return

    markup = InlineKeyboardMarkup()
    markup.add(colored_button("📨 إنشاء بريد عشوائي", callback_data="generate_email", style="success"))
    markup.add(colored_button("👨‍💻 المطور", url="tg://user?id=6043858925", style="primary"))
    
    welcome_text = (
        "👋 **أهلًا بك في بوت GMAIL**\n\n"
        "1️⃣ إنشاء بريد عشوائي\n"
        "2️⃣ استخدمه للتسجيل بأي موقع\n"
        "3️⃣ فحص الوارد لاستقبال الرمز/الرابط\n\n"
        "🛡️ يحميك من السبام على بريدك الحقيقي"
    )
    bot.reply_to(message, welcome_text, reply_markup=markup, parse_mode="Markdown")

# ================= التعامل مع الأزرار =================

@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    user_id = call.from_user.id
    track_user(user_id, call.from_user.username)

    # نظام الحماية من الضغط المستمر
    if is_rate_limited(user_id):
        bot.answer_callback_query(call.id, "⏳ يرجى الانتظار قليلاً بين الطلبات!", show_alert=True)
        return

    if call.data == "check_sub":
        if check_subscription(user_id):
            bot.answer_callback_query(call.id, "✅ تم التحقق بنجاح! يمكنك الآن استخدام البوت.", show_alert=True)
            bot.delete_message(call.message.chat.id, call.message.message_id)
            send_welcome(call.message)
        else:
            bot.answer_callback_query(call.id, "❌ لم تشترك في القناة بعد!", show_alert=True)

    elif call.data == "generate_email":
        log_request(user_id, "generate_email")
        try:
            mailbox = create_temp_email()
        except Exception as e:
            print(f"[generate_email] فشل كل مزودي الخدمة: {e}")
            bot.answer_callback_query(
                call.id,
                "❌ تعذر إنشاء البريد حاليًا (كل الخدمات معطلة مؤقتًا)، حاول بعد قليل.",
                show_alert=True,
            )
            return

        user_emails[user_id] = mailbox  # حفظ بيانات البريد الكاملة (بما فيها التوكن إن وجد)
        user_seen_messages[user_id] = set()  # تصفير قائمة الرسائل المعروضة سابقًا مع كل بريد جديد
        email = mailbox["address"]

        markup = InlineKeyboardMarkup()
        markup.add(colored_button("🔄 فحص صندوق الوارد", callback_data="check_inbox", style="success"))
        markup.add(colored_button("🗑️ تغيير البريد", callback_data="generate_email", style="danger"))

        text = (
            "✅ **تم إنشاء بريدك بنجاح**\n"
            "━━━━━━━━━━━━━━\n"
            f"📧 `{email}`\n"
            "(اضغط على البريد لنسخه)\n\n"
            "1️⃣ استخدم هذا البريد للتسجيل في أي موقع\n"
            "2️⃣ اضغط \"فحص صندوق الوارد\" لاستقبال الرسائل ورموز التحقق"
        )

        # تعديل الرسالة السابقة بدلاً من إرسال رسالة جديدة لتجربة مستخدم أفضل
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=text, reply_markup=markup, parse_mode="Markdown")

    elif call.data == "check_inbox":
        log_request(user_id, "check_inbox")
        if user_id not in user_emails:
            bot.answer_callback_query(call.id, "⚠️ لم تقم بإنشاء بريد بعد!", show_alert=True)
            return
            
        mailbox = user_emails[user_id]
        email = mailbox["address"]

        # فحص الرسائل
        try:
            req = get_inbox(mailbox)
        except Exception as e:
            print(f"[check_inbox] getMessages error: {e}")
            bot.answer_callback_query(
                call.id, "❌ تعذر الاتصال بخدمة البريد، حاول لاحقًا.", show_alert=True
            )
            return

        if len(req) == 0:
            bot.answer_callback_query(call.id, "📭 صندوق الوارد فارغ. لم تصل أي رسائل بعد.", show_alert=True)
            return

        seen = user_seen_messages.setdefault(user_id, set())
        new_messages = [msg for msg in req if msg['id'] not in seen]

        if not new_messages:
            bot.answer_callback_query(
                call.id, "✅ لا توجد رسائل جديدة (كل الرسائل السابقة تم عرضها).", show_alert=True
            )
            return

        bot.answer_callback_query(call.id, "📬 توجد رسائل جديدة!", show_alert=False)
        for msg in new_messages:
            msg_id = msg['id']
            seen.add(msg_id)  # وضع علامة "تم عرضها" فورًا لتفادي التكرار
            # جلب محتوى الرسالة
            try:
                read_msg = get_message_detail(mailbox, msg_id)
            except Exception as e:
                print(f"[check_inbox] readMessage error: {e}")
                bot.send_message(user_id, f"⚠️ تعذر جلب محتوى رسالة.")
                continue

            sender = read_msg.get('from', 'غير معروف')
            if isinstance(sender, dict):  # mail.tm يعيد from كـ {address, name}
                sender = sender.get('address', 'غير معروف')
            subject = read_msg.get('subject', 'بدون عنوان')
            raw_body = read_msg.get('textBody') or read_msg.get('text') or ''
            if isinstance(raw_body, list):  # بعض الحقول قد تأتي كقائمة
                raw_body = "\n".join(raw_body)

            code = extract_verification_code(subject, raw_body)
            link = extract_main_link(raw_body)

            parts = [
                "✉️ **رسالة جديدة**",
                f"من: `{sender}`",
                f"الموضوع: {subject}",
                "",
            ]
            if code:
                parts.append(f"🔑 كود التحقق:  `{code}`")
            if link:
                parts.append(f"🔗 [رابط التحقق]({link})")

            # نعرض النص الكامل فقط إذا لم نجد كود أو رابط (حالة نادرة) لتفادي الحشو
            if not code and not link:
                clean_body = clean_message_body(raw_body, max_len=500)
                if clean_body:
                    parts.append(clean_body)

            msg_text = "\n".join(parts).strip()
            if len(msg_text) > 4000:
                msg_text = msg_text[:4000].rstrip() + "…"

            try:
                bot.send_message(user_id, msg_text, parse_mode="Markdown", disable_web_page_preview=True)
            except Exception as e:
                # قد يحتوي محتوى البريد على رموز (* _ ` إلخ) تكسر تنسيق Markdown
                print(f"[check_inbox] فشل الإرسال بتنسيق Markdown، إعادة المحاولة كنص عادي: {e}")
                bot.send_message(user_id, msg_text, disable_web_page_preview=True)

    elif call.data == "refresh_stats":
        if user_id != ADMIN_ID:
            bot.answer_callback_query(call.id, "🚫 هذا الزر للأدمن فقط.", show_alert=True)
            return
        try:
            bot.edit_message_text(
                format_stats_message(),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=stats_keyboard(),
                parse_mode="Markdown",
            )
            bot.answer_callback_query(call.id, "✅ تم التحديث")
        except Exception as e:
            # يحدث لو الإحصائيات نفسها لم تتغير منذ آخر تحديث (تيليجرام يرفض تعديل رسالة بنفس المحتوى)
            bot.answer_callback_query(call.id, "ℹ️ لا يوجد جديد منذ آخر تحديث.")


# ================= لوحة الأدمن: الإحصائيات =================

def format_stats_message():
    s = get_stats()
    now_str = datetime.utcnow().strftime("%H:%M:%S")
    action_names = {
        "start": "🚀 بدء تشغيل (/start)",
        "generate_email": "📨 إنشاء بريد",
        "check_inbox": "📬 فحص الوارد",
    }
    lines = [
        "📊 **لوحة إحصائيات بوت GMAIL**",
        "─────────────────────",
        f"🟢 **نشطون الآن:** {s['active_now']}",
        f"👥 **إجمالي المستخدمين:** {s['total_users']}",
        "",
        "**📈 المستخدمون النشطون**",
        f"  ▫️ اليوم: {s['active_today']}",
        f"  ▫️ آخر 7 أيام: {s['active_7d']}",
        f"  ▫️ آخر 30 يوم: {s['active_30d']}",
        "",
        "**📥 عدد الطلبات**",
        f"  ▫️ اليوم: {s['requests_today']}",
        f"  ▫️ آخر 7 أيام: {s['requests_7d']}",
        f"  ▫️ آخر 30 يوم: {s['requests_30d']}",
    ]
    if s["breakdown_today"]:
        lines.append("")
        lines.append("**🧾 تفاصيل طلبات اليوم**")
        for action, count in s["breakdown_today"]:
            lines.append(f"  {action_names.get(action, action)}: {count}")
    lines.append("")
    lines.append(f"🕐 آخر تحديث: {now_str} UTC")
    return "\n".join(lines)


def stats_keyboard():
    markup = InlineKeyboardMarkup()
    markup.add(colored_button("🔄 تحديث الإحصائيات", callback_data="refresh_stats", style="primary"))
    return markup


# ================= لوحة الأدمن: الإذاعة التدريجية =================

def run_broadcast(from_chat_id, from_message_id):
    """يرسل رسالة (أي نوع محتوى) لكل مستخدمي البوت تدريجيًا لتفادي حظر تيليجرام للبوت."""
    user_ids = get_all_user_ids()
    sent, failed = 0, 0
    for uid in user_ids:
        try:
            bot.copy_message(uid, from_chat_id, from_message_id)
            sent += 1
        except Exception:
            failed += 1
        time.sleep(0.07)  # تأخير بسيط بين كل رسالة لتفادي تجاوز حدود تيليجرام
    try:
        bot.send_message(
            ADMIN_ID,
            f"✅ **اكتملت الإذاعة**\n\nتم الإرسال بنجاح لـ {sent} مستخدم.\nفشل الإرسال لـ {failed} (على الأغلب حظروا البوت).",
            parse_mode="Markdown",
        )
    except Exception:
        pass


@bot.message_handler(commands=['checksub'])
def checksub_command(message):
    """أمر تشخيص فوري للأدمن: يفحص اشتراك مستخدم معيّن ويعرض السبب الحقيقي لأي فشل مباشرة في تيليجرام."""
    if message.from_user.id != ADMIN_ID:
        return

    parts = message.text.strip().split()
    target_id = parts[1] if len(parts) > 1 else str(message.from_user.id)

    report = [f"🔍 **تشخيص الاشتراك**\n\nالقناة المستخدمة: `{CHANNEL_USERNAME}`\nمعرف المستخدم المفحوص: `{target_id}`\n"]

    # 1) فحص هوية البوت نفسه ودوره
    try:
        bot_info = bot.get_me()
        report.append(f"🤖 البوت: @{bot_info.username} (ID: {bot_info.id})")
    except Exception as e:
        report.append(f"❌ فشل جلب معلومات البوت نفسه: {type(e).__name__}: {e}")

    # 2) فحص هل البوت فعلاً مشرف في القناة (وين المشكلة الحقيقية غالبًا)
    try:
        bot_member = bot.get_chat_member(CHANNEL_USERNAME, bot.get_me().id)
        report.append(f"👮 حالة البوت في القناة: `{bot_member.status}`")
        if bot_member.status not in ["administrator", "creator"]:
            report.append("⚠️ **هذا هو السبب على الأغلب:** البوت ليس مشرفًا فعليًا من منظور تيليجرام (تأكد إنه مضاف من قسم Administrators في إعدادات القناة، مو بس عضو عادي).")
    except Exception as e:
        report.append(f"❌ فشل فحص عضوية البوت بالقناة: {type(e).__name__}: {e}")
        report.append("⚠️ هذا الفشل بالذات يعني البوت غالبًا غير مضاف كمشرف على الإطلاق، أو اسم القناة خاطئ.")

    # 3) فحص المستخدم المطلوب تحديدًا
    try:
        member = bot.get_chat_member(CHANNEL_USERNAME, int(target_id))
        report.append(f"\n👤 حالة المستخدم `{target_id}`: `{member.status}`")
        if member.status in ['member', 'administrator', 'creator']:
            report.append("✅ هذا المستخدم مشترك فعليًا - يفترض يدخل البوت بدون مشاكل.")
        else:
            report.append(f"❌ هذا المستخدم فعليًا حالته '{member.status}' في القناة (غير مشترك أو غادر).")
    except Exception as e:
        report.append(f"\n❌ فشل فحص المستخدم `{target_id}`: {type(e).__name__}: {e}")

    bot.reply_to(message, "\n".join(report), parse_mode="Markdown")


@bot.message_handler(commands=['broadcast', 'اذاعة'])
def broadcast_command(message):
    if message.from_user.id != ADMIN_ID:
        return
    admin_waiting_broadcast["active"] = True
    bot.reply_to(
        message,
        "📢 **وضع الإذاعة مفعّل**\n\nأرسل الآن الرسالة (نص، صورة، فيديو...) التي تريد إذاعتها لكل مستخدمي البوت.",
        parse_mode="Markdown",
    )


# ================= معالج النصوص العام (إحصائيات + استقبال محتوى الإذاعة) =================

@bot.message_handler(func=lambda m: True, content_types=['text', 'photo', 'video', 'document', 'animation'])
def general_text_handler(message):
    user_id = message.from_user.id

    # --- استقبال محتوى الإذاعة إن كان الأدمن في وضع الانتظار ---
    if user_id == ADMIN_ID and admin_waiting_broadcast.get("active"):
        admin_waiting_broadcast["active"] = False
        bot.reply_to(message, "🚀 جاري إرسال الإذاعة لكل المستخدمين تدريجيًا...")
        # تشغيل الإذاعة في Thread منفصل حتى لا تتوقف استجابة البوت للمستخدمين الآخرين أثناء الإرسال
        threading.Thread(
            target=run_broadcast,
            kwargs={
                "from_chat_id": message.chat.id,
                "from_message_id": message.message_id,
            },
            daemon=True,
        ).start()
        return

    # --- أمر الإحصائيات (نصي، للأدمن فقط) ---
    if message.content_type == "text" and message.text.strip() in ["احصائيات", "إحصائيات"]:
        if user_id != ADMIN_ID:
            return  # يتجاهل الطلب تمامًا لغير الأدمن دون أي رد
        bot.reply_to(message, format_stats_message(), reply_markup=stats_keyboard(), parse_mode="Markdown")
        return

    # أي رسالة نصية أخرى من مستخدم عادي: لا شيء (تفادي إزعاج المستخدم بردود غير ضرورية)


# ================= تشغيل البوت والخادم =================
if __name__ == "__main__":
    # استدعاء السيرفر الوهمي لاستقبال النقرات
    keep_alive()

    print("Bot is running...")

    # ملاحظة مهمة بخصوص "حفظ طلبات المستخدمين وقت التعطل":
    # تيليجرام نفسه يحتفظ بأي رسالة يرسلها المستخدم في طابور انتظار (حتى 24 ساعة تقريبًا)
    # طالما ما فيه Webhook مفعّل، وطالما البوت ما يطلب تخطي الرسائل القديمة (skip_pending).
    # بمجرد ما يرجع البوت يتصل بالسيرفر بعد أي انقطاع (صيانة/سقوط الاستضافة)، يستقبل
    # كل الرسائل المتراكمة تلقائيًا ويرد عليها بالترتيب - بدون أي كود إضافي مطلوب لهذا الجزء.
    #
    # الإضافة هنا: حلقة إعادة تشغيل تلقائية، بحيث لو صار خطأ غير متوقع أوقف الاتصال بتيليجرام
    # (مشكلة شبكة مؤقتة، إلخ)، البوت يعيد المحاولة من تلقاء نفسه بدل ما يتوقف نهائيًا.
    while True:
        try:
            bot.infinity_polling(timeout=10, long_polling_timeout=5, skip_pending=False)
        except Exception as e:
            print(f"[main] توقف البوت بشكل غير متوقع، إعادة المحاولة خلال 5 ثوانٍ: {e}")
            time.sleep(5)















