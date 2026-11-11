"""سجلات التشخيص التفصيلية لمشروع مفرّغ الوقّاد (Waqqad Transcriber).

توفر هذه الوحدة نظام تسجيل تشغيلي موسّع لكل مهمة تفريغ (ناجحة أو فاشلة)،
بجانب السجل المختصر الحالي، دون تغيير السجل الحالي ودون التأثير على أداء
التفريغ أو كشف أي أسرار.
"""

import json
import logging
import os
import re
import resource
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger("diagnostics")

DIAGNOSTIC_LOG_CHAT_ID = os.environ.get("TRANSCRIBER_DIAGNOSTIC_LOG_CHAT_ID", "").strip()
MAX_TELEGRAM_MESSAGE_CHARS = 3800

_SENT_DIAGNOSTIC_JOBS = set()
_SENT_LOCK = threading.Lock()


def reset_sent_diagnostic_jobs():
    """تفريغ سجل المهام المرسلة (مفيد للاختبارات)."""
    with _SENT_LOCK:
        _SENT_DIAGNOSTIC_JOBS.clear()


def get_target_chat_ids() -> List[str]:
    """استخراج معرفات القنوات المستهدفة دون تكرار.
    تعتمد تلقائياً على TRANSCRIBER_LOG_CHAT_ID (قناة الأرشيف وسجلات التفريغ)،
    وتقبل DIAGNOSTIC_LOG_CHAT_ID كقناة إضافية إن وُجدت واختلفت.
    """
    targets: List[str] = []
    main_chat = None
    try:
        import app
        main_chat = getattr(app, "TRANSCRIBER_LOG_CHAT_ID", None)
    except Exception:
        pass
    if not main_chat:
        main_chat = os.environ.get("TRANSCRIBER_LOG_CHAT_ID", "").strip()

    if main_chat:
        targets.append(str(main_chat).strip())

    diag_chat = (
        os.environ.get("TRANSCRIBER_DIAGNOSTIC_LOG_CHAT_ID")
        or os.environ.get("DIAGNOSTIC_LOG_CHAT_ID")
        or DIAGNOSTIC_LOG_CHAT_ID
        or ""
    ).strip()

    if diag_chat and str(diag_chat) not in targets:
        targets.append(str(diag_chat))

    return targets

# أنماط إخفاء الأسرار والمفاتيح والتوكنات
_SECRET_PATTERNS = [
    (re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.]{6,}", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(Token\s+)[A-Za-z0-9_\-\.]{6,}", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"((?:api[_-]?key|token|secret|password|auth|bot_token)[=:\s]+)[^\s,;&\"']+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(https?://[^\s?\"']+)\?[^\s\"']+", re.IGNORECASE), r"\1?[QUERY_REDACTED]"),
    (re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{20,}\b"), "[BOT_TOKEN_REDACTED]"),
]


def sanitize_secrets(value: Any) -> Any:
    """إزالة أي أسرار أو مفاتيح أو رموز توثيق من القيم لتأمين السجلات."""
    if isinstance(value, str):
        sanitized = value
        for pattern, repl in _SECRET_PATTERNS:
            sanitized = pattern.sub(repl, sanitized)
        return sanitized
    elif isinstance(value, dict):
        sanitized_dict = {}
        for k, v in value.items():
            key_lower = str(k).lower()
            if any(s in key_lower for s in ("key", "token", "secret", "password", "auth", "signature")):
                sanitized_dict[k] = "[REDACTED]"
            else:
                sanitized_dict[k] = sanitize_secrets(v)
        return sanitized_dict
    elif isinstance(value, list):
        return [sanitize_secrets(item) for item in value]
    return value


def get_memory_rss_mb() -> Optional[float]:
    """قراءة استهلاك الذاكرة الحالي للعملية (Resident Set Size) بطريقة خفيفة."""
    try:
        # ru_maxrss على Linux هو بالكيلوبايت
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 2)
    except Exception:
        return None


