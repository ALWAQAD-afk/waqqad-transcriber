import os
import uuid
import json
import threading
import mimetypes
import subprocess
import shutil
import re
import time
import hmac
import hashlib
import html
from urllib.parse import parse_qsl
from pathlib import Path
from warraq import handle_warraq_update
from warraq_miniapp import register_warraq_miniapp
import requests

# --- إضافة التنقيح v3 - نظام 3 ملفات تلقائي + تقسيم دفعات ---
try:
    from refine import (
        GeminiFatalError,
        refine_arabic_text,
        refine_conservative,
        refine_liberal,
        refine_document,
        split_text_smart,
    )
    REFINE_ENABLED = True
    print("[REFINE] v3 Module loaded - 3 files auto mode", flush=True)
except Exception as e:
    print(f"[REFINE] Disabled: {e}", flush=True)
    GeminiFatalError = None
    refine_arabic_text = None
    refine_conservative = None
    refine_liberal = None
    refine_document = None
    split_text_smart = None
    REFINE_ENABLED = False
# --- نهاية الإضافة ---
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from werkzeug.utils import secure_filename

# --- الاستقبال المباشر، التصدير، والطابور ---
import job_queue
import transcripts as transcript_store
import url_media
from bot_upload import (
    DownloadFailed,
    FileTooLarge,
    MAX_UPLOAD_BYTES,
    UnsupportedFile,
    download_telegram_file,
    extract_attachment,
    human_size as format_size,
    is_supported,
)
from exporters import DOCX_MIME, TXT_MIME, build_docx, build_txt
# --- نهاية الإضافة ---


app = Flask(__name__)
register_warraq_miniapp(app)

CORS(
    app,
    resources={
        r"/*": {
            "origins": "https://alwaqad-afk.github.io",
            "methods": ["GET", "POST", "OPTIONS"],
            "allow_headers": [
                "Content-Type",
                "X-Telegram-Init-Data",
            ],
        }
    }
)


DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

# مفتاح واحد أو عدة مفاتيح مفصولة بفواصل:
# COHERE_API_KEYS=key1,key2,key3
# وللتوافق مع الاختبار المحلي يمكن أيضًا استخدام COHERE_API_KEY لمفتاح واحد.
COHERE_API_KEYS = [
    key.strip()
    for key in (
        os.environ.get("COHERE_API_KEYS")
        or os.environ.get("COHERE_API_KEY")
        or ""
    ).split(",")
    if key.strip()
]

COHERE_URL = "https://api.cohere.com/v2/audio/transcriptions"
COHERE_MODEL = "cohere-transcribe-arabic-07-2026"
COHERE_LANGUAGE = "ar"

# 24 kbps ≈ 21.6 MB لكل ساعتين؛ أي بهامش جيد تحت حد Cohere البالغ 25 MB.
COHERE_SEGMENT_SECONDS = 7200
COHERE_MAX_FILE_BYTES = 24_000_000
COHERE_TIMEOUT = 1800

# الصيغ التي يقبلها Cohere مباشرة دون تحويل.
COHERE_DIRECT_EXTENSIONS = {
    ".flac",
    ".mp3",
    ".mpeg",
    ".mpga",
    ".ogg",
    ".wav",
}

# MP3 يمكن تقطيعه بنسخ الضغط دون إعادة ترميز (أسرع بكثير).
COHERE_COPY_EXTENSIONS = {
    ".mp3",
    ".mpeg",
    ".mpga",
}

# توزيع الطلبات على عدد غير محدود من المفاتيح (Round-robin).
cohere_key_lock = threading.Lock()
cohere_key_cursor = 0

# أخطاء تعني أن المفتاح نفسه لا يستطيع متابعة الطلب؛ نجرب مفتاحًا آخر.
COHERE_KEY_FAILOVER_STATUSES = {401, 402, 403, 429, 498}

# أخطاء تعني أن خدمة Cohere نفسها غير متاحة مؤقتًا؛ بعد تجربة مناسبة
# نسمح بالانتقال إلى Deepgram.
COHERE_SERVICE_FALLBACK_STATUSES = {500, 501, 503, 504}

BASE_URL = os.environ.get(
    "BASE_URL",
    "https://waqqad-transcriber.onrender.com"
)

TRANSCRIBER_WEBAPP_URL = os.environ.get(
    "TRANSCRIBER_WEBAPP_URL",
    "https://alwaqad-afk.github.io/waqqad-transcriber-web/"
)

TRANSCRIBER_LOG_CHAT_ID = os.environ.get(
    "TRANSCRIBER_LOG_CHAT_ID",
    ""
).strip()

# حد تنزيل الروابط أكبر من حد تيليجرام، لأن الرابط لا يمر عبر البوت.
# الافتراضي 150MB يكفي محاضرة طويلة بصيغة mp3 مضغوطة.
TRANSCRIBER_URL_MAX_MB = float(
    os.environ.get("TRANSCRIBER_URL_MAX_MB", "150") or 150
)
MAX_URL_BYTES = int(TRANSCRIBER_URL_MAX_MB * 1024 * 1024)

# هوية المطوّر والمساهمين، تظهر في /about و/start.
DEVELOPER_NAME = os.environ.get(
    "DEVELOPER_NAME", "الوقّاد"
).strip() or "الوقّاد"
DEVELOPER_CONTACT = os.environ.get(
    "DEVELOPER_CONTACT", ""
).strip()

# معرّف تليجرام للمطوّر: تستخدمه /stats وتصل إليه رسائل /contact.
DEVELOPER_CHAT_ID = os.environ.get(
    "DEVELOPER_CHAT_ID", "7991076455"
).strip()
DEVELOPER_USERNAME = os.environ.get(
    "DEVELOPER_USERNAME", ""
).strip().lstrip("@")


def developer_direct_link():
    """رابط مباشر اختياري، يظهر بجانب /contact إن أُعدّ."""
    if DEVELOPER_USERNAME:
        return f"https://t.me/{DEVELOPER_USERNAME}"
    return DEVELOPER_CONTACT


def is_developer(user_id):
    return bool(DEVELOPER_CHAT_ID) and str(user_id) == str(DEVELOPER_CHAT_ID)


# الحجب: جدول مستقل في القاعدة حتى يبقى ساريًا بعد إعادة التشغيل.
_BLOCKED_THROTTLE = {}

BLOCKED_GATE_TEXT = (
    "⚠️ تم إيقاف استخدام هذه الخدمة لحسابك من قبل المطوّر."
)

FEEDBACK_VERDICTS = {
    "good": "👍 ممتاز",
    "ok": "👌 جيد",
    "bad": "👎 يحتاج تحسين",
}

DEVELOPER_ONLY_COMMANDS = [
    {"command": "stats", "description": "إحصاءات الخدمة"},
    {"command": "users", "description": "آخر 20 مستخدمًا"},
    {"command": "allusers", "description": "كل المستخدمين من التأسيس"},
    {"command": "cookies", "description": "حالة كوكيز يوتيوب"},
    {"command": "ytinfo", "description": "فحص صيغ رابط يوتيوب: /ytinfo الرابط"},
    {"command": "block", "description": "إيقاف مستخدم: /block معرفه"},
    {"command": "unblock", "description": "إلغاء إيقاف مستخدم"},
    {"command": "blocked", "description": "قائمة المحجوبين"},
    {"command": "msg", "description": "مراسلة مستخدم: /msg الرقم"},
]

PUBLIC_COMMANDS = [
    {"command": "start", "description": "ابدأ استخدام المفرّغ"},
    {"command": "help", "description": "كيفية الاستخدام"},
    {"command": "find", "description": "البحث في تفريغاتك"},
    {"command": "about", "description": "عن المطوّر والمساهمين"},
    {"command": "contact", "description": "مراسلة المطوّر"},
    {"command": "cancel", "description": "إلغاء وضع الكتابة"},
]


def register_bot_commands():
    """تسجيل الأوامر حتى تظهر أوتوبوسلوك بعد علامة /.

    Telegram لا يعرض قائمة تلقائية من الكود إلا عبر setMyCommands.
    نغيّر قائمة عامة للجميع، وقائمة خاصة بالمطوّر في محادثته فقط.
    """
    if not TELEGRAM_BOT_TOKEN:
        return

    api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

    def _call(label, payload):
        try:
            response = requests.post(
                f"{api}/setMyCommands",
                json=payload,
                timeout=20
            ).json()
            okay = response.get("ok")
            detail = "ok" if okay else str(response.get("description"))[:150]
            print(f"[MENU] {label}: {detail}", flush=True)
        except Exception as e:
            print(f"[MENU] {label} failed: {e}", flush=True)

    _call("المستخدمين", {"commands": PUBLIC_COMMANDS})

    if DEVELOPER_CHAT_ID:
        _call(
            "المطوّر",
            {
                "commands": PUBLIC_COMMANDS + DEVELOPER_ONLY_COMMANDS,
                "scope": {
                    "type": "chat",
                    "chat_id": int(DEVELOPER_CHAT_ID),
                },
            }
        )


def is_blocked(user_id):
    """هل الحساب موقوف؟ القاعدة مرجع السلطة."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM blocked_users WHERE user_id = %s",
                    (str(user_id),)
                )
                return cur.fetchone() is not None
    except Exception as e:
        print(f"[BLOCK] check failed: {e}", flush=True)
        return False


def _blocked_gate_now(user_id):
    """قفل الرسالة الإيقاعية: مرة كل 30 دقيقة بدل إزعاج متكرر.

    يعيد True إذا كان مسموحًا إرسال الإشعار الآن.
    """
    key = str(user_id)
    now = time.time()
    with _CONTACT_LOCK:
        last = _BLOCKED_THROTTLE.get(key, 0)
        if now - last < 1800:
            return False
        _BLOCKED_THROTTLE[key] = now
        if len(_BLOCKED_THROTTLE) > 2000:
            for old_key in list(_BLOCKED_THROTTLE)[:500]:
                _BLOCKED_THROTTLE.pop(old_key, None)
    return True


def handle_blocked_gate(user_id):
    """إشعار متقطّع للمحجوب بدل تجاهل صامت كامل."""
    if not _blocked_gate_now(user_id):
        return
    try:
        telegram_send_message(user_id, BLOCKED_GATE_TEXT)
    except Exception as e:
        print(f"[BLOCK] gate notify failed: {e}", flush=True)


def handle_block_command(chat_id, user_id, argument):
    if not is_developer(user_id):
        telegram_send_message(chat_id, "هذا الأمر للمطوّر فقط.")
        return

    target = (argument or "").strip().lstrip("@")
    if not target:
        telegram_send_message(
            chat_id,
            "استخدم: /block <رقم المستخدم>\n"
            "مثال: /block 123456789\n\n"
            "/blocked لعرض المحجوبين."
        )
        return

    if str(target) == str(DEVELOPER_CHAT_ID):
        telegram_send_message(chat_id, "لا يمكنك حجب نفسك 🙂")
        return

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO blocked_users (user_id)
                    VALUES (%s)
                    ON CONFLICT (user_id) DO NOTHING
                    """,
                    (str(target),)
                )
                conn.commit()
    except Exception as e:
        print(f"[BLOCK] save failed: {e}", flush=True)
        telegram_send_message(chat_id, "⚠️ تعذّر الحفظ. حاول من جديد.")
        return

    info = telegram_get_chat(target)
    name = " ".join(
        part for part in (
            str(info.get("first_name") or ""),
            str(info.get("last_name") or "")
        ) if part
    ).strip()
    username = str(info.get("username") or "").strip().lstrip("@")
    who = (
        f"@{username} ({name})" if username and name
        else f"@{username}" if username
        else name or "بلا اسم"
    )

    telegram_send_message(chat_id, f"🔒 تم إيقاف استخدام: {who}\n🆔 {target}")


def handle_unblock_command(chat_id, user_id, argument):
    if not is_developer(user_id):
        telegram_send_message(chat_id, "هذا الأمر للمطوّر فقط.")
        return

    target = (argument or "").strip().lstrip("@")
    if not target:
        telegram_send_message(chat_id, "استخدم: /unblock <رقم المستخدم>")
        return

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM blocked_users WHERE user_id = %s",
                    (str(target),)
                )
                removed = cur.rowcount
                conn.commit()
    except Exception as e:
        print(f"[BLOCK] unblock failed: {e}", flush=True)
        telegram_send_message(chat_id, "⚠️ تعذّر الإلغاء. حاول من جديد.")
        return

    if removed:
        telegram_send_message(chat_id, f"✅ أعيد فتح استخدام: {target}")
    else:
        telegram_send_message(chat_id, f"ℹ️ هذا الرقم غير محجوب أصلًا: {target}")


