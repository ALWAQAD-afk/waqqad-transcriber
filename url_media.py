"""تنزيل الصوت من رابط يوتيوب أو رابط ملف مباشر.

الوحدة لا تعرف شيئًا عن Flask أو قاعدة البيانات: تستقبل رابطًا ومجلدًا،
وتعيد مسار ملف صوتي جاهز للتفريغ. هذا يجعلها قابلة للاختبار وحدها.

حدود الأمان:
- http/https فقط.
- رفض العناوين الداخلية (localhost وشبكات private) لمنع SSRF.
- فحص كل تحويل (redirect) قبل اتباعه.
- حدّ أعلى لحجم التنزيل.
- يوتيوب فقط يُمرَّر إلى yt-dlp؛ بقية الروابط تُحمَّل مباشرة عبر HTTP.
"""

import ipaddress
import mimetypes
import os
import re
import socket
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests

SUPPORTED_EXTENSIONS = {
    ".aac", ".flac", ".m4a", ".mkv", ".mov",
    ".mp3", ".mp4", ".ogg", ".opus", ".wav", ".webm",
}

YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}

MAX_REDIRECTS = 5
DOWNLOAD_TIMEOUT = (20, 120)
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class UrlError(Exception):
    """خطأ عام في الرابط."""


class UnsupportedUrl(UrlError):
    """الرابط ليس يوتيوب ولا ملف صوت/فيديو مباشر."""


class UrlTooLarge(UrlError):
    """الملف يتجاوز الحد المسموح."""


class UrlDownloadError(UrlError):
    """تعذّر تنزيل الرابط."""


def is_url(text):
    """هل النص رابط http/https صالح الشكل؟"""
    if not isinstance(text, str):
        return False
    text = text.strip()
    if not text or any(ch.isspace() for ch in text):
        return False
    parsed = urlparse(text)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def extract_url(text):
    """يستخرج أول رابط من رسالة، أو None."""
    if not isinstance(text, str):
        return None
    match = re.search(r"https?://[^\s<>\"']+", text.strip())
    if not match:
        return None
    candidate = match.group(0).rstrip(".,;:!?)]}")
    return candidate if is_url(candidate) else None


def is_youtube(url):
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return host in YOUTUBE_HOSTS


def _is_public_host(host):
    """يرفض localhost والشبكات الداخلية. يفشل مغلقًا عند الشك."""
    host = (host or "").strip().lower().rstrip(".")
    if not host or host == "localhost" or host.endswith(".localhost"):
        return False
    if host.endswith(".local") or host.endswith(".internal"):
        return False

    try:
        addresses = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False

    for address in addresses:
        raw_ip = address[4][0].split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False

    return True


def _validate_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsupportedUrl("الرابط يجب أن يبدأ بـ http أو https.")
    if not parsed.hostname:
        raise UnsupportedUrl("الرابط لا يحتوي على عنوان صالح.")
    if not _is_public_host(parsed.hostname):
        raise UnsupportedUrl("هذا العنوان غير مسموح لأسباب أمنية.")
    return parsed


def _safe_filename(name, fallback="media"):
    name = unquote(str(name or "")).strip()
    name = Path(name).name
    name = re.sub(r"[^\w\u0600-\u06FF.\- ]+", "_", name)
    name = re.sub(r"\s+", " ", name).strip(" ._")
    if not name:
        name = fallback
    return name[:120]


def _name_from_url(url):
    parsed = urlparse(url)
    base = unquote(Path(parsed.path).name)
    return _safe_filename(base, fallback="")


def _extension_for_type(content_type, url):
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in SUPPORTED_EXTENSIONS:
        return suffix

    guessed = mimetypes.guess_extension(
        (content_type or "").split(";", 1)[0].strip().lower()
    )
    if guessed == ".oga":
        return ".ogg"
    if guessed == ".m4v":
        return ".mp4"
    return guessed if guessed in SUPPORTED_EXTENSIONS else ""


