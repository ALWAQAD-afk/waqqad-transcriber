import os
import requests


WARRAQ_BOT_TOKEN = os.environ.get("WARRAQ_BOT_TOKEN")
WARRAQ_STORAGE_CHAT_ID = os.environ.get("WARRAQ_STORAGE_CHAT_ID")


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

    return telegram_api(
        "sendMessage",
        payload
    )


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
# الأقسام
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
                    "callback_data":
                        f"save:books:{message_id}"
                },
                {
                    "text": "🎓 الجامعة",
                    "callback_data":
                        f"save:university:{message_id}"
                }
            ],
            [
                {
                    "text": "🎧 المحاضرات",
                    "callback_data":
                        f"save:lectures:{message_id}"
                },
                {
                    "text": "🛠 المشاريع",
                    "callback_data":
                        f"save:projects:{message_id}"
                }
            ],
            [
                {
                    "text": "📦 أخرى",
                    "callback_data":
                        f"save:other:{message_id}"
                },
                {
                    "text": "⚡ بلا تصنيف",
                    "callback_data":
                        f"save:uncategorized:{message_id}"
                }
            ]
        ]
    }


# ============================================================
# حفظ الملف
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
            "chat_id":
                WARRAQ_STORAGE_CHAT_ID,

            "from_chat_id":
                source_chat_id,

            "message_id":
                source_message_id,

            "caption":
                caption
        }
    )

    return response, folder_name


# ============================================================
# الرسائل
# ============================================================

def handle_message(message):

    chat = message.get(
        "chat",
        {}
    )

    chat_id = chat.get("id")

    user = message.get(
        "from",
        {}
    )

    user_id = user.get("id")

    message_id = message.get(
        "message_id"
    )

    text = message.get("text")


    # --------------------------------------------------------
    # /start
    # --------------------------------------------------------

    if text == "/start":

        send_message(
            chat_id,
            "📚 أهلاً بك في الوَرّاق.\n\n"
            "أرسل إليّ أي ملف، "
            "ثم اختر أين تريد حفظه."
        )

        return


    # --------------------------------------------------------
    # /help
    # --------------------------------------------------------

    if text == "/help":

        send_message(
            chat_id,
            "📚 الوَرّاق\n\n"
            "أرسل ملفًا إلى البوت، "
            "ثم اختر القسم المناسب.\n\n"
            "الملف يُخزن داخل Telegram "
            "ولا يمر عبر خادم الوقّاد."
        )

        return


    # --------------------------------------------------------
    # ملف
    # --------------------------------------------------------

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

        size_mb = (
            file_size
            / 1024
            / 1024
        )

        send_message(
            chat_id,
            (
                f"📄 {filename}\n"
                f"📦 {size_mb:.2f} MB\n\n"
                "أين تريد حفظ الملف؟"
            ),
            folder_keyboard(
                message_id
            )
        )

        return


    # --------------------------------------------------------
    # أي شيء آخر
    # --------------------------------------------------------

    send_message(
        chat_id,
        "📎 أرسل ملفًا إلى الوَرّاق."
    )


# ============================================================
# الأزرار
# ============================================================

def handle_callback(callback):

    callback_id = callback.get("id")

    user = callback.get(
        "from",
        {}
    )

    user_id = user.get("id")

    message = callback.get(
        "message",
        {}
    )

    chat_id = (
        message
        .get("chat", {})
        .get("id")
    )

    data = callback.get(
        "data",
        ""
    )


    if not data.startswith("save:"):
        answer_callback(
            callback_id
        )
        return


    try:

        _, folder_code, source_message_id = (
            data.split(":")
        )

        source_message_id = int(
            source_message_id
        )

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


    response, folder_name = save_file(
        source_chat_id=chat_id,
        source_message_id=source_message_id,
        user_id=user_id,
        folder_code=folder_code
    )


    if response.ok:

        send_message(
            chat_id,
            (
                "✅ تم حفظ الملف في الوَرّاق.\n\n"
                f"📁 {folder_name}"
            )
        )

    else:

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


# ============================================================
# المدخل الرئيسي
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
