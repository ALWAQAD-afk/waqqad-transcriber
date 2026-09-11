import os
import uuid
import json
import threading
import mimetypes
from pathlib import Path
from warraq import handle_warraq_update
import requests
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS


app = Flask(__name__)

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
# إرسال الملف إلى Deepgram
# ============================================================

def submit_to_deepgram(job_id, input_file):

    try:
        update_job(
            job_id,
            status="uploading",
            stage="uploading_to_deepgram"
        )

        content_type = (
            mimetypes.guess_type(str(input_file))[0]
            or "audio/mpeg"
        )

        callback_url = (
            f"{BASE_URL}/deepgram-callback/{job_id}"
        )

        url = "https://api.deepgram.com/v1/listen"

        params = {
            "model": "nova-3",
            "language": "ar",
            "smart_format": "true",
            "callback": callback_url
        }

        headers = {
            "Authorization":
                f"Token {DEEPGRAM_API_KEY}",
            "Content-Type": content_type
        }

        print(
            f"[DEEPGRAM] Uploading job {job_id}...",
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
            f"[DEEPGRAM] Submit HTTP "
            f"{response.status_code}",
            flush=True
        )

        if response.status_code not in (200, 202):

            print(
                response.text[:1000],
                flush=True
            )

            update_job(
                job_id,
                status="failed",
                stage="error",
                error=(
                    "Deepgram submit HTTP "
                    f"{response.status_code}"
                )
            )

            return

        data = response.json()

        request_id = data.get("request_id")

        update_job(
            job_id,
            status="processing",
            stage="waiting_deepgram",
            deepgram_request_id=request_id
        )

        print(
            f"[DEEPGRAM] Job {job_id} accepted. "
            f"request_id={request_id}",
            flush=True
        )

    except Exception as e:

        print(
            f"[ERROR] Submit failed for job "
            f"{job_id}: {e}",
            flush=True
        )

        update_job(
            job_id,
            status="failed",
            stage="error",
            error=str(e)
        )

    finally:

        try:
            input_file.unlink(missing_ok=True)
        except Exception:
            pass


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
            "telegram_user_id":
                telegram_user_id,
            "telegram_sent": False,
            "telegram_error": None
        }
    )

    thread = threading.Thread(
        target=submit_to_deepgram,
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

    work_dir = (
        Path("/tmp")
        / f"waqqad_{job_id}"
    )

    work_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    output_file = (
        work_dir
        / "transcription.txt"
    )

    output_file.write_text(
        transcript,
        encoding="utf-8"
    )

    telegram_user_id = job.get(
        "telegram_user_id"
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
            telegram_error = (
                "No Telegram user ID "
                "stored for this job"
            )

    except Exception as e:

        telegram_sent = False
        telegram_error = str(e)

        print(
            f"[TELEGRAM ERROR] "
            f"Job {job_id}: {e}",
            flush=True
        )

    update_job(
        job_id,
        status="completed",
        stage="done",
        output_file=str(output_file),
        telegram_sent=telegram_sent,
        telegram_error=telegram_error
    )

    print(
        f"[CALLBACK] Job {job_id} "
        f"completed successfully. "
        f"telegram_sent={telegram_sent}",
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
