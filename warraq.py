import os
import requests
import psycopg


WARRAQ_BOT_TOKEN = os.environ.get("WARRAQ_BOT_TOKEN")
WARRAQ_STORAGE_CHAT_ID = os.environ.get("WARRAQ_STORAGE_CHAT_ID")
WARRAQ_DATABASE_URL = os.environ.get("WARRAQ_DATABASE_URL")


# ============================================================
# Database
# ============================================================

def get_db():
    return psycopg.connect(WARRAQ_DATABASE_URL)


def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS files (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    filename TEXT NOT NULL,
                    file_size BIGINT NOT NULL DEFAULT 0,
                    telegram_file_id TEXT,
                    storage_message_id BIGINT NOT NULL,
                    folder TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)


def add_file_to_db(
    user_id,
    filename,
    file_size,
    telegram_file_id,
    storage_message_id,
    folder
):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO files (
                    user_id,
                    filename,
                    file_size,
                    telegram_file_id,
                    storage_message_id,
                    folder
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    user_id,
                    filename,
                    file_size,
                    telegram_file_id,
                    storage_message_id,
                    folder
                )
            )

            return cur.fetchone()[0]


# ============================================================
# Telegram API
# ============================================================

def telegram_api(method, data=None):
    url = (
        f"https://api.telegram.org/"
        f"bot{WARRAQ_BOT_TOKEN}/{method}"
    )

    return requests.post(
        url,
        json=data or {},
        timeout=30
    )


def send_message(chat_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text
    }

    if reply_markup:
        payload["reply_markup"] = reply_markup

    return telegram_api("sendMessage", payload)


def answer_callback(callback_query_id, text=None):
    payload = {
        "callback_query_id": callback_query_id
    }

    if text:
        payload["text"] = text

    return telegram_api(
        "answerCallbackQuery",
        payload
    )


# ============================================================
# Folders
# ============================================================

FOLDERS = {
    "books": "📚 الكتب",
    "university": "🎓 الجامعة",
    "lectures": "🎧 المحاضرات",
    "projects": "🛠 المشاريع",
    "other": "📦 أخرى",
    "uncategorized": "📥 غير مصنف"
}


def folder_keyboard(message_id):
    return {
        "inline_keyboard": [
            [
                {
                    "text": "📚 الكتب",
                    "callback_data": f"save:books:{message_id}"
                },
                {
                    "text": "🎓 الجامعة",
                    "callback_data": f"save:university:{message_id}"
                }
            ],
            [
                {
                    "text": "🎧 المحاضرات",
                    "callback_data": f"save:lectures:{message_id}"
                },
                {
                    "text": "🛠 المشاريع",
                    "callback_data": f"save:projects:{message_id}"
                }
            ],
            [
                {
                    "text": "📦 أخرى",
                    "callback_data": f"save:other:{message_id}"
                },
                {
                    "text": "⚡ بلا تصنيف",
                    "callback_data": f"save:uncategorized:{message_id}"
                }
            ]
        ]
    }


# ============================================================
# Save
# ============================================================

def save_file(
    source_chat_id,
    source_message_id,
    user_id,
    folder_code
):
    folder_name = FOLDERS.get(
        folder_code,
        "📥 غير مصنف"
    )

    caption = (
        f"📚 الوَرّاق\n\n"
        f"📁 {folder_name}\n"
        f"👤 المالك: {user_id}\n\n"
        f"#warraq "
        f"#u_{user_id} "
        f"#f_{folder_code}"
    )

    response = telegram_api(
        "copyMessage",
        {
            "chat_id": WARRAQ_STORAGE_CHAT_ID,
            "from_chat_id": source_chat_id,
            "message_id": source_message_id,
            "caption": caption
        }
    )

    return response, folder_name


# ============================================================
# Messages
# ============================================================

