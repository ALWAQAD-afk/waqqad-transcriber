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

import base64
import ipaddress
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import time
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


# عبارات تظهر في خطأ يوتيوب عندما يطلب التحقق من أن الطلب ليس آليًا.
# «Failed to extract any player response» غالبًا نفس الحجب لكن بعبارات
# كليّة في النسخ الحديثة؛ من دونها لن تُجرَّب العملاء البدلاء أصلًا.
BOT_CHECK_HINTS = (
    "sign in to confirm",
    "not a bot",
    "confirm you're not a bot",
    "confirm you’re not a bot",
    "use --cookies",
    "cookies-from-browser",
    "login required",
    "failed to extract any player response",
    "unable to extract player response",
)

# عملاء لا تحتاج جلسة حساب ولا تحل تحدي JavaScript. هذه أول ما يجب
# تجربته لأنها تعمل من عنوان الخادم حتى من دون كوكيز أو PO Token.
# الأسماء تُفلتر دائمًا مقابل قائمة yt-dlp المثبّتة.
YOUTUBE_JS_FREE_CLIENTS = ("visionos", "android_vr", "ios", "tv_samsung")

# عملاء تدعم الكوكيز. مع جلسة يعتبرها yt-dlp موثّقة يكون هذا الترتيب
# أفضل من العملاء المجهولة، لأن يوتيوب يمنحها صيغًا إضافية أحيانًا.
YOUTUBE_COOKIE_CLIENTS = (
    "tv_samsung", "web_embedded", "web", "mweb", "web_safari", "tv",
)

# عملاء ويب تحتاج PO Token. لا تُجرَّب إلا بعد التحقق من أن المزوّد
# يعمل فعليًا (لا يكفي وجود ملفاته)، وإلا فهي تعيد «قصصًا» فقط.
YOUTUBE_POT_CLIENTS = (
    ("mweb",),
    ("web_safari", "web"),
    ("tv",),
)

# آخر محاولة دائمًا: بلا كوكيز وبأعمدة لا تحتاج JS. لا تُحذف أبدًا؛
# هي شبكة الأمان التي تعمل حين تفشل الكوكيز أو مزوّد PO Token.
YOUTUBE_BARE_CLIENTS = ("visionos", "android_vr", "ios", "tv_samsung")

# مزوّد توكن إثبات الأصل (PO Token): يوتيوب يرفض منح صيغ الصوت لعملاء
# الويب القادمين من عنوان مركز بيانات إلا بتوكن يُولَّد بتشغيل متصفح
# وهمي على الخادم. السكربت ووحداته تأتيان من صورة bgutil في Dockerfile.
POT_SERVER_HOME = "/opt/bgutil/server"
POT_SCRIPT_NAME = "generate_once.ts"
POT_CHECK_TIMEOUT = float(os.environ.get("YTDLP_POT_CHECK_TIMEOUT", "60"))
_POT_STATE_CACHE = {"checked_at": 0.0, "value": None}
POT_STATE_TTL_READY = 600
POT_STATE_TTL_FAILED = 300

# مفاتيح الكوكيز التي تدل على جلسة حساب يوتيوب كاملة. غياب بعضها يعني
# غالبًا أن الملف صُدِّر تصديرًا ناقصًا، لا أن الحساب نفسه معطوب.
COOKIE_KEY_NAMES = (
    "SID",
    "HSID",
    "SSID",
    "APISID",
    "SAPISID",
    "__Secure-1PSID",
    "__Secure-3PSID",
    "__Secure-1PAPISID",
    "__Secure-3PAPISID",
    "LOGIN_INFO",
)

# yt-dlp يشترط LOGIN_INFO مع أحد مفاتيح SAPISID كي يعتبر الكوكيز جلسة
# مسجّلة. غياب LOGIN_INFO يجعلها «مجهولة» حتى لو كان المتصفح مسجّلًا.
COOKIE_AUTH_SAPISID_KEYS = (
    "SAPISID",
    "__Secure-3PAPISID",
    "__Secure-1PAPISID",
)


def supported_youtube_clients():
    """أسماء العملاء المدعومة فعليًا في نسخة yt-dlp المثبّتة."""
    try:
        from yt_dlp.extractor.youtube._base import INNERTUBE_CLIENTS
    except Exception:
        return ()
    return tuple(sorted(
        name for name in INNERTUBE_CLIENTS if not name.startswith("_")
    ))


def _youtube_clients(names):
    """يحذف أي اسم عميل غير مدعوم في النسخة الحالية."""
    supported = set(supported_youtube_clients())
    if not supported:
        return tuple(names)
    return tuple(name for name in names if name in supported)


