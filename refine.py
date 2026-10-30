import os
import re
import time
import random
import difflib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests


# ============================================================
# إعدادات المدقق
# ============================================================

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

# حجم القطعة آمن للـ API، ويصغر تلقائياً إذا رفض الخادم النص.
REFINE_CHUNK_CHARS = int(os.environ.get("REFINE_CHUNK_CHARS", "3500"))
REFINE_MIN_CHUNK_CHARS = int(os.environ.get("REFINE_MIN_CHUNK_CHARS", "500"))
REFINE_TIMEOUT = int(os.environ.get("REFINE_TIMEOUT", "180"))
REFINE_MAX_RETRIES = int(os.environ.get("REFINE_MAX_RETRIES", "3"))
REFINE_REQUEST_GAP = float(os.environ.get("REFINE_REQUEST_GAP", "1.0"))
REFINE_THINKING_LEVEL = os.environ.get(
    "REFINE_THINKING_LEVEL", "minimal"
).strip() or "minimal"
REFINE_CONCURRENCY = max(
    1, int(os.environ.get("REFINE_CONCURRENCY", "3"))
)


class GeminiError(RuntimeError):
    """خطأ مؤقت في قطعة واحدة؛ نرجع النص الأصلي لها."""


class GeminiFatalError(RuntimeError):
    """خطأ يمنع التدقيق كله (مفتاح، موديل، حصة)."""


class GeminiInputTooLong(GeminiError):
    pass


class GeminiTruncated(GeminiError):
    pass


class GeminiBlocked(GeminiError):
    pass


class GeminiQuotaError(GeminiFatalError):
    pass


# ============================================================
# تقسيم النص
# ============================================================

def _hard_split(text, max_chars):
    """تقسيم اضطراري عند الكلمات أو في منتصف النص، دون فقدان حرف."""
    parts = []
    remaining = text
    while len(remaining) > max_chars:
        cut = remaining.rfind(" ", 0, max_chars + 1)
        if cut < max_chars // 2:
            cut = max_chars
        parts.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        parts.append(remaining)
    return [p for p in parts if p]


def _split_long_block(block, max_chars):
    """تقسيم فقرة طويلة عند نهايات الجمل ثم الكلمات."""
    sentences = [
        s.strip()
        for s in re.split(r'(?<=[.!؟…])\s+', block)
        if s.strip()
    ]

    chunks = []
    current = ""
    for sentence in sentences:
        if len(sentence) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_hard_split(sentence, max_chars))
        elif not current:
            current = sentence
        elif len(current) + 1 + len(sentence) <= max_chars:
            current = f"{current} {sentence}"
        else:
            chunks.append(current)
            current = sentence

    if current:
        chunks.append(current)
    return chunks


def split_text_smart(text, max_chars=None):
    """يقسم النص مع احترام الفقرات والجمل دون حذف أي محتوى."""
    if max_chars is None:
        max_chars = max(REFINE_CHUNK_CHARS, REFINE_MIN_CHUNK_CHARS)
    max_chars = max(1, int(max_chars))

    text = (
        (text or "")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .strip()
    )
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]

    chunks = []
    current = ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= max_chars:
            current = candidate
            continue

        if current:
            chunks.append(current)
            current = ""

        if len(block) <= max_chars:
            current = block
        else:
            parts = _split_long_block(block, max_chars)
            if parts:
                chunks.extend(parts[:-1])
                current = parts[-1]

    if current:
        chunks.append(current)
    return chunks