def handle_message(message):
    chat_id = message.get(
        "chat", {}
    ).get("id")

    user_id = message.get(
        "from", {}
    ).get("id")

    message_id = message.get("message_id")
    text = message.get("text")


    if text == "/start":
        send_message(
            chat_id,
            "📚 أهلاً بك في الوَرّاق.\n\n"
            "أرسل إليّ أي ملف، ثم اختر أين تريد حفظه."
        )
        return


    if text == "/help":
        send_message(
            chat_id,
            "📚 الوَرّاق\n\n"
            "أرسل ملفًا، ثم اختر القسم المناسب."
        )
        return


    if "document" in message:
        document = message["document"]

        filename = document.get(
            "file_name",
            "ملف"
        )

        file_size = document.get(
            "file_size",
            0
        )

        size_mb = file_size / 1024 / 1024

        send_message(
            chat_id,
            (
                f"📄 {filename}\n"
                f"📦 {size_mb:.2f} MB\n\n"
                "أين تريد حفظ الملف؟"
            ),
            folder_keyboard(message_id)
        )

        return


    send_message(
        chat_id,
        "📎 أرسل ملفًا إلى الوَرّاق."
    )


# ============================================================
# Buttons
# ============================================================

def handle_callback(callback):
    callback_id = callback.get("id")

    user_id = callback.get(
        "from", {}
    ).get("id")

    message = callback.get(
        "message", {}
    )

    chat_id = message.get(
        "chat", {}
    ).get("id")

    data = callback.get("data", "")


    if not data.startswith("save:"):
        answer_callback(callback_id)
        return


    try:
        _, folder_code, source_message_id = data.split(":")
        source_message_id = int(source_message_id)

    except Exception:
        answer_callback(
            callback_id,
            "❌ زر غير صالح."
        )
        return


    answer_callback(
        callback_id,
        "⏳ جارٍ الحفظ..."
    )


    # نحتاج بيانات الملف الأصلي من Telegram
    # callback نفسه لا يحتوي document،
    # لذلك نستعمل forwardMessage مؤقتًا؟ لا.
    #
    # المعلومات المهمة نستخرجها من الرسالة الأصلية
    # عبر copyMessage ثم نسجل ما نستطيع الآن.

    response, folder_name = save_file(
        source_chat_id=chat_id,
        source_message_id=source_message_id,
        user_id=user_id,
        folder_code=folder_code
    )


    if not response.ok:
        print(
            "[WARRAQ COPY ERROR]",
            response.status_code,
            response.text,
            flush=True
        )

        send_message(
            chat_id,
            "❌ تعذر حفظ الملف."
        )
        return


    result = response.json().get(
        "result",
        {}
    )

    storage_message_id = result.get(
        "message_id"
    )


    try:
        file_db_id = add_file_to_db(
            user_id=user_id,
            filename="Telegram file",
            file_size=0,
            telegram_file_id=None,
            storage_message_id=storage_message_id,
            folder=folder_code
        )

    except Exception as e:
        print(
            "[WARRAQ DATABASE ERROR]",
            repr(e),
            flush=True
        )

        send_message(
            chat_id,
            "⚠️ حُفظ الملف في Telegram، "
            "لكن تعذر تسجيله في الفهرس."
        )
        return


    send_message(
        chat_id,
        (
            "✅ تم حفظ الملف في الوَرّاق.\n\n"
            f"📁 {folder_name}\n"
            f"🆔 {file_db_id}"
        )
    )


# ============================================================
# Main
# ============================================================

def handle_warraq_update(update):
    if "callback_query" in update:
        handle_callback(
            update["callback_query"]
        )
        return

    if "message" in update:
        handle_message(
            update["message"]
        )


# إنشاء الجدول عند تحميل التطبيق
try:
    init_db()
    print(
        "[WARRAQ DATABASE] Ready",
        flush=True
    )

except Exception as e:
    print(
        "[WARRAQ DATABASE INIT ERROR]",
        repr(e),
        flush=True
    )
