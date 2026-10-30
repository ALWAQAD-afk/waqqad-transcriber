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
    """إنشاء جدول المهام إن لم يكن موجودًا."""
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
            conn.commit()
    print("[DB] Jobs table ready", flush=True)

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

if not TELEGRAM_WEBHOOK_SECRET or not WARRAQ_WEBHOOK_SECRET:
    print(
        "[SECURITY] Webhook secrets are not fully configured. "
        "Set TELEGRAM_WEBHOOK_SECRET and WARRAQ_WEBHOOK_SECRET.",
        flush=True
    )

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
# ============================================================
# محرك Cohere الأساسي + Deepgram كخط احتياط
# ============================================================

def prepare_cohere_parts(job_id, input_file):
    """تحويل أي مدخل إلى MP3 mono 16kHz/24kbps وتقسيمه إلى قطع ساعتين."""
    work_dir = Path("/tmp") / f"waqqad_{job_id}"
    parts_dir = work_dir / "cohere_parts"
    parts_dir.mkdir(parents=True, exist_ok=True)

    output_pattern = parts_dir / "part_%03d.mp3"

    update_job(
        job_id,
        status="processing",
        stage="preparing_audio",
        engine="cohere"
    )

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

    print(
        f"[COHERE] Preparing audio for job {job_id}...",
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
                            "audio/mpeg"
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
        "✅ اكتمل تفريغ الملف بنجاح.\n"
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

    try:
        if telegram_user_id:
            send_transcript_to_telegram(
                telegram_user_id,
                output_file,
                download_name=download_name,
                caption=caption
            )
            telegram_sent = True
            telegram_error = None
        else:
            telegram_sent = False
            telegram_error = "No Telegram user ID stored for this job"
    except Exception as e:
        telegram_sent = False
        telegram_error = str(e)
        print(f"[TELEGRAM ERROR] Job {job_id}: {e}", flush=True)

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


def telegram_send_message(chat_id, text, reply_markup=None):
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

    if response.status_code != 200:
        raise RuntimeError(
            f"Telegram sendMessage HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )


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
        f"👤 المستخدم: {job.get('telegram_user_id') or 'غير معروف'}\n"
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

    send_transcriber_log(
        "❌ فشل تفريغ\n"
        f"👤 المستخدم: {job.get('telegram_user_id') or 'غير معروف'}\n"
        f"📄 الملف: {filename}\n"
        f"📦 الحجم: {size_text}\n"
        f"📍 المرحلة: {job.get('stage') or 'غير معروفة'}\n"
        f"⚠️ السبب: {error}\n"
        f"🆔 المهمة: {job_id}"
    )


def handle_transcriber_chat_update(update):
    """Reply only to /start and /help; ordinary messages are ignored."""
    message = update.get("message")
    if not message:
        return

    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    sender = message.get("from") or {}
    if not chat_id or sender.get("is_bot"):
        return

    raw_text = (message.get("text") or "").strip()
    if not raw_text.startswith("/"):
        print(
            f"[TRANSCRIBER CHAT] Ignored ordinary message from chat_id={chat_id}",
            flush=True
        )
        return

    command = raw_text.split(None, 1)[0].split("@", 1)[0].lower()
    if command not in {"/start", "/help"}:
        return

    keyboard = {
        "inline_keyboard": [[{
            "text": "🎙️ فتح مفرّغ النصوص",
            "web_app": {"url": TRANSCRIBER_WEBAPP_URL}
        }]]
    }

    if command == "/start":
        reply = (
            "أهلًا بك في مفرّغ الوقّاد 🎙️\n\n"
            "افتح مفرّغ النصوص من الزر أدناه، ثم اختر ملفك الصوتي. "
            "ستصل النتيجة إلى هذه المحادثة فور اكتمالها."
        )
    else:
        reply = (
            "🎙️ لاستخدام مفرّغ الوقّاد:\n"
            "1) افتح مفرّغ النصوص من الزر أدناه.\n"
            "2) اختر الملف الصوتي وابدأ التفريغ.\n"
            "3) بعد تأكيد استلام الملف يمكنك إغلاق الصفحة؛ "
            "وستصل النتيجة هنا عند اكتمالها."
        )

    telegram_send_message(chat_id, reply, reply_markup=keyboard)


# ============================================================
# إرسال النتيجة عبر Telegram
# ============================================================

def send_transcript_to_telegram(
    telegram_user_id,
    output_file,
    download_name="transcription.txt",
    caption="✅ اكتمل تفريغ الملف بنجاح."
):
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")

    url = (
        "https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    )

    with open(output_file, "rb") as document:
        response = requests.post(
            url,
            data={
                "chat_id": telegram_user_id,
                "caption": caption
            },
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

    try:
        telegram_send_message(
            telegram_user_id,
            "✅ تم استخراج النص الخام.\n"
            "⏳ جارٍ الآن تجهيز نسختين إضافيتين: "
            "مصححة إملائياً، ومحسّنة للقراءة.\n"
            "قد يستغرق الأمر بضع دقائق حسب طول الملف."
        )
    except Exception as e:
        print(f"[AUTO-REFINE] Status message failed: {e}", flush=True)

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

            send_transcript_to_telegram(
                telegram_user_id,
                refined_path,
                download_name=refined_path.name,
                caption=caption
            )
            print(
                f"[AUTO-REFINE] {mode} sent for job {job_id}",
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
        {
            "job_id": job_id,
            "status": "queued",
            "stage": "queued",
            "error": None,
            "output_file": None,
            "original_filename": uploaded_file.filename,
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
            "telegram_user_id":
                telegram_user_id,
            "telegram_sent": False,
            "telegram_error": None
        }
    )

    thread = threading.Thread(
        target=process_transcription_job,
        args=(
            job_id,
            input_file
        ),
        daemon=True
    )

    thread.start()

    return jsonify({
        "job_id": job_id,
        "status": "queued",
        "message":
            "Transcription job started."
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