class JobDiagnosticCollector:
    """جامع البيانات التشخيصية لمهمة تفريغ واحدة بطريقة آمنة مع تعدد الخيوط."""

    def __init__(self, job_id: str):
        self.job_id = job_id
        self._lock = threading.Lock()

        # أولاً: معلومات المهمة
        self.created_at = time.time()
        self.started_at: Optional[float] = None
        self.completed_at: Optional[float] = None
        self.status = "queued"
        self.stage = "queued"
        self.source = "unknown"
        self.original_filename = "unknown"
        self.file_size_bytes = 0
        self.audio_duration_seconds: Optional[float] = None

        # ثانياً: استخراج الصوت وتجهيزه
        self.extraction_method: Optional[str] = None
        self.extraction_duration_seconds: Optional[float] = None
        self.audio_info: Dict[str, Any] = {}
        self.parts_count: int = 0
        self.parts: List[Dict[str, Any]] = []
        self.split_duration_seconds: Optional[float] = None

        # ثالثاً: تفاصيل المحرك
        self.engine: Optional[str] = None
        self.api_requests_count: int = 0
        self.actual_concurrency: int = 1
        self.max_concurrency_allowed: int = 1
        self.execution_mode: str = "sequential"
        self.parts_requests: List[Dict[str, Any]] = []
        self.retries: List[Dict[str, Any]] = []
        self.fallback_used: bool = False
        self.fallback_reason: Optional[str] = None
        self.fallback_engine: Optional[str] = None

        # رابعاً: تجميع النتائج والتنظيف
        self.merge_duration_seconds: Optional[float] = None
        self.parts_order_verified: Optional[bool] = None
        self.final_chars_count: Optional[int] = None
        self.final_words_count: Optional[int] = None
        self.archive_duration_seconds: Optional[float] = None
        self.cleanup: Dict[str, Any] = {"cleaned_up": False, "cleanup_error": None}

        # خامساً: موارد التشغيل
        self.memory_start_mb = get_memory_rss_mb()
        self.memory_end_mb: Optional[float] = None
        self.active_jobs_at_start: int = 0
        self.waiting_jobs_at_start: int = 0
        self.concurrency_settings: Dict[str, Any] = {}

        # تفاصيل الفشل إن حدث
        self.error_type: Optional[str] = None
        self.error_message: Optional[str] = None
        self.error_detail: Optional[str] = None

    def record_job_start(
        self,
        source: str = None,
        filename: str = None,
        file_size: int = None,
        audio_duration: float = None,
        active_jobs: int = 0,
        waiting_jobs: int = 0,
        concurrency_settings: dict = None,
    ):
        with self._lock:
            if not self.started_at:
                self.started_at = time.time()
            self.status = "processing"
            self.stage = "processing"
            if source:
                self.source = source
            if filename:
                self.original_filename = filename
            if file_size is not None:
                self.file_size_bytes = file_size
            if audio_duration is not None:
                self.audio_duration_seconds = audio_duration
            self.active_jobs_at_start = active_jobs
            self.waiting_jobs_at_start = waiting_jobs
            if concurrency_settings:
                self.concurrency_settings = concurrency_settings

    def record_audio_extraction(
        self,
        method: str,
        duration_seconds: float,
        audio_info: dict = None,
    ):
        with self._lock:
            self.extraction_method = method
            self.extraction_duration_seconds = round(duration_seconds, 3)
            if audio_info:
                self.audio_info = audio_info

    def record_audio_split(
        self,
        split_duration: float,
        parts_count: int,
        parts_list: list = None,
    ):
        with self._lock:
            self.split_duration_seconds = round(split_duration, 3)
            self.parts_count = parts_count
            if parts_list:
                self.parts = parts_list

    def record_cohere_plan(
        self,
        actual_concurrency: int,
        max_concurrency: int,
        execution_mode: str,
    ):
        with self._lock:
            self.engine = "cohere"
            self.actual_concurrency = actual_concurrency
            self.max_concurrency_allowed = max_concurrency
            self.execution_mode = execution_mode

    def record_part_request(
        self,
        part_index: int,
        duration_seconds: float,
        part_size_bytes: int = 0,
        chars_count: int = 0,
        status: str = "success",
        key_index: Any = None,
        error: str = None,
        started_at: float = None,
        ended_at: float = None,
    ):
        with self._lock:
            self.api_requests_count += 1
            now = time.time()
            end_t = ended_at or now
            start_t = started_at or (end_t - duration_seconds)

            req_info = {
                "part_index": part_index,
                "duration_seconds": round(duration_seconds, 3),
                "part_size_bytes": part_size_bytes,
                "chars_count": chars_count,
                "status": status,
                "key_index": f"#{key_index}" if key_index is not None else None,
                "started_at": start_t,
                "ended_at": end_t,
                "error": sanitize_secrets(error) if error else None,
            }
            self.parts_requests.append(req_info)

    def record_fallback(self, engine: str, reason: str):
        with self._lock:
            self.fallback_used = True
            self.fallback_engine = engine
            self.fallback_reason = sanitize_secrets(reason)
            self.engine = engine

    def record_merge(
        self,
        duration_seconds: float,
        order_verified: bool,
        final_chars_count: int,
        final_words_count: int,
    ):
        with self._lock:
            self.merge_duration_seconds = round(duration_seconds, 3)
            self.parts_order_verified = order_verified
            self.final_chars_count = final_chars_count
            self.final_words_count = final_words_count

    def record_archive(self, duration_seconds: float):
        with self._lock:
            self.archive_duration_seconds = round(duration_seconds, 3)

    def record_cleanup(self, cleaned_up: bool = True, error: str = None):
        with self._lock:
            self.cleanup = {
                "cleaned_up": cleaned_up,
                "cleanup_error": sanitize_secrets(error) if error else None,
            }

    def record_failure(self, stage: str, error: Any = None, error_detail: Any = None):
        with self._lock:
            self.status = "failed"
            if stage:
                self.stage = stage
            self.completed_at = time.time()
            self.memory_end_mb = get_memory_rss_mb()
            if error is not None:
                if isinstance(error, Exception):
                    self.error_type = type(error).__name__
                    self.error_message = sanitize_secrets(str(error))
                else:
                    self.error_type = self.error_type or "Error"
                    self.error_message = sanitize_secrets(str(error))
            elif not self.error_message:
                self.error_type = "Error"
                self.error_message = "Unknown error"
            if error_detail:
                self.error_detail = sanitize_secrets(str(error_detail))

    def record_success(self, engine: str = None):
        with self._lock:
            self.status = "completed"
            self.stage = "done"
            self.completed_at = time.time()
            self.memory_end_mb = get_memory_rss_mb()
            if engine:
                self.engine = engine

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            now = time.time()
            comp_time = self.completed_at or now
            start_time = self.started_at or self.created_at

            queue_wait = max(0.0, round(start_time - self.created_at, 3))
            total_processing = max(0.0, round(comp_time - start_time, 3))

            data = {
                "job_id": self.job_id,
                "status": self.status,
                "stage": self.stage,
                "timestamps": {
                    "created_at": self.created_at,
                    "started_at": self.started_at,
                    "completed_at": self.completed_at,
                    "created_at_iso": datetime.fromtimestamp(self.created_at, tz=timezone.utc).isoformat(),
                    "started_at_iso": (
                        datetime.fromtimestamp(self.started_at, tz=timezone.utc).isoformat()
                        if self.started_at
                        else None
                    ),
                    "completed_at_iso": (
                        datetime.fromtimestamp(self.completed_at, tz=timezone.utc).isoformat()
                        if self.completed_at
                        else None
                    ),
                    "queue_wait_seconds": queue_wait,
                    "total_processing_seconds": total_processing,
                },
                "job_info": {
                    "source": self.source,
                    "original_filename": self.original_filename,
                    "file_size_bytes": self.file_size_bytes,
                    "file_size_mb": (
                        round(self.file_size_bytes / (1024 * 1024), 2)
                        if self.file_size_bytes
                        else 0
                    ),
                    "audio_duration_seconds": self.audio_duration_seconds,
                },
                "audio_preparation": {
                    "extraction_method": self.extraction_method,
                    "extraction_duration_seconds": self.extraction_duration_seconds,
                    "audio_info": self.audio_info,
                    "parts_count": self.parts_count,
                    "split_duration_seconds": self.split_duration_seconds,
                    "parts": self.parts,
                },
                "transcription_engine": {
                    "engine": self.engine,
                    "execution_mode": self.execution_mode,
                    "actual_concurrency": self.actual_concurrency,
                    "max_concurrency_allowed": self.max_concurrency_allowed,
                    "api_requests_count": self.api_requests_count,
                    "parts_requests": self.parts_requests,
                    "retries": self.retries,
                    "fallback_used": self.fallback_used,
                    "fallback_reason": self.fallback_reason,
                    "fallback_engine": self.fallback_engine,
                },
                "results_and_cleanup": {
                    "merge_duration_seconds": self.merge_duration_seconds,
                    "parts_order_verified": self.parts_order_verified,
                    "final_chars_count": self.final_chars_count,
                    "final_words_count": self.final_words_count,
                    "archive_duration_seconds": self.archive_duration_seconds,
                    "cleanup": self.cleanup,
                },
                "runtime_resources": {
                    "memory_start_mb": self.memory_start_mb,
                    "memory_end_mb": self.memory_end_mb,
                    "active_jobs_at_start": self.active_jobs_at_start,
                    "waiting_jobs_at_start": self.waiting_jobs_at_start,
                    "concurrency_settings": self.concurrency_settings,
                },
                "failure_details": (
                    {
                        "error_type": self.error_type,
                        "error_message": self.error_message,
                        "error_detail": self.error_detail,
                    }
                    if self.status == "failed"
                    else None
                ),
            }
            return sanitize_secrets(data)

    def format_report(self) -> str:
        d = self.to_dict()
        ts = d["timestamps"]
        ji = d["job_info"]
        ap = d["audio_preparation"]
        te = d["transcription_engine"]
        rc = d["results_and_cleanup"]
        rr = d["runtime_resources"]
        fd = d["failure_details"]

        status_icon = "✅ ناجح" if d["status"] == "completed" else "❌ فاشل"
        lines = [
            "🔍 سجل تشخيصي تفصيلي (Detailed Diagnostic Log)",
            "══════════════════════════════════════════════",
            f"🆔 المهمة: {d['job_id']} [{status_icon}]",
            f"📄 الملف: {ji['original_filename']} ({ji['file_size_mb']} MB) | المصدر: {ji['source']}",
            f"⏱️ الأوقات: الانتظار: {ts['queue_wait_seconds']} ث | المعالجة الإجمالية: {ts['total_processing_seconds']} ث",
        ]

        if ap["extraction_method"] or ap["split_duration_seconds"] is not None:
            lines.append("🎧 الصوت والتجهيز:")
            if ap["extraction_method"]:
                lines.append(f"  • الاستخراج: {ap['extraction_method']} ({ap['extraction_duration_seconds'] or 0} ث)")
            if ap["split_duration_seconds"] is not None:
                lines.append(f"  • التقسيم: {ap['parts_count']} أجزاء ({ap['split_duration_seconds']} ث)")
            if ap["parts"]:
                for p in ap["parts"]:
                    size_kb = round(p.get("size_bytes", 0) / 1024.0, 1)
                    lines.append(f"    - جزء {p.get('part_index')}: {size_kb} KB")

        lines.append(f"⚙️ المحرك ({te['engine'] or 'غير معروف'}):")
        lines.append(
            f"  • نمط التنفيذ: {te['execution_mode']} (تزامن فعلي: {te['actual_concurrency']} / أقصى مسموح: {te['max_concurrency_allowed']})"
        )
        lines.append(f"  • طلبات API: {te['api_requests_count']}")

        if te["parts_requests"]:
            for req in te["parts_requests"]:
                key_str = f" | المفتاح #{req['key_index']}" if req.get("key_index") is not None else ""
                lines.append(
                    f"    - جزء {req['part_index']}: {req['duration_seconds']} ث | {req['chars_count']} حرف{key_str} [{req['status']}]"
                )

        if te["fallback_used"]:
            lines.append(f"  ⚠️ تحويل إلى {te['fallback_engine']}: {te['fallback_reason']}")

        if rc["merge_duration_seconds"] is not None or rc["final_chars_count"] is not None:
            lines.append("📝 التجميع والأرشفة:")
            if rc["merge_duration_seconds"] is not None:
                verified_str = "نعم ✅" if rc["parts_order_verified"] else "لا ⚠️"
                lines.append(f"  • مدة التجميع: {rc['merge_duration_seconds']} ث | الترتيب سليم: {verified_str}")
            if rc["final_chars_count"] is not None:
                lines.append(f"  • المحصول: {rc['final_chars_count']} حرف ({rc['final_words_count'] or 0} كلمة)")
            if rc["archive_duration_seconds"] is not None:
                lines.append(f"  • مدة الأرشفة: {rc['archive_duration_seconds']} ث")
            cl = rc["cleanup"]
            cl_status = "تم بنجاح ✅" if cl.get("cleaned_up") else f"فشل: {cl.get('cleanup_error')}"
            lines.append(f"  • تنظيف المؤقتات: {cl_status}")

        lines.append("💻 الموارد:")
        lines.append(f"  • الذاكرة (RSS): {rr['memory_end_mb'] or rr['memory_start_mb'] or 'غير متاح'} MB")
        lines.append(f"  • مهام عند البدء: {rr['active_jobs_at_start']} نشطة، {rr['waiting_jobs_at_start']} منتظرة")

        if fd:
            lines.append("❌ تفاصيل الخطأ:")
            lines.append(f"  • المرحلة: {d['stage']}")
            lines.append(f"  • النوع: {fd['error_type']}")
            lines.append(f"  • الرسالة: {fd['error_message']}")
            if fd.get("error_detail"):
                lines.append(f"  • التفاصيل: {fd['error_detail']}")

        lines.append("══════════════════════════════════════════════")
        return "\n".join(lines)