def handle_blocked_list(chat_id, user_id):
    if not is_developer(user_id):
        telegram_send_message(chat_id, "هذا الأمر للمطوّر فقط.")
        return

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT b.user_id, b.created_at
                    FROM blocked_users b
                    ORDER BY b.created_at DESC
                """)
                rows = cur.fetchall()
    except Exception as e:
        print(f"[BLOCK] list failed: {e}", flush=True)
        telegram_send_message(chat_id, "⚠️ تعذّر قراءة القائمة.")
        return

    if not rows:
        telegram_send_message(chat_id, "لا يوجد مستخدمون محجوبون.")
        return

    lines = [f"🚫 المحجوبون ({len(rows)}):"]
    for row in rows:
        seen = row.get("created_at")
        when = seen.strftime("%Y-%m-%d") if seen else "غير متاح"
        lines.append(f"• {row.get('user_id')} — منذ {when}")
    telegram_send_message(chat_id, "\n".join(lines))


def _display_target(target):
    """بطاقة قصيرة للعميل عند المراسلة: الاسم/الحساب/الرسالة."""
    info = telegram_get_chat(target)
    name = " ".join(
        part for part in (
            str(info.get("first_name") or ""),
            str(info.get("last_name") or "")
        ) if part
    ).strip()
    username = str(info.get("username") or "").strip().lstrip("@")
    if username:
        return (
            f"@{username} ({name})" if name else f"@{username}"
        )
    return name or "بلا اسم مستخدم"


def send_developer_message(target, text):
    """يوصّل رسالة المطوّر إلى عميل بوت دون طلب مسبق.

    يعيد رقم رسالة البوت لتوجيه ندرة على respondsها؛ أو None عند فشل الإرسال.
    """
    body = (
        "💬 رسالة من مطوّر مفرّغ الوقّاد:\n\n"
        f"{(text or '').strip()[:MSG_MAX_CHARS]}\n\n"
        "↩️ للردّ، اضغط زر «ردّ» على هذه الرسالة وسيردّ إليك المطوّر."
    )
    try:
        message_id = telegram_send_message(target, body)
    except Exception as e:
        print(f"[OUTREACH] send failed: {e}", flush=True)
        return None
    if message_id:
        with _OUTREACH_LOCK:
            _OUTREACH_REPLIES[str(message_id)] = str(target)
            if len(_OUTREACH_REPLIES) > 500:
                for old_key in list(_OUTREACH_REPLIES)[:100]:
                    _OUTREACH_REPLIES.pop(old_key, None)
    return message_id


def handle_msg_command(chat_id, user_id, argument):
    """أمر المطوّر /msg <id> <نص> آو /msg <id> ثم نص بعد تحفيز."""
    if not is_developer(user_id):
        telegram_send_message(chat_id, "هذا الأمر للمطوّر فقط.")
        return

    if not argument:
        telegram_send_message(
            chat_id,
            "استخدم: /msg <رقم المستخدم> <الرسالة>\n"
            "مثال: /msg 123456789 مرحبًا، كيف الخدمة؟\n\n"
            "الأرقام تظهر في /users و/allusers."
        )
        return

    head, _, text = (argument or "").partition(" ")
    text = text.strip()
    try:
        target = int(head.strip())
    except ValueError:
        telegram_send_message(
            chat_id,
            "الرجاء رقم مستخدم صحيح. مثال: /msg 123456789 مرحبًا"
        )
        return

    if not text:
        # وضع انتظار: نص المطوّر التالي يمرّر لهذا المستخدم مباشرة.
        with _OUTREACH_LOCK:
            _PENDING_TO[str(user_id)] = {
                "mode": _MESSAGES_MODE,
                "user_id": str(target),
                "started": time.time(),
            }
        who = _display_target(target)
        telegram_send_message(
            chat_id,
            f"سأرسل رسالتك التالية إلى: {who}\n🆔 {target}\n\n"
            "اكتب الرسالة الآن في الرسالة التالية، أو /cancel للإلغاء."
        )
        return

    if send_developer_message(target, text) is None:
        telegram_send_message(
            chat_id,
            "⚠️ تعذّر إرسال الرسالة. ربما حظر المستخدم البوت."
        )
        return
    telegram_send_message(
        chat_id,
        f"✅ أُرسلت الرسالة إلى: {target}"
    )


def _consume_pending_outreach(user_id):
    """هل قيد إرسال رسالة من المطوّر لعائلة مستخدم الآن؟"""
    key = str(user_id)
    with _OUTREACH_LOCK:
        entry = _PENDING_TO.get(key)
        if entry is None:
            return None
        _PENDING_TO.pop(key, None)

    if time.time() - entry.get("started", 0) > CONTACT_WINDOW_SECONDS:
        telegram_send_message(
            user_id,
            "انتهت مهلة الرسالة أعد /msg من جديد."
        )
        return None
    return entry


def _clear_pending_outreach(user_id):
    with _OUTREACH_LOCK:
        _PENDING_TO.pop(str(user_id), None)


def forward_outreach_reply(chat_id, user_id, sender, message):
    """ردّ المستخدم على رسالة صادرة يصل للمطوّر تلقائيًا.

    يعيد True إذا استُهلك هذا التحديث، بحيث لا يعالج كرفع ملف أو غيره.
    """
    replied_id = ((message.get("reply_to_message") or {}).get("message_id"))
    if not replied_id:
        return False

    with _OUTREACH_LOCK:
        owner = _OUTREACH_REPLIES.get(str(replied_id))
    if not owner or str(owner) != str(user_id):
        return False

    text = (message.get("text") or message.get("caption") or "").strip()
    if not text:
        telegram_send_message(
            chat_id,
            "اكتب ردّك نصاً وسيصل للمطوّر مباشرة."
        )
        return True

    if not _forward_contact(chat_id, user_id, sender, text):
        return True

    # رسالة التواصل تصل للمطوّر؛ نعلن للقارئ أنها ردّ على مراسلة استباقية.
    return True


def handle_allusers(chat_id, user_id):
    """كل مستخدم استخدم البوت من التأسيس، للمطوّر فقط."""
    if not is_developer(user_id):
        telegram_send_message(chat_id, "هذا الأمر للمطوّر فقط.")
        return

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                # الفصل الأسماء المؤهَّلة؛ created_at موجود في الجدولين.
                cur.execute("""
                    SELECT
                        j.data->>'telegram_user_id' AS user_id,
                        MIN(j.created_at) AS first_seen,
                        MAX(j.created_at) AS last_seen,
                        COUNT(*) AS jobs,
                        COALESCE(
                            MAX(NULLIF(j.data->>'telegram_username', '')),
                            ''
                        ) AS username,
                        COALESCE(
                            MAX(NULLIF(j.data->>'telegram_name', '')),
                            ''
                        ) AS name,
                        (b.user_id IS NOT NULL) AS blocked
                    FROM jobs j
                    LEFT JOIN blocked_users b
                        ON b.user_id = j.data->>'telegram_user_id'
                    WHERE j.data->>'telegram_user_id' IS NOT NULL
                      AND j.data->>'telegram_user_id' <> ''
                    GROUP BY 1, b.user_id
                    ORDER BY MIN(j.created_at) ASC
                """)
                rows = cur.fetchall()
    except Exception as e:
        print(f"[ALLUSERS] query failed: {e}", flush=True)
        telegram_send_message(chat_id, "⚠️ تعذّر قراءة المستخدمين.")
        return

    if not rows:
        telegram_send_message(
            chat_id,
            "لا يوجد مستخدمون مسجّلون منذ تأسيس البوت."
        )
        return

    blocked_total = sum(1 for row in rows if row.get("blocked"))
    lines = [
        f"👥 كل المستخدمين منذ التأسيس: {len(rows)}"
        + (f" (منهم محجوب: {blocked_total})" if blocked_total else "")
    ]

    for index, row in enumerate(rows, start=1):
        tg_id = row.get("user_id") or "غير معروف"
        username = str(row.get("username") or "").strip().lstrip("@")
        name = str(row.get("name") or "").strip()

        if not username:
            info = telegram_get_chat(tg_id)
            if info:
                username = str(
                    info.get("username") or ""
                ).strip().lstrip("@")
                fetched_name = " ".join(
                    part for part in (
                        str(info.get("first_name") or "").strip(),
                        str(info.get("last_name") or "").strip(),
                    ) if part
                )
                name = name or fetched_name
                _save_user_identity(tg_id, username, name)

        if username:
            who = f"@{username}"
            if name:
                who += f" ({name})"
        elif name:
            who = f"{name} (بلا اسم مستخدم)"
        else:
            who = "بلا اسم مستخدم"

        first_seen = row.get("first_seen")
        started = first_seen.strftime("%Y-%m-%d") if first_seen else "?"
        blocked = "🔒 " if row.get("blocked") else ""
        lines.append(
            f"{index}. {blocked}{who}\n"
            f"   🆔 {tg_id} — أول استخدام: {started}"
            f" — المهام: {row.get('jobs') or 0}"
        )

    # رسالة واحدة لا تتعدى 4096 محرف؛ نقسّم الأسماء على رسائل متتابعة.
    chunk = _USERS_CHUNK
    blocks = [lines[i:i + chunk] for i in range(0, len(lines), chunk)]
    for position, block in enumerate(blocks, start=1):
        if position > 1:
            block = [f"👤 طبقة {position} من {len(blocks)}"] + block
        try:
            telegram_send_message(chat_id, "\n".join(block))
        except Exception as e:
            print(f"[ALLUSERS] send chunk {position} failed: {e}", flush=True)
            break


def save_feedback(job_id, verdict):
    """تقييم واحد لكل مهمة."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO feedback (job_id, verdict) VALUES (%s, %s)",
                (str(job_id), verdict)
            )
            conn.commit()


def has_feedback(job_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM feedback WHERE job_id = %s",
                    (str(job_id),)
                )
                return cur.fetchone() is not None
    except Exception:
        # عند فشل القاعدة نمنع الإزعاج بدل تكرار الطلب.
        return True


def send_feedback_prompt(job_id, user_id):
    """تقييم يصل للمطوّر مباشرة، اختياري ولا يوقظ إزعاجًا للعميل."""
    user_id = int(user_id) if isinstance(user_id, str) else user_id
    if is_developer(user_id):
        return

    try:
        if has_feedback(job_id):
            return
    except Exception:
        return

    keyboard = {
        "inline_keyboard": [
            [
                {"text": "👍 ممتاز", "callback_data": f"fb:{job_id}:good"},
                {"text": "👌 جيد", "callback_data": f"fb:{job_id}:ok"},
                {"text": "👎 يحتاج تحسين", "callback_data": f"fb:{job_id}:bad"},
            ],
            [{"text": "💬 أرسل ملاحظة", "callback_data": f"fb:{job_id}:note"}],
        ]
    }
    try:
        telegram_send_message(
            user_id,
            "ساعد المطوّر في تطوير الخدمة: كيف كانت جودة التفريغ؟\n"
            "اختيارك يصل له مباشرة، وتقدر أيضًا ترسل ملاحظة نصية.",
            keyboard
        )
    except Exception as e:
        print(f"[FEEDBACK] prompt failed job={job_id}: {e}", flush=True)


def answer_feedback_callback(callback_id, choice, job_id, callback):
    """معالجة زر التقييم أو ملاحظة المستخدم."""
    sender = callback.get("from") or {}
    user_id = sender.get("id")
    job = get_job(job_id) or {}
    owner = job.get("telegram_user_id")

    if not job or (owner and str(owner) != str(user_id)):
        answer_callback(callback_id, "هذا التقييم غير متاح لك", alert=True)
        return

    if choice == "note":
        with _CONTACT_LOCK:
            _CONTACT_PENDING[str(user_id)] = time.time()
        answer_callback(callback_id, "اكتب ملاحظتك في الرسالة التالية")
        telegram_send_message(user_id, CONTACT_INTRO)
        return

    if choice not in FEEDBACK_VERDICTS:
        answer_callback(callback_id, "خيار غير معروف", alert=True)
        return

    if has_feedback(job_id):
        answer_callback(callback_id, "سُجّل تقييمك سابقًا، شكرًا 🙏")
        return

    try:
        save_feedback(job_id, choice)
    except Exception as e:
        print(f"[FEEDBACK] save failed job={job_id}: {e}", flush=True)
        answer_callback(callback_id, "تعذّر حفظ التقييم الآن", alert=True)
        return

    answer_callback(callback_id, "شكرًا على تقييمك 🙏")

    sender_name = _display_name(sender)
    username = (sender.get("username") or "").strip()
    handle = f"@{username}" if username else "بلا اسم مستخدم"
    body = (
        "📊 تغذية راجعة جديدة\n"
        f"النتيجة: {FEEDBACK_VERDICTS[choice]}\n"
        f"الملف: {job_label(job, 'تفريغ')}\n"
        f"👤 {sender_name} ({handle}) — ID: {user_id}"
    )
    try:
        telegram_send_message(DEVELOPER_CHAT_ID, body)
    except Exception as e:
        print(f"[FEEDBACK] notify failed: {e}", flush=True)


# التواصل يمرّ عبر البوت حتى لا تُنشر بيانات المطوّر الشخصية.
# الانتظار في الذاكرة يكفي لأن الخدمة تعمل بعامل واحد (workers=1).
_CONTACT_PENDING = {}
_CONTACT_REPLIES = {}
_CONTACT_LOCK = threading.Lock()
CONTACT_WINDOW_SECONDS = 30 * 60
CONTACT_MAX_CHARS = 3000

# مراسلة المطوّر للمستخدمين دون أن يراسلوه أولًا.
# _PENDING_TO: وضع «سأرسل رسالتي التالية إلى عميل X».
# _OUTREACH_REPLIES: رسالة البوت الصادرة → صاحبها، توجّه ردود المستخدم.
_OUTREACH_LOCK = threading.Lock()
_PENDING_TO = {}
_OUTREACH_REPLIES = {}
_MESSAGES_MODE = "outreach"

MSG_MAX_CHARS = 3000

_ITEM_COUNT = threading.Lock()  # لا يُستخدم؛ نُبقي الاسم قصيرًا للسجل فقط.
_USERS_CHUNK = 18  # عدد الأسماء في كل رسالة أسماء بعيدة.


def contributors_text():
    """سطر المساهمين، قابل للتعديل دون لمس الكود."""
    custom = os.environ.get("CONTRIBUTORS", "").strip()
    if custom:
        return custom
    return "عدة نماذج لغوية ساعدت في التصميم والكتابة والاختبار"


JOBS_FILE = Path("/tmp/waqqad_jobs.json")
jobs_lock = threading.Lock()

SUBMIT_TIMEOUT = 1800

# ============================================================
# حماية الطلبات: مصادقة تيليجرام + أسرار الويبهوك
# ============================================================

TELEGRAM_WEBHOOK_SECRET = os.environ.get(
    "TELEGRAM_WEBHOOK_SECRET", ""
).strip()
WARRAQ_WEBHOOK_SECRET = os.environ.get(
    "WARRAQ_WEBHOOK_SECRET", ""
).strip()
TELEGRAM_INITDATA_MAX_AGE = int(
    os.environ.get("TELEGRAM_INITDATA_MAX_AGE", "86400")
)


def _verify_telegram_init_data(init_data, max_age=None):
    """التحقق من توقيع Telegram WebApp initData."""
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is not configured")
    if not init_data:
        raise ValueError("Missing Telegram initData")

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    their_hash = pairs.pop("hash", None)
    if not their_hash:
        raise ValueError("Missing Telegram hash")

    data_check_string = "\n".join(
        f"{key}={pairs[key]}" for key in sorted(pairs)
    )
    secret_key = hmac.new(
        b"WebAppData",
        TELEGRAM_BOT_TOKEN.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    expected_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected_hash, their_hash):
        raise ValueError("Invalid Telegram signature")

    now = int(time.time())
    auth_date = int(pairs.get("auth_date", "0") or 0)
    limit = TELEGRAM_INITDATA_MAX_AGE if max_age is None else max_age
    if not auth_date or now - auth_date > limit or auth_date - now > 300:
        raise ValueError("Expired Telegram session")

    user = json.loads(pairs.get("user") or "{}")
    if not user.get("id"):
        raise ValueError("Missing Telegram user")
    return user


def _current_telegram_user():
    return _verify_telegram_init_data(
        request.headers.get("X-Telegram-Init-Data", "")
    )


def _unauthorized(error):
    return jsonify({
        "error": "Unauthorized",
        "detail": str(error),
    }), 401


def _job_belongs_to(job, user):
    return (
        str(job.get("telegram_user_id") or "")
        == str(user["id"])
    )


def _callback_token(job_id):
    key = (
        TELEGRAM_BOT_TOKEN
        or DEEPGRAM_API_KEY
        or "waqqad"
    ).encode("utf-8")
    return hmac.new(
        key,
        f"deepgram:{job_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _verify_callback_token(job_id, token):
    expected = _callback_token(job_id)
    return bool(token) and hmac.compare_digest(expected, token)


def _webhook_secret_ok(configured):
    """إذا لم يُضبط السر نسمح مؤقتًا للتوافق مع الإعداد الحالي."""
    if not configured:
        return True
    received = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token", ""
    )
    return hmac.compare_digest(received, configured)


def ensure_webhook_updates():
    """تضمن أن تليجرام يرسل ضغطات الأزرار (callback_query).

    سبب وجود هذه الدالة عملي: إن سُجّل الويبهوك سابقًا بقائمة
    allowed_updates لا تحتوي callback_query، فإن أزرار TXT/DOCX لا تصل
    إلى الخادم أصلًا، فيبدو الزر «ميتًا» بلا أي خطأ في السجلات.
    """
    if not TELEGRAM_BOT_TOKEN:
        return

    target_url = f"{BASE_URL.rstrip('/')}/telegram/transcriber"
    api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

    try:
        info = requests.get(f"{api}/getWebhookInfo", timeout=20).json()
    except Exception as e:
        print(f"[WEBHOOK CHECK] Could not read webhook info: {e}", flush=True)
        return

    if not info.get("ok"):
        print(
            f"[WEBHOOK CHECK] getWebhookInfo failed: {info.get('description')}",
            flush=True
        )
        return

    result = info.get("result") or {}
    allowed = result.get("allowed_updates") or []
    current_url = (result.get("url") or "").strip() or target_url

    # قائمة فارغة تعني «كل التحديثات»؛ لا نتدخل حينها.
    if not allowed or "callback_query" in allowed:
        print(
            "[WEBHOOK CHECK] callback_query delivery is enabled.",
            flush=True
        )
        return

    wanted = sorted(set(allowed) | {"message", "callback_query"})
    payload = {
        "url": current_url,
        "allowed_updates": json.dumps(wanted),
        "drop_pending_updates": "false",
    }
    if TELEGRAM_WEBHOOK_SECRET:
        payload["secret_token"] = TELEGRAM_WEBHOOK_SECRET

    try:
        response = requests.post(
            f"{api}/setWebhook", data=payload, timeout=30
        ).json()
    except Exception as e:
        print(f"[WEBHOOK CHECK] Could not update webhook: {e}", flush=True)
        return

    if response.get("ok"):
        print(
            f"[WEBHOOK CHECK] Enabled callback_query. allowed_updates={wanted}",
            flush=True
        )
    else:
        print(
            "[WEBHOOK CHECK] setWebhook refused: "
            f"{response.get('description')}",
            flush=True
        )


# ============================================================
# تخزين المهام - PostgreSQL (Neon)
# ============================================================

import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.environ.get("DATABASE_URL")

def get_db_connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def _safe_upload_name(original_name):
    """اسم ملف آمن مع الحفاظ على امتداده، دون قيود على الامتدادات."""
    raw = (original_name or "").strip()
    suffix = Path(raw).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
        suffix = ""
    stem = secure_filename(Path(raw).stem)[:80].strip("._")
    if not stem:
        stem = f"upload_{uuid.uuid4().hex[:12]}"
    return f"{stem}{suffix}"