def _split_in_half(text):
    """تقسيم قريب من المنتصف عند جملة أو كلمة."""
    if len(text) < 2:
        return [text]

    middle = len(text) // 2
    window = max(80, len(text) // 5)

    candidates = [
        m.end()
        for m in re.finditer(r"(?<=[.!؟…])\s+", text)
    ]
    nearby = [p for p in candidates if abs(p - middle) <= window]
    if nearby:
        cut = min(nearby, key=lambda p: abs(p - middle))
    else:
        space_before = text.rfind(" ", 0, middle)
        space_after = text.find(" ", middle)
        options = [p for p in (space_before, space_after) if p > 0]
        cut = (
            min(options, key=lambda p: abs(p - middle))
            if options else middle
        )

    left, right = text[:cut].strip(), text[cut:].strip()
    if not left or not right:
        cut = middle
        left, right = text[:cut].strip(), text[cut:].strip()
    if not left or not right:
        return [text]
    return [left, right]


# ============================================================
# التعليمات
# ============================================================

CONSERVATIVE_PROMPT = """أنت مدقق إملائي عربي محافظ. مهمتك الوحيدة تصحيح الأخطاء الإملائية الواضحة، دون أي إعادة صياغة.

المسموح فقط:
- تصحيح الهمزات (أ إ آ ء)، والألف المقصورة (ى)، والتاء المربوطة (ة).
- تصحيح الأخطاء الإملائية الواضحة.
- إضافة أو تعديل علامات الترقيم عند الضرورة.

الممنوع منعاً باتاً:
- تغيير أي كلمة سليمة أو استبدالها بمرادفها.
- إعادة ترتيب الجمل أو الفقرات أو دمجها أو تفريقها.
- حذف أو إضافة أي جملة أو معلومة.
- إضافة تشكيل أو شرح أو عناوين أو ترقيم قوائم.
- تغيير الأسماء أو الأرقام أو الآيات أو الأحاديث أو الشعر أو الأمثال.
- إضافة أسماء إنجليزية أو اختصارات أو أرقام أو تواريخ غير موجودة حرفياً.
- استبدال كلمة عربية بأخرى، أو ترجمة أي اسم أو مصطلح.

إذا ورد شعر أو بيت أو نص موزون: انقله كما هو، ولا تحوله إلى نثر، ولا تعيد ترتيب الشطرين، وحافظ على فواصل الأسطر.

حافظ على فواصل الأسطر والفقرات كما هي.
أعد النص المصحح فقط، دون أي مقدمة أو خاتمة أو أسوار كود.

النص:
---
{chunk}
---

النص المصحح:"""

FULL_PROMPT = """أنت محرر عربي محترف لنصوص المحاضرات والدروس. حسّن النص الآتي ليصبح سهل القراءة دون تغيير معناه.

المسموح:
- تصحيح الإملاء والهمزات والترقيم.
- ترتيب الفقرات، وإضافة عناوين فرعية أو نقاط عند وجود تعداد واضح.
- تصحيح أخطاء التفريغ الصوتي الواضحة.

الممنوع:
- إضافة أي معلومة غير موجودة أو استنتاجات.
- حذف أي فكرة أو جملة مفيدة.
- تغيير المعنى أو الأسماء أو الأرقام أو المصطلحات.
- الترجمة أو إضافة شرح أو تعليق.
- اختلاق عناوين لا يعكسها النص.
- إضافة أسماء إنجليزية أو اختصارات أو أرقام أو تواريخ أو شرح غير موجود.
- ترجمة الأسماء أو المصطلحات أو كتابة مقابلها الأجنبي.
- إعادة صياغة الشعر أو تحويله إلى نثر أو دمج شطرين.

إذا ورد شعر أو بيت أو نص موزون: اتركه حرفياً في فقرة مستقلة، دون إعادة صياغة أو تقطيع أو إضافة عنوان أو شرح.

حافظ على كل المحتوى والأرقام والأسماء كما هي.
أعد النص النهائي فقط، دون مقدمات أو أسوار كود.

النص:
---
{chunk}
---

النص المحسن:"""

POETRY_PROMPT = """أنت ناسخ أمين لنص يحتوي شعراً أو نصاً موزوناً. مهمتك تصحيح الإملاء والهمزات والترقيم فقط دون أي تغيير في الألفاظ.

الممنوع منعاً باتاً:
- تغيير أي كلمة أو استبدالها.
- إعادة ترتيب الشطرين أو دمجهما أو تفريقهما.
- تحويل الشعر إلى نثر أو إضافة عنوان أو شرح.
- إضافة تشكيل أو أسماء أو أرقام غير موجودة.

حافظ على فواصل الأسطر كما وردت.
أعد النص فقط دون مقدمة أو خاتمة أو أسوار كود.

النص:
---
{chunk}
---

النص المصحح:"""

POETRY_CUES = re.compile(
    r"(قال\s+الشاعر|يقول\s+الشاعر|أنشد|قصيدة|أبيات|بيت\s+شعر"
    r"|شطر|ديوان|مطلع|قافية|بحر\s+الشعر|الشعر\s+العربي)"
)


def _looks_like_poetry(text):
    return bool(POETRY_CUES.search(text or ""))


STRICT_SUFFIX = """

تنبيه إضافي مهم:
- لا تضف أي كلمة من عندك.
- لا تحذف أي كلمة من النص الأصلي.
- أعد النص بنفس ترتيبه وفقراته.
- إذا لم تكن واثقاً من تصحيح كلمة فاتركها كما هي.
"""


def _build_prompt(chunk, mode, strict=False):
    if _looks_like_poetry(chunk):
        template = POETRY_PROMPT
    else:
        template = FULL_PROMPT if mode == "liberal" else CONSERVATIVE_PROMPT

    prompt = template.format(chunk=chunk)
    if strict:
        prompt += STRICT_SUFFIX
    return prompt


# ============================================================
# استدعاء Gemini
# ============================================================

def _looks_too_long(body):
    return bool(re.search(
        r"(too\s+long|token|length|size|limit|exceed|maximum|max\b|حجم|طويل)",
        body or "",
        re.IGNORECASE,
    ))


def _looks_auth_error(body):
    return bool(re.search(
        r"(api[\s_-]?key|unauthenticated|unauthorized|permission[\s_-]?denied"
        r"|credential|invalid authentication|forbidden)",
        body or "",
        re.IGNORECASE,
    ))


def _looks_model_error(body):
    return bool(re.search(
        r"(model[^\n]*(not found|does not exist|not supported|unsupported"
        r"|invalid)|unknown model|unsupported model)",
        body or "",
        re.IGNORECASE,
    ))


def _sleep_backoff(attempt, retry_after=None):
    delay = min(30.0, (2 ** attempt) + random.uniform(0.0, 0.75))
    if retry_after:
        try:
            delay = max(delay, min(60.0, float(retry_after)))
        except (TypeError, ValueError):
            pass
    time.sleep(delay)


def _clean_output(text):
    text = (text or "").strip()

    text = re.sub(r"^```[a-zA-Z0-9_-]*\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)

    text = re.sub(
        r"^(?:إليك\s+)?(?:النص\s+)?(?:المصحح|المحسن|النهائي|الناتج)\s*:\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"^(?:Output|Result|Corrected text)\s*:\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _parse_response(response):
    try:
        data = response.json()
    except Exception as exc:
        raise GeminiError("رد غير صالح من خدمة التدقيق") from exc

    feedback = data.get("promptFeedback") or {}
    if feedback.get("blockReason"):
        raise GeminiBlocked(
            f"حجب المزوّد النص: {feedback.get('blockReason')}"
        )

    candidates = data.get("candidates") or []
    if not candidates:
        raise GeminiBlocked("لم يعُد المزوّد بأي نص")

    candidate = candidates[0]
    finish_reason = candidate.get("finishReason")

    if finish_reason == "MAX_TOKENS":
        raise GeminiTruncated("اقتُطع رد المزوّد")
    if finish_reason in {
        "SAFETY",
        "RECITATION",
        "PROHIBITED_CONTENT",
        "BLOCKLIST",
        "SPII",
    }:
        raise GeminiBlocked(f"توقف المزوّد: {finish_reason}")

    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(
        part.get("text", "")
        for part in parts
        if not part.get("thought")
    )
    text = _clean_output(text)
    if not text:
        raise GeminiError("رد فارغ من خدمة التدقيق")

    return text


def _max_tokens_for(chunk):
    estimate = int(len(chunk) * 1.6) + 512
    return max(1024, min(32768, estimate))


def _post_gemini(prompt, chunk, mode):
    temperature = 0.1 if mode == "conservative" else 0.3
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": _max_tokens_for(chunk),
            "thinkingConfig": {
                "thinkingLevel": REFINE_THINKING_LEVEL,
            },
        },
    }
    headers = {
        "x-goog-api-key": GEMINI_API_KEY,
        "Content-Type": "application/json",
    }

    last_error = None
    attempt = 0
    thinking_config = True

    while attempt <= REFINE_MAX_RETRIES:
        try:
            response = requests.post(
                GEMINI_URL,
                headers=headers,
                json=payload,
                timeout=REFINE_TIMEOUT,
            )
        except requests.RequestException as exc:
            last_error = exc
            if attempt >= REFINE_MAX_RETRIES:
                raise GeminiError(
                    f"تعذر الاتصال بخدمة التدقيق: {exc}"
                ) from exc
            _sleep_backoff(attempt)
            attempt += 1
            continue

        status = response.status_code

        if status == 200:
            return _parse_response(response)

        body = response.text[:500]

        if status in (401, 403):
            raise GeminiFatalError(
                "مفتاح خدمة التدقيق غير صالح أو غير مصرح"
            )

        if status == 404:
            raise GeminiFatalError(
                f"موديل التدقيق غير متاح: {GEMINI_MODEL}"
            )

        if status in (400, 413):
            if _looks_auth_error(body):
                raise GeminiFatalError(
                    "مفتاح خدمة التدقيق غير صالح أو غير مصرح"
                )

            if status == 413 or _looks_too_long(body):
                raise GeminiInputTooLong("رفض المزوّد النص لطوله")

            if thinking_config:
                # بعض الموديلات لا تدعم thinkingConfig؛ نعيد بدونها فوراً.
                payload["generationConfig"].pop("thinkingConfig", None)
                thinking_config = False
                continue

            if _looks_model_error(body):
                raise GeminiFatalError(
                    f"موديل التدقيق غير متاح: {GEMINI_MODEL}"
                )

            raise GeminiError(
                f"خطأ من خدمة التدقيق (HTTP {status}): {body}"
            )

        if status == 429:
            if attempt >= REFINE_MAX_RETRIES:
                raise GeminiQuotaError(
                    "تم تجاوز حد استخدام خدمة التدقيق مؤقتاً"
                )
            _sleep_backoff(
                attempt,
                response.headers.get("Retry-After"),
            )
            last_error = GeminiError("HTTP 429")
            attempt += 1
            continue

        if status in (500, 502, 503, 504):
            if attempt >= REFINE_MAX_RETRIES:
                raise GeminiError(
                    f"خدمة التدقيق غير متاحة (HTTP {status})"
                )
            _sleep_backoff(attempt)
            last_error = GeminiError(f"HTTP {status}")
            attempt += 1
            continue

        raise GeminiError(
            f"خطأ من خدمة التدقيق (HTTP {status}): {body}"
        )

    raise GeminiError(
        f"فشل التدقيق بعد عدة محاولات: {last_error}"
    )


_DIGITS = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
    "01234567890123456789",
)
_PERSIAN_LETTERS = "پچژگکی"