def _youtube_client_supports_cookies(name):
    try:
        from yt_dlp.extractor.youtube._base import INNERTUBE_CLIENTS
        return bool(INNERTUBE_CLIENTS.get(name, {}).get("SUPPORTS_COOKIES"))
    except Exception:
        return name in YOUTUBE_COOKIE_CLIENTS


def ejs_solver_state():
    """هل حزمة حل تحديات JavaScript (EJS) مثبّتة مع yt-dlp؟

    yt-dlp لا يسحب حزمة yt-dlp-ejs تلقائيًا عند التثبيت العادي؛ ومن
    دونها يفشل حل تحدّي n/sig حتى لو كانت Deno مثبّتة.
    """
    try:
        import yt_dlp_ejs  # noqa: F401
    except Exception:
        return "غير مثبّت ❌ (لن يُحل تحدّي JavaScript)"
    return "متوفر ✅"


def _pot_script_path():
    return Path(POT_SERVER_HOME) / "src" / POT_SCRIPT_NAME


def _pot_quick_issue():
    server = Path(POT_SERVER_HOME)
    if not _pot_script_path().is_file():
        return "غير مثبّت"
    if not (server / "node_modules").is_dir():
        return "ناقص (node_modules)"
    if not shutil.which("deno"):
        return "غير مثبّت (لا Deno)"
    return None


def _pot_script_command(deno_path):
    server = Path(POT_SERVER_HOME)
    node_modules = server / "node_modules"
    cache_dir = Path(
        os.environ.get("XDG_CACHE_HOME")
        or (Path(os.environ.get("HOME") or server) / ".cache")
    ) / "bgutil-ytdlp-pot-provider"
    command = [
        deno_path, "run",
        "--allow-env", "--allow-net",
        f"--allow-ffi={node_modules}",
        f"--allow-write={cache_dir}",
        f"--allow-read={cache_dir},{node_modules}",
        str(_pot_script_path()),
    ]
    return command


def _pot_script_env():
    env = os.environ.copy()
    env["DENO_NO_PROMPT"] = "1"
    env["DENO_NO_UPDATE_CHECK"] = "1"
    env["FORCE_COLOR"] = "false"
    return env


def pot_provider_state(force=False):
    """هل مزوّد PO Token جاهز فعليًا، لا مجرد موجود على القرص؟

    يفحص أن سكربت Deno يعمل ويطبع إصداره، وهو نفس الفحص الذي يجريه
    yt-dlp قبل استخدام المزوّد. النتيجة تُخزَّن مؤقتًا لأن الفحص قد
    يستغرق ثواني على المعالجات البطيئة.
    """
    now = time.monotonic()
    cached = _POT_STATE_CACHE.get("value")
    if cached is not None and not force:
        ttl = (
            POT_STATE_TTL_READY
            if cached.startswith("جاهز")
            else POT_STATE_TTL_FAILED
        )
        if now - _POT_STATE_CACHE["checked_at"] < ttl:
            return cached

    issue = _pot_quick_issue()
    if issue:
        _store_pot_state(issue, issue.startswith("جاهز"))
        return issue

    deno_path = shutil.which("deno")
    try:
        completed = subprocess.run(
            _pot_script_command(deno_path) + ["--version"],
            env=_pot_script_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=POT_CHECK_TIMEOUT,
            text=True,
            check=False,
        )
    except subprocess.TimeoutExpired:
        value = "غير جاهز (انتهت مهلة فحص Deno)"
        _store_pot_state(value, False)
        return value
    except Exception as e:
        value = f"غير جاهز (تعذّر تشغيل Deno: {type(e).__name__})"
        _store_pot_state(value, False)
        return value

    output = (completed.stdout or "").strip().splitlines()
    version = output[-1].strip() if output else ""
    if completed.returncode or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        value = "غير جاهز (فشل سكربت Deno)"
        _store_pot_state(value, False)
        return value

    value = f"جاهز ✅ (سكربت {version})"
    _store_pot_state(value, True)
    return value


def _store_pot_state(value, ready=None):
    _POT_STATE_CACHE["value"] = value
    _POT_STATE_CACHE["checked_at"] = time.monotonic()


def pot_provider_ready():
    return pot_provider_state().startswith("جاهز")