# سجل الجامعين في الذاكرة
_LOCK = threading.Lock()
_COLLECTORS: Dict[str, JobDiagnosticCollector] = {}


def get_collector(job_id: str, create_if_missing: bool = True) -> Optional[JobDiagnosticCollector]:
    """استرجاع جامع التشخيصات الخاص بالمهمة أو إنشاؤه عند الحاجة."""
    with _LOCK:
        if job_id not in _COLLECTORS and create_if_missing:
            _COLLECTORS[job_id] = JobDiagnosticCollector(job_id)
        return _COLLECTORS.get(job_id)


def pop_collector(job_id: str) -> Optional[JobDiagnosticCollector]:
    """سحب جامع التشخيصات من الذاكرة وحذفه بعد حفظه."""
    with _LOCK:
        return _COLLECTORS.pop(job_id, None)


def init_table():
    """إنشاء جدول job_diagnostic_logs في قاعدة البيانات إن لم يكن موجوداً."""
    try:
        from db import get_db_connection
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS job_diagnostic_logs (
                        job_id TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        data JSONB NOT NULL,
                        formatted_text TEXT NOT NULL,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    );
                """)
                conn.commit()
    except Exception as e:
        log.warning("Could not initialize job_diagnostic_logs table: %s", e)


def send_diagnostic_telegram(job_id: str, formatted_text: str, original_filename: Optional[str] = None):
    """
    إرسال السجل التشخيصي إلى قناة الأرشيف تلقائياً بعد السجل المختصر.
    - يُرسل تلقائياً إلى TRANSCRIBER_LOG_CHAT_ID (أو DIAGNOSTIC_LOG_CHAT_ID إن وُجدت).
    - إذا كان السجل طويلاً (> 3800 حرفاً)، يُرسل كملف TXT بدلاً من رسالة نصية مع عنوان يتضمن معرف المهمة واسم الملف.
    - يمنع التكرار (Idempotent) حتى عند إعادة محاولة الأرشفة أو الاستدعاء المتكرر.
    - يمنع إرسال نسختين مكررتين إذا كانت DIAGNOSTIC_LOG_CHAT_ID مطابقة لـ TRANSCRIBER_LOG_CHAT_ID.
    - آمن تماماً من الاستثناءات، ولا يؤثر على التفريغ أو الأرشفة إطلاقاً.
    """
    if not job_id or not formatted_text:
        return

    # فحص منع التكرار
    with _SENT_LOCK:
        if job_id in _SENT_DIAGNOSTIC_JOBS:
            log.info("Diagnostic log already sent to Telegram for job %s, skipping duplicate", job_id)
            return
        _SENT_DIAGNOSTIC_JOBS.add(job_id)

    target_chats = get_target_chat_ids()
    if not target_chats:
        log.debug("No target chat configured for diagnostic logs (job %s)", job_id)
        return

    try:
        import app
        from pathlib import Path

        filename = original_filename or "unknown"
        stem = Path(filename).stem.strip() or "audio"
        stem_clean = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", stem)
        stem_clean = re.sub(r"\s+", " ", stem_clean).strip()
        doc_filename = f"diagnostic_{job_id}_{stem_clean}.txt"

        caption = (
            "📋 سجل تفريغ تفصيلي (تشخيصي)\n"
            f"📄 الملف: {filename}\n"
            f"🆔 المهمة: {job_id}"
        )

        is_long = len(formatted_text) > MAX_TELEGRAM_MESSAGE_CHARS

        for chat_id in target_chats:
            try:
                if is_long:
                    # إرسال كملف TXT عبر send_document_bytes
                    app.send_document_bytes(
                        chat_id=chat_id,
                        payload=formatted_text.encode("utf-8"),
                        download_name=doc_filename,
                        caption=caption,
                        mime="text/plain; charset=utf-8"
                    )
                else:
                    try:
                        app.telegram_send_message(chat_id, formatted_text)
                    except Exception as send_err:
                        # إن فشل الإرسال كنص بسبب الطول غير المتوقع، أرسله كملف TXT
                        err_str = str(send_err).lower()
                        if "too long" in err_str or "400" in err_str:
                            log.info("Falling back to document for diagnostic log (job %s) to %s", job_id, chat_id)
                            app.send_document_bytes(
                                chat_id=chat_id,
                                payload=formatted_text.encode("utf-8"),
                                download_name=doc_filename,
                                caption=caption,
                                mime="text/plain; charset=utf-8"
                            )
                        else:
                            raise send_err
            except Exception as e:
                log.warning("Could not send diagnostic log for %s to chat %s: %s", job_id, chat_id, e)

    except Exception as outer_err:
        log.warning("send_diagnostic_telegram failed for %s: %s", job_id, outer_err)


def save_diagnostic_log(
    job_id: str,
    status: str,
    diag_data: dict,
    formatted_text: str,
    original_filename: Optional[str] = None
):
    """
    حفظ السجل التشخيصي المفصل في قاعدة البيانات وإرساله إلى القنوات المستهدفة.
    لا يُلقي أي استثناءات مطلقاً لضمان عدم تأثر عملية التفريغ أو الأرشفة.
    """
    filename = (
        original_filename
        or diag_data.get("job_info", {}).get("original_filename")
    )

    # 1. إرسال إلى تيليجرام
    send_diagnostic_telegram(job_id, formatted_text, filename)

    # 2. الحفظ في قاعدة البيانات
    try:
        from db import get_db_connection
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO job_diagnostic_logs (job_id, status, data, formatted_text)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (job_id) DO UPDATE
                    SET status = EXCLUDED.status,
                        data = EXCLUDED.data,
                        formatted_text = EXCLUDED.formatted_text,
                        created_at = NOW()
                """, (job_id, status, json.dumps(diag_data), formatted_text))
                conn.commit()
    except Exception as e:
        log.warning("Could not persist to job_diagnostic_logs: %s", e)

    # طباعة ملخص في سجلات النظام
    log.info("Detailed Diagnostic Log for %s (%s):\n%s", job_id, status, formatted_text)