def init_db():
    """إنشاء جدول المهام والإعدادات والحظر والتغذية الراجعة."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    data JSONB NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS blocked_users (
                    user_id TEXT PRIMARY KEY,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS feedback (
                    job_id TEXT PRIMARY KEY,
                    verdict TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            conn.commit()
    print("[DB] Jobs, settings, blocked and feedback tables ready", flush=True)


YOUTUBE_COOKIES_KEY = "youtube_cookies"
MAX_COOKIES_BYTES = 512 * 1024


def set_setting(key, value):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_settings (key, value)
                VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE
                SET value = EXCLUDED.value,
                    updated_at = NOW()
                """,
                (key, value)
            )
            conn.commit()


def get_setting(key):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM app_settings WHERE key = %s",
                (key,)
            )
            row = cur.fetchone()
            return row["value"] if row else None


def get_setting_row(key):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value, updated_at FROM app_settings WHERE key = %s",
                (key,)
            )
            return cur.fetchone()


def delete_setting(key):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app_settings WHERE key = %s", (key,))
            conn.commit()

def fail_orphaned_jobs():
    """المهام التي كانت تعمل قبل إعادة التشغيل لن تكتمل."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE jobs
                SET data = jsonb_set(
                        jsonb_set(
                            jsonb_set(data, '{status}', '"failed"'),
                            '{stage}', '"error"'
                        ),
                        '{error}',
                        to_jsonb(
                            'انقطع الاتصال قبل اكتمال المهمة (إعادة تشغيل الخدمة).'::text
                        )
                    ),
                    updated_at = NOW()
                WHERE data->>'status' IN ('queued', 'processing', 'uploading')
            """)
            conn.commit()
    print("[DB] Orphaned jobs marked as failed", flush=True)


def cleanup_stale_workdirs():
    """حذف مجلدات العمل المؤقتة من تشغيل سابق."""
    removed = 0
    for path in Path("/tmp").glob("waqqad_*"):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    if removed:
        print(f"[CLEANUP] Removed {removed} stale work dirs", flush=True)


# تهيئة الجدول عند بدء التشغيل
try:
    init_db()
    fail_orphaned_jobs()
except Exception as e:
    print(f"[DB] Warning: could not initialize table: {e}", flush=True)
finally:
    try:
        cleanup_stale_workdirs()
    except Exception as e:
        print(f"[CLEANUP] Warning: {e}", flush=True)

# جدول النصوص: أساس التصدير والبحث والأرشيف
try:
    if transcript_store.init_table():
        print("[DB] Transcripts table ready", flush=True)
except Exception as e:
    print(
        f"[DB] Warning: could not initialize transcripts table: {e}",
        flush=True
    )

if not TELEGRAM_WEBHOOK_SECRET or not WARRAQ_WEBHOOK_SECRET:
    print(
        "[SECURITY] Webhook secrets are not fully configured. "
        "Set TELEGRAM_WEBHOOK_SECRET and WARRAQ_WEBHOOK_SECRET.",
        flush=True
    )

# أزرار التصدير لا تعمل إن لم تكن callback_query ضمن allowed_updates.
# نجري الفحص في خيط خلفي حتى لا يتأخر إقلاع التطبيق أو الاختبارات.
def _webhook_check_startup():
    try:
        ensure_webhook_updates()
    except Exception as e:
        print(f"[WEBHOOK CHECK] Warning: {e}", flush=True)

    # بيئة تنزيل يوتيوب في السجل: نسخة yt-dlp وتوفر بيئة JS.
    try:
        url_media.log_ytdlp_runtime()
    except Exception as e:
        print(f"[YTDLP] runtime log warning: {e}", flush=True)

    # قائمة الأوامر بعد علامة /: تسجيل غير متكرّر ولشاشة قليلة التكلفة.
    try:
        register_bot_commands()
    except Exception as e:
        print(f"[MENU] Warning: {e}", flush=True)


threading.Thread(
    target=_webhook_check_startup,
    name="webhook-check",
    daemon=True
).start()

def create_job(job_id, data):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO jobs (job_id, data)
                VALUES (%s, %s)
                ON CONFLICT (job_id) DO UPDATE
                SET data = EXCLUDED.data,
                    updated_at = NOW()
                """,
                (job_id, json.dumps(data))
            )
            conn.commit()

def update_job(job_id, **updates):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT data FROM jobs WHERE job_id = %s FOR UPDATE",
                (job_id,)
            )
            row = cur.fetchone()
            if not row:
                return

            current = row["data"]
            if isinstance(current, str):
                current = json.loads(current)

            current.update(updates)

            cur.execute(
                """
                UPDATE jobs
                SET data = %s,
                    updated_at = NOW()
                WHERE job_id = %s
                """,
                (json.dumps(current), job_id)
            )
            conn.commit()

def get_job(job_id):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT data FROM jobs WHERE job_id = %s",
                (job_id,)
            )
            row = cur.fetchone()
            if not row:
                return None

            data = row["data"]
            if isinstance(data, str):
                return json.loads(data)
            return data


def job_label(job, fallback="ملف"):
    """اسم قصير يميّز المهمة في الرسائل المتزامنة."""
    name = (job or {}).get("original_filename") or fallback
    name = Path(str(name)).name
    if len(name) > 40:
        name = name[:37] + "…"
    return name


def telegram_identity(sender):
    """اسم المستخدم واسمه الظاهر، ليظهرا في السجل بدل المعرّف وحده."""
    sender = sender or {}
    username = str(sender.get("username") or "").strip().lstrip("@")
    full_name = " ".join(
        part for part in (
            str(sender.get("first_name") or "").strip(),
            str(sender.get("last_name") or "").strip(),
        ) if part
    )
    return {
        "telegram_username": username,
        "telegram_name": full_name,
    }


def new_job_payload(
    job_id,
    original_filename,
    file_size,
    telegram_user_id,
    source="web",
    telegram_username="",
    telegram_name="",
    original_url=None
):
    """بنية موحّدة للمهمة، سواء جاءت من الويب أو من المحادثة."""
    return {
        "job_id": job_id,
        "status": "queued",
        "stage": "queued",
        "error": None,
        "output_file": None,
        "original_filename": original_filename,
        "original_url": original_url,
        "file_size_bytes": file_size,
        "started_at": time.time(),
        "completed_at": None,
        "processing_seconds": None,
        "deepgram_request_id": None,
        "engine": None,
        "fallback_used": False,
        "fallback_reason": None,
        "cohere_key_index": None,
        "cohere_keys_used": [],
        "current_part": None,
        "total_parts": None,
        "telegram_user_id": telegram_user_id,
        "telegram_username": telegram_username or "",
        "telegram_name": telegram_name or "",
        "telegram_sent": False,
        "telegram_error": None,
        "source": source,
    }

# ============================================================
# محرك Cohere الأساسي + Deepgram كخط احتياط
# ============================================================

def _audio_content_type(path):
    suffix = Path(path).suffix.lower()
    if suffix == ".wav":
        return "audio/wav"
    if suffix == ".ogg":
        return "audio/ogg"
    if suffix == ".flac":
        return "audio/flac"
    return "audio/mpeg"


def _probe_audio_duration(path):
    """مدة الملف بالثواني عبر ffprobe، أو None إذا تعذر."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            duration = float(result.stdout.strip())
            if duration > 0:
                return duration
    except Exception as e:
        print(f"[COHERE] ffprobe failed: {e}", flush=True)

    return None


def _run_ffmpeg(cmd, job_id):
    print(
        f"[COHERE] Running ffmpeg for job {job_id}...",
        flush=True
    )

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        raise RuntimeError(
            "FFmpeg failed: "
            + (result.stderr[-1500:] or "unknown error")
        )


def _clean_parts_dir(parts_dir):
    shutil.rmtree(parts_dir, ignore_errors=True)
    parts_dir.mkdir(parents=True, exist_ok=True)


def prepare_cohere_parts(job_id, input_file):
    """يجهّز قطع Cohere بأسرع مسار ممكن دون تغيير النص أو الصوت."""
    work_dir = Path("/tmp") / f"waqqad_{job_id}"
    parts_dir = work_dir / "cohere_parts"
    parts_dir.mkdir(parents=True, exist_ok=True)

    output_pattern = parts_dir / "part_%03d.mp3"
    suffix = input_file.suffix.lower()
    file_size = input_file.stat().st_size

    update_job(
        job_id,
        status="processing",
        stage="preparing_audio",
        engine="cohere"
    )

    # 1) ملف صغير بصيغة مدعومة: يُرسل كما هو دون أي تفريغ أو تحويل.
    if (
        file_size <= COHERE_MAX_FILE_BYTES
        and suffix in COHERE_DIRECT_EXTENSIONS
    ):
        print(
            f"[COHERE] Direct upload for job {job_id} "
            f"({file_size / 1_000_000:.2f}MB, {suffix or 'no ext'})",
            flush=True
        )
        update_job(
            job_id,
            total_parts=1,
            prepare_mode="direct"
        )
        return [input_file]

    # 2) MP3 كبير: تقطيع بنسخ الضغط دون إعادة ترميز (سريع جداً).
    duration = None
    if suffix in COHERE_COPY_EXTENSIONS:
        duration = _probe_audio_duration(input_file)

    if duration:
        target_seconds = int(
            (COHERE_MAX_FILE_BYTES * 0.90 / file_size) * duration
        )
        target_seconds = max(60, target_seconds)

        copy_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-i", str(input_file),
            "-vn",
            "-map", "0:a:0",
            "-c:a", "copy",
            "-f", "segment",
            "-segment_time", str(target_seconds),
            "-reset_timestamps", "1",
            str(output_pattern),
        ]

        try:
            _run_ffmpeg(copy_cmd, job_id)
            parts = sorted(parts_dir.glob("part_*.mp3"))
            oversized = [
                p for p in parts
                if p.stat().st_size > COHERE_MAX_FILE_BYTES
            ]
            if parts and not oversized:
                print(
                    f"[COHERE] Fast-split job {job_id} into "
                    f"{len(parts)} part(s) without re-encoding",
                    flush=True
                )
                update_job(
                    job_id,
                    total_parts=len(parts),
                    prepare_mode="copy"
                )
                return parts

            print(
                f"[COHERE] Copy split unusable for job {job_id} "
                f"(parts={len(parts)}, oversized={len(oversized)}); "
                "falling back to re-encode",
                flush=True
            )
        except Exception as e:
            print(
                f"[COHERE] Copy split failed for job {job_id}: {e}; "
                "falling back to re-encode",
                flush=True
            )

        _clean_parts_dir(parts_dir)

    # 3) المسار الموثوق: تحويل وضغط وتقسيم في مرور واحد.
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", str(input_file),
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-c:a", "libmp3lame",
        "-b:a", "24k",
        "-f", "segment",
        "-segment_time", str(COHERE_SEGMENT_SECONDS),
        "-reset_timestamps", "1",
        str(output_pattern),
    ]

    _run_ffmpeg(cmd, job_id)

    parts = sorted(parts_dir.glob("part_*.mp3"))

    if not parts:
        raise RuntimeError("FFmpeg produced no audio parts")

    # حماية إضافية إذا خرجت قطعة أكبر من حد Cohere.
    oversized = [
        p for p in parts
        if p.stat().st_size > COHERE_MAX_FILE_BYTES
    ]
    if oversized:
        raise RuntimeError(
            "Prepared Cohere part exceeds safe size limit: "
            + ", ".join(
                f"{p.name}={p.stat().st_size / 1_000_000:.2f}MB"
                for p in oversized
            )
        )

    print(
        f"[COHERE] Prepared {len(parts)} part(s): "
        + ", ".join(
            f"{p.name}={p.stat().st_size / 1_000_000:.2f}MB"
            for p in parts
        ),
        flush=True
    )

    update_job(
        job_id,
        total_parts=len(parts),
        prepare_mode="reencode"
    )
    return parts


class CohereUnavailableError(RuntimeError):
    """Cohere/keys are genuinely unavailable; Deepgram fallback is allowed."""
    pass


class LocalProcessingError(RuntimeError):
    """Input/config/code problem; Deepgram fallback must NOT be consumed."""
    pass


def _cohere_key_order():
    """ترتيب دائري يبدأ كل طلب بمفتاح مختلف، بلا حد برمجي لعدد المفاتيح."""
    global cohere_key_cursor

    count = len(COHERE_API_KEYS)
    if count == 0:
        return []

    with cohere_key_lock:
        start = cohere_key_cursor % count
        cohere_key_cursor = (cohere_key_cursor + 1) % count

    return [
        ((start + offset) % count, COHERE_API_KEYS[(start + offset) % count])
        for offset in range(count)
    ]


def cohere_transcribe_part(job_id, part, part_index, part_count):
    """
    يبدأ بمفتاح مختلف بطريقة Round-robin.
    عند خطأ خاص بالمفتاح/الحصة يجرب بقية المفاتيح.
    أخطاء الطلب/الملف لا تشغّل Deepgram كي لا نهدر رصيده بسبب bug محلي.
    """
    if not COHERE_API_KEYS:
        raise CohereUnavailableError(
            "No Cohere API keys configured"
        )

    key_order = _cohere_key_order()
    key_failures = []
    service_failure = None

    for zero_index, api_key in key_order:
        key_index = zero_index + 1

        update_job(
            job_id,
            status="processing",
            stage="transcribing_cohere",
            engine="cohere",
            current_part=part_index,
            total_parts=part_count,
            cohere_key_index=key_index
        )

        print(
            f"[COHERE] Job {job_id}: part "
            f"{part_index}/{part_count}, key #{key_index}",
            flush=True
        )

        try:
            with open(part, "rb") as audio:
                response = requests.post(
                    COHERE_URL,
                    headers={
                        "Authorization": f"Bearer {api_key}"
                    },
                    data={
                        "model": COHERE_MODEL,
                        "language": COHERE_LANGUAGE,
                    },
                    files={
                        "file": (
                            part.name,
                            audio,
                            _audio_content_type(part)
                        )
                    },
                    timeout=COHERE_TIMEOUT
                )
        except requests.RequestException as e:
            # تعذر الوصول إلى Cohere نفسه: يسمح بالـ fallback.
            raise CohereUnavailableError(
                f"Cohere network error: {e}"
            ) from e

        if response.status_code == 200:
            data = response.json()
            transcript = data.get("text")

            if not isinstance(transcript, str):
                # استجابة 200 غير سليمة = لا نحرق Deepgram بسبب خلل غير مفهوم.
                raise LocalProcessingError(
                    "Cohere returned HTTP 200 without transcript text"
                )

            return transcript, key_index

        body = response.text[:1000]
        error_text = f"Cohere HTTP {response.status_code}: {body}"

        print(
            f"[COHERE] Key #{key_index} failed: "
            f"HTTP {response.status_code}",
            flush=True
        )

        if response.status_code in COHERE_KEY_FAILOVER_STATUSES:
            key_failures.append(
                f"key #{key_index}: HTTP {response.status_code}"
            )
            continue

        if response.status_code in COHERE_SERVICE_FALLBACK_STATUSES:
            service_failure = error_text
            break

        # 400/404/422/499 ونحوها قد تعني model/parameters/file/bug:
        # تفشل المهمة ولا نستهلك Deepgram.
        raise LocalProcessingError(error_text)

    if service_failure:
        raise CohereUnavailableError(service_failure)

    raise CohereUnavailableError(
        "All Cohere keys unavailable: "
        + "; ".join(key_failures)
    )