def _content_disposition_name(value):
    if not value:
        return ""
    match = re.search(
        r"filename\*=(?:UTF-8'')?([^;]+)",
        value,
        flags=re.IGNORECASE,
    )
    if match:
        return unquote(match.group(1).strip().strip('"'))
    match = re.search(r'filename="?([^";]+)', value, flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _open_stream(url):
    """يفتح الرابط مع فحص كل تحويل، ويعيد (response, final_url)."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        _validate_url(current)
        try:
            response = requests.get(
                current,
                stream=True,
                allow_redirects=False,
                timeout=DOWNLOAD_TIMEOUT,
                headers={"User-Agent": USER_AGENT},
            )
        except requests.RequestException as e:
            raise UrlDownloadError(f"تعذّر الوصول إلى الرابط: {e}") from e

        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise UrlDownloadError("تحويل غير صالح من الخادم.")
            current = urljoin(current, location)
            continue

        if response.status_code not in (200, 206):
            response.close()
            raise UrlDownloadError(
                f"الخادم رفض تنزيل الرابط (HTTP {response.status_code})."
            )

        return response, current

    raise UrlDownloadError("عدد التحويلات في الرابط كبير جدًا.")


def _download_direct(url, destination_dir, max_bytes):
    response, final_url = _open_stream(url)

    try:
        content_type = (response.headers.get("Content-Type") or "").lower()
        suffix = _extension_for_type(content_type, final_url)

        if not (
            content_type.startswith("audio/")
            or content_type.startswith("video/")
            or suffix
        ):
            raise UnsupportedUrl(
                "الرابط ليس ملفًا صوتيًا أو مرئيًا مباشرًا. "
                "أرسل رابط يوتيوب أو رابط ملف ينتهي بـ mp3/m4a/wav/mp4."
            )

        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise UrlTooLarge("حجم الملف أكبر من الحد المسموح.")

        name = _content_disposition_name(
            response.headers.get("Content-Disposition")
        )
        if not name:
            name = _name_from_url(final_url)

        stem = Path(name).stem if name else "media"
        if not suffix:
            suffix = Path(name).suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            suffix = ".mp3"
        filename = _safe_filename(f"{stem}{suffix}", fallback="media.mp3")

        target = (destination_dir / filename).resolve()
        if destination_dir.resolve() not in target.parents:
            raise UrlDownloadError("اسم الملف الناتج غير صالح.")

        written = 0
        try:
            with target.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > max_bytes:
                        raise UrlTooLarge(
                            "حجم الملف أكبر من الحد المسموح."
                        )
                    handle.write(chunk)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        finally:
            response.close()

        if written == 0:
            target.unlink(missing_ok=True)
            raise UrlDownloadError("الرابط أعاد ملفًا فارغًا.")

        return target, written, target.name
    finally:
        response.close()


def _download_youtube(url, destination_dir, max_bytes):
    try:
        import yt_dlp
    except ImportError as e:
        raise UrlDownloadError(
            "دعم يوتيوب غير مثبّت على الخادم."
        ) from e

    options = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "max_filesize": max_bytes,
        "retries": 2,
        "fragment_retries": 2,
        "socket_timeout": 60,
        "outtmpl": {"default": "%(title).120B.%(ext)s"},
        "paths": {"home": str(destination_dir)},
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "64",
        }],
        "postprocessor_args": ["-threads", "1"],
    }

    cookies_file = os.environ.get("YTDLP_COOKIES_FILE", "").strip()
    if cookies_file and Path(cookies_file).exists():
        options["cookiefile"] = cookies_file

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as e:
        message = str(e)
        if "larger than max-filesize" in message.lower():
            raise UrlTooLarge("حجم الفيديو أكبر من الحد المسموح.") from e
        raise UrlDownloadError(
            "تعذّر تنزيل رابط يوتيوب. قد يكون الفيديو خاصًا أو محجوبًا."
        ) from e

    title = str((info or {}).get("title") or "").strip()
    files = [
        path for path in destination_dir.iterdir()
        if path.is_file()
        and path.suffix.lower() in SUPPORTED_EXTENSIONS
        and not path.name.endswith(".part")
    ]
    if not files:
        raise UrlDownloadError("لم ينتج عن تنزيل يوتيوب ملف صوتي.")

    target = max(files, key=lambda path: path.stat().st_size)
    size = target.stat().st_size
    if size > max_bytes:
        target.unlink(missing_ok=True)
        raise UrlTooLarge("حجم الملف أكبر من الحد المسموح.")

    display_name = _safe_filename(title, fallback="") or target.name
    return target, size, display_name


def display_name_for_url(url):
    """اسم مبدئي يظهر في رسالة الاستلام قبل معرفة عنوان يوتيوب."""
    if is_youtube(url):
        return "رابط يوتيوب"
    name = _name_from_url(url)
    return name or "رابط خارجي"


def download_url(url, destination_dir, max_bytes):
    """ينزّل الرابط ويعيد (المسار، الحجم، الاسم الظاهر)."""
    if not is_url(url):
        raise UnsupportedUrl("الرابط غير صالح.")
    _validate_url(url)

    destination = Path(destination_dir)
    destination.mkdir(parents=True, exist_ok=True)
    max_bytes = max(1, int(max_bytes))

    if is_youtube(url):
        return _download_youtube(url, destination, max_bytes)
    return _download_direct(url, destination, max_bytes)