def _relax_pot_timeouts():
    """يرفع مهلة فحص/توليد المزوّد على الخوادم البطيئة.

    مهلة فحص السكربت في إضافة bgutil هي 15 ثانية، وقد يتجاوزها تشغيل
    Deno البارد على معالجات صغيرة فيُعدّ المزوّد غير متاح رغم سلامته.
    """
    try:
        from yt_dlp_plugins.extractor.getpot_bgutil import BgUtilPTPBase
        BgUtilPTPBase._GETPOT_TIMEOUT = max(
            getattr(BgUtilPTPBase, "_GETPOT_TIMEOUT", 0.0), 90.0
        )
    except Exception:
        pass
    try:
        from yt_dlp_plugins.extractor.getpot_bgutil_script import (
            BgUtilScriptPTPBase,
        )
        BgUtilScriptPTPBase._GET_SCRIPT_VSN_TIMEOUT = max(
            getattr(BgUtilScriptPTPBase, "_GET_SCRIPT_VSN_TIMEOUT", 0.0),
            90.0,
        )
    except Exception:
        pass


def _redact_media_urls(message):
    """يحجب رموز الروابط الحسّاسة قبل كتابة أي سطر في السجل."""
    message = re.sub(
        r"(/pot/)[A-Za-z0-9_\-%~.]+",
        r"\1<محجوب>",
        message,
        flags=re.IGNORECASE,
    )
    message = re.sub(
        r"([?&](?:sig|signature|token|pot|expire|ip|sparams|lsig)=)[^&\s]+",
        r"\1<محجوب>",
        message,
        flags=re.IGNORECASE,
    )
    return message


def _short_reason(error):
    """سطر قصير من خطأ yt-dlp يصلح للسجلّات دون أسرار."""
    message = " ".join(str(error or "").split())
    if message.startswith("ERROR: "):
        message = message[7:]
    return _redact_media_urls(message)[:280]


def classify_youtube_error(message):
    """يصنّف خطأ يوتيوب: fatal/auth/player/format/download/size/other."""
    lower = (message or "").lower()
    if "larger than max-filesize" in lower:
        return "size"
    if "ffmpeg" in lower and "not found" in lower:
        return "ffmpeg"
    if any(hint in lower for hint in FATAL_YOUTUBE_HINTS):
        return "fatal"
    if any(
        hint in lower
        for hint in (
            "sign in to confirm",
            "not a bot",
            "confirm you're not a bot",
            "confirm you’re not a bot",
            "login required",
            "use --cookies",
        )
    ):
        return "auth"
    if any(
        hint in lower
        for hint in (
            "failed to extract any player response",
            "unable to extract player response",
            "the page needs to be reloaded",
            "player response",
        )
    ):
        return "player"
    if any(
        hint in lower
        for hint in (
            "requested format is not available",
            "no video formats found",
            "no audio formats found",
        )
    ):
        return "format"
    if any(
        hint in lower
        for hint in (
            "http error 403",
            "403: forbidden",
            "unable to download",
            "got error",
            "connection",
            "timed out",
            "timeout",
            "fragment",
        )
    ):
        return "download"
    return "other"


def _looks_like_bot_check(message):
    lower = (message or "").lower()
    return any(hint in lower for hint in BOT_CHECK_HINTS)


def _youtube_failure(message, category=None):
    """رسالة عربية للمستخدم، مع التفاصيل الحقيقية في ``detail`` للسجل."""
    category = category or classify_youtube_error(message)
    if category == "size":
        return UrlTooLarge("حجم الفيديو أكبر من الحد المسموح.")

    lower = (message or "").lower()
    if category == "fatal" or any(
        word in lower
        for word in ("private", "unavailable", "removed", "blocked")
    ):
        error = UrlDownloadError(
            "الفيديو غير متاح: قد يكون خاصًا أو محذوفًا "
            "أو محجوبًا في منطقة الخادم."
        )
    elif category == "auth" or _looks_like_bot_check(message):
        error = UrlDownloadError(
            "يوتيوب رفض الطلب من عنوان الخادم (فحص تحقق آلي). "
            "أضف كوكيز يوتيوب صالحة في إعدادات الخدمة ثم أعد المحاولة."
        )
    elif category == "format":
        error = UrlDownloadError(
            "يوتيوب لم يعطِ أي صيغة صوتية قابلة للاستخدام لهذا الخادم "
            "(المشكلة في بيئة الخادم، وليست في الفيديو). "
            "تأكد من تثبيت Deno وحزمة EJS، ومن عمل مزوّد PO Token."
        )
    elif category == "player":
        error = UrlDownloadError(
            "يوتيوب أعاد استجابة مشغّل غير صالحة لهذا العميل. "
            "جرّب عميلًا آخر أو أعد المحاولة بعد قليل."
        )
    elif category == "ffmpeg":
        error = UrlDownloadError(
            "نُزّل الصوت لكن الخادم يفتقد ffmpeg/ffprobe لتحويله. "
            "ثبّتهما في صورة الخادم ثم أعد المحاولة."
        )
    elif category == "download":
        error = UrlDownloadError(
            "تعذّر تنزيل مسار الصوت من يوتيوب (رابط مرفوض أو 403). "
            "قد يكون السبب توكن إثبات أصل ناقصًا أو شبكة الخادم."
        )
    else:
        error = UrlDownloadError(
            "تعذّر تنزيل رابط يوتيوب. قد يكون الفيديو خاصًا أو محجوبًا."
        )

    error.detail = _short_reason(message)
    return error