def finalize_transcript(job_id, transcript, engine):
    """Save the transcript using the original filename and send a short summary."""
    work_dir = Path("/tmp") / f"waqqad_{job_id}"
    work_dir.mkdir(parents=True, exist_ok=True)

    job = get_job(job_id) or {}
    original_filename = job.get("original_filename") or "transcription"
    download_name = transcript_download_name(original_filename)

    output_file = work_dir / download_name
    output_file.write_text(transcript, encoding="utf-8")

    telegram_user_id = job.get("telegram_user_id")
    started_at = job.get("started_at")
    completed_at = time.time()
    processing_seconds = (
        max(0.0, completed_at - float(started_at))
        if started_at is not None else None
    )

    engine_label = (
        "Cohere Arabic"
        if engine == "cohere"
        else "Deepgram (احتياطي)"
    )
    total_parts = job.get("total_parts") or 1

    if processing_seconds is None:
        duration_text = "غير متاح"
    elif processing_seconds < 60:
        duration_text = f"{processing_seconds:.1f} ثانية"
    else:
        minutes = int(processing_seconds // 60)
        seconds = int(round(processing_seconds % 60))
        duration_text = f"{minutes} د {seconds} ث"

    caption = (
        f"✅ اكتمل تفريغ: {job_label(job, original_filename)}\n"
        f"⏱️ وقت المعالجة: {duration_text}\n"
        f"⚙️ المحرك: {engine_label}\n"
        f"🧩 الأجزاء: {total_parts}"
    )

    update_job(
        job_id,
        status="processing",
        stage="sending_to_telegram",
        engine=engine,
        output_file=str(output_file),
        output_name=download_name,
        processing_seconds=processing_seconds
    )

    # حفظ النص في قاعدة البيانات: أساس البحث والتصدير عند الطلب.
    transcript_store.save_transcript(
        job_id,
        telegram_user_id,
        "raw",
        transcript,
        title=Path(original_filename).stem or None,
        duration_secs=(
            job.get("duration_seconds")
            or job.get("audio_duration_seconds")
        )
    )

    _archive_transcript(
        job_id,
        transcript,
        {**job, "output_name": download_name}
    )

    keyboard = _result_keyboard(
        job_id,
        transcript_store.available_kinds(job_id) or {"raw"}
    )

    caption = (
        caption
        + "\n\n💾 محفوظ في أرشيفك — /find للبحث عنه لاحقًا."
    )

    try:
        if telegram_user_id:
            send_transcript_to_telegram(
                telegram_user_id,
                output_file,
                download_name=download_name,
                caption=caption,
                reply_markup=keyboard
            )
            telegram_sent = True
            telegram_error = None

            # بعد وصول النتيجة تتبخّر رسالة الحالة.
            try:
                delete_status_message(job_id)
            except Exception:
                pass
        else:
            telegram_sent = False
            telegram_error = "No Telegram user ID stored for this job"
    except Exception as e:
        telegram_sent = False
        telegram_error = str(e)
        print(f"[TELEGRAM ERROR] Job {job_id}: {e}", flush=True)
        try:
            update_status_message(
                job_id,
                f"❌ {job_label(job, original_filename)}: "
                "تعذّر إرسال النتيجة.\n"
                "التفريغ محفوظ، ويمكنك تنزيله بأمر /find."
            )
        except Exception:
            pass

    update_job(
        job_id,
        status="completed",
        stage="done",
        engine=engine,
        output_file=str(output_file),
        output_name=download_name,
        completed_at=completed_at,
        processing_seconds=processing_seconds,
        telegram_sent=telegram_sent,
        telegram_error=telegram_error
    )

    log_transcription_success(job_id)

    # --- إطلاق التنقيح التلقائي في الخلفية (لا يوقف الرد) ---
    if REFINE_ENABLED:
        try:
            thread = threading.Thread(
                target=send_refined_versions_async,
                args=(job_id, str(output_file)),
                daemon=True
            )
            thread.start()
            print(f"[AUTO-REFINE] Thread started for job {job_id}", flush=True)
        except Exception as e:
            print(f"[AUTO-REFINE] Failed to start thread: {e}", flush=True)

def submit_to_deepgram(job_id, input_file, fallback_reason=None):
    """Deepgram Nova-3: المحرك الاحتياطي، مع callback كما في V3."""
    if not DEEPGRAM_API_KEY:
        update_job(
            job_id,
            status="failed",
            stage="error",
            error="DEEPGRAM_API_KEY is not configured"
        )
        log_transcription_failure(job_id)
        return

    try:
        update_job(
            job_id,
            status="uploading",
            stage="uploading_to_deepgram",
            engine="deepgram",
            fallback_used=bool(fallback_reason),
            fallback_reason=fallback_reason
        )

        content_type = (
            mimetypes.guess_type(str(input_file))[0]
            or "audio/mpeg"
        )

        callback_url = (
            f"{BASE_URL}/deepgram-callback/{job_id}"
            f"?token={_callback_token(job_id)}"
        )
        url = "https://api.deepgram.com/v1/listen"

        params = {
            "model": "nova-3",
            "language": "ar",
            "smart_format": "true",
            "callback": callback_url
        }

        headers = {
            "Authorization": f"Token {DEEPGRAM_API_KEY}",
            "Content-Type": content_type
        }

        print(
            f"[DEEPGRAM] Uploading job {job_id} "
            f"(fallback={bool(fallback_reason)})...",
            flush=True
        )

        with open(input_file, "rb") as audio:
            response = requests.post(
                url,
                params=params,
                headers=headers,
                data=audio,
                timeout=SUBMIT_TIMEOUT
            )

        print(
            f"[DEEPGRAM] Submit HTTP {response.status_code}",
            flush=True
        )

        if response.status_code not in (200, 202):
            print(response.text[:1000], flush=True)
            update_job(
                job_id,
                status="failed",
                stage="error",
                error=f"Deepgram submit HTTP {response.status_code}"
            )
            log_transcription_failure(job_id)
            return

        data = response.json()
        request_id = data.get("request_id")

        update_job(
            job_id,
            status="processing",
            stage="waiting_deepgram",
            engine="deepgram",
            deepgram_request_id=request_id
        )

        print(
            f"[DEEPGRAM] Job {job_id} accepted. "
            f"request_id={request_id}",
            flush=True
        )

    except Exception as e:
        print(
            f"[ERROR] Deepgram submit failed for job {job_id}: {e}",
            flush=True
        )
        update_job(
            job_id,
            status="failed",
            stage="error",
            error=str(e)
        )
        log_transcription_failure(job_id)

    finally:
        # بعد قبول Deepgram للرفع لا نحتاج الملف الأصلي محليًا.
        try:
            input_file.unlink(missing_ok=True)
        except Exception:
            pass


def process_transcription_job(job_id, input_file):
    """
    Cohere هو المحرك الأساسي.
    Deepgram يعمل فقط إذا كانت Cohere/مفاتيحها غير متاحة فعلًا.
    أخطاء FFmpeg/الملف/الطلب البرمجي تفشل المهمة ولا تحرق رصيد Deepgram.
    """
    if not COHERE_API_KEYS:
        reason = "No Cohere API keys configured"
        print(f"[COHERE] {reason}; using Deepgram.", flush=True)
        submit_to_deepgram(
            job_id,
            input_file,
            fallback_reason=reason
        )
        return

    parts_dir = (
        Path("/tmp")
        / f"waqqad_{job_id}"
        / "cohere_parts"
    )

    try:
        # إن فشل FFmpeg هنا فهو خطأ محلي، فلا Deepgram.
        parts = prepare_cohere_parts(job_id, input_file)

        transcripts = []
        used_keys = []

        for index, part in enumerate(parts, start=1):
            transcript, key_index = cohere_transcribe_part(
                job_id,
                part,
                index,
                len(parts)
            )
            transcripts.append(transcript.strip())
            used_keys.append(key_index)

        update_job(
            job_id,
            status="processing",
            stage="merging",
            engine="cohere",
            cohere_keys_used=sorted(set(used_keys))
        )

        final_text = "\n\n".join(
            t for t in transcripts if t
        )

        if not final_text.strip():
            raise LocalProcessingError(
                "Cohere returned an empty transcript"
            )

        finalize_transcript(
            job_id,
            final_text,
            engine="cohere"
        )

        update_job(
            job_id,
            fallback_used=False,
            fallback_reason=None
        )

        print(
            f"[COHERE] Job {job_id} completed successfully.",
            flush=True
        )

        try:
            input_file.unlink(missing_ok=True)
        except Exception:
            pass

    except CohereUnavailableError as e:
        reason = str(e)

        print(
            f"[COHERE UNAVAILABLE] Job {job_id}: {reason}",
            flush=True
        )

        update_job(
            job_id,
            status="processing",
            stage="fallback_to_deepgram",
            engine="deepgram",
            fallback_used=True,
            fallback_reason=reason
        )

        # لا نمزج محركين: Deepgram يعيد تفريغ الملف الأصلي كاملًا.
        submit_to_deepgram(
            job_id,
            input_file,
            fallback_reason=reason
        )

    except Exception as e:
        # FFmpeg، ملف تالف، 400/422، bug... لا Deepgram.
        error = str(e)

        print(
            f"[LOCAL/REQUEST ERROR] Job {job_id}: {error}",
            flush=True
        )

        update_job(
            job_id,
            status="failed",
            stage="error",
            error=error,
            fallback_used=False,
            fallback_reason=None
        )
        log_transcription_failure(job_id)

    finally:
        shutil.rmtree(
            parts_dir,
            ignore_errors=True
        )


def transcript_download_name(original_filename):
    """Build a TXT filename from the original upload name."""
    name = Path(original_filename or "transcription").name
    stem = Path(name).stem.strip() or "transcription"
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    return f"{stem}.txt"


def telegram_send_message(chat_id, text, reply_markup=None, parse_mode=None):
    """Send a normal Telegram message from the transcriber bot."""
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")

    payload = {
        "chat_id": chat_id,
        "text": text,
    }
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(
            reply_markup,
            ensure_ascii=False
        )
    if parse_mode:
        payload["parse_mode"] = parse_mode

    response = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        data=payload,
        timeout=30
    )
    print(
        f"[TRANSCRIBER CHAT] Telegram HTTP {response.status_code}: "
        f"{response.text[:500]}",
        flush=True
    )

    data = response.json() if response.content else {}

    if response.status_code != 200 or not data.get("ok"):
        raise RuntimeError(
            f"Telegram sendMessage HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )

    return (data.get("result") or {}).get("message_id")


def telegram_edit_message(chat_id, message_id, text):
    """تعديل رسالة قائمة. «not modified» ليست خطأ."""
    if not chat_id or not message_id:
        return False

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/"
            "editMessageText",
            data={
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text[:4096],
                "disable_web_page_preview": "true",
            },
            timeout=20
        )
        data = response.json() if response.content else {}
        description = str(data.get("description") or "")
        if data.get("ok") or "message is not modified" in description:
            return True

        print(
            f"[STATUS] edit failed HTTP {response.status_code}: "
            f"{description[:200]}",
            flush=True
        )
    except Exception as e:
        print(f"[STATUS] edit error: {e}", flush=True)

    return False


def telegram_delete_message(chat_id, message_id):
    """حذف رسالة أرسلها البوت. الفشل غير قاتل."""
    if not chat_id or not message_id:
        return False

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/"
            "deleteMessage",
            data={"chat_id": chat_id, "message_id": message_id},
            timeout=20
        )
        data = response.json() if response.content else {}
        if data.get("ok"):
            return True
        print(
            f"[STATUS] delete failed HTTP {response.status_code}: "
            f"{str(data.get('description'))[:200]}",
            flush=True
        )
    except Exception as e:
        print(f"[STATUS] delete error: {e}", flush=True)

    return False


def _status_keys(prefix):
    return f"{prefix}_chat_id", f"{prefix}_message_id"


def send_status_message(job_id, chat_id, text, prefix="status"):
    """يرسل رسالة حالة ويحفظ معرّفها في المهمة لتُعدَّل أو تُحذف لاحقًا."""
    if not chat_id:
        return None

    try:
        message_id = telegram_send_message(chat_id, text)
    except Exception as e:
        print(f"[STATUS] send failed: {e}", flush=True)
        return None

    if message_id:
        chat_key, message_key = _status_keys(prefix)
        update_job(
            job_id,
            **{chat_key: chat_id, message_key: message_id}
        )

    return message_id


def update_status_message(job_id, text, prefix="status"):
    """يعدّل رسالة الحالة نفسها، فلا تتراكم رسائل لكل مرحلة."""
    job = get_job(job_id) or {}
    chat_key, message_key = _status_keys(prefix)
    chat_id = job.get(chat_key)
    message_id = job.get(message_key)

    if chat_id and message_id:
        if telegram_edit_message(chat_id, message_id, text):
            return True
        # إن تعذّر التعديل (رسالة قديمة أو محذوفة) نرسل بديلًا واحدًا.
        print(
            f"[STATUS] falling back to a new message for job {job_id}",
            flush=True
        )

    return send_status_message(
        job_id,
        chat_id or job.get("telegram_user_id"),
        text,
        prefix
    ) is not None


def delete_status_message(job_id, prefix="status"):
    """تُبخّر رسالة الحالة بعد وصول النتيجة."""
    job = get_job(job_id) or {}
    chat_key, message_key = _status_keys(prefix)
    chat_id = job.get(chat_key)
    message_id = job.get(message_key)

    deleted = telegram_delete_message(chat_id, message_id)

    update_job(job_id, **{chat_key: None, message_key: None})
    return deleted


def _format_duration(seconds):
    if seconds is None:
        return "غير متاح"
    seconds = max(0, int(round(float(seconds))))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours} س {minutes} د {secs} ث"
    if minutes:
        return f"{minutes} د {secs} ث"
    return f"{secs} ث"


def send_transcriber_log(text):
    """Send metadata-only telemetry to the private Telegram log channel."""
    if not TRANSCRIBER_LOG_CHAT_ID:
        return
    try:
        telegram_send_message(TRANSCRIBER_LOG_CHAT_ID, text)
    except Exception as e:
        # Logging must never make a transcription fail.
        print(
            f"[TRANSCRIBER LOG ERROR] {type(e).__name__}: {e}",
            flush=True
        )


def _job_user_line(job):
    """سطر موحّد في السجل: الاسم واسم المستخدم والمعرّف."""
    user_id = job.get("telegram_user_id") or "غير معروف"
    username = str(job.get("telegram_username") or "").strip().lstrip("@")
    name = str(job.get("telegram_name") or "").strip()

    if username:
        who = f"@{username}"
        if name:
            who += f" ({name})"
    elif name:
        who = f"{name} (بلا اسم مستخدم)"
    else:
        who = "بلا اسم مستخدم"

    return f"👤 المستخدم: {who} — ID: {user_id}"


def log_transcription_success(job_id):
    job = get_job(job_id) or {}
    filename = job.get("original_filename") or "غير معروف"
    file_size = job.get("file_size_bytes")
    size_text = (
        f"{file_size / (1024 * 1024):.2f} MB"
        if isinstance(file_size, (int, float))
        else "غير متاح"
    )
    engine = job.get("engine")
    engine_label = (
        "Cohere Arabic" if engine == "cohere"
        else "Deepgram (احتياطي)" if engine == "deepgram"
        else str(engine or "غير معروف")
    )
    keys = job.get("cohere_keys_used") or []
    keys_text = "، ".join(f"#{k}" for k in keys) if keys else "—"

    send_transcriber_log(
        "📊 تفريغ مكتمل\n"
        f"{_job_user_line(job)}\n"
        f"📄 الملف: {filename}\n"
        f"📦 الحجم: {size_text}\n"
        f"⚙️ المحرك: {engine_label}\n"
        f"🧩 الأجزاء: {job.get('total_parts') or 1}\n"
        f"🔑 Cohere: {keys_text}\n"
        f"⏱️ المعالجة: {_format_duration(job.get('processing_seconds'))}\n"
        f"🆔 المهمة: {job_id}\n"
        "✅ نجح"
    )


def log_transcription_failure(job_id):
    job = get_job(job_id) or {}
    filename = job.get("original_filename") or "غير معروف"
    file_size = job.get("file_size_bytes")
    size_text = (
        f"{file_size / (1024 * 1024):.2f} MB"
        if isinstance(file_size, (int, float))
        else "غير متاح"
    )
    error = str(job.get("error") or "سبب غير معروف")
    if len(error) > 1200:
        error = error[:1200] + "…"

    detail = str(job.get("error_detail") or "").strip()
    if detail:
        error += f"\n🔧 التفاصيل: {detail[:600]}"

    send_transcriber_log(
        "❌ فشل تفريغ\n"
        f"{_job_user_line(job)}\n"
        f"📄 الملف: {filename}\n"
        f"📦 الحجم: {size_text}\n"
        f"📍 المرحلة: {job.get('stage') or 'غير معروفة'}\n"
        f"⚠️ السبب: {error}\n"
        f"🆔 المهمة: {job_id}"
    )


CONTACT_INTRO = (
    "📬 للتواصل مع مطوّر مفرّغ الوقّاد\n\n"
    "قبل الإرسال، راعِ هذه الشروط:\n"
    "1) اكتب رسالتك واضحة، ويفضّل في رسالة واحدة.\n"
    "2) إن كان بلاغاً عن خلل، اذكر اسم الملف أو الأمر.\n"
    "3) لا ترسل ملفات صوتية أو فيديو هنا؛ التفريغ في المحادثة نفسها.\n"
    "4) لا ترسل كلمات مرور أو بيانات حساسة.\n"
    "5) الردّ تطوّعي، وقد يتأخر حسب الوقت.\n\n"
    "أرسل رسالتك الآن في الرسالة التالية.\n"
    "للإلغاء: /cancel"
)


