import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, BotCommand, BotCommandScopeChat
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

# نفتح بورت الخادم الوهمي فورًا وأول شيء، قبل أي محاولة اتصال بقاعدة البيانات.
# هذا يضمن أن ريندر يكتشف الخدمة "تعمل" حتى لو تعطل الاتصال بـ Turso أو تأخر،
# بدل ما يظل الديبلوي عالق للأبد وهو يدور على بورت مفتوح.
keep_alive()

# ================= قاعدة بيانات المستخدمين والإحصائيات (Turso / libSQL) =================
# بدل SQLite المحلية (كانت تنمسح كل مرة يعيد ريندر تشغيل الخدمة على الخطة المجانية)،
# نحفظ كل شيء على قاعدة بيانات Turso السحابية الدائمة.
import libsql_client
from datetime import datetime, timedelta

def _normalize_turso_url(url):
    """
    يحول رابط libsql:// إلى https:// عشان الاتصال يصير عبر HTTP عادي
    بدل WebSocket. مكتبة libsql_client (archived/متوقفة عن التطوير) فيها
    مشاكل توافق معروفة بالـ WebSocket Handshake مع بايثون الحديثة —
    اتصال HTTP أكثر استقرارًا ومضمون الدعم.
    """
    if url and url.startswith("libsql://"):
        return "https://" + url[len("libsql://"):]
    return url


TURSO_DATABASE_URL = _normalize_turso_url(os.getenv("TURSO_DATABASE_URL"))
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

db_client = libsql_client.create_client_sync(
    url=TURSO_DATABASE_URL,
    auth_token=TURSO_AUTH_TOKEN,
)
# عميل libsql_client مبني داخليًا على asyncio، فنقفل الوصول له عشان يكون آمن
# حتى لو استخدمته أكثر من Thread بنفس الوقت (زي Thread الإذاعة).
db_lock = threading.Lock()


def db_execute(sql, params=None):
    """
    تنفيذ استعلام واحد على Turso بأمان من عدة Threads.
    ننتظر القفل لمدة محدودة فقط (8 ثوانٍ) بدل الانتظار للأبد — لو تعلّق طلب واحد
    (مثلاً بسبب بطء بالشبكة)، باقي أوامر البوت (زي /start) ما تتوقف وتسكت،
    بل تفشل بسرعة وتدخل بمسار except الموجود أصلاً عند كل استدعاء.
    """
    acquired = db_lock.acquire(timeout=8)
    if not acquired:
        raise TimeoutError("تعذر الحصول على قفل قاعدة البيانات خلال 8 ثوانٍ (طلب آخر متعلّق)")
    try:
        return db_client.execute(sql, params or [])
    finally:
        db_lock.release()