def _content_signature(text):
    normalized = (text or "").translate(_DIGITS)
    numbers = [
        re.sub(r"[,٬.]", "", token)
        for token in re.findall(r"\d[\d,٬.]*", normalized)
    ]
    return (
        Counter(numbers),
        Counter(
            word.lower()
            for word in re.findall(
                r"[A-Za-z][A-Za-z0-9+.#_-]*", text or ""
            )
        ),
        Counter(ch for ch in (text or "") if ch in _PERSIAN_LETTERS),
    )


def _preserves_content(src, output):
    src_numbers, src_latin, src_persian = _content_signature(src)
    out_numbers, out_latin, out_persian = _content_signature(output)

    if src_numbers != out_numbers:
        return False
    if src_latin != out_latin:
        return False
    if out_persian - src_persian:
        return False
    return True


def _is_valid(src, output, mode):
    if not output or not output.strip():
        return False
    if not _preserves_content(src, output):
        return False

    ratio = len(output) / max(1, len(src))

    if mode == "conservative" or _looks_like_poetry(src):
        if not 0.6 <= ratio <= 1.5:
            return False

        def normalize(value):
            return re.sub(r"\s+", " ", value).strip()

        left, right = normalize(src), normalize(output)
        if left == right:
            return True

        similarity = difflib.SequenceMatcher(
            None, left, right, autojunk=False
        ).ratio()
        return similarity >= 0.70

    return 0.5 <= ratio <= 2.5