def validate_cookies_text(text):
    """يتحقق من ملف كوكيز Netscape ويعدّ سطور يوتيوب الصالحة."""
    if not isinstance(text, str) or not text.strip():
        raise UrlDownloadError("ملف الكوكيز فارغ.")

    if "\x00" in text:
        raise UrlDownloadError("الملف يبدو ثنائيًا وليس ملف كوكيز نصيًا.")

    count = 0
    for line in text.splitlines():
        candidate = (
            line[len("#HttpOnly_"):]
            if line.startswith("#HttpOnly_")
            else line
        )
        if not candidate or candidate.startswith("#"):
            continue
        if "youtube.com" not in candidate:
            continue
        if len(candidate.split("\t")) < 7:
            continue
        count += 1

    if count:
        return count

    lower = text.lower()
    if (
        "netscape http cookie file" not in lower
        and "# http cookie file" not in lower
    ):
        raise UrlDownloadError(
            "الملف ليس بصيغة كوكيز Netscape. "
            "صدّره من المتصفح كملف cookies.txt."
        )

    raise UrlDownloadError(
        "الملف لا يحتوي كوكيز يوتيوب صالحة. "
        "تأكد من تسجيل الدخول إلى youtube.com قبل التصدير."
    )


def _youtube_cookies_file(destination_dir, cookies_text=None):
    """يعيد (مسار الكوكيز، هل هو مؤقت؟).

    الأولوية: النص المحفوظ في قاعدة البيانات، ثم ملف على الخادم،
    ثم نص base64 في متغيّر بيئة (أنسب لـ Render).
    """
    if cookies_text and cookies_text.strip():
        path = destination_dir / ".youtube-cookies.txt"
        path.write_text(cookies_text, encoding="utf-8")
        path.chmod(0o600)
        return path, True

    existing = os.environ.get("YTDLP_COOKIES_FILE", "").strip()
    if existing and Path(existing).is_file():
        return Path(existing), False

    encoded = os.environ.get("YTDLP_COOKIES_B64", "").strip()
    if not encoded:
        return None, False

    try:
        raw = base64.b64decode(encoded, validate=True)
    except Exception as e:
        raise UrlDownloadError(
            "إعداد كوكيز يوتيوب غير صالح (YTDLP_COOKIES_B64). "
            "تأكد من نسخ الملف كاملًا بصيغة base64."
        ) from e

    if not raw.strip():
        raise UrlDownloadError("ملف كوكيز يوتيوب فارغ.")

    path = destination_dir / ".youtube-cookies.txt"
    path.write_bytes(raw)
    path.chmod(0o600)
    return path, True


def _youtube_options(
    destination_dir,
    max_bytes,
    cookies_file=None,
    clients=None,
    missing_pot=False,
    pot=False,
    force_pot=False,
    extract_audio=True,
):
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
    }
    if extract_audio:
        options["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "64",
        }]
        options["postprocessor_args"] = ["-threads", "1"]

    if cookies_file:
        options["cookiefile"] = str(cookies_file)

    # مسار ffmpeg صريح: بعض بيئات التشغيل لا تمرّر PATH إلى العمليات
    # الفرعية، فيفشل التحويل الصوتي رغم أن ffmpeg مثبّت.
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg and ffprobe:
        options["ffmpeg_location"] = str(Path(ffmpeg).parent)

    # يوتيوب يحجب الصيغ التي تحتاج PO Token إن لم يتوفر، فلا تظهر أصلًا.
    # إدراجها اختياري: نحاول أولًا الوضع الطبيعي، ثم نضيفها في محاولات
    # لاحقة (قد تنجح، وقد ترجع 403 — نجرب ولا نخسر المحاولات الأخرى).
    youtube_args = {}
    if clients:
        youtube_args["player_client"] = list(clients)
    if missing_pot:
        youtube_args["formats"] = ["missing_pot"]
    if pot and force_pot:
        # السياسة الافتراضية auto لا تطلب توكن المشغّل إلا إذا اعتبره
        # يوتيوب «إلزاميًا»، وهذا لا يحدث مع web/mweb. always تفرض
        # التوليد، وpot_trace يُظهر في السجل ما جرى فعلًا. وplayer_skip
        # يمنع إعادة استخدام استجابة الصفحة المخفّضة (قصص فقط) بدل
        # طلب مشغّل حقيقي بالتوكن.
        youtube_args["fetch_pot"] = ["always"]
        youtube_args["pot_trace"] = ["true"]
        youtube_args["player_skip"] = ["webpage"]
    if youtube_args:
        options["extractor_args"] = {"youtube": youtube_args}
    if pot:
        # يطلب من إضافة bgutil توليد التوكن عبر سكربت Deno الجاهز.
        _relax_pot_timeouts()
        options.setdefault("extractor_args", {})
        options["extractor_args"]["youtubepot-bgutilscript"] = {
            "server_home": [POT_SERVER_HOME]
        }
        deno_path = shutil.which("deno")
        if deno_path:
            options["js_runtimes"] = {"deno": {"path": deno_path}}
    # مخرج اختياري إن حجب يوتيوب عنوان الخادم نهائيًا.
    proxy = os.environ.get("YTDLP_PROXY", "").strip()
    if proxy:
        options["proxy"] = proxy
    return options


