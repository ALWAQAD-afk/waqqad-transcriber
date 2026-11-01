"""استقبال الملفات الصوتية المرسلة إلى البوت مباشرة.

تليجرام يقبل أن يرسل المستخدم أي ملف إلى البوت، لكن البوت لا يستطيع
تنزيل ما يتجاوز 20MB عبر getFile. لذلك نتحقق من الحجم قبل التنزيل،
ونتعامل مع الحد برسالة واضحة وبديل عملي بدل رفض جاف.

هذه الوحدة لا تستورد app.py ولا تعرف شيئًا عن قاعدة البيانات أو الطابور،
فتظل قابلة للاختبار وحدها.
"""

import logging
import os
from pathlib import Path

import requests

log = logging.getLogger("bot_upload")

TELEGRAM_API = "https://api.telegram.org"

MAX_UPLOAD_MB = float(
    os.environ.get("TRANSCRIBER_MAX_UPLOAD_MB", "20") or 20
)
MAX_UPLOAD_BYTES = int(MAX_UPLOAD_MB * 1024 * 1024)

AUDIO_EXTENSIONS = {
    ".mp3", ".m4a", ".m4b", ".aac", ".ogg", ".oga", ".opus", ".wav",
    ".flac", ".wma", ".amr", ".mpga", ".mpeg", ".3gp",
}

VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".3gp",
}

SUPPORTED_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS


class UploadError(Exception):
    """خطأ قابل للعرض للمستخدم."""


class FileTooLarge(UploadError):
    def __init__(self, size, limit):
        self.size = int(size or 0)
        self.limit = int(limit or 0)
        super().__init__(
            f"file is {human_size(self.size)}, limit is {human_size(self.limit)}"
        )


class UnsupportedFile(UploadError):
    pass


class DownloadFailed(UploadError):
    pass


def human_size(num_bytes):
    """حجم مقروء بالعربية المختصرة."""
    try:
        value = float(num_bytes or 0)
    except (TypeError, ValueError):
        return "غير معروف"
    for unit in ("بايت", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "بايت":
                return f"{int(value)} بايت"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _extension(name):
    return Path(name or "").suffix.lower()


def extract_attachment(message):
    """يستخرج الملف من رسالة تليجرام إن وُجد.

    يعيد dict أو None. لا يقرر القبول النهائي؛ القرار في is_supported.
    """
    message = message or {}

    candidates = (
        ("audio", message.get("audio")),
        ("voice", message.get("voice")),
        ("document", message.get("document")),
        ("video", message.get("video")),
    )

    for kind, obj in candidates:
        if not obj:
            continue

        default_name = {
            "audio": "audio.mp3",
            "voice": "voice.ogg",
            "document": "document",
            "video": "video.mp4",
        }[kind]

        return {
            "kind": kind,
            "file_id": obj.get("file_id"),
            "file_unique_id": obj.get("file_unique_id"),
            "file_name": obj.get("file_name") or default_name,
            "file_size": obj.get("file_size") or 0,
            "mime_type": obj.get("mime_type"),
            "duration": obj.get("duration"),
        }

    return None


def is_supported(attachment):
    """هل الملف صوت/فيديو يمكن لـ ffmpeg التعامل معه؟"""
    if not attachment or not attachment.get("file_id"):
        return False

    mime = (attachment.get("mime_type") or "").lower()
    if mime.startswith(("audio/", "video/")):
        return True

    extension = _extension(attachment.get("file_name"))
    if not extension:
        # ملفات تليجرام بلا اسم ولا نوع: نجرّب، ffmpeg سيحكم.
        return attachment.get("kind") in {"audio", "voice", "video"}

    return extension in SUPPORTED_EXTENSIONS


def download_telegram_file(
    bot_token,
    file_id,
    destination,
    max_bytes=MAX_UPLOAD_BYTES,
    timeout=(15, 600),
):
    """ينزّل الملف إلى المسار المحدد ويعيد حجمه بالبايت.

    التنزيل متدفّق مع سقف صارم: لا نثق بـ file_size القادم من تليجرام
    وحده. عند التجاوز نحذف ما نزّلناه ونرفع FileTooLarge.
    """
    if not bot_token or not file_id:
        raise DownloadFailed("missing token or file id")

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    session = requests.Session()

    try:
        response = session.get(
            f"{TELEGRAM_API}/bot{bot_token}/getFile",
            params={"file_id": file_id},
            timeout=timeout,
        )
        payload = response.json() if response.content else {}

        if response.status_code != 200 or not payload.get("ok"):
            raise DownloadFailed(
                f"getFile failed: HTTP {response.status_code} "
                f"{str(payload)[:200]}"
            )

        file_path = (payload.get("result") or {}).get("file_path")
        reported_size = int((payload.get("result") or {}).get("file_size") or 0)

        if reported_size and reported_size > max_bytes:
            raise FileTooLarge(reported_size, max_bytes)

        if not file_path:
            raise DownloadFailed("getFile returned no file_path")

        written = 0
        with session.get(
            f"{TELEGRAM_API}/file/bot{bot_token}/{file_path}",
            stream=True,
            timeout=timeout,
        ) as stream:
            if stream.status_code != 200:
                raise DownloadFailed(
                    f"download failed: HTTP {stream.status_code}"
                )

            with open(destination, "wb") as handle:
                for chunk in stream.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > max_bytes:
                        handle.close()
                        destination.unlink(missing_ok=True)
                        raise FileTooLarge(written, max_bytes)
                    handle.write(chunk)

        if written == 0:
            destination.unlink(missing_ok=True)
            raise DownloadFailed("downloaded file is empty")

        return written

    except UploadError:
        raise
    except requests.RequestException as exc:
        destination.unlink(missing_ok=True)
        raise DownloadFailed(str(exc)) from exc
    finally:
        session.close()
