"""طابور مهام التفريغ.

السياسة المعلنة للمشروع: خدمة بلا حدود استخدام لكل مستخدم. الترجمة
الهندسية لذلك ليست خيوطًا غير محدودة، بل طابور عادل وعدد ثابت من العمال:
كل من يرسل ملفًا يُخدَم، لكن بترتيب، بدل أن يتزاحم عشرون خيطًا على
ffmpeg والذاكرة فيفشل بعضها بصمت.

ملاحظة نشر: Gunicorn يعمل هنا بعامل واحد (workers=1)، فالطابور داخل
العملية. تشغيل أكثر من عامل سيُنشئ طابورًا لكل عامل.
"""

import logging
import os
import queue
import threading
import time
from collections import deque

log = logging.getLogger("job_queue")

WORKERS = max(1, int(os.environ.get("TRANSCRIBER_QUEUE_WORKERS", "2")))

_QUEUE = queue.Queue()
_LOCK = threading.Lock()

_WAITING = []        # معرّفات المهام المنتظرة بالترتيب
_RUNNING = {}        # job_id -> وقت البدء
_DURATIONS = deque(maxlen=20)
_STARTED = False


def start():
    """تشغيل العمال مرة واحدة ( lazy، آمن من التزامن)."""
    global _STARTED
    with _LOCK:
        if _STARTED:
            return
        _STARTED = True
        for index in range(WORKERS):
            thread = threading.Thread(
                target=_worker,
                name=f"tx-worker-{index + 1}",
                daemon=True,
            )
            thread.start()
    log.info("Transcription queue started with %s worker(s)", WORKERS)


def enqueue(job_id, fn, *args, **kwargs):
    """إضافة مهمة، وإرجاع ترتيبها في الانتظار (1 = التالي)."""
    start()
    with _LOCK:
        _WAITING.append(job_id)
        position = len(_WAITING)
    _QUEUE.put((job_id, fn, args, kwargs))
    return position


def _worker():
    while True:
        job_id, fn, args, kwargs = _QUEUE.get()
        started_at = time.time()

        with _LOCK:
            if job_id in _WAITING:
                _WAITING.remove(job_id)
            _RUNNING[job_id] = started_at

        try:
            fn(*args, **kwargs)
        except Exception:
            # فشل مهمة لا يجب أن يوقف العامل.
            log.exception("Job %s crashed inside queue worker", job_id)
        finally:
            _QUEUE.task_done()
            with _LOCK:
                _RUNNING.pop(job_id, None)
                _DURATIONS.append(time.time() - started_at)


def position(job_id):
    """ترتيب المهمة في الانتظار، أو None إن كانت تعمل أو انتهت."""
    with _LOCK:
        try:
            return _WAITING.index(job_id) + 1
        except ValueError:
            return None


def depth():
    """عدد المهام المنتظرة (دون التي تعمل الآن)."""
    with _LOCK:
        return len(_WAITING)


def running_count():
    with _LOCK:
        return len(_RUNNING)


def average_seconds():
    """متوسط زمن آخر 20 مهمة، أساس تقدير الانتظار."""
    with _LOCK:
        if not _DURATIONS:
            return None
        return sum(_DURATIONS) / len(_DURATIONS)


def estimate_wait(position_number):
    """تقدير تقريبي بالثواني لمهمة في موضع معين."""
    if not position_number or position_number < 1:
        return 0
    average = average_seconds()
    if not average:
        return None
    workers = max(1, WORKERS)
    waves = (position_number - 1) // workers + 1
    return int(average * waves)


def stats():
    with _LOCK:
        return {
            "workers": WORKERS,
            "waiting": len(_WAITING),
            "running": len(_RUNNING),
            "average_seconds": (
                sum(_DURATIONS) / len(_DURATIONS) if _DURATIONS else None
            ),
        }


def _reset_state_for_tests():
    """للاستعمال في الاختبارات فقط."""
    global _STARTED
    with _LOCK:
        _WAITING.clear()
        _RUNNING.clear()
        _DURATIONS.clear()
        _STARTED = False
    while True:
        try:
            _QUEUE.get_nowait()
            _QUEUE.task_done()
        except queue.Empty:
            break
