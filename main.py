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
bot = telebot.TeleBot(BOT_TOKEN, threaded=True, num_threads=12)

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
    # سجل كل البريد الذي أنشأه المستخدم (ميزة "بريداتي المحفوظة")
    db_execute("""
        CREATE TABLE IF NOT EXISTS saved_mailboxes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            provider TEXT,
            address TEXT,
            password TEXT,
            token TEXT,
            login TEXT,
            domain TEXT,
            created_at TEXT
        )
    """)
    # إعدادات البوت القابلة للتحكم من لوحة الأدمن (مفتاح/قيمة)
    db_execute("CREATE TABLE IF NOT EXISTS bot_settings (k TEXT PRIMARY KEY, v TEXT)")
    # سجل الإذاعات + الرسائل المرسلة لكل مستخدم (لإمكانية حذف الإذاعة من عند الجميع)
    db_execute("""
        CREATE TABLE IF NOT EXISTS broadcasts (
            id INTEGER PRIMARY KEY,
            created_at TEXT,
            total INTEGER,
            sent INTEGER,
            failed INTEGER,
            status TEXT
        )
    """)
    db_execute("""
        CREATE TABLE IF NOT EXISTS broadcast_msgs (
            bid INTEGER,
            user_id INTEGER,
            message_id INTEGER
        )
    """)
    # سجل أخطاء البوت
    db_execute("""
        CREATE TABLE IF NOT EXISTS error_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            location TEXT,
            message TEXT
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


# ================= إعدادات البوت (قابلة للتعديل من لوحة الأدمن) + سجل الأخطاء =================
DEFAULT_SETTINGS = {
    "maintenance": "0",    # وضع الصيانة
    "force_sub": "1",      # الاشتراك الإجباري
    "auto_notify": "1",    # الإشعار التلقائي للوارد
    "auto_interval": "25", # ثواني بين كل دورة فحص تلقائي
    "rate_limit": "3",     # ثواني بين ضغطات الأزرار
    "max_saved": "20",     # أقصى عدد بريد محفوظ لكل مستخدم
    "welcome_text": "",    # رسالة ترحيب مخصصة (فارغ = الافتراضية)
}
_settings_cache = {}


def load_settings():
    """يحمّل الإعدادات من Turso للذاكرة (تُستدعى مرة عند التشغيل)."""
    try:
        for k, v in db_execute("SELECT k, v FROM bot_settings").rows:
            _settings_cache[k] = v
    except Exception as e:
        print(f"[load_settings] {e}")


def get_setting(key):
    """القراءة دائمًا من الذاكرة (سريعة ولا تعلّق أبدًا). القيمة الفارغة = الافتراضي."""
    v = _settings_cache.get(key)
    if v is None or v == "":
        return DEFAULT_SETTINGS.get(key, "")
    return v


def get_bool(key):
    return str(get_setting(key)) == "1"


def get_int(key):
    try:
        return int(get_setting(key))
    except Exception:
        return int(DEFAULT_SETTINGS.get(key, 0) or 0)


def set_setting(key, value):
    value = "" if value is None else str(value)
    _settings_cache[key] = value
    try:
        db_execute("DELETE FROM bot_settings WHERE k = ?", [key])
        if value != "":
            db_execute("INSERT INTO bot_settings (k, v) VALUES (?, ?)", [key, value])
    except Exception as e:
        print(f"[set_setting] {e}")


def log_error(where, err):
    """يسجل خطأ بسجل الأخطاء (يُعرض بلوحة الأدمن). يعمل بخيط منفصل حتى لا يعلّق شيئًا."""
    msg = f"{type(err).__name__}: {err}"[:500]
    print(f"[ERROR:{where}] {msg}")

    def _w():
        try:
            db_execute(
                "INSERT INTO error_log (created_at, location, message) VALUES (?, ?, ?)",
                [datetime.utcnow().isoformat(), where, msg],
            )
        except Exception:
            pass
    threading.Thread(target=_w, daemon=True).start()


class _ExceptionLogger:
    """يلتقط أي خطأ يحدث داخل معالجات تيليجرام ويسجله بدل أن يضيع."""
    def handle(self, exception):
        log_error("telebot", exception)
        return True


bot.exception_handler = _ExceptionLogger()


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


def count_saved_mailboxes(user_id):
    try:
        return db_execute("SELECT COUNT(*) FROM saved_mailboxes WHERE user_id = ?", [user_id]).rows[0][0]
    except Exception:
        return 0


def save_mailbox_history(user_id, mailbox):
    """يحفظ البريد (اختياريًا بضغطة المستخدم). يرجع: saved / exists / full / error."""
    try:
        exists = db_execute(
            "SELECT id FROM saved_mailboxes WHERE user_id = ? AND address = ?", [user_id, mailbox.get("address")]
        ).rows
        if exists:
            return "exists"
        if count_saved_mailboxes(user_id) >= get_int("max_saved"):
            return "full"
        db_execute(
            "INSERT INTO saved_mailboxes (user_id, provider, address, password, token, login, domain, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [user_id, mailbox.get("provider"), mailbox.get("address"), mailbox.get("password"),
             mailbox.get("token"), mailbox.get("login"), mailbox.get("domain"), datetime.utcnow().isoformat()],
        )
        return "saved"
    except Exception as e:
        log_error("save_mailbox_history", e)
        return "error"


def delete_saved_mailbox(user_id, row_id):
    try:
        db_execute("DELETE FROM saved_mailboxes WHERE user_id = ? AND id = ?", [user_id, row_id])
        return True
    except Exception as e:
        log_error("delete_saved_mailbox", e)
        return False


def list_saved_mailboxes(user_id, limit=50):
    try:
        rows = db_execute(
            "SELECT id, address FROM saved_mailboxes WHERE user_id = ? ORDER BY id DESC LIMIT ?", [user_id, limit]
        ).rows
        return [(r[0], r[1]) for r in rows]
    except Exception as e:
        print(f"[list_saved_mailboxes] {e}")
        return []


def get_saved_mailbox(user_id, row_id):
    try:
        rows = db_execute(
            "SELECT provider, address, password, token, login, domain FROM saved_mailboxes WHERE user_id = ? AND id = ?",
            [user_id, row_id],
        ).rows
        if not rows:
            return None
        r = rows[0]
        return {"provider": r[0], "address": r[1], "password": r[2], "token": r[3], "login": r[4], "domain": r[5]}
    except Exception as e:
        print(f"[get_saved_mailbox] {e}")
        return None


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
        load_settings()
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
    if user_id == ADMIN_ID or not get_bool("force_sub"): # تجاوز الأدمن + خيار إيقاف الاشتراك الإجباري
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
        if current_time - user_last_action[user_id] < get_int("rate_limit"):
            return True
    user_last_action[user_id] = current_time
    return False

# ================= أوامر البوت =================

# ================= لوحة أزرار ملونة (بصيغة JSON خام لدعم ميزة ألوان الأزرار) =================
import json


class RawMarkup(telebot.types.JsonSerializable):
    """
    غلاف بسيط لإرسال لوحة أزرار بصيغة JSON خام تدعم حقل الألوان
    (style: primary/success/danger) الذي أضافته تيليجرام حديثًا، والذي لا
    توفره نسخة pyTelegramBotAPI الحالية كمعامل بايثون رسمي بعد.
    مكتبة pyTelegramBotAPI تستدعي دائمًا markup.to_json() قبل الإرسال،
    لذلك نوفر هذه الدالة بأنفسنا بدل الاعتماد على InlineKeyboardMarkup.
    """
    def __init__(self, keyboard_dict):
        self._keyboard_dict = keyboard_dict

    def to_dict(self):
        return self._keyboard_dict

    def to_json(self):
        return json.dumps(self._keyboard_dict)


# سجل أزرار البوت: المفتاح -> (الاسم الظاهر بلوحة الأدمن، اللون الافتراضي). اللون يتغير من لوحة الأدمن.
BUTTON_KEYS = {
    "create_email": ("📨 إنشاء بريد عشوائي", "primary"),
    "my_emails": ("📚 بريداتي المحفوظة", "success"),
    "channel": ("📢 قناة البوت", "primary"),
    "join": ("📢 انضم إلى القناة", "primary"),
    "verify": ("✅ زر التحقق", "success"),
    "inbox": ("🔄 فحص الوارد", "primary"),
    "change": ("🗑️ تغيير البريد", "danger"),
    "save": ("💾 حفظ هذا البريد", "success"),
    "back": ("🔙 رجوع", "danger"),
    "domain": ("@ أزرار الدومينات", "primary"),
    "random": ("🎲 عشوائي", "success"),
    "use_mail": ("📧 زر البريد المحفوظ", "primary"),
    "del_mail": ("🗑 زر حذف البريد المحفوظ", "danger"),
}
STYLE_NAMES = {"primary": "🔵 أزرق", "success": "🟢 أخضر", "danger": "🔴 أحمر", "none": "⚪ بدون لون"}


def get_style(key):
    """لون الزر الحالي: المخصص من لوحة الأدمن، وإلا الافتراضي."""
    custom = _settings_cache.get("style:" + key)
    if custom in STYLE_NAMES:
        return custom
    return BUTTON_KEYS[key][1] if key in BUTTON_KEYS else "primary"


def fbtn(text, style, callback_data=None, url=None):
    """زر بلون ثابت (يُستخدم في لوحة الأدمن). style=None أو 'none' = بدون لون."""
    b = {"text": text}
    if style and style != "none":
        b["style"] = style
    if url:
        b["url"] = url
    else:
        b["callback_data"] = callback_data
    return b


def btn(key, text, callback_data=None, url=None):
    """زر ملوّن حسب إعدادات الأدمن. كل أزرار المستخدمين تمر من هنا."""
    return fbtn(text, get_style(key), callback_data=callback_data, url=url)


def colored(rows):
    return RawMarkup({"inline_keyboard": rows})


CHANNEL_URL = f"https://t.me/{CHANNEL_USERNAME[1:]}"


def build_subscribe_keyboard():
    return colored([
        [btn("join", "📢 انضم إلى قناة البوت", url=CHANNEL_URL)],
        [btn("verify", "✅ زر التحقق", callback_data="check_sub")],
    ])


def build_welcome_keyboard():
    return colored([
        [btn("create_email", "📨 إنشاء بريد عشوائي", callback_data="generate_email")],
        [btn("my_emails", "📚 بريداتي المحفوظة", callback_data="my_emails")],
        [btn("channel", "📢 قناة البوت", url=CHANNEL_URL)],
    ])


def build_mailbox_keyboard():
    return colored([
        [btn("inbox", "🔄 فحص صندوق الوارد", callback_data="check_inbox")],
        [btn("save", "💾 حفظ هذا البريد", callback_data="save_mail"),
         btn("change", "🗑️ تغيير البريد", callback_data="generate_email")],
        [btn("my_emails", "📚 بريداتي المحفوظة", callback_data="my_emails")],
    ])


def build_mailbox_text(email, title="✅ **تم إنشاء بريدك بنجاح**"):
    return (
        f"{title}\n"
        "━━━━━━━━━━━━━━\n"
        f"📧 `{email}`\n"
        "(اضغط على البريد لنسخه)\n\n"
        "1️⃣ استخدم هذا البريد للتسجيل في أي موقع\n"
        "2️⃣ اضغط \"فحص صندوق الوارد\" لاستقبال الرسائل ورموز التحقق\n"
        "🔔 أو فقط انتظر — سيصلك أي كود جديد تلقائيًا بدون أي ضغط\n"
        "💾 اضغط \"حفظ هذا البريد\" إن أردت الاحتفاظ به"
    )


def build_domain_keyboard():
    """لوحة اختيار الدومين + خيار عشوائي + رجوع."""
    try:
        domains = mailtm_list_domains()[:6]
    except Exception as e:
        print(f"[build_domain_keyboard] تعذر جلب الدومينات: {e}")
        domains = []

    rows, pair = [], []
    for d in domains:
        pair.append(btn("domain", f"@{d}", callback_data=f"pick_domain:{d}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([btn("random", "🎲 عشوائي", callback_data="pick_domain:random")])
    rows.append([btn("back", "🔙 رجوع", callback_data="back_menu")])
    return colored(rows)


def build_my_emails_keyboard(user_id):
    rows = []
    for rid, addr in list_saved_mailboxes(user_id):
        rows.append([
            btn("use_mail", f"📧 {addr}", callback_data=f"use_mail:{rid}"),
            btn("del_mail", "🗑", callback_data=f"del_mail:{rid}"),
        ])
    rows.append([btn("back", "🔙 رجوع", callback_data="back_menu")])
    return colored(rows)


def my_emails_text(user_id):
    n = count_saved_mailboxes(user_id)
    mx = get_int("max_saved")
    if n == 0:
        return "📭 لا يوجد لديك بريد محفوظ.\nأنشئ بريدًا ثم اضغط \"💾 حفظ هذا البريد\"."
    return f"📚 **بريداتك المحفوظة** ({n}/{mx})\nاضغط على البريد لتفعيله، أو 🗑 لحذفه:"


def show_main_menu(chat_id, user_id, first_name=None):
    """
    يعرض القائمة الرئيسية للبوت مباشرة لصاحب user_id/chat_id المحددين.
    دالة مستقلة (بدل الاعتماد على كائن message) حتى تعمل بشكل صحيح
    سواء استُدعيت من أمر /start مباشرة أو من زر "التحقق" بعد الاشتراك.
    """
    markup = build_welcome_keyboard()
    custom = get_setting("welcome_text")
    if custom:
        bot.send_message(chat_id, custom, reply_markup=markup)  # نص مخصص من الأدمن (بدون تنسيق لتفادي الأخطاء)
        return
    welcome_text = (
        "👋 **أهلًا بك في بوت GMAIL**\n\n"
        "1️⃣ إنشاء بريد عشوائي\n"
        "2️⃣ استخدمه للتسجيل بأي موقع\n"
        "3️⃣ فحص الوارد لاستقبال الرمز/الرابط\n\n"
        "🛡️ يحميك من السبام على بريدك الحقيقي"
    )
    bot.send_message(chat_id, welcome_text, reply_markup=markup, parse_mode="Markdown")


def _bg_track_start(user_id, username):
    """تسجيل المستخدم والطلب في الخلفية — حتى لا يؤخر الرد أبدًا."""
    try:
        track_user(user_id, username)
        log_request(user_id, "start")
    except Exception as e:
        print(f"[_bg_track_start] {e}")


def _report_error_to_admin(where, user_id, err):
    """يرسل سبب الفشل الحقيقي للأدمن في المحادثة (تشخيص بدون الحاجة لسجلات Render)."""
    log_error(where, err)
    try:
        bot.send_message(ADMIN_ID, f"⚠️ خطأ في {where}\nالمستخدم: {user_id}\n{type(err).__name__}: {err}"[:3500])
    except Exception as e2:
        print(f"[_report_error_to_admin] {e2}")


@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    print(f"[start] وصل أمر /start من {user_id}")

    if get_bool("maintenance") and user_id != ADMIN_ID:
        bot.send_message(message.chat.id, "🔧 البوت تحت الصيانة حاليًا، عد لاحقًا بإذن الله.")
        return

    # 1) الرد أولًا — قبل أي قاعدة بيانات
    try:
        threading.Thread(target=_bg_track_start, args=(user_id, message.from_user.username), daemon=True).start()

        try:
            subscribed = check_subscription(user_id)
        except Exception as e:
            print(f"[start] check_subscription: {e}")
            subscribed = False

        if not subscribed:
            first_name = message.from_user.first_name or "صديقي"
            sub_text = (
                f"مرحبًا بك يا {first_name} في بوت GMAIL الذكي 👋\n\n"
                "طريقة الاستخدام:\n\n"
                "1️⃣ انضم إلى قناة البوت أولًا\n"
                "2️⃣ اضغط على زر التحقق"
            )
            try:
                bot.send_message(message.chat.id, sub_text, reply_markup=build_subscribe_keyboard())
            except Exception as e:
                _report_error_to_admin("start/subscribe", user_id, e)
                bot.send_message(message.chat.id, sub_text)
            return

        try:
            show_main_menu(message.chat.id, user_id, message.from_user.first_name)
        except Exception as e:
            _report_error_to_admin("start/show_main_menu", user_id, e)
            # بديل مضمون: بدون ألوان وبدون Markdown
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("📨 إنشاء بريد عشوائي", callback_data="generate_email"))
            bot.send_message(message.chat.id, "👋 أهلًا بك في بوت GMAIL\nاضغط الزر لإنشاء بريد عشوائي", reply_markup=kb)
    except Exception as e:
        _report_error_to_admin("start", user_id, e)

# ================= التعامل مع الأزرار =================

@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    # ملاحظة مهمة: نستخدم call.from_user للحصول على هوية الشخص الذي ضغط الزر،
    # وليس call.message.from_user الذي يرجّع هوية البوت نفسه (لأن call.message
    # هو الرسالة التي أرسلها البوت) — هذا كان سبب خلل سابق بعد التحقق من الاشتراك.
    user_id = call.from_user.id

    # لوحة تحكم الأدمن (كل أزرارها تبدأ بـ adm:)
    if call.data == "refresh_stats":
        call.data = "adm:stats"
    if call.data.startswith("adm:"):
        admin_callback(call)
        return

    track_user(user_id, call.from_user.username)

    if get_bool("maintenance") and user_id != ADMIN_ID:
        bot.answer_callback_query(call.id, "🔧 البوت تحت الصيانة حاليًا.", show_alert=True)
        return

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

        markup = build_mailbox_keyboard()
        text = build_mailbox_text(email)

        # تعديل الرسالة السابقة بدلاً من إرسال رسالة جديدة لتجربة مستخدم أفضل
        bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=text, reply_markup=markup, parse_mode="Markdown")

    elif call.data == "my_emails":
        try:
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                  text=my_emails_text(user_id), reply_markup=build_my_emails_keyboard(user_id),
                                  parse_mode="Markdown")
        except Exception as e:
            log_error("my_emails", e)
            bot.send_message(call.message.chat.id, my_emails_text(user_id),
                             reply_markup=build_my_emails_keyboard(user_id), parse_mode="Markdown")

    elif call.data == "save_mail":
        mailbox = get_mailbox(user_id)
        if not mailbox:
            bot.answer_callback_query(call.id, "⚠️ لا يوجد بريد نشط لحفظه.", show_alert=True)
            return
        result = save_mailbox_history(user_id, mailbox)
        mx = get_int("max_saved")
        msgs = {
            "saved": f"✅ تم حفظ البريد ({count_saved_mailboxes(user_id)}/{mx})",
            "exists": "ℹ️ هذا البريد محفوظ مسبقًا.",
            "full": f"⚠️ وصلت للحد الأقصى ({mx} بريد). احذف بريدًا من \"بريداتي المحفوظة\" ثم أعد المحاولة.",
            "error": "❌ تعذر الحفظ حاليًا، حاول لاحقًا.",
        }
        bot.answer_callback_query(call.id, msgs[result], show_alert=(result in ("full", "error")))

    elif call.data.startswith("del_mail:"):
        try:
            row_id = int(call.data.split(":", 1)[1])
        except ValueError:
            return
        ok = delete_saved_mailbox(user_id, row_id)
        bot.answer_callback_query(call.id, "🗑 تم حذف البريد" if ok else "❌ تعذر الحذف")
        try:
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                  text=my_emails_text(user_id), reply_markup=build_my_emails_keyboard(user_id),
                                  parse_mode="Markdown")
        except Exception as e:
            log_error("del_mail", e)

    elif call.data.startswith("use_mail:"):
        try:
            row_id = int(call.data.split(":", 1)[1])
        except ValueError:
            return
        mailbox = get_saved_mailbox(user_id, row_id)
        if not mailbox:
            bot.answer_callback_query(call.id, "⚠️ هذا البريد غير موجود.", show_alert=True)
            return
        if mailbox["provider"] == "mailtm" and mailbox.get("password"):
            try:  # تجديد التوكن (قد يكون انتهى)
                tr = requests.post(f"{MAILTM_BASE}/token",
                                   json={"address": mailbox["address"], "password": mailbox["password"]}, timeout=10)
                tr.raise_for_status()
                mailbox["token"] = tr.json().get("token")
            except Exception as e:
                print(f"[use_mail] تعذر تجديد التوكن: {e}")
        user_emails[user_id] = mailbox
        save_active_mailbox(user_id, mailbox)
        bot.answer_callback_query(call.id, "✅ تم تفعيل البريد")
        try:
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id,
                                  text=build_mailbox_text(mailbox["address"], "📧 **البريد المفعّل**"),
                                  reply_markup=build_mailbox_keyboard(), parse_mode="Markdown")
        except Exception as e:
            print(f"[use_mail] {e}")

    elif call.data == "back_menu":
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        show_main_menu(call.message.chat.id, user_id, call.from_user.first_name)

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


# ================= لوحة تحكم الأدمن (/settings) =================
admin_state = {"mode": None, "pending": None}   # mode: await_broadcast / await_welcome
broadcast_run = {"on": False, "stop": False}


def progress_bar(done, total, width=12):
    pct = 100 if total <= 0 else int(done * 100 / total)
    filled = width if total <= 0 else int(width * done / total)
    return "▓" * filled + "░" * (width - filled) + f" {pct}%"


def adm_back(to="adm:main"):
    return [fbtn("🔙 رجوع", "danger", callback_data=to)]


def adm_main_text():
    return (
        "🛠️ لوحة تحكم البوت\n"
        "━━━━━━━━━━━━━━\n"
        f"🔧 الصيانة: {'مفعّلة' if get_bool('maintenance') else 'متوقفة'}\n"
        f"📢 الاشتراك الإجباري: {'مفعّل' if get_bool('force_sub') else 'متوقف'}\n"
        f"🔔 الإشعار التلقائي: {'مفعّل' if get_bool('auto_notify') else 'متوقف'}\n\n"
        "اختر القسم:"
    )


def adm_main_kb():
    return colored([
        [fbtn("📊 الإحصائيات", "primary", "adm:stats"), fbtn("📢 الإذاعة", "primary", "adm:bc")],
        [fbtn("⚙️ إعدادات البوت", "success", "adm:set"), fbtn("🎨 ألوان الأزرار", "success", "adm:colors")],
        [fbtn("🐞 أخطاء البوت", "danger", "adm:err")],
        [fbtn("❌ إغلاق", "danger", "adm:close")],
    ])


def adm_show(call, text, kb, md=False):
    """يعدّل رسالة اللوحة. يتجاهل خطأ 'الرسالة لم تتغير'."""
    try:
        bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id,
                              reply_markup=kb, parse_mode="Markdown" if md else None)
        return True
    except Exception as e:
        if "not modified" in str(e):
            return False
        log_error("adm_show", e)
        try:
            bot.send_message(call.message.chat.id, text, reply_markup=kb, parse_mode="Markdown" if md else None)
        except Exception as e2:
            log_error("adm_show/send", e2)
        return True


def adm_flag(key):
    return "✅ مفعّل" if get_bool(key) else "⛔ متوقف"


def adm_settings_kb():
    return colored([
        [fbtn(f"🔧 وضع الصيانة: {adm_flag('maintenance')}", "danger" if get_bool("maintenance") else "success", "adm:tg:maintenance")],
        [fbtn(f"📢 الاشتراك الإجباري: {adm_flag('force_sub')}", "primary", "adm:tg:force_sub")],
        [fbtn(f"🔔 الإشعار التلقائي: {adm_flag('auto_notify')}", "primary", "adm:tg:auto_notify")],
        [fbtn(f"⏱ فاصل الفحص التلقائي: {get_int('auto_interval')} ث", "primary", "adm:cy:auto_interval")],
        [fbtn(f"🛡 الحد بين الضغطات: {get_int('rate_limit')} ث", "primary", "adm:cy:rate_limit")],
        [fbtn(f"💾 أقصى بريد محفوظ: {get_int('max_saved')}", "primary", "adm:cy:max_saved")],
        [fbtn("✏️ تعديل رسالة الترحيب", "success", "adm:welcome")],
        adm_back(),
    ])


CYCLE_VALUES = {
    "auto_interval": [10, 25, 60, 120],
    "rate_limit": [1, 3, 5, 10],
    "max_saved": [10, 20, 30, 50],
}


def adm_colors_kb():
    rows = []
    for key, (label, _default) in BUTTON_KEYS.items():
        rows.append([fbtn(f"{STYLE_NAMES[get_style(key)].split()[0]} {label}", get_style(key), f"adm:cl:{key}")])
    rows.append([
        fbtn("🔵 كل الأزرار", "primary", "adm:call:primary"),
        fbtn("🟢 كل الأزرار", "success", "adm:call:success"),
        fbtn("🔴 كل الأزرار", "danger", "adm:call:danger"),
    ])
    rows.append([fbtn("↩️ استرجاع كل الألوان الافتراضية", "none", "adm:call:default")])
    rows.append(adm_back())
    return colored(rows)


def adm_color_pick_kb(key):
    return colored([
        [fbtn("🔵 أزرق", "primary", f"adm:cs:{key}:primary")],
        [fbtn("🟢 أخضر", "success", f"adm:cs:{key}:success")],
        [fbtn("🔴 أحمر", "danger", f"adm:cs:{key}:danger")],
        [fbtn("⚪ بدون لون", "none", f"adm:cs:{key}:none")],
        [fbtn("↩️ الافتراضي", "none", f"adm:cs:{key}:default")],
        adm_back("adm:colors"),
    ])


def adm_broadcast_menu():
    try:
        rows = db_execute("SELECT id, created_at, sent, status FROM broadcasts ORDER BY id DESC LIMIT 5").rows
    except Exception as e:
        log_error("adm_broadcast_menu", e)
        rows = []
    kb = [[fbtn("📝 إذاعة جديدة", "success", "adm:bcnew")]]
    lines = ["📢 الإذاعة", "━━━━━━━━━━━━━━", "أرسل أي رسالة (نص/صورة/فيديو/ملف/محوّلة من قناة) وتُذاع للجميع كما هي تمامًا.", ""]
    if rows:
        lines.append("آخر الإذاعات:")
        for bid, created, sent, status in rows:
            st = {"done": "✅ مكتملة", "running": "⏳ جارية", "stopped": "⏹ أوقفت", "deleted": "🗑 محذوفة"}.get(status, status)
            lines.append(f"• #{str(created)[:16].replace('T', ' ')} — {sent} مستلم — {st}")
            if status != "deleted":
                kb.append([fbtn(f"🗑 حذف إذاعة {str(created)[5:16].replace('T', ' ')} من عند الجميع", "danger", f"adm:bcdelask:{bid}")])
        lines.append("\n⚠️ تيليجرام يسمح بحذف رسائل البوت خلال 48 ساعة من إرسالها.")
    else:
        lines.append("لا توجد إذاعات سابقة.")
    kb.append(adm_back())
    return "\n".join(lines), colored(kb)


def _flush_msgs(buf):
    if not buf:
        return
    try:
        ph = ",".join(["(?, ?, ?)"] * len(buf))
        params = [x for row in buf for x in row]
        db_execute(f"INSERT INTO broadcast_msgs (bid, user_id, message_id) VALUES {ph}", params)
    except Exception as e:
        log_error("flush_msgs", e)
    buf.clear()


def _edit_progress(chat_id, msg_id, text, kb=None):
    try:
        bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id, reply_markup=kb)
    except Exception:
        pass


def run_broadcast(from_chat, from_msg, admin_chat, prog_id):
    """يذيع الرسالة كما هي (copy_message) لكل المستخدمين مع شريط تقدم، ويحفظ كل رسالة لإمكانية حذفها لاحقًا."""
    broadcast_run.update(on=True, stop=False)
    bid = int(time.time())
    try:
        users = get_all_user_ids()
    except Exception as e:
        log_error("run_broadcast/users", e)
        users = []
    total, sent, failed, buf = len(users), 0, 0, []
    try:
        db_execute("INSERT INTO broadcasts (id, created_at, total, sent, failed, status) VALUES (?, ?, ?, 0, 0, 'running')",
                   [bid, datetime.utcnow().isoformat(), total])
    except Exception as e:
        log_error("run_broadcast/insert", e)
    stop_kb = colored([[fbtn("⏹ إيقاف الإذاعة", "danger", "adm:bcstop")]])
    last = 0.0
    status = "done"
    for i, uid in enumerate(users, 1):
        if broadcast_run["stop"]:
            status = "stopped"
            break
        try:
            m = bot.copy_message(uid, from_chat, from_msg)
            sent += 1
            buf.append((bid, uid, m.message_id))
        except Exception:
            failed += 1
        if len(buf) >= 40:
            _flush_msgs(buf)
        if time.time() - last > 2:
            last = time.time()
            _edit_progress(admin_chat, prog_id,
                           f"🚀 جاري الإذاعة...\n{progress_bar(i, total)}\n({i}/{total})  ✅ {sent}  ❌ {failed}", stop_kb)
        time.sleep(0.05)
    _flush_msgs(buf)
    try:
        db_execute("UPDATE broadcasts SET sent = ?, failed = ?, status = ? WHERE id = ?", [sent, failed, status, bid])
    except Exception as e:
        log_error("run_broadcast/update", e)
    title = "✅ اكتملت الإذاعة" if status == "done" else "⏹ تم إيقاف الإذاعة"
    _edit_progress(admin_chat, prog_id,
                   f"{title}\n{progress_bar(sent + failed, total)}\n✅ نجح: {sent}\n❌ فشل: {failed} (غالبًا حظروا البوت)",
                   colored([[fbtn("🗑 حذف هذه الإذاعة من عند الجميع", "danger", f"adm:bcdelask:{bid}")],
                            adm_back("adm:bc")]))
    broadcast_run["on"] = False


def delete_broadcast(bid, admin_chat, prog_id):
    """يحذف الإذاعة من عند كل من استلمها مع شريط تقدم."""
    try:
        rows = db_execute("SELECT user_id, message_id FROM broadcast_msgs WHERE bid = ?", [bid]).rows
    except Exception as e:
        log_error("delete_broadcast/read", e)
        rows = []
    total, ok, fail, last = len(rows), 0, 0, 0.0
    for i, (uid, mid) in enumerate(rows, 1):
        try:
            bot.delete_message(uid, mid)
            ok += 1
        except Exception:
            fail += 1
        if time.time() - last > 2:
            last = time.time()
            _edit_progress(admin_chat, prog_id, f"🗑 جاري الحذف من عند الجميع...\n{progress_bar(i, total)}\n({i}/{total})")
        time.sleep(0.05)
    try:
        db_execute("DELETE FROM broadcast_msgs WHERE bid = ?", [bid])
        db_execute("UPDATE broadcasts SET status = 'deleted' WHERE id = ?", [bid])
    except Exception as e:
        log_error("delete_broadcast/update", e)
    _edit_progress(admin_chat, prog_id, f"✅ تم حذف الإذاعة\n{progress_bar(total, total)}\n🗑 حُذفت: {ok}\n⚠️ تعذر حذف: {fail} (قديمة أو حظروا البوت)",
                   colored([adm_back("adm:bc")]))


def format_errors_text():
    try:
        rows = db_execute("SELECT created_at, location, message FROM error_log ORDER BY id DESC LIMIT 10").rows
    except Exception as e:
        return f"تعذر قراءة السجل: {e}"
    if not rows:
        return "🐞 سجل الأخطاء\n━━━━━━━━━━━━━━\n✅ لا توجد أخطاء مسجلة."
    out = ["🐞 آخر 10 أخطاء", "━━━━━━━━━━━━━━"]
    for created, where, msg in rows:
        out.append(f"🕐 {str(created)[5:19].replace('T', ' ')} UTC\n📍 {where}\n{msg}\n")
    return "\n".join(out)[:3800]


def admin_callback(call):
    """يعالج كل أزرار لوحة الأدمن (adm:...)."""
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "🚫 هذه اللوحة للأدمن فقط.", show_alert=True)
        return
    parts = call.data.split(":")
    sec = parts[1] if len(parts) > 1 else "main"
    chat_id, msg_id = call.message.chat.id, call.message.message_id

    try:
        if sec == "main":
            admin_state["mode"] = None
            adm_show(call, adm_main_text(), adm_main_kb())

        elif sec == "close":
            admin_state["mode"] = None
            try:
                bot.delete_message(chat_id, msg_id)
            except Exception:
                pass

        elif sec == "stats":
            changed = adm_show(call, format_stats_message(),
                               colored([[fbtn("🔄 تحديث", "success", "adm:stats")], adm_back()]), md=True)
            bot.answer_callback_query(call.id, "✅ تم التحديث" if changed else "ℹ️ لا جديد منذ آخر تحديث")
            return

        # ---------- الإذاعة ----------
        elif sec == "bc":
            admin_state["mode"] = None
            text, kb = adm_broadcast_menu()
            adm_show(call, text, kb)

        elif sec == "bcnew":
            if broadcast_run["on"]:
                bot.answer_callback_query(call.id, "⏳ توجد إذاعة جارية حاليًا.", show_alert=True)
                return
            admin_state["mode"] = "await_broadcast"
            adm_show(call, "📝 أرسل الآن المحتوى المراد إذاعته.\n\nيقبل: نص منسق، صورة، فيديو، ملف، صوت، ملصق، أو رسالة محوّلة من قناة — تُرسل للجميع كما هي تمامًا.",
                     colored([[fbtn("❌ إلغاء", "danger", "adm:bc")]]))

        elif sec == "bcgo":
            pending = admin_state.get("pending")
            if not pending:
                bot.answer_callback_query(call.id, "⚠️ لا توجد رسالة جاهزة للإذاعة.", show_alert=True)
                return
            if broadcast_run["on"]:
                bot.answer_callback_query(call.id, "⏳ توجد إذاعة جارية حاليًا.", show_alert=True)
                return
            admin_state["pending"] = None
            adm_show(call, "🚀 جاري بدء الإذاعة...", None)
            threading.Thread(target=run_broadcast, args=(pending[0], pending[1], chat_id, msg_id), daemon=True).start()

        elif sec == "bcstop":
            broadcast_run["stop"] = True
            bot.answer_callback_query(call.id, "⏹ جاري الإيقاف...")
            return

        elif sec == "bcdelask":
            bid = parts[2]
            adm_show(call, "⚠️ هل تريد حذف هذه الإذاعة من عند جميع المستخدمين؟\nلا يمكن التراجع.",
                     colored([[fbtn("✅ نعم، احذفها من عند الجميع", "danger", f"adm:bcdel:{bid}")],
                              adm_back("adm:bc")]))

        elif sec == "bcdel":
            bid = int(parts[2])
            adm_show(call, "🗑 جاري الحذف...", None)
            threading.Thread(target=delete_broadcast, args=(bid, chat_id, msg_id), daemon=True).start()

        # ---------- الإعدادات ----------
        elif sec == "set":
            admin_state["mode"] = None
            adm_show(call, "⚙️ إعدادات البوت\nاضغط على أي خيار لتغييره فورًا:", adm_settings_kb())

        elif sec == "tg":
            key = parts[2]
            if key in ("maintenance", "force_sub", "auto_notify"):
                set_setting(key, "0" if get_bool(key) else "1")
            adm_show(call, "⚙️ إعدادات البوت\nاضغط على أي خيار لتغييره فورًا:", adm_settings_kb())

        elif sec == "cy":
            key = parts[2]
            vals = CYCLE_VALUES.get(key)
            if vals:
                cur = get_int(key)
                nxt = vals[(vals.index(cur) + 1) % len(vals)] if cur in vals else vals[0]
                set_setting(key, nxt)
            adm_show(call, "⚙️ إعدادات البوت\nاضغط على أي خيار لتغييره فورًا:", adm_settings_kb())

        elif sec == "welcome":
            admin_state["mode"] = "await_welcome"
            cur = get_setting("welcome_text") or "(الافتراضية)"
            adm_show(call, f"✏️ أرسل الآن نص رسالة الترحيب الجديدة.\nأرسل كلمة: افتراضي — لإرجاع الرسالة الأصلية.\n\nالحالية:\n{cur}",
                     colored([[fbtn("❌ إلغاء", "danger", "adm:set")]]))

        # ---------- ألوان الأزرار ----------
        elif sec == "colors":
            adm_show(call, "🎨 ألوان الأزرار\nاختر زرًا لتغيير لونه (أو لوّن الكل دفعة واحدة):", adm_colors_kb())

        elif sec == "cl":
            key = parts[2]
            if key in BUTTON_KEYS:
                adm_show(call, f"🎨 {BUTTON_KEYS[key][0]}\nاللون الحالي: {STYLE_NAMES[get_style(key)]}\nاختر اللون الجديد:",
                         adm_color_pick_kb(key))

        elif sec == "cs":
            key, val = parts[2], parts[3]
            if key in BUTTON_KEYS:
                set_setting("style:" + key, "" if val == "default" else val)
                bot.answer_callback_query(call.id, "✅ تم تغيير اللون")
                adm_show(call, f"🎨 {BUTTON_KEYS[key][0]}\nاللون الحالي: {STYLE_NAMES[get_style(key)]}\nاختر اللون الجديد:",
                         adm_color_pick_kb(key))
                return

        elif sec == "call":
            val = parts[2]
            for key in BUTTON_KEYS:
                set_setting("style:" + key, "" if val == "default" else val)
            bot.answer_callback_query(call.id, "✅ تم تطبيق اللون على كل الأزرار")
            adm_show(call, "🎨 ألوان الأزرار\nاختر زرًا لتغيير لونه (أو لوّن الكل دفعة واحدة):", adm_colors_kb())
            return

        # ---------- الأخطاء ----------
        elif sec == "err":
            changed = adm_show(call, format_errors_text(),
                               colored([[fbtn("🔄 تحديث", "success", "adm:err"), fbtn("🧹 مسح السجل", "danger", "adm:errclear")],
                                        adm_back()]))
            bot.answer_callback_query(call.id, "✅ تم التحديث" if changed else "ℹ️ لا جديد")
            return

        elif sec == "errclear":
            try:
                db_execute("DELETE FROM error_log")
            except Exception as e:
                log_error("errclear", e)
            adm_show(call, format_errors_text(),
                     colored([[fbtn("🔄 تحديث", "success", "adm:err"), fbtn("🧹 مسح السجل", "danger", "adm:errclear")],
                              adm_back()]))
    except Exception as e:
        log_error(f"admin_callback:{call.data}", e)
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass


@bot.message_handler(commands=['settings', 'stats', 'broadcast', 'اعدادات', 'الاعدادات', 'اذاعة'])
def settings_command(message):
    """أمر الأدمن الوحيد: يفتح لوحة التحكم الكاملة."""
    if message.from_user.id != ADMIN_ID:
        return  # يتجاهل الطلب تمامًا لغير الأدمن دون أي رد
    admin_state["mode"] = None
    try:
        bot.send_message(message.chat.id, adm_main_text(), reply_markup=adm_main_kb())
    except Exception as e:
        log_error("settings_command", e)


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


# ================= معالج عام (استقبال محتوى الإذاعة/الترحيب من الأدمن) =================
ALL_CONTENT_TYPES = ['text', 'photo', 'video', 'document', 'animation', 'audio', 'voice',
                     'sticker', 'video_note', 'poll', 'location', 'contact', 'venue', 'dice']


@bot.message_handler(func=lambda m: True, content_types=ALL_CONTENT_TYPES)
def general_handler(message):
    if message.from_user.id != ADMIN_ID:
        return  # مستخدم عادي: لا شيء (تفادي إزعاجه بردود غير ضرورية)

    mode = admin_state.get("mode")

    if mode == "await_broadcast":
        admin_state["mode"] = None
        admin_state["pending"] = (message.chat.id, message.message_id)
        try:
            bot.copy_message(message.chat.id, message.chat.id, message.message_id)  # معاينة كما ستصل للمستخدمين
        except Exception as e:
            log_error("broadcast/preview", e)
        bot.send_message(
            message.chat.id,
            "👆 هذه معاينة الإذاعة. هل تؤكد إرسالها لكل المستخدمين؟",
            reply_markup=colored([[fbtn("✅ تأكيد وبدء الإذاعة", "success", "adm:bcgo")],
                                  [fbtn("❌ إلغاء", "danger", "adm:bc")]]),
        )
        return

    if mode == "await_welcome" and message.content_type == "text":
        admin_state["mode"] = None
        new_text = message.text.strip()
        set_setting("welcome_text", "" if new_text in ("افتراضي", "الافتراضي") else new_text)
        bot.send_message(message.chat.id, "✅ تم تحديث رسالة الترحيب.", reply_markup=adm_settings_kb())
        return

    if message.content_type == "text" and message.text.strip() in ["احصائيات", "إحصائيات", "اعدادات", "إعدادات", "الاعدادات", "الإعدادات"]:
        bot.send_message(message.chat.id, adm_main_text(), reply_markup=adm_main_kb())


# ================= المراقبة التلقائية للوارد (إشعار فوري بدون ضغط المستخدم) =================


def auto_notify_loop():
    """يفحص كل البريد النشط (من Turso) بالخلفية دوريًا، ويرسل أي رسالة جديدة تلقائيًا."""
    while True:
        time.sleep(get_int("auto_interval"))
        if not get_bool("auto_notify"):
            continue
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
        BotCommand("settings", "🛠️ الإعدادات (لوحة تحكم الأدمن)"),
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