# أخطاء تعني أن الفيديو نفسه غير متاح؛ لا فائدة من تجربة عملاء آخرين.
FATAL_YOUTUBE_HINTS = (
    "video unavailable",
    "private video",
    "this video is private",
    "removed by the uploader",
    "members-only",
    "who has blocked it",
)


def _youtube_error_is_fatal(message):
    lower = (message or "").lower()
    return any(hint in lower for hint in FATAL_YOUTUBE_HINTS)


def _yt_version():
    try:
        import yt_dlp
        return yt_dlp.version.__version__
    except Exception:
        return "غير معروفة"


def log_ytdlp_runtime():
    """سجل بدء التشغيل: يوتيوب يحتاج Deno + حزمة EJS معًا."""
    print(
        f"[YTDLP] yt-dlp {_yt_version()}؛ JS runtime: {js_runtime_state()}؛ "
        f"EJS: {ejs_solver_state()}",
        flush=True,
    )


def js_runtime_state():
    """حالة بيئة JS على الخادم؛ دنو مفعّل افتراضيًا في yt-dlp الحديث."""
    deno = shutil.which("deno")
    if deno:
        return f"deno ({deno})"
    node = shutil.which("node")
    if node:
        return f"node ({node}؛ غير مفعّل تلقائيًا في yt-dlp)"
    return "غير متوفر — ستعطي يوتيوب صيغًا ناقصة"


