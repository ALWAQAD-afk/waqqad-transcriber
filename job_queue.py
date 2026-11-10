"""طابور مهام التفريغ العادل (Fair-Share Transcription Queue).

السياسة المعلنة للمشروع: خدمة عادلة بلا حدود استخدام تعسفية.
الهندسة تعتمد على جدولة عادلة (Deficit / Fair-Share Queueing):
- إذا أرسل مستخدم ملفات كثيرة متتالية، لا يحجب المستخدمين الآخرين.
- يتم تفضيل المستخدمين الذين لديهم مهام قيد المعالجة أقل.
- الحفاظ على الترتيب الأصلي (FIFO) داخل مهام المستخدم الواحد.
- الحفاظ على استقرار التطبيق داخل عامل Gunicorn واحد (workers=1).
"""

import logging
import os
import threading
import time
from collections import Counter, deque
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("job_queue")

WORKERS = max(1, int(os.environ.get("TRANSCRIBER_QUEUE_WORKERS", "2")))

_LOCK = threading.Lock()
_CONDITION = threading.Condition(_LOCK)

_WAITING_TASKS: List[Dict[str, Any]] = []
_RUNNING: Dict[str, Dict[str, Any]] = {}
_DURATIONS = deque(maxlen=20)
_STARTED = False
_STOPPING = False
_V_SYSTEM: float = 0.0
_USER_VIRTUAL_TIME: Dict[Any, float] = {}


def _get_fair_order_of_waiting() -> List[str]:
    """
    ترتيب المهام المنتظرة حالياً وفق خوارزمية الجدولة العادلة.
    """
    if not _WAITING_TASKS:
        return []

    # إذا كانت المهام تحتوي على virtual_time محسوب
    if all("virtual_time" in t for t in _WAITING_TASKS):
        sorted_tasks = sorted(
            _WAITING_TASKS,
            key=lambda t: (t.get("virtual_time", 0.0), t.get("enqueued_at", 0.0))
        )
        return [t["job_id"] for t in sorted_tasks]

    # محاكاة احتياطية في حال إضافة مهام يدوياً دون virtual_time
    pending = list(_WAITING_TASKS)
    running_counts = Counter(
        info["user_id"] for info in _RUNNING.values() if info.get("user_id") is not None
    )

    ordered = []
    while pending:
        first_per_user = {}
        for task in pending:
            uid = task["user_id"]
            if uid not in first_per_user:
                first_per_user[uid] = task

        best_task = min(
            first_per_user.values(),
            key=lambda t: (running_counts[t["user_id"]], t["enqueued_at"]),
        )

        ordered.append(best_task["job_id"])
        pending.remove(best_task)
        if best_task["user_id"] is not None:
            running_counts[best_task["user_id"]] += 1

    return ordered


def start():
    """تشغيل عمال الطابور مرة واحدة (Lazy & Thread-safe)."""
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


def enqueue(job_id, fn, *args, user_id=None, **kwargs):
    """
    إضافة مهمة إلى الطابور العادل، وإرجاع ترتيبها المتوقع (1 = التالي).

    معلمة user_id اختيارية: تُستخدم للموازنة العادلة بين المستخدمين.
    """
    start()
    now = time.time()
    with _LOCK:
        uid = str(user_id) if user_id is not None else f"_anon_{job_id}"
        has_active = any(t.get("uid") == uid for t in _WAITING_TASKS) or any(
            info.get("uid") == uid for info in _RUNNING.values()
        )
        if has_active:
            v_time = _USER_VIRTUAL_TIME.get(uid, _V_SYSTEM) + 1.0
        else:
            v_time = _V_SYSTEM + 1.0

        _USER_VIRTUAL_TIME[uid] = v_time

        _WAITING_TASKS.append({
            "job_id": job_id,
            "user_id": user_id,
            "uid": uid,
            "fn": fn,
            "args": args,
            "kwargs": kwargs,
            "enqueued_at": now,
            "virtual_time": v_time,
        })
        order = _get_fair_order_of_waiting()
        pos = order.index(job_id) + 1 if job_id in order else len(_WAITING_TASKS)
        _CONDITION.notify()
    return pos


def _worker():
    global _V_SYSTEM
    while True:
        with _LOCK:
            while not _WAITING_TASKS and not _STOPPING:
                _CONDITION.wait()

            if _STOPPING and not _WAITING_TASKS:
                break

            fair_order = _get_fair_order_of_waiting()
            next_job_id = fair_order[0]
            selected_task = next(t for t in _WAITING_TASKS if t["job_id"] == next_job_id)
            _WAITING_TASKS.remove(selected_task)

            job_id = selected_task["job_id"]
            user_id = selected_task["user_id"]
            uid = selected_task.get("uid")
            fn = selected_task["fn"]
            args = selected_task["args"]
            kwargs = selected_task["kwargs"]
            v_time = selected_task.get("virtual_time", 0.0)
            started_at = time.time()

            _RUNNING[job_id] = {
                "started_at": started_at,
                "user_id": user_id,
                "uid": uid,
            }
            if v_time > 0:
                _V_SYSTEM = max(_V_SYSTEM, v_time - 1.0)

        try:
            fn(*args, **kwargs)
        except Exception:
            log.exception("Job %s crashed inside queue worker", job_id)
        finally:
            with _LOCK:
                _RUNNING.pop(job_id, None)
                _DURATIONS.append(time.time() - started_at)
                if uid is not None:
                    has_remaining = any(t.get("uid") == uid for t in _WAITING_TASKS) or any(
                        info.get("uid") == uid for info in _RUNNING.values()
                    )
                    if not has_remaining:
                        _USER_VIRTUAL_TIME.pop(uid, None)


def position(job_id):
    """ترتيب المهمة في الانتظار حسب الجدولة العادلة، أو None إن كانت تعمل أو انتهت."""
    with _LOCK:
        order = _get_fair_order_of_waiting()
        try:
            return order.index(job_id) + 1
        except ValueError:
            return None


def depth():
    """عدد المهام المنتظرة (دون التي تعمل الآن)."""
    with _LOCK:
        return len(_WAITING_TASKS)


def running_count():
    """عدد المهام قيد التنفيذ حالياً."""
    with _LOCK:
        return len(_RUNNING)


def average_seconds():
    """متوسط زمن آخر 20 مهمة، كأساس لتقدير الانتظار."""
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
            "waiting": len(_WAITING_TASKS),
            "running": len(_RUNNING),
            "average_seconds": (
                sum(_DURATIONS) / len(_DURATIONS) if _DURATIONS else None
            ),
        }


class _WaitingListProxy:
    def __len__(self):
        return depth()

    def __contains__(self, item):
        with _LOCK:
            return any(t["job_id"] == item for t in _WAITING_TASKS)

    def index(self, item):
        pos = position(item)
        if pos is None:
            raise ValueError(f"{item} is not in list")
        return pos - 1


_WAITING = _WaitingListProxy()


def _reset_state_for_tests():
    """للاستعمال في الاختبارات فقط لتنظيف الحالة."""
    global _STARTED, _V_SYSTEM
    with _LOCK:
        _WAITING_TASKS.clear()
        _RUNNING.clear()
        _DURATIONS.clear()
        _USER_VIRTUAL_TIME.clear()
        _V_SYSTEM = 0.0
        _STARTED = False
