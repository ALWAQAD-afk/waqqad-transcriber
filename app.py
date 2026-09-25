import os
import uuid
import json
import threading
import mimetypes
import subprocess
import shutil
import time
from pathlib import Path
from warraq import handle_warraq_update
from warraq_miniapp import register_warraq_miniapp
import requests
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS


app = Flask(__name__)
register_warraq_miniapp(app)

CORS(
    app,
    resources={
        r"/*": {
            "origins": "https://alwaqad-afk.github.io"
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

JOBS_FILE = Path("/tmp/waqqad_jobs.json")
jobs_lock = threading.Lock()

SUBMIT_TIMEOUT = 1800


# ============================================================
# تخزين المهام
# ============================================================

def _load_jobs():
    if JOBS_FILE.exists():
        try:
            return json.loads(
                JOBS_FILE.read_text(encoding="utf-8")
            )
        except Exception:
            return {}

    return {}


def _save_jobs(jobs):
    JOBS_FILE.write_text(
        json.dumps(jobs),
        encoding="utf-8"
    )


def create_job(job_id, data):
    with jobs_lock:
        jobs = _load_jobs()
        jobs[job_id] = data
        _save_jobs(jobs)


def update_job(job_id, **updates):
    with jobs_lock:
        jobs = _load_jobs()

        if job_id in jobs:
            jobs[job_id].update(updates)
            _save_jobs(jobs)


def get_job(job_id):
    with jobs_lock:
        jobs = _load_jobs()
        return jobs.get(job_id)


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
    """حفظ النص وإرساله إلى Telegram وتحديث حالة المهمة."""
    work_dir = Path("/tmp") / f"waqqad_{job_id}"
    work_dir.mkdir(parents=True, exist_ok=True)

    output_file = work_dir / "transcription.txt"
    output_file.write_text(
        transcript,
        encoding="utf-8"
    )

    job = get_job(job_id) or {}
    telegram_user_id = job.get("telegram_user_id")

    update_job(
        job_id,
        status="processing",
        stage="sending_to_telegram",
        engine=engine,
        output_file=str(output_file)
    )

    try:
        if telegram_user_id:
            send_transcript_to_telegram(
                telegram_user_id,
                output_file
            )
            telegram_sent = True
            telegram_error = None
        else:
            telegram_sent = False
            telegram_error = "No Telegram user ID stored for this job"
    except Exception as e:
        telegram_sent = False
        telegram_error = str(e)
        print(
            f"[TELEGRAM ERROR] Job {job_id}: {e}",
            flush=True
        )

    update_job(
        job_id,
        status="completed",
        stage="done",
        engine=engine,
        output_file=str(output_file),
        telegram_sent=telegram_sent,
        telegram_error=telegram_error
    )


def submit_to_deepgram(job_id, input_file, fallback_reason=None):
    """Deepgram Nova-3: المحرك الاحتياطي، مع callback كما في V3."""
    if not DEEPGRAM_API_KEY:
        update_job(
            job_id,
            status="failed",
            stage="error",
            error="DEEPGRAM_API_KEY is not configured"
        )
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

        callback_url = f"{BASE_URL}/deepgram-callback/{job_id}"
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

    finally:
        shutil.rmtree(
            parts_dir,
            ignore_errors=True
        )


# ============================================================
# إرسال النتيجة عبر Telegram
# ============================================================

def send_transcript_to_telegram(
    telegram_user_id,
    output_file
):

    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not configured"
        )

    url = (
        "https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    )

    with open(output_file, "rb") as document:

        response = requests.post(
            url,
            data={
                "chat_id": telegram_user_id,
                "caption":
                    "✅ اكتمل تفريغ الملف بنجاح."
            },
            files={
                "document": (
                    "transcription.txt",
                    document,
                    "text/plain"
                )
            },
            timeout=60
        )

    print(
        f"[TELEGRAM] HTTP "
        f"{response.status_code}: "
        f"{response.text[:500]}",
        flush=True
    )

    if response.status_code != 200:
        raise RuntimeError(
            "Telegram sendDocument HTTP "
            f"{response.status_code}"
        )


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

    telegram_user_id = request.form.get(
        "telegram_user_id"
    )

    if not telegram_user_id:
        return jsonify({
            "error": "Missing Telegram user ID"
        }), 400

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

    input_file = (
        original_dir
        / uploaded_file.filename
    )

    print(
        f"[UPLOAD] Saving: "
        f"{uploaded_file.filename}",
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

    job = get_job(job_id)

    if job is None:
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

    job = get_job(job_id)

    if job is None:
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
            "transcription.txt",
        mimetype="text/plain"
    )

@app.route(
    "/telegram/warraq",
    methods=["POST"]
)
def warraq_webhook():

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
if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=10000
    )
