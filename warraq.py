import os
import requests


WARRAQ_BOT_TOKEN = os.environ.get("WARRAQ_BOT_TOKEN")
WARRAQ_STORAGE_CHAT_ID = os.environ.get("WARRAQ_STORAGE_CHAT_ID")


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


def send_message(chat_id, text):
    return telegram_api(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text
        }
    )


def handle_warraq_update(update):

    message = update.get("message")

    if not message:
        return

    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")

    if not chat_id or not message_id:
        return

    # /start
    if message.get("text") == "/start":
        send_message(
            chat_id,
            "📚 أهلاً بك في الوَرّاق.\n\n"
            "أرسل إليّ أي ملف وسأحفظه لك."
        )
        return

    # ملف
    if "document" in message:

        response = telegram_api(
            "copyMessage",
            {
                "chat_id": WARRAQ_STORAGE_CHAT_ID,
                "from_chat_id": chat_id,
                "message_id": message_id
            }
        )

        if response.ok:
            send_message(
                chat_id,
                "✅ تم حفظ الملف في الوَرّاق."
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

        return

    send_message(
        chat_id,
        "📎 أرسل ملفًا لأحفظه في الوَرّاق."
    )