def _parse_netscape_cookies(cookies_text):
    """يحوّل ملف كوكيز Netscape إلى قاموس name -> value."""
    cookies = {}
    for raw_line in (cookies_text or "").splitlines():
        line = raw_line.strip("\r\n")
        if not line:
            continue
        if line.startswith("#") and not line.startswith("#HttpOnly_"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        name, value = parts[5].strip(), parts[6].strip()
        if name:
            cookies[name] = value
    return cookies


def _cookies_authenticated(parsed):
    """هل سيعتبرها yt-dlp جلسة موثّقة؟ (LOGIN_INFO + SAPISID)."""
    has_login_info = bool((parsed or {}).get("LOGIN_INFO"))
    has_sapisid = any(
        (parsed or {}).get(name) for name in COOKIE_AUTH_SAPISID_KEYS
    )
    return bool(has_login_info and has_sapisid)


def cookies_auth_state(cookies_text):
    """ملخّص آمن: هل الكوكيز «موثّقة» لدى yt-dlp أم مجهولة؟"""
    parsed = _parse_netscape_cookies(cookies_text)
    if not parsed:
        return "لا توجد كوكيز صالحة الشكل"
    if _cookies_authenticated(parsed):
        return "كوكيز موثّقة لدى yt-dlp ✅"
    missing = []
    if not parsed.get("LOGIN_INFO"):
        missing.append("LOGIN_INFO")
    if not any(parsed.get(name) for name in COOKIE_AUTH_SAPISID_KEYS):
        missing.append("SAPISID/__Secure-3PAPISID")
    return (
        "كوكيز غير موثّقة لدى yt-dlp (ستُعامل كجلسة مجهولة) — ناقص: "
        + ", ".join(missing)
    )


def _cookies_file_authenticated(path):
    """نفس الفحص لكن من ملف كوكيز على القرص."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return False
    return _cookies_authenticated(_parse_netscape_cookies(text))


def _format_has_audio(fmt):
    return fmt.get("acodec") not in (None, "", "none")


def _format_is_usable_audio(fmt):
    """صيغة صوت قابلة للاستخدام فعلًا: ليست قصة/DRM/ناقصة التوكن."""
    if not _format_has_audio(fmt):
        return False
    if fmt.get("has_drm") in (True, "maybe"):
        return False
    note = str(fmt.get("format_note") or "").upper()
    if "MISSING POT" in note:
        return False
    return True


def usable_audio_formats(info):
    return [
        fmt for fmt in ((info or {}).get("formats") or [])
        if isinstance(fmt, dict) and _format_is_usable_audio(fmt)
    ]


def youtube_formats_summary(info):
    """ملخّص للفيديو: كم صيغة، كم صوت صالح، وكم صيغة محجوبة."""
    formats = [
        fmt for fmt in ((info or {}).get("formats") or [])
        if isinstance(fmt, dict)
    ]
    audio = usable_audio_formats(info)
    missing_pot = sum(
        1 for fmt in formats
        if "MISSING POT" in str(fmt.get("format_note") or "").upper()
    )
    drm = sum(1 for fmt in formats if fmt.get("has_drm") in (True, "maybe"))
    return {
        "total": len(formats),
        "audio": len(audio),
        "missing_pot": missing_pot,
        "drm": drm,
        "formats": audio,
    }


def _cookie_login_state_from_html(html_text):
    """True مسجّل، False غير مسجّل، None غير واضح."""
    if re.search(r'"LOGGED_IN"\s*:\s*true', html_text or ""):
        return True
    if re.search(r'"LOGGED_IN"\s*:\s*false', html_text or ""):
        return False
    return None


def youtube_cookie_status(cookies_text, timeout=20):
    """يفحص هل كوكيز الحساب ما زالت فعّالة من عنوان الخادم.

    يوتيوب قد يقبل ملف كوكيز شكله صحيح لكن جلسته منتهية، فتأتي النتيجة
    «قصص فقط». هذا الفحص يفصل بين «كوكيز ميتة» و«حجب صيغ» فورًا.
    """
    cookies = _parse_netscape_cookies(cookies_text)
    if not cookies:
        return "لا توجد كوكيز صالحة الشكل"

    try:
        response = requests.get(
            "https://www.youtube.com/",
            cookies=cookies,
            timeout=timeout,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "ar,en;q=0.8",
            },
        )
    except requests.RequestException as e:
        return f"تعذّر فحص الحساب ({type(e).__name__})"

    state = _cookie_login_state_from_html(response.text)
    if state is True:
        return "الحساب مسجّل الدخول ✅"
    if state is False:
        return "الكوكيز منتهية أو ناقصة ❌ — أعد تصديرها من المتصفح"
    return "تعذّر تحديد حالة الحساب (يوتيوب لم يفصح عنها)"


def cookie_names_state(cookies_text):
    """ملخّص عربي لمفاتيح الكوكيز الموجودة، دون كشف أي قيمة."""
    cookies = _parse_netscape_cookies(cookies_text)
    if not cookies:
        return "لا توجد كوكيز صالحة الشكل"
    present = [name for name in COOKIE_KEY_NAMES if cookies.get(name)]
    missing = [name for name in COOKIE_KEY_NAMES if not cookies.get(name)]
    if not missing:
        return f"مفاتيح الكوكيز: {len(present)}/{len(COOKIE_KEY_NAMES)} كاملة ✅"
    return (
        f"مفاتيح الكوكيز: {len(present)}/{len(COOKIE_KEY_NAMES)}"
        f" — ناقص: {', '.join(missing)}"
    )


def _log_formats(label, info, details=True):
    """يسجّل الصيغ الفعلية في سجل الخادم لتشخيص حجب يوتيوب."""
    summary = youtube_formats_summary(info)
    print(
        f"[YTDLP] {label}: {summary['total']} formats "
        f"(audio={summary['audio']} missing_pot={summary['missing_pot']} "
        f"drm={summary['drm']})",
        flush=True,
    )
    if not details:
        return
    for fmt in (info or {}).get("formats") or []:
        if not isinstance(fmt, dict):
            continue
        print(
            "[YTDLP]   "
            f"id={fmt.get('format_id')} ext={fmt.get('ext')} "
            f"acodec={fmt.get('acodec')} vcodec={fmt.get('vcodec')} "
            f"proto={fmt.get('protocol')} "
            f"drm={fmt.get('has_drm')} "
            f"note={fmt.get('format_note') or ''} "
            f"tbr={fmt.get('tbr')}",
            flush=True,
        )


def inspect_youtube(url, destination_dir, cookies_text=None, clients=None,
                    pot=False, force_pot=False, include_missing_pot=False):
    """فحص للمطوّر: ما الصيغ التي يقدمها يوتيوب من هذا الخادم فعلًا؟

    ``clients=None`` يستخدم العملاء الافتراضيين، وإلا تُفرض القائمة.
    ``include_missing_pot`` يدرج صيغًا تحتاج توكن غير متوفر للتشخيص فقط.
    يعيد (المعلومات أو None، ورسالة الخطأ أو "").
    """
    import yt_dlp

    cookies_file, temporary_cookies = _youtube_cookies_file(
        Path(destination_dir),
        cookies_text
    )
    options = _youtube_options(
        Path(destination_dir),
        10_000_000_000,
        cookies_file,
        clients,
        missing_pot=include_missing_pot,
        pot=pot,
        force_pot=force_pot,
    )
    label = "+".join(clients) if clients else "default"
    if pot:
        label += " +PO"
    if force_pot:
        label += " (forced)"
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            # process=False: قائمة الصيغ قبل تطبيق المحدد bestaudio/best.
            info = ydl.extract_info(url, download=False, process=False)
        _log_formats(label, info, details=True)
        return info, ""
    except Exception as e:
        print(
            f"[YTDLP] {label} failed: {_short_reason(e)}",
            flush=True
        )
        return None, _short_reason(e)
    finally:
        if temporary_cookies and cookies_file:
            cookies_file.unlink(missing_ok=True)


def _info_has_audio(info):
    """هل الصيغة التي اختارها yt-dlp تحتوي مسارًا صوتيًا؟"""
    if not info:
        return False
    selected = info.get("requested_formats") or []
    if selected:
        return any(
            fmt.get("acodec") not in (None, "", "none")
            for fmt in selected
            if isinstance(fmt, dict)
        )
    return info.get("acodec") not in (None, "", "none")


def _file_has_audio(path):
    """تحقق نهائي بـ ffprobe؛ لا يفشل إن لم يكن ffprobe متاحًا."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return True
    try:
        completed = subprocess.run(
            [
                ffprobe, "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_type", "-of", "csv=p=0",
                str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=20,
            text=True,
            check=False,
        )
    except Exception:
        return True
    return "audio" in (completed.stdout or "").lower()


def _attempt_files(destination_dir):
    return {
        path for path in Path(destination_dir).iterdir()
        if path.is_file()
    }


def _cleanup_attempt_files(destination_dir, before):
    """يحذف ملفات المحاولة الفاشلة كي لا تُختار بدل الناتج الصحيح."""
    for path in _attempt_files(destination_dir) - before:
        try:
            path.unlink()
        except OSError:
            pass


def _youtube_attempt_plan(cookies_file, cookies_auth, pot_ready):
    """يبني خطة محاولات مرتّبة بلا تكرار وبلا عملاء غير مدعومين."""
    attempts = []
    seen = set()

    def add(clients, use_cookies, use_pot=False, force_pot=False):
        if clients:
            clients = _youtube_clients(clients)
            if not clients:
                return
        key = (
            tuple(clients) if clients else None,
            bool(use_cookies),
            bool(use_pot),
            bool(force_pot),
        )
        if key in seen:
            return
        seen.add(key)
        attempts.append((clients, use_cookies, use_pot, force_pot))

    # 1) عملاء لا تحتاج حسابًا ولا JavaScript: الأثبت من عنوان الخادم.
    add(YOUTUBE_JS_FREE_CLIENTS, None)

    # 2) كوكيز موثّقة: اترك yt-dlp يختار عملاءه الموثّقين ثم صرّح بها.
    if cookies_auth:
        add(None, cookies_file)
        add(
            tuple(
                name for name in _youtube_clients(YOUTUBE_COOKIE_CLIENTS)
                if _youtube_client_supports_cookies(name)
            ),
            cookies_file,
        )

    # 3) عملاء الويب مع PO Token، فقط بعد تحقق حقيقي من المزوّد.
    if pot_ready:
        for group in YOUTUBE_POT_CLIENTS:
            add(
                group,
                cookies_file if cookies_auth else None,
                use_pot=True,
                force_pot=True,
            )

    # 4) محاولة بالكوكيز حتى لو كانت غير موثّقة؛ قد تفيد جلسة ويب سليمة.
    if cookies_file:
        add(None, cookies_file)

    # 5) شبكة الأمان: بلا كوكيز دائمًا، حتى لو جاءت من متغيّر بيئة.
    add(None, None)
    add(YOUTUBE_BARE_CLIENTS, None)
    return attempts


def _download_youtube(url, destination_dir, max_bytes, cookies_text=None):
    try:
        import yt_dlp
    except ImportError as e:
        raise UrlDownloadError(
            "دعم يوتيوب غير مثبّت على الخادم."
        ) from e

    destination_dir = Path(destination_dir)
    version = _yt_version()
    cookies_file, temporary_cookies = _youtube_cookies_file(
        destination_dir,
        cookies_text
    )
    cookies_auth = bool(cookies_file) and _cookies_file_authenticated(
        cookies_file
    )
    if cookies_file and not cookies_auth:
        print(
            "[YTDLP] الكوكيز لا تحقق شرط yt-dlp "
            "(LOGIN_INFO + SAPISID)؛ ستُعامل كجلسة مجهولة.",
            flush=True,
        )

    pot_state = pot_provider_state()
    pot_ready = pot_state.startswith("جاهز")
    print(
        f"[YTDLP] إعداد يوتيوب: yt-dlp {version}؛ "
        f"EJS: {ejs_solver_state()}؛ PO: {pot_state}",
        flush=True,
    )

    attempts = _youtube_attempt_plan(cookies_file, cookies_auth, pot_ready)
    info = None
    last_message = ""
    last_category = "other"

    def attempt(clients, use_cookies, use_pot, force_pot, extract_audio=True):
        nonlocal last_message, last_category
        label = "+".join(clients) if clients else "default"
        if use_pot:
            label += " +PO"
        if force_pot:
            label += " (forced)"
        if use_cookies is None:
            label += " (بدون كوكيز)"
        if not extract_audio:
            label += " (صوت أصلي)"

        before = _attempt_files(destination_dir)
        options = _youtube_options(
            destination_dir,
            max_bytes,
            use_cookies,
            clients,
            # لا ندرج صيغًا بلا PO Token في التنزيل: قد تُنتج 403.
            missing_pot=False,
            pot=use_pot,
            force_pot=force_pot,
            extract_audio=extract_audio,
        )
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                result = ydl.extract_info(url, download=True)
        except Exception as e:
            last_message = str(e)
            last_category = classify_youtube_error(last_message)
            print(
                f"[YTDLP] {label} failed ({last_category}): "
                f"{_short_reason(e)}",
                flush=True,
            )
            _cleanup_attempt_files(destination_dir, before)
            return None, e

        if not _info_has_audio(result):
            last_message = (
                "استخرج yt-dlp بيانات الفيديو لكن الصيغة المختارة "
                "لا تحتوي صوتًا."
            )
            last_category = "format"
            print(f"[YTDLP] {label} failed (format): {last_message}",
                  flush=True)
            _cleanup_attempt_files(destination_dir, before)
            return None, None
        return result, None

    ffmpeg_attempt = None
    try:
        for plan_item in attempts:
            info, error = attempt(*plan_item)
            if info is not None:
                break
            # أخطاء الفيديو أو الحجم لا تُحلّ بتجربة عميل آخر.
            if last_category in ("fatal", "size"):
                raise _youtube_failure(last_message) from error
            # الصوت نُزّل لكن التحويل فشل: أعد المحاولة دون تحويل بدل
            # إهدار الملف، ثم أبلغ عن ffmpeg فقط إن فشل ذلك أيضًا.
            if last_category == "ffmpeg":
                ffmpeg_attempt = plan_item
                break

        if info is None and ffmpeg_attempt is not None:
            print(
                "[YTDLP] ffmpeg غير متاح للتحويل؛ أحتفظ بالصوت الأصلي.",
                flush=True,
            )
            info, _ = attempt(*ffmpeg_attempt, extract_audio=False)

        if info is None:
            raise _youtube_failure(
                last_message or "unknown youtube error",
                category=last_category,
            )
    finally:
        if temporary_cookies and cookies_file:
            cookies_file.unlink(missing_ok=True)

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
    if not _file_has_audio(target):
        target.unlink(missing_ok=True)
        raise UrlDownloadError(
            "الملف الناتج من يوتيوب لا يحتوي مسارًا صوتيًا. "
            "المشكلة في بيئة الخادم (JavaScript/PO Token)."
        )
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


def download_url(url, destination_dir, max_bytes, cookies_text=None):
    """ينزّل الرابط ويعيد (المسار، الحجم، الاسم الظاهر)."""
    if not is_url(url):
        raise UnsupportedUrl("الرابط غير صالح.")
    _validate_url(url)

    destination = Path(destination_dir)
    destination.mkdir(parents=True, exist_ok=True)
    max_bytes = max(1, int(max_bytes))

    if is_youtube(url):
        return _download_youtube(
            url,
            destination,
            max_bytes,
            cookies_text=cookies_text
        )
    return _download_direct(url, destination, max_bytes)
