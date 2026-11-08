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

# عملاء بديلة يُجرَّبون معًا إن حجب يوتيوب العميل الافتراضي.
# مع الكوكيز يستخدم yt-dlp غالبًا tv_downgraded + web_safari؛
# المحاولة الثالثة جسر إضافي عند غياب بيئة JS.
YOUTUBE_ALT_CLIENTS = ("tv", "web_safari", "android_vr")
YOUTUBE_LAST_CLIENTS = ("tv_downgraded", "android_vr", "ios")

# آخر محاولة من غير الكوكيز إن رفضت يوتيوب كل المحاولات بالكوكيز.
# من دون كوكيز يستخدم yt-dlp تلقائيًا عملاء لا يحتاجون حسابًا، وقد
# تمنح صيغًا من عنوان الخادم بعد حل التحدي الجديد (بيئة JS).
YOUTUBE_BARE_CLIENTS = ("visionos", "android_vr", "web_safari", "ios")

# مزوّد توكن إثبات الأصل (PO Token): يوتيوب يرفض منح صيغ الصوت لعملاء
# الويب القادمين من عنوان مركز بيانات إلا بتوكن يُولَّد بتشغيل متصفح
# وهمي على الخادم. السكربت ووحداته تأتيان من صورة bgutil في Dockerfile.
POT_SERVER_HOME = "/opt/bgutil/server"
POT_CLIENT_SETS = (
    ("mweb",),
    ("web_safari", "web"),
    ("tv",),
)

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


def pot_provider_state():
    """هل مزوّد PO Token مثبّت وجاهز على هذا الخادم؟"""
    server = Path(POT_SERVER_HOME)
    if not (server / "src" / "generate_once.ts").is_file():
        return "غير مثبّت"
    if not (server / "node_modules").is_dir():
        return "ناقص (node_modules)"
    if not shutil.which("deno"):
        return "غير مثبّت (لا Deno)"
    return "جاهز ✅"


def _short_reason(error):
    """سطر قصير من خطأ yt-dlp يصلح للسجلّات."""
    message = " ".join(str(error or "").split())
    if message.startswith("ERROR: "):
        message = message[7:]
    return message[:280]


def _looks_like_bot_check(message):
    lower = (message or "").lower()
    return any(hint in lower for hint in BOT_CHECK_HINTS)


def _youtube_failure(message):
    """رسالة عربية للمستخدم، مع التفاصيل الحقيقية في ``detail`` للسجل."""
    if "larger than max-filesize" in (message or "").lower():
        return UrlTooLarge("حجم الفيديو أكبر من الحد المسموح.")

    lower = (message or "").lower()
    if _looks_like_bot_check(message):
        error = UrlDownloadError(
            "يوتيوب رفض الطلب من عنوان الخادم (فحص تحقق آلي). "
            "أضف كوكيز يوتيوب في إعدادات الخدمة ثم أعد المحاولة."
        )
    elif "larger than max-filesize" in lower:
        error = UrlDownloadError("حجم الفيديو أكبر من الحد المسموح.")
    elif "requested format is not available" in lower:
        error = UrlDownloadError(
            "يوتيوب لم يعطِ أي صيغة صوتية متاحة لهذا الخادم "
            "(المشكلة في بيئة الخادم، وليست في الفيديو). "
            "المطوّر سيصحح الإعداد قريبًا."
        )
    elif any(
        word in lower
        for word in ("private", "unavailable", "removed", "blocked")
    ):
        error = UrlDownloadError(
            "الفيديو غير متاح: قد يكون خاصًا أو محذوفًا "
            "أو محجوبًا في منطقة الخادم."
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
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "64",
        }],
        "postprocessor_args": ["-threads", "1"],
    }

    if cookies_file:
        options["cookiefile"] = str(cookies_file)

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
    """سجل بدء التشغيل: نسخة yt-dlp وتوفر بيئة JS (دنو)."""
    print(f"[YTDLP] yt-dlp {_yt_version()}؛ JS runtime: {js_runtime_state()}",
          flush=True)


def js_runtime_state():
    """حالة بيئة JS على الخادم؛ دنو مفعّل افتراضيًا في yt-dlp الحديث."""
    import shutil

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