def _refine_block(chunk, mode, strict=False):
    """يدقق قطعة، ويقسّمها تلقائياً إذا رفضها المزوّد أو اقتطعها."""
    try:
        return _post_gemini(
            _build_prompt(chunk, mode, strict),
            chunk,
            mode,
        )
    except (GeminiInputTooLong, GeminiTruncated) as exc:
        if len(chunk) <= REFINE_MIN_CHUNK_CHARS:
            raise GeminiError(str(exc)) from exc

        pieces = _split_in_half(chunk)
        if len(pieces) < 2:
            raise GeminiError("تعذر تقسيم الجزء الطويل") from exc

        texts = [
            _refine_block(piece, mode, strict)
            for piece in pieces
        ]
        return "\n\n".join(texts)


# ============================================================
# الواجهة العامة
# ============================================================

_PERSIAN_REPAIRS = {
    "ی": "ي",
    "ک": "ك",
    "پ": "ب",
    "چ": "ج",
    "ژ": "ز",
    "گ": "ك",
}


def _repair_common_chars(src, output):
    for bad, good in _PERSIAN_REPAIRS.items():
        if bad not in src:
            output = output.replace(bad, good)
    return output


def _refine_chunk(chunk, mode):
    result = _repair_common_chars(chunk, _refine_block(chunk, mode))

    if not _is_valid(chunk, result, mode):
        result = _repair_common_chars(
            chunk, _refine_block(chunk, mode, strict=True)
        )

    if not _is_valid(chunk, result, mode):
        raise GeminiError("لم يجتز الناتج فحص الجودة")

    return result


