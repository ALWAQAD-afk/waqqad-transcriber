"""إرسال رسالة جماعية إلى مستخدمي البوت بأمان.

الوحدة مستقلة عن Flask وتيليجرام كي تُختبر وحدها. هي مسؤولة عن:
- التحقق من نص الرسالة قبل الإرسال.
- احترام الفاصل الزمني بين الرسائل حتى لا يوقف تيليجرام البوت.
- إعادة المحاولة عند خطأ 429 باستخدام ``retry_after`` الذي يرسله تيليجرام.
- إحصاء الناجح والفاشل وإبلاغ المتصل بالتقدم.
"""

import re
import time

# حد تيليجرام 4096 محرفًا، ونترك هامشًا للرموز التعبيرية التي تُحسب
# محرفين أحيانًا، ولأي إضافة مستقبلية.
MAX_TEXT_CHARS = 4000

# نحو 20 رسالة في الثانية؛ أقل من حد تيليجرام العام (30/ثانية).
DEFAULT_GAP_SECONDS = 0.05

_RETRY_AFTER_RE = re.compile(r'"retry_after"\s*:\s*(\d+)')


def clean_text(text):
    """يطبّع نص الرسالة ويرفض الفارغ أو الأطول من حد تيليجرام."""
    text = (text or "").strip()
    if not text:
        raise ValueError("نص الرسالة فارغ.")
    # تيليجرام يقيس طول الرسالة بوحدات UTF-16، والرموز التعبيرية تُحسب
    # محرفين. نقيس الطولين معًا حتى لا تُرفض الرسالة من تيليجرام لاحقًا.
    utf16_units = len(text.encode("utf-16-le")) // 2
    if len(text) > MAX_TEXT_CHARS or utf16_units > MAX_TEXT_CHARS:
        raise ValueError(
            f"النص أطول من {MAX_TEXT_CHARS} حرفًا؛ اختصره ثم أعد المحاولة."
        )
    return text

def retry_after_seconds(error):
    """يستخرج مهلة الانتظار من رسالة خطأ تيليجرام، أو None."""
    match = _RETRY_AFTER_RE.search(str(error or ""))
    return int(match.group(1)) if match else None


def send_all(
    user_ids,
    text,
    send,
    sleep=time.sleep,
    gap=DEFAULT_GAP_SECONDS,
    on_progress=None,
    retry_limit=1,
):
    """يرسل النص لكل معرّف على انفراد، ويعيد ملخصًا.

    ``send`` دالة تستقبل (user_id, text) وترفع استثناءً عند الفشل.
    ``on_progress`` تُستدعى بعد كل مستخدم بـ
    (المنجز، الناجح، الفاشل، المجموع).
    """
    recipients = list(dict.fromkeys(str(uid) for uid in user_ids if uid))
    total = len(recipients)
    sent = 0
    failed_ids = []

    for index, user_id in enumerate(recipients, start=1):
        attempt = 0
        while True:
            try:
                send(user_id, text)
                sent += 1
                break
            except Exception as error:
                wait = retry_after_seconds(error)
                if wait is not None and attempt < retry_limit:
                    attempt += 1
                    sleep(max(1, min(wait, 60)))
                    continue
                failed_ids.append(user_id)
                break

        if on_progress is not None:
            on_progress(index, sent, len(failed_ids), total)

        if index < total and gap:
            sleep(gap)

    return {
        "total": total,
        "sent": sent,
        "failed": len(failed_ids),
        "failed_ids": failed_ids,
    }