def _display_name(sender):
    name = " ".join(
        part for part in (
            sender.get("first_name"),
            sender.get("last_name")
        ) if part
    ).strip()
    return name or "مستخدم"


def _forward_contact(chat_id, user_id, sender, text):
    """يوصل رسالة المستخدم إلى المطوّر مع إمكانية الردّ عليها."""
    text = (text or "").strip()
    if not text:
        telegram_send_message(chat_id, "اكتب رسالتك نصاً، ثم أرسلها.")
        return False

    if not DEVELOPER_CHAT_ID:
        telegram_send_message(
            chat_id,
            "⚠️ قناة التواصل غير مهيأة حالياً. حاول لاحقاً."
        )
        return False

    name = html.escape(_display_name(sender))
    username = (sender.get("username") or "").strip()
    handle = f"@{html.escape(username)}" if username else "بلا اسم مستخدم"
    try:
        mention = f'<a href="tg://user?id={int(user_id)}">{name}</a>'
    except (TypeError, ValueError):
        mention = name

    body = (
        "📬 رسالة تواصل جديدة\n"
        f"👤 {mention} ({handle})\n"
        f"🆔 <code>{html.escape(str(user_id))}</code>\n\n"
        f"{html.escape(text[:CONTACT_MAX_CHARS])}\n\n"
        "↩️ ردّ على هذه الرسالة ليصل ردّك إلى المستخدم."
    )

    try:
        forwarded_id = telegram_send_message(
            DEVELOPER_CHAT_ID,
            body,
            parse_mode="HTML"
        )
    except Exception as e:
        print(f"[CONTACT] forward failed: {e}", flush=True)
        telegram_send_message(
            chat_id,
            "⚠️ تعذّر إرسال رسالتك الآن. حاول بعد قليل."
        )
        return False

    if forwarded_id:
        with _CONTACT_LOCK:
            _CONTACT_REPLIES[str(forwarded_id)] = user_id
            # لا نحتفظ بقائمة لا نهائية في الذاكرة.
            if len(_CONTACT_REPLIES) > 500:
                for old_key in list(_CONTACT_REPLIES)[:100]:
                    _CONTACT_REPLIES.pop(old_key, None)

    telegram_send_message(
        chat_id,
        "✅ وصلت رسالتك إلى المطوّر. شكراً لك، وسيصلك الردّ هنا إن شاء الله."
    )
    return True


def handle_contact(chat_id, user_id, sender, argument):
    """/contact ثم رسالة المستخدم تُمرَّر إلى المطوّر."""
    if is_developer(user_id):
        telegram_send_message(
            chat_id,
            "أنت المطوّر؛ رسائل /contact من المستخدمين تصل إليك هنا."
        )
        return

    if argument:
        with _CONTACT_LOCK:
            _CONTACT_PENDING.pop(str(user_id), None)
        _forward_contact(chat_id, user_id, sender, argument)
        return

    with _CONTACT_LOCK:
        _CONTACT_PENDING[str(user_id)] = time.time()

    telegram_send_message(chat_id, CONTACT_INTRO)


def _consume_pending_contact(user_id):
    """هل المستخدم في وضع كتابة رسالة تواصل؟"""
    key = str(user_id)
    with _CONTACT_LOCK:
        started = _CONTACT_PENDING.pop(key, None)

    if started is None:
        return False

    if time.time() - started > CONTACT_WINDOW_SECONDS:
        telegram_send_message(
            user_id,
            "انتهت مهلة كتابة الرسالة. أرسل /contact من جديد."
        )
        return False

    return True


def handle_developer_reply(message, chat_id):
    """ردّ المطوّر على رسالة مُمرَّرة يعود إلى صاحبها."""
    replied = message.get("reply_to_message") or {}
    key = str(replied.get("message_id") or "")

    with _CONTACT_LOCK:
        target = _CONTACT_REPLIES.get(key)

    if not target:
        return False

    text = (message.get("text") or message.get("caption") or "").strip()
    if not text:
        telegram_send_message(
            chat_id,
            "اكتب الردّ نصاً حتى يصل للمستخدم."
        )
        return True

    try:
        telegram_send_message(
            target,
            "📬 ردّ من مطوّر مفرّغ الوقّاد:\n\n"
            f"{text[:CONTACT_MAX_CHARS]}"
        )
        telegram_send_message(chat_id, "✅ أُرسل ردّك إلى المستخدم.")
    except Exception as e:
        print(f"[CONTACT] reply failed: {e}", flush=True)
        telegram_send_message(
            chat_id,
            "⚠️ تعذّر إرسال الردّ. قد يكون المستخدم حظر البوت."
        )

    return True


def telegram_get_chat(chat_id):
    """يستعيد الاسم واسم المستخدم لمعرّف رآه البوت من قبل."""
    if not TELEGRAM_BOT_TOKEN:
        return {}

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getChat",
            data={"chat_id": chat_id},
            timeout=15
        )
        data = response.json() if response.content else {}
        if data.get("ok"):
            return data.get("result") or {}
        print(
            f"[USERS] getChat {chat_id} failed: "
            f"{str(data.get('description'))[:150]}",
            flush=True
        )
    except Exception as e:
        print(f"[USERS] getChat {chat_id} error: {e}", flush=True)

    return {}


def _save_user_identity(user_id, username, name):
    """يحفظ الاسم المستعاد في كل مهام المستخدم ليظهر في السجلات القادمة."""
    if not (username or name):
        return

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE jobs
                    SET data = data || jsonb_build_object(
                            'telegram_username', %s::text,
                            'telegram_name', %s::text
                        ),
                        updated_at = NOW()
                    WHERE data->>'telegram_user_id' = %s
                """, (username, name, str(user_id)))
            conn.commit()
    except Exception as e:
        print(f"[USERS] save identity failed: {e}", flush=True)


def handle_users(chat_id, user_id):
    """قائمة بمن استخدم البوت، مع استعادة الأسماء القديمة من تليجرام."""
    if not is_developer(user_id):
        telegram_send_message(chat_id, "هذا الأمر للمطوّر فقط.")
        return

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        data->>'telegram_user_id' AS user_id,
                        MAX(created_at) AS last_seen,
                        COUNT(*) AS jobs,
                        COALESCE(
                            MAX(NULLIF(data->>'telegram_username', '')),
                            ''
                        ) AS username,
                        COALESCE(
                            MAX(NULLIF(data->>'telegram_name', '')),
                            ''
                        ) AS name
                    FROM jobs
                    WHERE data->>'telegram_user_id' IS NOT NULL
                      AND data->>'telegram_user_id' <> ''
                    GROUP BY 1
                    ORDER BY MAX(created_at) DESC
                    LIMIT 20
                """)
                rows = cur.fetchall()
    except Exception as e:
        print(f"[USERS] query failed: {e}", flush=True)
        telegram_send_message(
            chat_id,
            "⚠️ تعذّر قراءة المستخدمين من قاعدة البيانات."
        )
        return

    if not rows:
        telegram_send_message(chat_id, "لا يوجد مستخدمون مسجّلون بعد.")
        return

    lines = ["👥 آخر المستخدمين (20):"]
    for index, row in enumerate(rows, start=1):
        tg_id = row.get("user_id") or "غير معروف"
        username = str(row.get("username") or "").strip().lstrip("@")
        name = str(row.get("name") or "").strip()

        # المعرّفات القديمة لا تحمل اسماً؛ نستعيده مرة واحدة ونحفظه.
        if not username:
            info = telegram_get_chat(tg_id)
            if info:
                username = str(
                    info.get("username") or ""
                ).strip().lstrip("@")
                fetched_name = " ".join(
                    part for part in (
                        str(info.get("first_name") or "").strip(),
                        str(info.get("last_name") or "").strip(),
                    ) if part
                )
                name = name or fetched_name
                _save_user_identity(tg_id, username, name)

        if username:
            who = f"@{username}"
            if name:
                who += f" ({name})"
        elif name:
            who = f"{name} (بلا اسم مستخدم)"
        else:
            who = "بلا اسم مستخدم"

        seen = row.get("last_seen")
        when = seen.strftime("%Y-%m-%d") if seen else "غير متاح"
        lines.append(
            f"{index}. {who}\n"
            f"   ID: {tg_id} — المهام: {row.get('jobs') or 0} — آخر: {when}"
        )

    telegram_send_message(chat_id, "\n".join(lines))
    if len(rows) >= 20:
        telegram_send_message(
            chat_id,
            "لعرض كل المستخدمين منذ التأسيس: /allusers\n"
            "خيارات إضافية: /block و/unblock و/blocked و/msg"
        )