def init_db():
    db_execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_seen TEXT,
            last_active TEXT
        )
    """)
    db_execute("""
        CREATE TABLE IF NOT EXISTS requests_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT,
            created_at TEXT
        )
    """)
    # بريد المستخدم النشط، محفوظ بشكل دائم حتى يبقى شغالًا بعد إعادة تشغيل السيرفر
    db_execute("""
        CREATE TABLE IF NOT EXISTS active_mailboxes (
            user_id INTEGER PRIMARY KEY,
            provider TEXT,
            address TEXT,
            password TEXT,
            token TEXT,
            login TEXT,
            domain TEXT,
            created_at TEXT
        )
    """)
    # الرسائل التي تم عرضها مسبقًا، محفوظة بشكل دائم لتفادي إعادة إرسالها بعد إعادة التشغيل
    db_execute("""
        CREATE TABLE IF NOT EXISTS seen_messages (
            user_id INTEGER,
            message_id TEXT,
            PRIMARY KEY (user_id, message_id)
        )
    """)


def track_user(user_id, username=None):
    """يسجل المستخدم إذا كان جديدًا، ويحدّث آخر نشاط له.
    ملاحظة: محاط بـ try/except عمدًا — أي تعطل بقاعدة البيانات لا يجوز أن يمنع البوت من الرد على المستخدم."""
    try:
        now = datetime.utcnow().isoformat()
        exists = db_execute("SELECT user_id FROM users WHERE user_id = ?", [user_id])
        if exists.rows:
            db_execute("UPDATE users SET last_active = ?, username = ? WHERE user_id = ?", [now, username, user_id])
        else:
            db_execute(
                "INSERT INTO users (user_id, username, first_seen, last_active) VALUES (?, ?, ?, ?)",
                [user_id, username, now, now],
            )
    except Exception as e:
        print(f"[track_user] تعذر الكتابة بقاعدة البيانات: {e}")


def log_request(user_id, action):
    """يسجل حدث/طلب معين لأغراض الإحصائيات (نفس ملاحظة track_user أعلاه)."""
    try:
        now = datetime.utcnow().isoformat()
        db_execute(
            "INSERT INTO requests_log (user_id, action, created_at) VALUES (?, ?, ?)",
            [user_id, action, now],
        )
    except Exception as e:
        print(f"[log_request] تعذر الكتابة بقاعدة البيانات: {e}")


def get_stats():
    """يحسب إحصائيات دقيقة: مستخدمين نشطين وطلبات، الآن/اليوم/7 أيام/30 يوم."""
    now = datetime.utcnow()
    today_start = now.strftime("%Y-%m-%d")
    d5min = (now - timedelta(minutes=5)).isoformat()
    d7 = (now - timedelta(days=7)).isoformat()
    d30 = (now - timedelta(days=30)).isoformat()

    total_users = db_execute("SELECT COUNT(*) FROM users").rows[0][0]
    active_now = db_execute("SELECT COUNT(*) FROM users WHERE last_active >= ?", [d5min]).rows[0][0]
    active_today = db_execute("SELECT COUNT(*) FROM users WHERE last_active LIKE ?", [f"{today_start}%"]).rows[0][0]
    active_7d = db_execute("SELECT COUNT(*) FROM users WHERE last_active >= ?", [d7]).rows[0][0]
    active_30d = db_execute("SELECT COUNT(*) FROM users WHERE last_active >= ?", [d30]).rows[0][0]

    requests_today = db_execute("SELECT COUNT(*) FROM requests_log WHERE created_at LIKE ?", [f"{today_start}%"]).rows[0][0]
    requests_7d = db_execute("SELECT COUNT(*) FROM requests_log WHERE created_at >= ?", [d7]).rows[0][0]
    requests_30d = db_execute("SELECT COUNT(*) FROM requests_log WHERE created_at >= ?", [d30]).rows[0][0]

    breakdown_rows = db_execute("""
        SELECT action, COUNT(*) FROM requests_log
        WHERE created_at LIKE ?
        GROUP BY action ORDER BY COUNT(*) DESC
    """, [f"{today_start}%"]).rows
    breakdown_today = [(row[0], row[1]) for row in breakdown_rows]

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
    rows = db_execute("SELECT user_id FROM users").rows
    return [row[0] for row in rows]


# ================= تخزين دائم للبريد النشط + الرسائل المقروءة (Turso) =================

def save_active_mailbox(user_id, mailbox):
    """يحفظ بريد المستخدم النشط بشكل دائم حتى لو أعاد السيرفر التشغيل."""
    try:
        db_execute("DELETE FROM active_mailboxes WHERE user_id = ?", [user_id])
        db_execute(
            "INSERT INTO active_mailboxes (user_id, provider, address, password, token, login, domain, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                user_id,
                mailbox.get("provider"),
                mailbox.get("address"),
                mailbox.get("password"),
                mailbox.get("token"),
                mailbox.get("login"),
                mailbox.get("domain"),
                datetime.utcnow().isoformat(),
            ],
        )
    except Exception as e:
        print(f"[save_active_mailbox] تعذر الحفظ: {e}")


def load_active_mailbox(user_id):
    """يحمّل بريد المستخدم النشط من Turso (يُستخدم بعد إعادة تشغيل السيرفر مثلًا)."""
    try:
        rows = db_execute(
            "SELECT provider, address, password, token, login, domain FROM active_mailboxes WHERE user_id = ?",
            [user_id],
        ).rows
        if not rows:
            return None
        r = rows[0]
        return {"provider": r[0], "address": r[1], "password": r[2], "token": r[3], "login": r[4], "domain": r[5]}
    except Exception as e:
        print(f"[load_active_mailbox] تعذر التحميل: {e}")
        return None


def get_all_active_mailboxes():
    """يرجع كل البريد النشط لكل المستخدمين، تُستخدم في مراقبة الوارد التلقائية بالخلفية."""
    try:
        rows = db_execute(
            "SELECT user_id, provider, address, password, token, login, domain FROM active_mailboxes"
        ).rows
        return [
            (r[0], {"provider": r[1], "address": r[2], "password": r[3], "token": r[4], "login": r[5], "domain": r[6]})
            for r in rows
        ]
    except Exception as e:
        print(f"[get_all_active_mailboxes] تعذر الجلب: {e}")
        return []


def get_mailbox(user_id):
    """يرجع بريد المستخدم النشط من الذاكرة، أو يحمّله من Turso إذا لم يكن محمّلًا بعد."""
    if user_id in user_emails:
        return user_emails[user_id]
    mailbox = load_active_mailbox(user_id)
    if mailbox:
        user_emails[user_id] = mailbox
    return mailbox


def get_seen_ids(user_id):
    try:
        rows = db_execute("SELECT message_id FROM seen_messages WHERE user_id = ?", [user_id]).rows
        return set(row[0] for row in rows)
    except Exception as e:
        print(f"[get_seen_ids] تعذر الجلب: {e}")
        return set()


def get_seen_set(user_id):
    """نسخة مخزّنة بالذاكرة من الرسائل المقروءة، تُحمّل من Turso أول مرة فقط."""
    if user_id not in user_seen_messages:
        user_seen_messages[user_id] = get_seen_ids(user_id)
    return user_seen_messages[user_id]


def mark_message_seen(user_id, message_id):
    get_seen_set(user_id).add(message_id)
    try:
        db_execute(
            "INSERT OR IGNORE INTO seen_messages (user_id, message_id) VALUES (?, ?)",
            [user_id, str(message_id)],
        )
    except Exception as e:
        print(f"[mark_message_seen] تعذر الحفظ: {e}")


def clear_seen(user_id):
    """يصفّر سجل الرسائل المقروءة عند إنشاء بريد جديد (البريد الجديد = صندوق فارغ)."""
    user_seen_messages[user_id] = set()
    try:
        db_execute("DELETE FROM seen_messages WHERE user_id = ?", [user_id])
    except Exception as e:
        print(f"[clear_seen] تعذر الحذف: {e}")


def get_user_personal_stats(user_id):
    """يجمع إحصائيات شخصية لمستخدم واحد: تاريخ انضمامه، عدد مرات إنشاء البريد، وعدد مرات فحص الوارد."""
    user_row = db_execute("SELECT first_seen FROM users WHERE user_id = ?", [user_id]).rows
    first_seen = user_row[0][0] if user_row else None

    emails_created = db_execute(
        "SELECT COUNT(*) FROM requests_log WHERE user_id = ? AND action = 'generate_email'", [user_id]
    ).rows[0][0]
    inbox_checks = db_execute(
        "SELECT COUNT(*) FROM requests_log WHERE user_id = ? AND action = 'check_inbox'", [user_id]
    ).rows[0][0]

    days_since_join = None
    if first_seen:
        try:
            joined_dt = datetime.fromisoformat(first_seen)
            days_since_join = (datetime.utcnow() - joined_dt).days
        except Exception:
            days_since_join = None

    return {
        "first_seen": first_seen,
        "days_since_join": days_since_join,
        "emails_created": emails_created,
        "inbox_checks": inbox_checks,
    }


def _init_db_safe():
    """ينشئ جداول Turso في Thread منفصل حتى لو تأخر الاتصال أو فشل، بدون ما يعلّق تشغيل البوت."""
    try:
        init_db()
        print("[Turso] تم الاتصال بقاعدة البيانات وإنشاء الجداول بنجاح.")
    except Exception as e:
        print(f"[Turso] تعذر الاتصال بقاعدة البيانات: {e}")


threading.Thread(target=_init_db_safe, daemon=True).start()

# ملاحظة: خادم Flask الخاص بإبقاء البوت حيًا تم تشغيله بالأعلى فورًا (keep_alive()).

# ================= طبقة خدمة البريد المؤقت (mail.tm أساسي + 1secmail احتياطي) =================
# 1secmail معروفة بعدم الاستقرار (403 / انقطاع متكرر) خصوصًا من IP استضافات مجانية مشتركة.
# لذلك نعتمد mail.tm كخدمة رئيسية أكثر استقرارًا، ونحتفظ بـ1secmail كخطة بديلة تلقائية.

MAILTM_BASE = "https://api.mail.tm"


def mailtm_list_domains():
    """يرجع قائمة الدومينات المتاحة حاليًا على mail.tm (تُستخدم لميزة اختيار الدومين)."""
    resp = requests.get(f"{MAILTM_BASE}/domains", timeout=10)
    resp.raise_for_status()
    domains = resp.json().get("hydra:member", [])
    return [d["domain"] for d in domains]


def _mailtm_create_account(domain=None):
    """ينشئ حساب mail.tm جديد ويعيد (email, password, token). يقبل دومين محدد اختياريًا."""
    if not domain:
        domains = mailtm_list_domains()
        if not domains:
            raise RuntimeError("لا توجد دومينات متاحة على mail.tm حاليًا")
        domain = domains[0]

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


def create_temp_email(domain=None):
    """يحاول mail.tm أولاً (بالدومين المحدد إن وُجد)، وإن فشل يتحول تلقائيًا إلى 1secmail."""
    try:
        return _mailtm_create_account(domain)
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


def build_incoming_message_text(read_msg):
    """يبني نص رسالة جديدة منسّق (كود/رابط/نص). يُستخدم في الفحص اليدوي والمراقبة التلقائية معًا."""
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
    return msg_text


def send_message_safe(chat_id, text):
    """يرسل برسالة Markdown، وإن فشل (رموز تكسر التنسيق) يعيد المحاولة كنص عادي."""
    try:
        bot.send_message(chat_id, text, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e:
        print(f"[send_message_safe] فشل الإرسال بتنسيق Markdown، إعادة المحاولة كنص عادي: {e}")
        bot.send_message(chat_id, text, disable_web_page_preview=True)


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
        status = bot.get_chat_member(CHANNEL_USERNAME, user_id).status
        if status in ['member', 'administrator', 'creator']:
            return True
        return False
    except:
        # في حال لم يكن البوت مشرفاً في القناة
        return False

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

# ================= لوحة أزرار ملونة (بصيغة JSON خام لدعم ميزة ألوان الأزرار) =================
import json


class RawMarkup:
    """
    غلاف بسيط لإرسال لوحة أزرار بصيغة JSON خام تدعم حقل الألوان
    (style: primary/success/danger) الذي أضافته تيليجرام حديثًا، والذي لا
    توفره نسخة pyTelegramBotAPI الحالية كمعامل بايثون رسمي بعد.
    مكتبة pyTelegramBotAPI تستدعي دائمًا markup.to_json() قبل الإرسال،
    لذلك نوفر هذه الدالة بأنفسنا بدل الاعتماد على InlineKeyboardMarkup.
    """
    def __init__(self, keyboard_dict):
        self._keyboard_dict = keyboard_dict

    def to_json(self):
        return json.dumps(self._keyboard_dict)


def build_subscribe_keyboard():
    keyboard = {
        "inline_keyboard": [
            [{"text": "📢 انضم إلى قناة البوت", "url": f"https://t.me/{CHANNEL_USERNAME[1:]}", "style": "primary"}],
            [{"text": "✅ زر التحقق", "callback_data": "check_sub", "style": "success"}],
        ]
    }
    return RawMarkup(keyboard)


def build_welcome_keyboard():
    keyboard = {
        "inline_keyboard": [
            [{"text": "📨 إنشاء بريد عشوائي", "callback_data": "generate_email", "style": "primary"}],
            [{"text": "👨‍💻 المطور", "url": "tg://user?id=6043858925", "style": "primary"}],
        ]
    }
    return RawMarkup(keyboard)


def build_domain_keyboard():
    """لوحة اختيار الدومين (ميزة الدومين المخصص) + خيار عشوائي."""
    try:
        domains = mailtm_list_domains()[:6]
    except Exception as e:
        print(f"[build_domain_keyboard] تعذر جلب الدومينات: {e}")
        domains = []

    rows = []
    pair = []
    for d in domains:
        pair.append({"text": f"@{d}", "callback_data": f"pick_domain:{d}", "style": "primary"})
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([{"text": "🎲 عشوائي", "callback_data": "pick_domain:random", "style": "success"}])

    return RawMarkup({"inline_keyboard": rows})


def show_main_menu(chat_id, user_id, first_name=None):
    """
    يعرض القائمة الرئيسية للبوت مباشرة لصاحب user_id/chat_id المحددين.
    دالة مستقلة (بدل الاعتماد على كائن message) حتى تعمل بشكل صحيح
    سواء استُدعيت من أمر /start مباشرة أو من زر "التحقق" بعد الاشتراك.
    """
    markup = build_welcome_keyboard()
    welcome_text = (
        "👋 **أهلًا بك في بوت GMAIL**\n\n"
        "1️⃣ إنشاء بريد عشوائي\n"
        "2️⃣ استخدمه للتسجيل بأي موقع\n"
        "3️⃣ فحص الوارد لاستقبال الرمز/الرابط\n\n"
        "🛡️ يحميك من السبام على بريدك الحقيقي"
    )
    bot.send_message(chat_id, welcome_text, reply_markup=markup, parse_mode="Markdown")


@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    track_user(user_id, message.from_user.username)
    log_request(user_id, "start")

    # فحص الاشتراك
    if not check_subscription(user_id):
        markup = build_subscribe_keyboard()
        first_name = message.from_user.first_name or "صديقي"
        sub_text = (
            f"مرحبًا بك يا {first_name} في بوت GMAIL الذكي 👋\n\n"
            "طريقة الاستخدام:\n\n"
            "1️⃣ انضم إلى قناة البوت أولًا\n"
            "2️⃣ اضغط على زر التحقق"
        )
        bot.send_message(user_id, sub_text, reply_markup=markup, parse_mode="Markdown")
        return

    show_main_menu(message.chat.id, user_id, message.from_user.first_name)

# ================= التعامل مع الأزرار =================

@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    # ملاحظة مهمة: نستخدم call.from_user للحصول على هوية الشخص الذي ضغط الزر،
    # وليس call.message.from_user الذي يرجّع هوية البوت نفسه (لأن call.message
    # هو الرسالة التي أرسلها البوت) — هذا كان سبب خلل سابق بعد التحقق من الاشتراك.
    user_id = call.from_user.id
    track_user(user_id, call.from_user.username)

    # نظام الحماية من الضغط المستمر
    if is_rate_limited(user_id):
        bot.answer_callback_query(call.id, "⏳ يرجى الانتظار قليلاً بين الطلبات!", show_alert=True)
        return

    if call.data == "check_sub":
        if check_subscription(user_id):
            bot.answer_callback_query(call.id, "✅ تم التحقق بنجاح! يمكنك الآن استخدام البوت.", show_alert=True)
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception:
                pass
            # تظهر القائمة الرئيسية تلقائيًا مباشرة دون الحاجة لإرسال /start مجددًا
            show_main_menu(call.message.chat.id, user_id, call.from_user.first_name)
        else:
            bot.answer_callback_query(call.id, "❌ لم تشترك في القناة بعد!", show_alert=True)

    elif call.data == "generate_email":
        # بدل الإنشاء المباشر، نعرض اختيار الدومين أولًا (ميزة الدومين المخصص)
        try:
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text="📬 اختر الدومين الذي تريد إنشاء بريدك عليه:",
                reply_markup=build_domain_keyboard(),
            )
        except Exception as e:
            print(f"[generate_email] تعذر عرض لوحة الدومين: {e}")
            bot.send_message(call.message.chat.id, "📬 اختر الدومين الذي تريد إنشاء بريدك عليه:", reply_markup=build_domain_keyboard())

    elif call.data.startswith("pick_domain:"):
        log_request(user_id, "generate_email")
        chosen = call.data.split(":", 1)[1]
        domain = None if chosen == "random" else chosen

        try:
            mailbox = create_temp_email(domain)
        except Exception as e:
            print(f"[pick_domain] فشل كل مزودي الخدمة: {e}")
            bot.answer_callback_query(
                call.id,
                "❌ تعذر إنشاء البريد حاليًا (كل الخدمات معطلة مؤقتًا)، حاول بعد قليل.",
                show_alert=True,
            )
            return

        user_emails[user_id] = mailbox  # تخزين سريع بالذاكرة
        save_active_mailbox(user_id, mailbox)  # حفظ دائم بـ Turso يبقى بعد إعادة تشغيل السيرفر
        clear_seen(user_id)  # بريد جديد = صندوق وارد فارغ
        email = mailbox["address"]

        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🔄 فحص صندوق الوارد", callback_data="check_inbox"))
        markup.add(InlineKeyboardButton("🗑️ تغيير البريد", callback_data="generate_email"))

        text = (
            "✅ **تم إنشاء بريدك بنجاح**\n"
            "━━━━━━━━━━━━━━\n"
            f"📧 `{email}`\n"
            "(اضغط على البريد لنسخه)\n\n"
            "1️⃣ استخدم هذا البريد للتسجيل في أي موقع\n"
            "2️⃣ اضغط \"فحص صندوق الوارد\" لاستقبال الرسائل ورموز التحقق\n"
            "🔔 أو فقط انتظر — سيصلك أي كود جديد تلقائيًا بدون أي ضغط"
        )

        # تعديل الرسالة السابقة بدلاً من إرسال رسالة جديدة لتجربة مستخدم أفضل
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=text, reply_markup=markup, parse_mode="Markdown")

    elif call.data == "check_inbox":
        log_request(user_id, "check_inbox")
        mailbox = get_mailbox(user_id)
        if not mailbox:
            bot.answer_callback_query(call.id, "⚠️ لم تقم بإنشاء بريد بعد!", show_alert=True)
            return

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

        seen = get_seen_set(user_id)
        new_messages = [msg for msg in req if msg['id'] not in seen]

        if not new_messages:
            bot.answer_callback_query(
                call.id, "✅ لا توجد رسائل جديدة (كل الرسائل السابقة تم عرضها).", show_alert=True
            )
            return

        bot.answer_callback_query(call.id, "📬 توجد رسائل جديدة!", show_alert=False)
        for msg in new_messages:
            mark_message_seen(user_id, msg['id'])  # وضع علامة "تم عرضها" فورًا (بالذاكرة + Turso) لتفادي التكرار
            try:
                read_msg = get_message_detail(mailbox, msg['id'])
            except Exception as e:
                print(f"[check_inbox] readMessage error: {e}")
                bot.send_message(user_id, "⚠️ تعذر جلب محتوى رسالة.")
                continue

            send_message_safe(user_id, build_incoming_message_text(read_msg))

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
    markup.add(InlineKeyboardButton("🔄 تحديث الإحصائيات", callback_data="refresh_stats"))
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


@bot.message_handler(commands=['stats'])
def stats_command(message):
    """أمر /stats بقائمة أوامر تيليجرام (للأدمن فقط)."""
    user_id = message.from_user.id
    track_user(user_id, message.from_user.username)
    if user_id != ADMIN_ID:
        return  # يتجاهل الطلب تمامًا لغير الأدمن دون أي رد
    try:
        bot.reply_to(message, format_stats_message(), reply_markup=stats_keyboard(), parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"⚠️ تعذر جلب الإحصائيات حاليًا (مشكلة اتصال بقاعدة البيانات).\n`{e}`", parse_mode="Markdown")


def format_user_stats_message(user_id, first_name):
    """يبني رسالة الإحصائيات الشخصية الموجهة للمستخدم نفسه."""
    s = get_user_personal_stats(user_id)

    if s["days_since_join"] is not None:
        days_text = f"{s['days_since_join']} يوم" if s["days_since_join"] > 0 else "اليوم"
    else:
        days_text = "غير معروف"

    lines = [
        f"📊 **إحصائياتك يا {first_name}**",
        "━━━━━━━━━━━━━━",
        f"🆔 معرّفك (ID): `{user_id}`",
        f"📅 عضو منذ: {days_text}",
        "",
        f"📨 مرات إنشاء بريد: **{s['emails_created']}**",
        f"📬 مرات فحص الوارد: **{s['inbox_checks']}**",
    ]
    return "\n".join(lines)


@bot.message_handler(commands=['mystats', 'احصائياتي'])
def my_stats_command(message):
    """أمر 'إحصائياتي 📊' المتاح لكل المستخدمين — يعرض إحصائياته الشخصية فقط."""
    user_id = message.from_user.id
    first_name = message.from_user.first_name or "صديقي"
    track_user(user_id, message.from_user.username)
    try:
        bot.reply_to(message, format_user_stats_message(user_id, first_name), parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, "⚠️ تعذر جلب إحصائياتك حاليًا، حاول لاحقًا.")
        print(f"[my_stats_command] خطأ: {e}")


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
        try:
            bot.reply_to(message, format_stats_message(), reply_markup=stats_keyboard(), parse_mode="Markdown")
        except Exception as e:
            bot.reply_to(message, f"⚠️ تعذر جلب الإحصائيات حاليًا (مشكلة اتصال بقاعدة البيانات).\n`{e}`", parse_mode="Markdown")
        return

    # أي رسالة نصية أخرى من مستخدم عادي: لا شيء (تفادي إزعاج المستخدم بردود غير ضرورية)


# ================= المراقبة التلقائية للوارد (إشعار فوري بدون ضغط المستخدم) =================
AUTO_CHECK_INTERVAL = 25  # ثواني بين كل دورة فحص تلقائي لكل البريد النشط


def auto_notify_loop():
    """يفحص كل البريد النشط (من Turso) بالخلفية دوريًا، ويرسل أي رسالة جديدة تلقائيًا."""
    while True:
        time.sleep(AUTO_CHECK_INTERVAL)
        try:
            mailboxes = get_all_active_mailboxes()
        except Exception as e:
            print(f"[auto_notify_loop] تعذر جلب قائمة البريد النشط: {e}")
            continue

        for user_id, mailbox in mailboxes:
            try:
                messages = get_inbox(mailbox)
            except Exception as e:
                print(f"[auto_notify_loop] تعذر فحص بريد المستخدم {user_id}: {e}")
                continue

            if not messages:
                continue

            seen = get_seen_set(user_id)
            new_messages = [m for m in messages if m['id'] not in seen]

            for msg in new_messages:
                mark_message_seen(user_id, msg['id'])
                try:
                    read_msg = get_message_detail(mailbox, msg['id'])
                except Exception as e:
                    print(f"[auto_notify_loop] تعذر جلب محتوى رسالة للمستخدم {user_id}: {e}")
                    continue
                send_message_safe(user_id, build_incoming_message_text(read_msg))

            time.sleep(0.3)  # تأخير بسيط بين كل مستخدم وآخر لتفادي ضغط زائد على APIs الخارجية


# ================= تشغيل البوت والخادم =================
def setup_bot_commands():
    """يسجل قائمة الأوامر التي تظهر لما يكتب المستخدم / بتيليجرام."""
    public_commands = [
        BotCommand("start", "🚀 بدء استخدام البوت"),
        BotCommand("mystats", "📊 إحصائياتي"),
    ]
    admin_commands = public_commands + [
        BotCommand("stats", "📊 إحصائيات البوت (أدمن)"),
    ]
    try:
        bot.set_my_commands(public_commands)  # القائمة الافتراضية لكل المستخدمين
    except Exception as e:
        print(f"[setup_bot_commands] تعذر تسجيل القائمة العامة: {e}")

    try:
        # قائمة أوسع (فيها /stats) تظهر فقط للأدمن، بدون ما تنكشف لبقية المستخدمين
        bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(ADMIN_ID))
    except Exception as e:
        print(f"[setup_bot_commands] تعذر تخصيص قائمة أوامر الأدمن: {e}")


if __name__ == "__main__":
    # ملاحظة: keep_alive() تم استدعاؤه بالفعل بأعلى الملف فور تشغيله (قبل Turso)،
    # عشان ريندر يكتشف البورت المفتوح بسرعة ولا يعلّق الديبلوي.
    try:
        setup_bot_commands()
    except Exception as e:
        # أي عطل هنا لا يجوز أن يمنع البوت من بدء الاستماع للرسائل إطلاقًا
        print(f"[main] تعذر تسجيل قائمة الأوامر: {e}")

    # تشغيل مراقبة الوارد التلقائية بالخلفية (ميزة الإشعار الفوري)
    threading.Thread(target=auto_notify_loop, daemon=True).start()

    print("Bot is running...")
    # تشغيل البوت
    bot.infinity_polling(timeout=10, long_polling_timeout=5)