def finalize_diagnostic_success(job_id: str):
    """
    إنهاء وحفظ السجل التشخيصي لمهمة ناجحة.
    آمن تماماً من الاستثناءات.
    """
    try:
        collector = get_collector(job_id, create_if_missing=True)
        collector.record_success()
        diag_data = collector.to_dict()
        formatted_text = collector.format_report()
        save_diagnostic_log(
            job_id,
            "completed",
            diag_data,
            formatted_text,
        )
    except Exception as e:
        log.exception("Failed to finalize diagnostic success for %s: %s", job_id, e)
    finally:
        pop_collector(job_id)


def finalize_diagnostic_failure(
    job_id: str,
    stage: str = None,
    error: Any = None,
    error_detail: Any = None
):
    """
    إنهاء وحفظ السجل التشخيصي لمهمة فاشلة.
    آمن تماماً من الاستثناءات.
    """
    try:
        collector = get_collector(job_id, create_if_missing=True)
        # إذا لم يتم تمرير الخطأ، ولم يكن مسجلاً مسبقاً، حاول قراءته من قاعدة بيانات الوظائف
        if error is None and not collector.error_message:
            try:
                import app
                job = app.get_job(job_id) or {}
                stage = stage or job.get("stage") or "error"
                error = job.get("error") or "Unknown error"
                error_detail = error_detail or job.get("error_detail")
            except Exception:
                pass
        collector.record_failure(stage or collector.stage or "error", error, error_detail)
        diag_data = collector.to_dict()
        formatted_text = collector.format_report()
        save_diagnostic_log(
            job_id,
            "failed",
            diag_data,
            formatted_text,
        )
    except Exception as e:
        log.exception("Failed to finalize diagnostic failure for %s: %s", job_id, e)
    finally:
        pop_collector(job_id)


def get_diagnostic_log(job_id: str) -> Optional[Dict[str, Any]]:
    """استرجاع السجل التشخيصي لمهمة معينة من الذاكرة أو قاعدة البيانات."""
    with _LOCK:
        collector = _COLLECTORS.get(job_id)
        if collector:
            return collector.to_dict()

    try:
        from db import get_db_connection
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT data, formatted_text, status FROM job_diagnostic_logs WHERE job_id = %s",
                    (job_id,)
                )
                row = cur.fetchone()
                if row:
                    data = row["data"]
                    if isinstance(data, str):
                        data = json.loads(data)
                    data["formatted_text"] = row["formatted_text"]
                    return data
    except Exception as e:
        log.warning("Could not read diagnostic log for %s: %s", job_id, e)

    return None