def handle_stats(chat_id, user_id):
    """لوحة أرقام للمطوّر فقط، من قاعدة البيانات والطابور."""
    if not is_developer(user_id):
        telegram_send_message(chat_id, "هذا الأمر للمطوّر فقط.")
        return

    queue_stats = job_queue.stats()
    lines = ["📊 إحصاءات مفرّغ الوقّاد", ""]

    lines.append(
        f"🧵 الطابور: يعمل الآن {queue_stats.get('running', 0)}، "
        f"ينتظر {queue_stats.get('waiting', 0)}، "
        f"العمال {queue_stats.get('workers', 0)}."
    )

    average_queue = queue_stats.get("average_seconds")
    if average_queue:
        lines.append(f"⏱️ متوسط مدة المهمة: {_format_duration(average_queue)}.")

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        COUNT(*) AS total,
                        COUNT(*) FILTER (
                            WHERE created_at > NOW() - INTERVAL '24 hours'
                        ) AS last_day,
                        COUNT(*) FILTER (
                            WHERE created_at > NOW() - INTERVAL '7 days'
                        ) AS last_week,
                        COUNT(*) FILTER (
                            WHERE data->>'status' = 'completed'
                        ) AS completed,
                        COUNT(*) FILTER (
                            WHERE data->>'status' = 'failed'
                        ) AS failed,
                        COUNT(DISTINCT data->>'telegram_user_id') AS users,
                        AVG(
                            CASE
                                WHEN data->>'processing_seconds'
                                     ~ '^[0-9]+(\\.[0-9]+)?$'
                                THEN (data->>'processing_seconds')::float
                            END
                        ) FILTER (
                            WHERE data->>'status' = 'completed'
                        ) AS avg_seconds
                    FROM jobs
                """)
                summary = cur.fetchone() or {}

                cur.execute("""
                    SELECT
                        COALESCE(NULLIF(data->>'engine', ''), 'غير معروف')
                            AS engine,
                        COUNT(*) AS count
                    FROM jobs
                    WHERE data->>'status' = 'completed'
                    GROUP BY 1
                    ORDER BY 2 DESC
                    LIMIT 5
                """)
                engines = cur.fetchall()

                cur.execute("""
                    SELECT
                        COUNT(*) AS count,
                        LEFT(
                            COALESCE(NULLIF(data->>'error', ''), 'غير معروف'),
                            80
                        ) AS reason
                    FROM jobs
                    WHERE data->>'status' = 'failed'
                      AND created_at > NOW() - INTERVAL '7 days'
                    GROUP BY 2
                    ORDER BY 1 DESC
                    LIMIT 4
                """)
                failures = cur.fetchall()

        lines.append("")
        lines.append(
            f"📥 المهام: {summary.get('total') or 0} "
            f"(اليوم {summary.get('last_day') or 0}، "
            f"الأسبوع {summary.get('last_week') or 0})."
        )
        lines.append(
            f"✅ مكتملة: {summary.get('completed') or 0} — "
            f"❌ فاشلة: {summary.get('failed') or 0} — "
            f"👥 مستخدمون: {summary.get('users') or 0}."
        )

        avg_seconds = summary.get("avg_seconds")
        if avg_seconds:
            lines.append(
                f"⏱️ متوسط التفريغ: {_format_duration(avg_seconds)}."
            )

        if engines:
            parts = [
                f"{row.get('engine')}: {row.get('count')}"
                for row in engines
            ]
            lines.append("⚙️ المحركات: " + "، ".join(parts) + ".")

        if failures:
            lines.append("")
            lines.append("⚠️ أسباب الفشل (آخر 7 أيام):")
            for row in failures:
                lines.append(
                    f"• {row.get('count')}× {row.get('reason')}"
                )

    except Exception as e:
        print(f"[STATS] jobs query failed: {e}", flush=True)
        lines.append("")
        lines.append("⚠️ تعذّر قراءة إحصاءات المهام من قاعدة البيانات.")

    try:
        overall = transcript_store.overall_stats() or {}
        if overall:
            lines.append("")
            lines.append(
                f"🗂️ التفريغات المحفوظة: "
                f"{overall.get('transcripts') or 0} — "
                f"آخر 24 ساعة: {overall.get('last_day') or 0} — "
                f"مستخدمون: {overall.get('users') or 0}."
            )
    except Exception as e:
        print(f"[STATS] transcript query failed: {e}", flush=True)

    telegram_send_message(chat_id, "\n".join(lines))


def _is_cookies_document(message):
    document = message.get("document") or {}
    name = (document.get("file_name") or "").strip().lower()
    return bool(document.get("file_id")) and name.endswith(".txt")


def handle_cookies_upload(message, chat_id):
    """يستقبل cookies.txt من المطوّر ويحفظه لاستخدامه في يوتيوب."""
    file_id = (message.get("document") or {}).get("file_id")
    work_dir = Path("/tmp") / f"waqqad_cookies_{uuid.uuid4().hex}"
    work_dir.mkdir(parents=True, exist_ok=True)
    target = work_dir / "cookies.txt"

    try:
        download_telegram_file(
            TELEGRAM_BOT_TOKEN,
            file_id,
            target,
            max_bytes=MAX_COOKIES_BYTES
        )
        raw = target.read_bytes()
        if len(raw) > MAX_COOKIES_BYTES:
            raise FileTooLarge(len(raw), MAX_COOKIES_BYTES)

        text = raw.decode("utf-8-sig")
        count = url_media.validate_cookies_text(text)
        set_setting(YOUTUBE_COOKIES_KEY, text)

        telegram_send_message(
            chat_id,
            f"✅ حُفظت كوكيز يوتيوب ({count} سطرًا).\n"
            "أعد إرسال الرابط الآن، وسأستخدمها تلقائيًا.\n\n"
            "🔐 محتواها لا يظهر في المحادثة."
        )
        print(f"[COOKIES] saved {count} youtube cookie lines", flush=True)
    except FileTooLarge:
        telegram_send_message(
            chat_id,
            f"⚠️ الملف أكبر من الحد ({MAX_COOKIES_BYTES // 1024}KB). "
            "أرسل ملف الكوكيز الأصلي دون تعديل."
        )
    except UnicodeDecodeError:
        telegram_send_message(
            chat_id,
            "⚠️ الملف ليس نصًا صالحًا. أرسل cookies.txt كما صدّرته من المتصفح."
        )
    except DownloadFailed as e:
        print(f"[COOKIES] download failed: {e}", flush=True)
        telegram_send_message(
            chat_id,
            "⚠️ تعذّر تنزيل الملف من تليجرام. أعد إرساله."
        )
    except url_media.UrlError as e:
        telegram_send_message(chat_id, f"⚠️ {e}")
    except Exception as e:
        print(
            f"[COOKIES] unexpected error: {type(e).__name__}: {e}",
            flush=True
        )
        telegram_send_message(
            chat_id,
            "⚠️ حدث خطأ غير متوقع أثناء حفظ الكوكيز. أعد المحاولة."
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def handle_ytinfo_command(chat_id, user_id, argument):
    """فحص تشخيصي للمطوّر: سبر الصيغ عبر عملاء متعدد مع الكوكيز والبدون.

    يعمل في خيط خلفي حتى لا يعلّق الويبهوك، ويعرض النتيجة كرسائل متتابعة.
    """
    if not is_developer(user_id):
        telegram_send_message(chat_id, "هذا الأمر للمطوّر فقط.")
        return

    url = (argument or "").strip()
    if not url:
        telegram_send_message(
            chat_id,
            "استخدم: /ytinfo <رابط يوتيوب>\n"
            "سأفحص عدة عملاء مع الكوكيز والبدون؛ قد يستغرق حتى دقيقة."
        )
        return

    def _sweep():
        work_dir = Path("/tmp") / f"waqqad_ytinfo_{uuid.uuid4().hex}"
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            runtime = url_media.js_runtime_state()
            try:
                cookies_text = get_setting(YOUTUBE_COOKIES_KEY)
            except Exception as e:
                print(f"[YTINFO] cookies read failed: {e}", flush=True)
                cookies_text = None

            probes = [
                ("الافتراضي", None),
                ("visionos", ("visionos",)),
                ("web_safari", ("web_safari",)),
                ("tv_downgraded", ("tv_downgraded",)),
                ("web", ("web",)),
                ("web_embedded", ("web_embedded",)),
                ("mweb", ("mweb",)),
                ("android", ("android",)),
                ("ios", ("ios",)),
            ]
            results = []
            first_title = None
            for label, clients in probes:
                info, error = url_media.inspect_youtube(
                    url,
                    work_dir,
                    cookies_text=cookies_text,
                    clients=clients,
                )
                if error:
                    results.append((f"🍪 {label}", f"خطأ: {error}"))
                    continue
                if first_title is None:
                    first_title = str((info or {}).get("title") or "").strip()
                formats = (info or {}).get("formats") or []
                audible = [
                    f for f in formats
                    if f.get("acodec") not in (None, "none")
                ]
                results.append(
                    (
                        f"🍪 {label}",
                        f"{len(formats)} صيغة — صوت: {len(audible)}"
                    )
                )
                if audible:
                    ids = ", ".join(
                        str(f.get("format_id")) for f in audible[:5]
                    )
                    results.append((f"🍪 {label} — صيغ الصوت", ids))

            if cookies_text:
                info, error = url_media.inspect_youtube(
                    url,
                    work_dir,
                    cookies_text=None,
                    clients=None,
                )
                if error:
                    results.append(
                        ("🚫 بدون كوكيز — الافتراضي", f"خطأ: {error}")
                    )
                else:
                    formats = (info or {}).get("formats") or []
                    audible = [
                        f for f in formats
                        if f.get("acodec") not in (None, "none")
                    ]
                    results.append(
                        (
                            "🚫 بدون كوكيز — الافتراضي",
                            f"{len(formats)} صيغة — صوت: {len(audible)}"
                        )
                    )
                    if audible:
                        ids = ", ".join(
                            str(f.get("format_id")) for f in audible[:5]
                        )
                        results.append(
                            ("🚫 بدون كوكيز — صيغ الصوت", ids)
                        )

            title_part = f" — {first_title}" if first_title else ""
            header = (
                f"ℹ️ سبر: {url}{title_part}\n"
                f"🧾 نسخة: {url_media._yt_version()}"
                f" — 🖥️ بيئة JS: {runtime}\n"
                f"🍪 الافتراضيات مع الكوكيز — 🚫 آخر سطر بدونها\n"
                "------------------------------"
            )
            lines = [header]
            for label, summary in results:
                lines.append(f"{label}: {summary}")
            message = "\n".join(lines)
            for start in range(0, len(message), 3800):
                try:
                    telegram_send_message(
                        chat_id,
                        message[start:start + 3800]
                    )
                except Exception as e:
                    print(f"[YTINFO] send failed: {e}", flush=True)
                    break
            print(f"[YTINFO] sweep finished for {url}", flush=True)
        except Exception as e:
            print(f"[YTINFO] sweep failed: {e}", flush=True)
            try:
                telegram_send_message(
                    chat_id,
                    f"⚠️ فشل السبر: {url_media._short_reason(e)}"
                )
            except Exception:
                pass
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    thread = threading.Thread(
        target=_sweep,
        name="ytinfo-sweep",
        daemon=True
    )
    thread.start()
    print(f"[YTINFO] sweep started for {url}", flush=True)

def handle_cookies_command(chat_id, user_id, argument):
    if not is_developer(user_id):
        telegram_send_message(chat_id, "هذا الأمر للمطوّر فقط.")
        return

    if argument.strip().lower() in ("clear", "delete", "حذف"):
        delete_setting(YOUTUBE_COOKIES_KEY)
        telegram_send_message(
            chat_id,
            "🗑️ حُذفت كوكيز يوتيوب. "
            "أرسل ملف cookies.txt جديدًا عند الحاجة."
        )
        return

    row = get_setting_row(YOUTUBE_COOKIES_KEY)
    if not row:
        telegram_send_message(
            chat_id,
            "🕓 لا توجد كوكيز يوتيوب محفوظة.\n"
            "أرسل ملف cookies.txt كملف مستند في هذه المحادثة."
        )
        return

    try:
        count = url_media.validate_cookies_text(row.get("value") or "")
    except url_media.UrlError:
        count = 0

    updated = row.get("updated_at")
    when = (
        updated.strftime("%Y-%m-%d %H:%M")
        if hasattr(updated, "strftime")
        else str(updated or "")
    )

    telegram_send_message(
        chat_id,
        f"✅ كوكيز يوتيوب محفوظة ({count} سطرًا).\n"
        f"🕓 آخر تحديث: {when}\n\n"
        "للتحديث: أرسل ملف cookies.txt جديدًا.\n"
        "للحذف: /cookies clear"
    )


def handle_transcriber_chat_update(update):
    """الملفات تُفرّغ مباشرة، والأوامر تُخدم، وغير ذلك يُتجاهل بهدوء."""
    message = update.get("message")
    if not message:
        return

    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    sender = message.get("from") or {}
    if not chat_id or sender.get("is_bot"):
        return

    user_id = sender.get("id")

    # البوابة الحاجزة: المحجوب لا يستخدم أي شيء داخل البوت.
    if (
        user_id
        and chat.get("type") == "private"
        and not is_developer(user_id)
        and is_blocked(user_id)
    ):
        handle_blocked_gate(user_id)
        return

    # ردّ المطوّر على رسالة تواصل يعود لصاحب الرسالة.
    if is_developer(user_id) and message.get("reply_to_message"):
        if handle_developer_reply(message, chat_id):
            return

    # ردّ المستخدم على رسالة بدأها المطوّر يُوجَّه للمطوّر تلقائيًا.
    if (
        not is_developer(user_id)
        and chat.get("type") == "private"
        and message.get("reply_to_message")
        and forward_outreach_reply(chat_id, user_id, sender, message)
    ):
        return

    # ملف cookies.txt من المطوّر يُحفظ لتفريغ روابط يوتيوب.
    if is_developer(user_id) and _is_cookies_document(message):
        handle_cookies_upload(message, chat_id)
        return

    attachment = extract_attachment(message)
    if attachment:
        handle_bot_attachment(chat, sender, attachment)
        return

    raw_text = (message.get("text") or "").strip()

    # رسالة المطوّر أثناء وضع /msg تمرّر إلى المستخدم المقصود.
    if is_developer(user_id) and raw_text and not raw_text.startswith("/"):
        entry = _consume_pending_outreach(user_id)
        if entry:
            target = entry.get("user_id")
            if send_developer_message(target, raw_text):
                telegram_send_message(chat_id, f"✅ أُرسلت رسالتك إلى: {target}")
            else:
                telegram_send_message(
                    chat_id,
                    "⚠️ تعذّر إرسال الرسالة. ربما حظر المستخدم البوت."
                )
            return

    # رسالة عادية بعد /contact تُمرَّر إلى المطوّر بدل ردّ البوت المعتاد.
    if (
        chat.get("type") == "private"
        and raw_text
        and not raw_text.startswith("/")
        and not is_developer(user_id)
        and _consume_pending_contact(user_id)
    ):
        _forward_contact(chat_id, user_id, sender, raw_text)
        return

    # رابط يوتيوب أو رابط ملف مباشر يُفرّغ تلقائيًا.
    if chat.get("type") == "private" and raw_text and not raw_text.startswith("/"):
        found_url = url_media.extract_url(raw_text)
        if found_url:
            handle_bot_url(chat, sender, found_url)
            return

    if not raw_text.startswith("/"):
        if chat.get("type") == "private" and (
            raw_text or message.get("photo") or message.get("sticker")
        ):
            telegram_send_message(
                chat_id,
                "🎧 أرسل ملفاً صوتياً أو محاضرة، وسأفرّغها هنا مباشرة.\n"
                "🔗 أو الصق رابط يوتيوب أو رابط ملف مباشر.\n"
                "🔍 للبحث في تفريغاتك: /find\n"
                "❓ لمعرفة الأوامر: /help"
            )
        else:
            print(
                f"[TRANSCRIBER CHAT] Ignored ordinary message "
                f"from chat_id={chat_id}",
                flush=True
            )
        return

    command, _, argument = raw_text.partition(" ")
    command = command.split("@", 1)[0].lower()
    argument = argument.strip()

    # أي أمر آخر ينهي وضع كتابة رسالة تواصل أو رسالة مُوجَّهة للمطوّر.
    if command not in ("/contact", "/msg"):
        with _CONTACT_LOCK:
            _CONTACT_PENDING.pop(str(user_id), None)
        _clear_pending_outreach(user_id)

    keyboard = {
        "inline_keyboard": [[{
            "text": "🎙️ فتح مفرّغ النصوص",
            "web_app": {"url": TRANSCRIBER_WEBAPP_URL}
        }]]
    }

    if command == "/start":
        reply = (
            "أهلًا بك في مفرّغ الوقّاد 🎙️\n\n"
            "أرسل ملفاً صوتياً أو محاضرة في هذه المحادثة، "
            "وسأعيد لك التفريغ هنا.\n\n"
            "• حتى 20MB يُفرّغ مباشرة.\n"
            "• الأكبر من ذلك من صفحة الرفع.\n"
            "• أو أرسل رابط يوتيوب أو رابط ملف مباشر.\n"
            "• /find للبحث في تفريغاتك السابقة.\n\n"
            f"👨‍💻 تطوير: {DEVELOPER_NAME} — /about للتفاصيل."
        )
    elif command == "/help":
        reply = (
            "🎙️ كيف تستخدم مفرّغ الوقّاد:\n"
            "1) أرسل الملف الصوتي أو الفيديو في هذه المحادثة، "
            "أو الصق رابط يوتيوب أو رابط ملف مباشر.\n"
            "2) انتظر، وستصلك النسخة الخام ثم المصححة والمحسّنة.\n"
            "3) نزّل DOCX أو TXT من الأزرار أسفل النتيجة.\n"
            "4) بعد اكتمال التدقيق يظهر تقييم سريع؛ تقييمك يصل للمطوّر مباشرة.\n\n"
            "🔍 /find كلمة — البحث في تفريغاتك.\n"
            "🔍 /find — آخر تفريغاتك.\n"
            "🗂️ كل تفريغ يُحفظ في أرشيفك الخاص.\n"
            "👨‍💻 /about — المطوّر والمساهمون.\n"
            "📬 /contact — التواصل مع المطوّر.\n\n"
            "الملفات الأكبر من 20MB تُرفع من صفحة الرفع أدناه."
        )
    elif command == "/about":
        direct = developer_direct_link()
        reply = (
            "🎙️ مفرّغ الوقّاد\n\n"
            f"👨‍💻 المطوّر: {DEVELOPER_NAME}\n"
            f"🤝 المساهمون: {contributors_text()}\n\n"
            "📬 للتواصل والملاحظات: أرسل /contact ثم رسالتك، "
            "وستصل إلى المطوّر مباشرة."
        )
        if direct:
            reply += f"\n🔗 أو مباشرة: {direct}"
        reply += (
            "\n\n🔐 تفريغاتك محفوظة في أرشيفك الخاص، "
            "ولا يراها مستخدم آخر.\n"
            "🗑️ لطلب حذف تفريغ، استخدم /contact."
        )
    elif command == "/contact":
        handle_contact(chat_id, user_id, sender, argument)
        return
    elif command == "/cancel":
        with _CONTACT_LOCK:
            _CONTACT_PENDING.pop(str(user_id), None)
        _clear_pending_outreach(user_id)
        reply = "أُلغي وضع كتابة الرسالة."
    elif command == "/stats":
        handle_stats(chat_id, user_id)
        return
    elif command == "/users":
        handle_users(chat_id, user_id)
        return
    elif command == "/allusers":
        handle_allusers(chat_id, user_id)
        return
    elif command == "/block":
        handle_block_command(chat_id, user_id, argument)
        return
    elif command == "/unblock":
        handle_unblock_command(chat_id, user_id, argument)
        return
    elif command == "/blocked":
        handle_blocked_list(chat_id, user_id)
        return
    elif command == "/msg":
        handle_msg_command(chat_id, user_id, argument)
        return
    elif command == "/cookies":
        handle_cookies_command(chat_id, user_id, argument)
        return
    elif command == "/ytinfo":
        handle_ytinfo_command(chat_id, user_id, argument)
        return
    elif command == "/find":
        handle_find(chat_id, user_id, argument)
        return
    else:
        telegram_send_message(
            chat_id,
            "أمر غير معروف.\n/help لعرض الأوامر المتاحة."
        )
        return

    telegram_send_message(chat_id, reply, reply_markup=keyboard)


def handle_bot_attachment(chat, sender, attachment):
    """ملف وصل إلى البوت: تحقّق، ثم مهمة في الطابور."""
    chat_id = chat.get("id")
    user_id = sender.get("id")

    if chat.get("type") != "private":
        telegram_send_message(
            chat_id,
            "أرسل الملف في محادثة خاصة معي، حتى تبقى النتيجة لك وحدك."
        )
        return

    if not is_supported(attachment):
        telegram_send_message(
            chat_id,
            "هذا الملف ليس صوتاً ولا فيديو.\n"
            "أرسل المحاضرة بصيغة mp3 أو m4a أو ogg أو wav، "
            "أو أرسلها فيديو."
        )
        return

    size = int(attachment.get("file_size") or 0)
    if size > MAX_UPLOAD_BYTES:
        limit_mb = MAX_UPLOAD_BYTES // (1024 * 1024)
        telegram_send_message(
            chat_id,
            f"⚠️ حجم الملف {format_size(size)}، وتليجرام لا يسمح "
            f"للبوت بتنزيل أكثر من {limit_mb}MB.\n\n"
            "ارفعه من صفحة الرفع، وستصل النتيجة إلى هذه المحادثة:",
            reply_markup={
                "inline_keyboard": [[{
                    "text": "📤 فتح صفحة الرفع",
                    "web_app": {"url": TRANSCRIBER_WEBAPP_URL}
                }]]
            }
        )
        return

    job_id = str(uuid.uuid4())
    original_dir = Path("/tmp") / f"waqqad_{job_id}" / "original"
    original_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_upload_name(attachment.get("file_name"))
    destination = (original_dir / safe_name).resolve()

    create_job(
        job_id,
        new_job_payload(
            job_id,
            attachment.get("file_name") or safe_name,
            size,
            str(user_id),
            source="bot",
            **telegram_identity(sender)
        )
    )

    stats_before = job_queue.stats()
    ahead = stats_before.get("waiting", 0) + stats_before.get("running", 0)

    if ahead <= 0:
        queue_note = "⚡ سأبدأ به الآن."
    else:
        queue_note = f"⏳ أمامك {ahead} مهمة في الطابور."

    # رسالة استلام واحدة تتطور لاحقًا إلى «جارٍ التفريغ» ثم تتبخّر.
    send_status_message(
        job_id,
        chat_id,
        f"📥 وصلني: {attachment.get('file_name')}\n"
        f"📦 الحجم: {format_size(size)}\n"
        f"{queue_note}\n\n"
        "يمكنك إغلاق المحادثة، وستصل النتيجة هنا."
    )

    try:
        job_queue.enqueue(
            job_id,
            run_bot_job,
            job_id,
            attachment,
            destination
        )
    except Exception as e:
        print(f"[QUEUE ERROR] job {job_id}: {e}", flush=True)
        update_job(
            job_id,
            status="failed",
            stage="error",
            error=f"enqueue failed: {e}"
        )
        update_status_message(
            job_id,
            f"❌ {attachment.get('file_name')}: تعذّر إدخال الملف "
            "في الطابور. أعد المحاولة."
        )
        log_transcription_failure(job_id)


def create_url_job(url, user_id, sender, source):
    """ينشئ مهمة من رابط قبل تنزيله، حتى تظهر حالتها فورًا."""
    job_id = str(uuid.uuid4())
    work_dir = (
        Path("/tmp")
        / f"waqqad_{job_id}"
        / "original"
    )
    work_dir.mkdir(parents=True, exist_ok=True)

    display_name = url_media.display_name_for_url(url)

    create_job(
        job_id,
        new_job_payload(
            job_id,
            display_name,
            0,
            str(user_id),
            source=source,
            original_url=url,
            **telegram_identity(sender)
        )
    )

    return job_id, work_dir


def _url_status(job_id, text, notify):
    if not notify:
        return
    try:
        update_status_message(job_id, text)
    except Exception as e:
        print(f"[URL STATUS] {job_id}: {e}", flush=True)


def _fail_url_job(job_id, label, reason, notify, user_message, detail=""):
    update_job(
        job_id,
        status="failed",
        stage="error",
        error=str(reason),
        error_detail=str(detail or "")[:600] or None
    )
    log_transcription_failure(job_id)
    _url_status(job_id, user_message, notify)


def run_url_job(job_id, url, destination, notify=True):
    """ينزّل الرابط ثم يمرّره إلى مسار التفريغ نفسه."""
    job = get_job(job_id) or {}
    label = job_label(job, url_media.display_name_for_url(url))

    try:
        update_job(job_id, status="processing", stage="downloading_url")
        _url_status(
            job_id,
            f"⬇️ {label}\n"
            "جارٍ تنزيل الرابط وتجهيز الصوت…\n"
            "قد يستغرق التنزيل بضع دقائق حسب الرابط.",
            notify
        )

        try:
            cookies_text = get_setting(YOUTUBE_COOKIES_KEY)
        except Exception as e:
            print(f"[COOKIES] read failed: {e}", flush=True)
            cookies_text = None

        media_path, size, display_name = url_media.download_url(
            url,
            destination,
            MAX_URL_BYTES,
            cookies_text=cookies_text
        )

        update_job(
            job_id,
            original_filename=display_name or label,
            file_size_bytes=size,
            stage="processing"
        )

        _url_status(
            job_id,
            f"🎧 {display_name or label}\n"
            f"✅ تم التنزيل ({format_size(size)}).\n"
            "⏳ جارٍ التفريغ الآن، وسيصلك النص عند اكتماله.",
            notify
        )

        process_transcription_job(job_id, media_path)

    except url_media.UrlTooLarge:
        _fail_url_job(
            job_id,
            label,
            "url file too large",
            notify,
            f"⚠️ {label}: الملف أكبر من الحد المسموح "
            f"({int(TRANSCRIBER_URL_MAX_MB)}MB).\n"
            "أرسل مقطعًا أقصر أو ارفع ملفًا مضغوطًا."
        )
    except url_media.UnsupportedUrl as e:
        _fail_url_job(
            job_id,
            label,
            f"unsupported url: {e}",
            notify,
            f"❌ {label}: {e}\n"
            "أرسل رابط يوتيوب أو رابط ملف مباشر."
        )
    except url_media.UrlError as e:
        _fail_url_job(
            job_id,
            label,
            f"url download failed: {e}",
            notify,
            f"❌ {label}: تعذّر تنزيل الرابط.\n{e}",
            detail=getattr(e, "detail", "")
        )
    except Exception as e:
        print(
            f"[URL JOB ERROR] {job_id}: {type(e).__name__}: {e}",
            flush=True
        )
        _fail_url_job(
            job_id,
            label,
            str(e),
            notify,
            f"❌ {label}: حدث خطأ غير متوقع أثناء معالجة الرابط.\n"
            "أعد المحاولة، وإن تكرر فأبلغنا عبر /contact.",
            detail=f"{type(e).__name__}: {e}"
        )
    finally:
        # الملف المنزَّل قد يبلغ 150MB، فلا نُبقيه بعد النجاح أو الفشل.
        shutil.rmtree(destination.parent, ignore_errors=True)


def handle_bot_url(chat, sender, url):
    """رابط وصل إلى محادثة البوت: مهمة تنزيل ثم تفريغ."""
    chat_id = chat.get("id")
    user_id = sender.get("id")

    if chat.get("type") != "private":
        telegram_send_message(
            chat_id,
            "أرسل الرابط في محادثة خاصة معي، حتى تبقى النتيجة لك وحدك."
        )
        return

    job_id, destination = create_url_job(
        url,
        user_id,
        sender,
        source="bot_url"
    )

    display_name = url_media.display_name_for_url(url)
    send_status_message(
        job_id,
        chat_id,
        f"🔗 وصلني الرابط: {display_name}\n"
        f"📦 الحد الأقصى: {int(TRANSCRIBER_URL_MAX_MB)}MB\n\n"
        "سأنزّله وأفرّغه، وستصل النتيجة هنا.\n"
        "يمكنك إغلاق المحادثة."
    )

    try:
        job_queue.enqueue(
            job_id,
            run_url_job,
            job_id,
            url,
            destination,
            True
        )
    except Exception as e:
        print(f"[QUEUE ERROR] url job {job_id}: {e}", flush=True)
        _fail_url_job(
            job_id,
            display_name,
            f"enqueue failed: {e}",
            True,
            f"❌ {display_name}: تعذّر إدخال الرابط في الطابور. "
            "أعد المحاولة."
        )


def run_bot_job(job_id, attachment, destination):
    """يعمل داخل عامل الطابور: تنزيل الملف ثم تفريغه."""
    job = get_job(job_id) or {}
    label = job_label(job, attachment.get("file_name") or "ملف")

    try:
        update_job(job_id, stage="downloading")
        size = download_telegram_file(
            TELEGRAM_BOT_TOKEN,
            attachment.get("file_id"),
            destination,
            MAX_UPLOAD_BYTES
        )

        update_job(job_id, stage="processing", file_size_bytes=size)

        update_status_message(
            job_id,
            f"🎧 {label}\n"
            "✅ تم التنزيل.\n"
            "⏳ جارٍ التفريغ الآن، وسيصلك النص عند اكتماله."
        )

        process_transcription_job(job_id, destination)

    except FileTooLarge as e:
        update_job(
            job_id,
            status="failed",
            stage="error",
            error=f"file too large: {e}"
        )
        log_transcription_failure(job_id)
        update_status_message(
            job_id,
            f"⚠️ {label}: تجاوز الملف الحد المسموح للتنزيل.\n"
            f"ارفعه من صفحة الرفع وستصل النتيجة هنا:\n"
            f"{TRANSCRIBER_WEBAPP_URL}"
        )

    except (DownloadFailed, UnsupportedFile) as e:
        update_job(
            job_id,
            status="failed",
            stage="error",
            error=f"download failed: {e}"
        )
        log_transcription_failure(job_id)
        update_status_message(
            job_id,
            f"❌ {label}: تعذّر تنزيل الملف من تليجرام.\n"
            "أعد إرساله، أو ارفعه من صفحة الرفع."
        )

    except Exception as e:
        print(
            f"[BOT JOB ERROR] {job_id}: {type(e).__name__}: {e}",
            flush=True
        )
        update_job(
            job_id,
            status="failed",
            stage="error",
            error=str(e)
        )
        log_transcription_failure(job_id)
        update_status_message(
            job_id,
            f"❌ {label}: حدث خطأ غير متوقع أثناء التجهيز.\n"
            "أعد المحاولة، وإن تكرر فأبلغنا عبر /about."
        )


def handle_find(chat_id, user_id, query):
    """البحث في تفريغات المستخدم، محصورًا به وحده."""
    rows = transcript_store.search_transcripts(user_id, query, limit=8)

    if not rows:
        if query:
            telegram_send_message(
                chat_id,
                f"🔍 لا نتائج عن «{query}».\n"
                "جرّب كلمة أقصر أو جزءًا منها."
            )
        else:
            telegram_send_message(
                chat_id,
                "📭 لا يوجد تفريغ محفوظ بعد.\n"
                "أرسل ملفاً صوتياً وسأحفظه لك."
            )
        return

    header = (
        f"🔍 نتائج «{query}» ({len(rows)}):"
        if query
        else f"🗂️ آخر تفريغاتك ({len(rows)}):"
    )
    lines = [header]
    keyboard = []

    for index, row in enumerate(rows, start=1):
        title = row.get("title") or "تفريغ"
        created = row.get("created_at")
        when = created.strftime("%Y-%m-%d") if created else ""
        chars = row.get("chars") or 0
        lines.append(f"{index}. {title} — {when} — {chars} حرف")
        short = title[:28]
        keyboard.append([
            {
                "text": f"📝 {index}. {short} DOCX",
                "callback_data": f"f:{row['job_id']}:raw:docx"
            },
            {
                "text": f"📄 {index}. {short} TXT",
                "callback_data": f"f:{row['job_id']}:raw:txt"
            }
        ])

    telegram_send_message(
        chat_id,
        "\n".join(lines),
        reply_markup={"inline_keyboard": keyboard}
    )


# ============================================================
# إرسال النتيجة عبر Telegram
# ============================================================

def send_transcript_to_telegram(
    telegram_user_id,
    output_file,
    download_name="transcription.txt",
    caption="✅ اكتمل تفريغ الملف بنجاح.",
    reply_markup=None
):
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")

    url = (
        "https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    )

    data = {
        "chat_id": telegram_user_id,
        "caption": caption[:1024]
    }
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(
            reply_markup,
            ensure_ascii=False
        )

    with open(output_file, "rb") as document:
        response = requests.post(
            url,
            data=data,
            files={
                "document": (
                    download_name,
                    document,
                    "text/plain"
                )
            },
            timeout=60
        )

    print(
        f"[TELEGRAM] HTTP {response.status_code}: "
        f"{response.text[:500]}",
        flush=True
    )

    if response.status_code != 200:
        raise RuntimeError(
            "Telegram sendDocument HTTP "
            f"{response.status_code}"
        )


def send_document_bytes(
    chat_id,
    payload,
    download_name,
    caption="",
    mime="application/octet-stream",
    reply_markup=None
):
    """إرسال ملف من الذاكرة (تصدير DOCX/TXT عند الطلب)."""
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")

    data = {
        "chat_id": chat_id,
        "caption": (caption or "")[:1024]
    }
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(
            reply_markup,
            ensure_ascii=False
        )

    response = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
        data=data,
        files={"document": (download_name, payload, mime)},
        timeout=120
    )

    print(
        f"[TELEGRAM DOC] HTTP {response.status_code}: "
        f"{response.text[:300]}",
        flush=True
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Telegram sendDocument HTTP {response.status_code}"
        )


def answer_callback(callback_id, text=None, alert=False):
    """إيقاف مؤشر الانتظار على الزر؛ الفشل هنا لا يهم."""
    if not callback_id or not TELEGRAM_BOT_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/"
            "answerCallbackQuery",
            data={
                "callback_query_id": callback_id,
                "text": (text or "")[:200],
                "show_alert": "true" if alert else "false"
            },
            timeout=15
        )
    except Exception as e:
        print(f"[CALLBACK ANSWER ERROR] {e}", flush=True)


def _user_hash(user_id):
    """بصمة ثابتة للمستخدم تُنشر في قناة الأرشيف بدل معرّفه."""
    try:
        return hmac.new(
            (TELEGRAM_BOT_TOKEN or "waqqad").encode("utf-8"),
            str(user_id or "").encode("utf-8"),
            hashlib.sha256
        ).hexdigest()[:12]
    except Exception:
        return "unknown"


def _archive_transcript(job_id, text, job):
    """نسخة احتياطية من النص في قناة السجل الخاصة.

    نرسل الملف لا الرسالة، لأن رسالة تليجرام محدودة بـ4096 حرفًا
    والمحاضرة قد تتجاوز ذلك بعشرات المرات.
    """
    if not TRANSCRIBER_LOG_CHAT_ID or not text.strip():
        return

    try:
        output_name = job.get("output_name") or "transcript.txt"
        name = f"{Path(output_name).stem}.txt"
        send_document_bytes(
            TRANSCRIBER_LOG_CHAT_ID,
            build_txt(text),
            name,
            caption=(
                "🗄️ أرشيف تفريغ\n"
                f"{_job_user_line(job)}\n"
                f"👤 بصمة: {_user_hash(job.get('telegram_user_id'))}\n"
                f"📄 الملف: {job.get('original_filename') or 'غير معروف'}\n"
                f"🔤 الأحرف: {len(text)}\n"
                f"🆔 المهمة: {job_id}"
            ),
            mime=TXT_MIME
        )
    except Exception as e:
        # الأرشفة لا تُفشل التفريغ.
        print(f"[ARCHIVE ERROR] Job {job_id}: {e}", flush=True)


def _result_keyboard(job_id, kinds):
    """أزرار التصدير لرسالة النتيجة."""
    rows = []

    if "raw" in kinds:
        rows.append([
            {
                "text": "📝 DOCX",
                "callback_data": f"f:{job_id}:raw:docx"
            },
            {
                "text": "📄 TXT",
                "callback_data": f"f:{job_id}:raw:txt"
            }
        ])

    return {"inline_keyboard": rows} if rows else None


def _refined_keyboard(job_id, kinds):
    """أزرار النسختين المصححة والمحسّنة."""
    rows = []

    for kind, label in (
        ("conservative", "📝 المصححة"),
        ("liberal", "✨ المحسّنة")
    ):
        if kind not in kinds:
            continue
        rows.append([
            {
                "text": f"{label} DOCX",
                "callback_data": f"f:{job_id}:{kind}:docx"
            },
            {
                "text": f"{label} TXT",
                "callback_data": f"f:{job_id}:{kind}:txt"
            }
        ])

    return {"inline_keyboard": rows} if rows else None


def handle_transcriber_callback(update):
    """أزرار التصدير f:<job>:<kind>:<fmt> وأزرار التقييم fb:<job>:<verdict>."""
    callback = update.get("callback_query") or {}
    if not callback:
        return

    callback_id = callback.get("id")
    data = callback.get("data") or ""
    sender = callback.get("from") or {}
    user_id = sender.get("id")
    message = callback.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id") or user_id

    print(
        f"[CALLBACK] id={callback_id} user={user_id} data={data!r}",
        flush=True
    )

    # المحجوب: أزرار لا تصدير ولا تقييم.
    if (
        user_id
        and not is_developer(user_id)
        and is_blocked(user_id)
    ):
        handle_blocked_gate(user_id)
        answer_callback(callback_id, "تم إيقاف استخدامك لهذه الخدمة", alert=True)
        return

    if data.startswith("fb:"):
        try:
            _, raw_job_id, choice = data.split(":", 2)
        except ValueError:
            answer_callback(callback_id, "خيار غير صالح", alert=True)
            return
        answer_feedback_callback(callback_id, choice.strip(), raw_job_id, callback)
        return

    if not data.startswith("f:"):
        answer_callback(callback_id, "خيار غير معروف")
        return

    try:
        _, job_id, kind, fmt = data.split(":", 3)
    except ValueError:
        answer_callback(callback_id, "خيار غير صالح", alert=True)
        return

    kind = (kind or "").strip()
    fmt = (fmt or "").strip().lower()

    if fmt not in {"txt", "docx"} or kind not in transcript_store.KINDS:
        answer_callback(callback_id, "صيغة تصدير غير معروفة", alert=True)
        return

    row = transcript_store.get_transcript(job_id, kind)
    job = get_job(job_id) or {}

    owner = row.get("user_id") if row else job.get("telegram_user_id")
    if owner is None or str(owner) != str(user_id):
        print(
            f"[CALLBACK] ownership rejected: owner={owner} user={user_id}",
            flush=True
        )
        answer_callback(callback_id, "هذا التفريغ ليس لك", alert=True)
        return

    if not row:
        print(
            f"[CALLBACK] transcript missing job={job_id} kind={kind}",
            flush=True
        )
        answer_callback(
            callback_id,
            "هذه النسخة غير محفوظة. جرّب النسخة الخام من /find.",
            alert=True
        )
        return

    text = row.get("content") or ""
    stem = Path(job.get("original_filename") or "transcript").stem
    suffix = {
        "conservative": "_med",
        "liberal": "_well",
        "summary": "_summary",
    }.get(kind, "")
    extension = fmt
    name = f"{stem}{suffix}.{extension}"

    # أُغلق المؤشر الآن، ثم يأتي الملف في رسالة مستقلة.
    answer_callback(callback_id, "جارٍ تجهيز الملف…")

    try:
        if extension == "docx":
            payload = build_docx(text, title=stem)
            mime = DOCX_MIME
        else:
            payload = build_txt(text)
            mime = TXT_MIME

        print(
            f"[CALLBACK] sending {name} ({len(payload)} bytes) "
            f"to chat {chat_id}",
            flush=True
        )
        send_document_bytes(
            chat_id,
            payload,
            name,
            caption=f"📄 {name}",
            mime=mime
        )
    except Exception as e:
        print(
            f"[EXPORT ERROR] job={job_id} kind={kind} fmt={fmt}: "
            f"{type(e).__name__}: {e}",
            flush=True
        )
        try:
            telegram_send_message(
                chat_id,
                "❌ تعذّر تجهيز الملف للتنزيل.\n"
                f"السبب: {type(e).__name__}\n"
                "أعد المحاولة بعد قليل، وإن تكرر أبلغنا."
            )
        except Exception:
            pass


def _refine_failure_hint(exc):
    """رسالة مبسطة للمستخدم بدل تفاصيل تقنية."""
    text = str(exc).lower()
    if "gemini_api_key" in text or "مفتاح" in text:
        return "سبب: إعداد مفتاح التدقيق غير صحيح."
    if "حد استخدام" in text or "quota" in text or "429" in text:
        return "سبب: تم بلوغ حد الاستخدام مؤقتاً. أعد المحاولة بعد قليل."
    if "غير متاح" in text or "404" in text:
        return "سبب: موديل التدقيق غير متاح حالياً."
    return "سبب مؤقت في خدمة التدقيق."


def send_refined_versions_async(job_id, raw_output_file):
    """ينشئ ويرسل النسختين المصححة والمحسنة تلقائياً."""
    if not REFINE_ENABLED or refine_document is None:
        print(f"[AUTO-REFINE] Disabled for job {job_id}", flush=True)
        return

    try:
        raw_path = Path(raw_output_file)
        if not raw_path.exists():
            print(
                f"[AUTO-REFINE] Raw file not found {raw_output_file}",
                flush=True
            )
            return

        raw_text = raw_path.read_text(encoding="utf-8")
        if not raw_text.strip():
            print(f"[AUTO-REFINE] Empty text for job {job_id}", flush=True)
            return

        job = get_job(job_id) or {}
        telegram_user_id = job.get("telegram_user_id")
        if not telegram_user_id:
            print(
                f"[AUTO-REFINE] No telegram_user_id for job {job_id}",
                flush=True
            )
            return

        original_name = job.get("original_filename") or "transcription"
        base_stem = Path(original_name).stem

    except Exception as e:
        print(
            f"[AUTO-REFINE] Fatal setup error job {job_id}: {e}",
            flush=True
        )
        return

    send_status_message(
        job_id,
        telegram_user_id,
        f"🎧 {job_label(job, original_name)}\n"
        "✅ تم استخراج النص الخام.\n"
        "⏳ جارٍ الآن تجهيز نسختين إضافيتين: "
        "مصححة إملائياً، ومحسّنة للقراءة.\n"
        "قد يستغرق الأمر بضع دقائق حسب طول الملف.",
        prefix="refine"
    )

    versions = [
        (
            "conservative",
            "_med.txt",
            "📝 النسخة المصححة إملائياً\n"
            "تصحيح الهمزات والترقيم فقط دون تغيير الكلمات.",
        ),
        (
            "liberal",
            "_well.txt",
            "✨ النسخة المحسّنة\n"
            "ترتيب الفقرات والعناوين مع الحفاظ على المعنى.",
        ),
    ]

    refined_files = []

    for mode, suffix, caption in versions:
        try:
            print(
                f"[AUTO-REFINE] Starting {mode} for job {job_id} "
                f"({len(raw_text)} chars)",
                flush=True
            )

            result = refine_document(raw_text, mode)
            text = (result.get("text") or "").strip()
            if not text:
                raise RuntimeError("الناتج فارغ")

            refined_path = raw_path.with_name(f"{base_stem}{suffix}")
            refined_path.write_text(text, encoding="utf-8")
            refined_files.append((refined_path, caption))

            transcript_store.save_transcript(
                job_id,
                telegram_user_id,
                mode,
                text,
                title=base_stem or None
            )
            print(
                f"[AUTO-REFINE] {mode} stored for job {job_id}",
                flush=True
            )

            fallbacks = int(result.get("fallbacks") or 0)
            chunks = int(result.get("chunks") or 0)
            if fallbacks:
                telegram_send_message(
                    telegram_user_id,
                    "⚠️ ملاحظة: تعذر تدقيق "
                    f"{fallbacks} من أصل {chunks} أجزاء في هذه النسخة، "
                    "فأُعيد النص الأصلي لها دون تغيير."
                )

        except Exception as e:
            print(
                f"[AUTO-REFINE] {mode} failed job {job_id}: "
                f"{type(e).__name__}: {e}",
                flush=True
            )
            label = (
                "المصححة إملائياً"
                if mode == "conservative"
                else "المحسّنة"
            )
            try:
                telegram_send_message(
                    telegram_user_id,
                    f"❌ تعذر إنشاء النسخة {label}.\n"
                    f"{_refine_failure_hint(e)}\n"
                    "النسخة الخام وصلتك سليمة ويمكنك استخدامها."
                )
            except Exception:
                pass

            if GeminiFatalError and isinstance(e, GeminiFatalError):
                print(
                    f"[AUTO-REFINE] Fatal error, skipping remaining "
                    f"versions for job {job_id}",
                    flush=True
                )
                break

    kinds = transcript_store.available_kinds(job_id)
    keyboard = _refined_keyboard(job_id, kinds)

    if keyboard:
        try:
            telegram_send_message(
                telegram_user_id,
                f"✨ اكتمل التدقيق: {job_label(job, original_name)}\n"
                "النسختان محفوظتان، ويمكنك تنزيلهما في أي وقت "
                "من هذه الأزرار أو بأمر /find.\n"
                "اختر الصيغة:",
                keyboard
            )
            delete_status_message(job_id, prefix="refine")
            # لوحة التقييم تظهر بعد إفاش النسختين، ليس قبلها.
            send_feedback_prompt(job_id, telegram_user_id)
        except Exception as e:
            print(f"[AUTO-REFINE] Panel message failed: {e}", flush=True)
            update_status_message(
                job_id,
                f"⚠️ {job_label(job, original_name)}: اكتمل التدقيق، "
                "لكن تعذّر إرسال أزرار التنزيل. استخدم /find.",
                prefix="refine"
            )
    else:
        # إن تعذّر الحفظ في القاعدة، أرسل الملفات كما كان البوت يفعل.
        for refined_path, caption in refined_files:
            try:
                send_transcript_to_telegram(
                    telegram_user_id,
                    refined_path,
                    download_name=refined_path.name,
                    caption=caption
                )
            except Exception as e:
                print(
                    f"[AUTO-REFINE] Fallback send failed: {e}",
                    flush=True
                )
        delete_status_message(job_id, prefix="refine")
        # ذا تعذّرت الأزرار، التقييم يبقى يظهر مرة واحدة بعد التسليم.
        send_feedback_prompt(job_id, telegram_user_id)


# ============================================================
# الصفحة الرئيسية
# ============================================================

@app.route("/")
def home():

    return jsonify({
        "service": "Waqqad Transcriber",
        "status": "online"
    })


@app.route("/health")
def health():

    return jsonify({
        "status": "ok"
    })


# ============================================================
# إنشاء مهمة جديدة
# ============================================================

@app.route(
    "/transcribe",
    methods=["POST"]
)
def transcribe():

    print(
        "========================================",
        flush=True
    )

    print(
        "[TRANSCRIBE] New request",
        flush=True
    )

    print(
        "========================================",
        flush=True
    )

    if "file" not in request.files:
        return jsonify({
            "error": "No file provided"
        }), 400

    uploaded_file = request.files["file"]

    try:
        tg_user = _current_telegram_user()
    except ValueError as auth_error:
        return _unauthorized(auth_error)

    telegram_user_id = str(tg_user["id"])

    if not uploaded_file.filename:
        return jsonify({
            "error": "Empty filename"
        }), 400

    job_id = str(uuid.uuid4())

    work_dir = (
        Path("/tmp")
        / f"waqqad_{job_id}"
    )

    original_dir = (
        work_dir
        / "original"
    )

    original_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    safe_name = _safe_upload_name(uploaded_file.filename)
    input_file = (original_dir / safe_name).resolve()

    if original_dir.resolve() not in input_file.parents:
        shutil.rmtree(work_dir, ignore_errors=True)
        return jsonify({
            "error": "Invalid filename"
        }), 400

    print(
        f"[UPLOAD] Saving: {safe_name}",
        flush=True
    )

    uploaded_file.save(input_file)

    file_size = input_file.stat().st_size

    print(
        f"[UPLOAD] Size: "
        f"{file_size / (1024 * 1024):.2f} MB",
        flush=True
    )

    create_job(
        job_id,
        new_job_payload(
            job_id,
            uploaded_file.filename,
            file_size,
            telegram_user_id,
            source="web",
            **telegram_identity(tg_user)
        )
    )

    job_queue.enqueue(
        job_id,
        process_transcription_job,
        job_id,
        input_file
    )

    return jsonify({
        "job_id": job_id,
        "status": "queued",
        "message":
            "Transcription job started."
    }), 202


@app.route(
    "/transcribe-url",
    methods=["POST"]
)
def transcribe_url():
    """مهمة من رابط يوتيوب أو ملف مباشر، من صفحة الرفع."""
    try:
        tg_user = _current_telegram_user()
    except ValueError as auth_error:
        return _unauthorized(auth_error)

    payload = request.get_json(silent=True) or request.form or {}
    url = str(payload.get("url") or "").strip()

    if not url_media.is_url(url):
        return jsonify({
            "error": "أرسل رابطًا صالحًا يبدأ بـ http أو https."
        }), 400

    telegram_user_id = str(tg_user["id"])
    job_id, destination = create_url_job(
        url,
        telegram_user_id,
        tg_user,
        source="web_url"
    )

    job_queue.enqueue(
        job_id,
        run_url_job,
        job_id,
        url,
        destination,
        False
    )

    return jsonify({
        "job_id": job_id,
        "status": "queued",
        "message": "URL transcription job started."
    }), 202


# ============================================================
# Deepgram Callback
# ============================================================

@app.route(
    "/deepgram-callback/<job_id>",
    methods=["POST"]
)
def deepgram_callback(job_id):

    print(
        f"[CALLBACK] Received result "
        f"for job {job_id}",
        flush=True
    )

    if not _verify_callback_token(
        job_id,
        request.args.get("token", "")
    ):
        print(
            f"[CALLBACK] Rejected untrusted callback for job {job_id}",
            flush=True
        )
        return jsonify({"error": "forbidden"}), 403

    job = get_job(job_id)

    if job is None:

        print(
            f"[CALLBACK] Unknown job "
            f"{job_id}",
            flush=True
        )

        return jsonify({
            "error": "unknown job"
        }), 404

    try:

        data = request.get_json(
            force=True
        )

        transcript = (
            data["results"]
            ["channels"][0]
            ["alternatives"][0]
            ["transcript"]
        )

    except Exception as e:

        print(
            f"[CALLBACK ERROR] "
            f"job {job_id}: {e}",
            flush=True
        )

        update_job(
            job_id,
            status="failed",
            stage="error",
            error=(
                "Bad callback payload "
                "from Deepgram"
            )
        )
        log_transcription_failure(job_id)

        return jsonify({
            "error": "bad payload"
        }), 400

    finalize_transcript(
        job_id,
        transcript,
        engine="deepgram"
    )

    print(
        f"[CALLBACK] Job {job_id} "
        f"completed successfully via Deepgram.",
        flush=True
    )

    return jsonify({
        "status": "ok"
    }), 200


# ============================================================
# حالة المهمة
# ============================================================

@app.route(
    "/status/<job_id>",
    methods=["GET"]
)
def job_status(job_id):

    try:
        tg_user = _current_telegram_user()
    except ValueError as auth_error:
        return _unauthorized(auth_error)

    job = get_job(job_id)

    if job is None or not _job_belongs_to(job, tg_user):
        return jsonify({
            "error": "Job not found"
        }), 404

    return jsonify(job)


# ============================================================
# تحميل النتيجة يدويًا
# ============================================================

@app.route(
    "/download/<job_id>",
    methods=["GET"]
)
def download_result(job_id):

    try:
        tg_user = _current_telegram_user()
    except ValueError as auth_error:
        return _unauthorized(auth_error)

    job = get_job(job_id)

    if job is None or not _job_belongs_to(job, tg_user):
        return jsonify({
            "error": "Job not found"
        }), 404

    if job["status"] != "completed":
        return jsonify({
            "error":
                "Job is not completed yet",
            "status":
                job["status"]
        }), 409

    output_file = job.get(
        "output_file"
    )

    if (
        not output_file
        or not os.path.exists(output_file)
    ):

        return jsonify({
            "error":
                "Output file not found"
        }), 404

    return send_file(
        output_file,
        as_attachment=True,
        download_name=
            job.get("output_name", "transcription.txt"),
        mimetype="text/plain"
    )

@app.route(
    "/telegram/transcriber",
    methods=["POST"]
)
def transcriber_webhook():

    if not _webhook_secret_ok(TELEGRAM_WEBHOOK_SECRET):
        print(
            "[TRANSCRIBER WEBHOOK] Rejected: bad secret token",
            flush=True
        )
        return jsonify({"error": "forbidden"}), 403

    update = request.get_json(
        force=True,
        silent=True
    )

    print(
        "[TRANSCRIBER WEBHOOK] update received",
        flush=True
    )

    if not update:
        print(
            "[TRANSCRIBER WEBHOOK] Empty/invalid JSON update.",
            flush=True
        )
        return jsonify({"status": "ok"}), 200

    print(
        f"[TRANSCRIBER WEBHOOK] update_id={update.get('update_id')}, "
        f"keys={list(update.keys())}",
        flush=True
    )

    try:
        if update.get("callback_query"):
            handle_transcriber_callback(update)
        else:
            handle_transcriber_chat_update(update)
    except Exception as e:
        print(
            f"[TRANSCRIBER BOT ERROR] {type(e).__name__}: {e}",
            flush=True
        )

    return jsonify({"status": "ok"}), 200


@app.route(
    "/telegram/warraq",
    methods=["POST"]
)
def warraq_webhook():

    if not _webhook_secret_ok(WARRAQ_WEBHOOK_SECRET):
        print(
            "[WARRAQ WEBHOOK] Rejected: bad secret token",
            flush=True
        )
        return jsonify({"error": "forbidden"}), 403

    update = request.get_json(
        force=True,
        silent=True
    )

    if update:
        try:
            handle_warraq_update(update)

        except Exception as e:
            print(
                f"[WARRAQ ERROR] {e}",
                flush=True
            )

    return jsonify({
        "status": "ok"
    }), 200


# ============================================================
# تنقيح النص - Gemini 3.5
# ============================================================

@app.route("/refine/<job_id>", methods=["POST"])
def refine_job(job_id):
    if not REFINE_ENABLED or refine_arabic_text is None:
        return jsonify({"error": "Refine disabled - check GEMINI_API_KEY env var"}), 503

    try:
        tg_user = _current_telegram_user()
    except ValueError as auth_error:
        return _unauthorized(auth_error)

    job = get_job(job_id)
    if job is None or not _job_belongs_to(job, tg_user):
        return jsonify({"error": "Job not found"}), 404

    output_file = job.get("output_file")
    if not output_file or not os.path.exists(output_file):
        return jsonify({"error": "Output file not found - job may have expired"}), 404

    try:
        raw_text = Path(output_file).read_text(encoding="utf-8")
        if not raw_text.strip():
            return jsonify({"error": "Empty transcript"}), 400

        refined = refine_arabic_text(raw_text)

        refined_path = Path(output_file).with_name(Path(output_file).stem + "_refined.txt")
        refined_path.write_text(refined, encoding="utf-8")

        telegram_user_id = job.get("telegram_user_id")
        if telegram_user_id:
            try:
                send_transcript_to_telegram(
                    telegram_user_id,
                    refined_path,
                    download_name=refined_path.name,
                    caption="✨ النسخة المحسنة إملائياً"
                )
            except Exception as te:
                print(f"[REFINE TELEGRAM ERROR] {te}", flush=True)

        return jsonify({"status": "ok", "refined_preview": refined[:800]}), 200

    except Exception as e:
        print(f"[REFINE ERROR] job {job_id}: {e}", flush=True)
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=10000
    )