def _refine_chunk_safe(index, chunk, mode):
    try:
        return index, _refine_chunk(chunk, mode), None
    except GeminiFatalError:
        raise
    except Exception as exc:
        return index, chunk, exc


def refine_document(raw_text, mode="conservative"):
    """
    يدقق النص كاملاً ويعيد:
    {
      "text": النص المدقق,
      "chunks": عدد القطع,
      "fallbacks": عدد القطع التي أُعيدت كما هي,
      "issues": قائمة أسباب مختصرة,
    }
    """
    if not GEMINI_API_KEY:
        raise GeminiFatalError("GEMINI_API_KEY غير موجود")

    raw_text = (raw_text or "").strip()
    if not raw_text:
        return {
            "text": "",
            "chunks": 0,
            "fallbacks": 0,
            "issues": [],
        }

    chunks = split_text_smart(raw_text)
    results = [None] * len(chunks)
    workers = min(REFINE_CONCURRENCY, len(chunks))

    if workers <= 1:
        for index, chunk in enumerate(chunks, start=1):
            results[index - 1] = _refine_chunk_safe(index, chunk, mode)

            print(
                f"[REFINE {mode}] {index}/{len(chunks)} "
                f"({len(chunk)} chars)",
                flush=True,
            )

            if index < len(chunks) and REFINE_REQUEST_GAP > 0:
                time.sleep(REFINE_REQUEST_GAP)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_refine_chunk_safe, index, chunk, mode): index
                for index, chunk in enumerate(chunks, start=1)
            }

            try:
                for future in as_completed(futures):
                    index, text, error = future.result()
                    results[index - 1] = (index, text, error)
                    print(
                        f"[REFINE {mode}] {index}/{len(chunks)} done",
                        flush=True,
                    )
            except GeminiFatalError:
                for future in futures:
                    future.cancel()
                raise

    refined_parts = []
    issues = []
    fallbacks = 0

    for index, text, error in results:
        if error is not None:
            fallbacks += 1
            issues.append(f"الجزء {index}/{len(chunks)}: {error}")
            text = chunks[index - 1]
        refined_parts.append(text)

    return {
        "text": "\n\n".join(refined_parts),
        "chunks": len(chunks),
        "fallbacks": fallbacks,
        "issues": issues,
    }


def refine_in_batches(raw_text, mode="conservative"):
    return refine_document(raw_text, mode)["text"]


def refine_conservative(text):
    return refine_document(text, mode="conservative")["text"]


def refine_liberal(text):
    return refine_document(text, mode="liberal")["text"]


def refine_arabic_text(text):
    return refine_conservative(text)
