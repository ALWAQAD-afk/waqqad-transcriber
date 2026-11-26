"""تشغيل مفرّغ الوقّاد على Modal كمنصة أساسية.

الفصل المقصود:
- ``web``: نقطة ASGI سريعة تستضيف تطبيق Flask نفسه (ويبهوك تيليجرام،
  صفحات الرفع، الحالة، التنزيل، والتصدير). لا تنفّذ معالجة صوتية ثقيلة.
- ``process_job``: دالة خلفية مستقلة (Modal Function) تنفّذ التنزيل
  (تيليجرام/روابط)، وffmpeg، والتفريغ، والتنقيح، والإرسال. لها مهلة
  طويلة وتنظيف إلزامي لمجلد العمل في ``finally``.

النشر:
    modal deploy modal_app.py

الأسرار:
    أنشئ Secret في Modal باسم ``waqqad-transcriber-env`` يحوي متغيرات
    البيئة نفسها الموجودة في ``.env.example`` (أو غيّر الاسم عبر
    ``MODAL_SECRET_NAMES``). لا تُوضع الأسرار داخل الصورة أو المستودع.

التراجع:
    هذا الملف إضافة مستقلة. يمكن حذفه أو ``git revert`` للالتزام الذي
    أضافه، ويبقى ``app.py`` قادرًا على العمل محليًا أو على Render.
"""

from __future__ import annotations

import os
import traceback
from typing import Any

import modal

APP_NAME = os.environ.get("MODAL_APP_NAME", "waqqad-transcriber").strip()
SECRET_NAMES = [
    name.strip()
    for name in os.environ.get(
        "MODAL_SECRET_NAMES",
        os.environ.get("MODAL_SECRET_NAME", "waqqad-transcriber-env"),
    ).split(",")
    if name.strip()
]

PROJECT_ROOT = "/root/project"


def _secrets() -> list[modal.Secret]:
    """أسرار Modal المطلوبة. الاسم قابل للتغيير دون تعديل الكود."""
    return [modal.Secret.from_name(name) for name in SECRET_NAMES]


# الصورة تحتوي Python + ffmpeg/ffprobe + مكتبات requirements.txt، ثم
# modal وFastAPI المطلوبين لتشغيل نقطة ASGI. نسخ المشروع المحلي متعمد
# ليشمل app.py ووحداته وwarraq.html وكل ما يقرأه التطبيق من القرص.
image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install(
        "ffmpeg",
        "curl",
        "ca-certificates",
        "unzip",
        "libcairo2",
        "libpango-1.0-0",
        "libjpeg62-turbo",
        "libgif7",
        "librsvg2-2",
        "libpixman-1-0",
    )
    .pip_install_from_requirements("requirements.txt")
    .pip_install(
        "modal>=1.0,<2.0",
        "fastapi[standard]>=0.115",
    )
    .add_local_dir(
        ".",
        remote_path=PROJECT_ROOT,
        copy=True,
        ignore=[
            ".git",
            ".git/**",
            ".venv",
            ".venv/**",
            "venv",
            "venv/**",
            "__pycache__",
            "**/__pycache__",
            "*.pyc",
            ".pytest_cache",
            ".env",
            ".env.*",
        ],
    )
    .workdir(PROJECT_ROOT)
    .env({
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": PROJECT_ROOT,
        "TMPDIR": "/tmp",
        "WAQQAD_MODAL_MODE": "1",
    })
)

app = modal.App(APP_NAME)