def _log_formats(label, info):
    """يسجّل الصيغ الفعلية في سجل الخادم لتشخيص حجب يوتيوب."""
    formats = (info or {}).get("formats") or []
    print(f"[YTDLP] {label}: {len(formats)} formats", flush=True)
    for fmt in formats:
        if not isinstance(fmt, dict):
            continue
        print(
            "[YTDLP]   "
            f"id={fmt.get('format_id')} ext={fmt.get('ext')} "
            f"acodec={fmt.get('acodec')} vcodec={fmt.get('vcodec')} "
            f"proto={fmt.get('protocol')} "
            f"tbr={fmt.get('tbr')}",
            flush=True
        )


def inspect_youtube(url, destination_dir, cookies_text=None, clients=None,
                    pot=False, force_pot=False):
    """فحص للمطوّر: ما الصيغ التي يقدمها يوتيوب من هذا الخادم فعلًا؟

    ``clients=None`` يستخدم العملاء الافتراضيين، وإلا تُفرض القائمة.
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
        missing_pot=True,
        pot=pot,
        force_pot=force_pot,
    )
    label = "+".join(clients) if clients else "default"
    if force_pot:
        label += " +PO forced"
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            # process=False: قائمة الصيغ قبل تطبيق المحدد bestaudio/best.
            info = ydl.extract_info(url, download=False, process=False)
        if pot:
            _log_formats(label, info)
        return info, ""
    except Exception as e:
        if pot:
            print(
                f"[YTDLP] {label} failed: {_short_reason(e)}",
                flush=True
            )
        return None, _short_reason(e)
    finally:
        if temporary_cookies and cookies_file:
            cookies_file.unlink(missing_ok=True)


def _download_youtube(url, destination_dir, max_bytes, cookies_text=None):
    try:
        import yt_dlp
    except ImportError as e:
        raise UrlDownloadError(
            "دعم يوتيوب غير مثبّت على الخادم."
        ) from e

    version = _yt_version()
    cookies_file, temporary_cookies = _youtube_cookies_file(
        destination_dir,
        cookies_text
    )
    info = None
    last_message = ""

    # أولًا: عملاء الويب مع توكن إثبات الأصل (إن كان المزوّد جاهزًا)،
    # لأنهم وحدهم يمنحون صيغ الصوت من عنوان الخادم. ثم السلسلة السابقة
    # كاملة كاحتياط حتى لا نكسر أي حالة كانت تعمل.
    attempts = []
    if pot_provider_state() == "جاهز ✅":
        for client_set in POT_CLIENT_SETS:
            # force_pot: سياسة auto لا تطلب توكن المشغّل مع web/mweb،
            # فنفرض التوليد في محاولات المزوّد تحديدًا.
            attempts.append((client_set, True, cookies_file, True, True))
    attempts.extend([
        (None, False, cookies_file, False, False),
        (None, True, cookies_file, False, False),
        (YOUTUBE_ALT_CLIENTS, True, cookies_file, False, False),
        (YOUTUBE_LAST_CLIENTS, True, cookies_file, False, False),
    ])
    if cookies_text:
        attempts.append((YOUTUBE_BARE_CLIENTS, True, None, False, False))

    try:
        for (
            clients,
            allow_missing_pot,
            use_cookies,
            use_pot,
            force_pot,
        ) in attempts:
            label = "+".join(clients) if clients else "default"
            if allow_missing_pot:
                label += " +missing_pot"
            if use_pot:
                label += " +PO"
            if force_pot:
                label += " (forced)"
            if use_cookies is None:
                label += " (بدون كوكيز)"
            options = _youtube_options(
                destination_dir,
                max_bytes,
                use_cookies,
                clients,
                missing_pot=allow_missing_pot,
                pot=use_pot,
                force_pot=force_pot,
            )
            try:
                with yt_dlp.YoutubeDL(options) as ydl:
                    info = ydl.extract_info(url, download=True)
                break
            except Exception as e:
                last_message = str(e)
                print(
                    f"[YTDLP] yt-dlp {version} "
                    f"{label} failed: "
                    f"{_short_reason(e)}",
                    flush=True
                )
                # خطأ يعني أن الفيديو نفسه غير متاح: لا نكمل المحاولات.
                if _youtube_error_is_fatal(last_message):
                    raise _youtube_failure(last_message) from e

        if info is None:
            raise _youtube_failure(last_message or "unknown youtube error")
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