@app.function(
    image=image,
    secrets=_secrets(),
    timeout=10 * 60,
    cpu=1.0,
    memory=1024,
)
@modal.concurrent(max_inputs=100)
@modal.asgi_app()
def web():
    """نقطة الويب السريعة: Flask عبر ASGI/WSGI داخل Modal.

    استخدام ``modal.asgi_app`` يمنحنا ASGI واحدًا يمكن لـ Modal توسيعه
    أفقيًا، بينما يبقى منطق المسارات في ``app.py`` كما هو دون تكرار.
    """
    os.environ.setdefault("WAQQAD_MODAL_MODE", "1")
    os.environ.setdefault("TMPDIR", "/tmp")

    from fastapi import FastAPI
    from starlette.middleware.wsgi import WSGIMiddleware

    from app import app as flask_app

    api = FastAPI(
        title="Waqqad Transcriber (Modal)",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # Mount at root so all existing Flask routes and webhooks keep working.
    api.mount("/", WSGIMiddleware(flask_app))
    return api


def _mark_worker_failure(waqqad, job_id: str, exc: BaseException) -> None:
    """تسجيل فشل غير متوقع في دالة Modal دون إخفائه عن لوحة Modal."""
    try:
        job = waqqad.get_job(job_id) or {}
        if job.get("status") in {"completed", "failed"}:
            return
        waqqad.update_job(
            job_id,
            status="failed",
            stage="error",
            error=f"Modal worker failed: {type(exc).__name__}: {exc}",
            processing_backend="modal",
        )
        waqqad.log_transcription_failure(job_id)
    except Exception as update_error:
        print(
            f"[MODAL WORKER] could not mark job {job_id} failed: "
            f"{update_error}",
            flush=True,
        )


@app.function(
    image=image,
    secrets=_secrets(),
    timeout=6 * 60 * 60,
    cpu=2.0,
    memory=4096,
)
def process_job(payload: dict[str, Any]) -> dict[str, Any]:
    """الدالة الثقيلة: تنزيل + ffmpeg + تفريغ + تنقيح + إرسال.

    ``payload`` أنواعه:
      - upload: يمرّر بايتات الملف من حاوية الويب إلى هنا.
      - bot: يمرّر بيانات مرفق تيليجرام، وتنزّله هذه الدالة بنفسها.
      - url: يمرّر الرابط، وتنزّله هذه الدالة بنفسها.
      - deepgram_finalize: نتيجة callback جاهزة تُحفظ وتُرسل هنا.

    لا تُستخدم أي مسارات محلية من حاوية الويب؛ كل مسار يُبنى من job_id
    داخل حاوية العامل. التنظيف في ``finally`` إلزامي في كل الحالات.
    """
    # يجب ضبط هذه الرايات قبل استيراد app لأن الإعدادات تُقرأ عند الاستيراد.
    os.environ["WAQQAD_MODAL_MODE"] = "1"
    os.environ["WAQQAD_MODAL_WORKER"] = "1"
    os.environ["WAQQAD_SYNC_REFINE"] = "1"
    os.environ.setdefault("TMPDIR", "/tmp")

    import app as waqqad

    job_id = str(payload.get("job_id") or "").strip()
    kind = str(payload.get("kind") or "").strip()

    if not job_id or kind not in {
        "upload", "bot", "url", "deepgram_finalize"
    }:
        raise ValueError(f"Invalid Modal job payload: {payload!r}")

    try:
        if kind == "upload":
            file_bytes = payload.get("file_bytes")
            if not isinstance(file_bytes, (bytes, bytearray)):
                raise ValueError("upload payload is missing file_bytes")

            filename = waqqad._safe_upload_name(
                payload.get("filename") or "upload"
            )
            destination = (
                waqqad.get_work_dir(job_id) / "original" / filename
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(bytes(file_bytes))
            waqqad.update_job(
                job_id,
                stage="processing",
                file_size_bytes=len(file_bytes),
            )
            waqqad.process_transcription_job(job_id, destination)

        elif kind == "bot":
            attachment = payload.get("attachment") or {}
            filename = waqqad._safe_upload_name(
                attachment.get("file_name") or "attachment"
            )
            destination = (
                waqqad.get_work_dir(job_id) / "original" / filename
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            waqqad.run_bot_job(job_id, attachment, destination)

        elif kind == "url":
            url = str(payload.get("url") or "").strip()
            if not url:
                raise ValueError("url payload is empty")
            destination = waqqad.get_work_dir(job_id) / "original"
            destination.mkdir(parents=True, exist_ok=True)
            waqqad.run_url_job(
                job_id,
                url,
                destination,
                bool(payload.get("notify", True)),
            )

        else:  # deepgram_finalize
            transcript = str(payload.get("transcript") or "")
            if not transcript.strip():
                raise ValueError("deepgram_finalize transcript is empty")
            waqqad.finalize_transcript(
                job_id,
                transcript,
                engine="deepgram",
            )

        return {"status": "ok", "job_id": job_id, "kind": kind}

    except Exception as exc:
        print(
            f"[MODAL WORKER] job {job_id} ({kind}) failed: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        traceback.print_exc()
        _mark_worker_failure(waqqad, job_id, exc)
        # إعادة الرفع تُظهر الفشل في Modal وتسمح للمراقبة بالتقاطه.
        raise

    finally:
        # تنظيف إلزامي حتى لو فشل التنزيل أو ffmpeg أو الإرسال أو التنقيح.
        waqqad.cleanup_job_workdir(job_id)
