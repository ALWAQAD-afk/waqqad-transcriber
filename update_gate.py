"""بوّابة تحديثات تيليجرام: منع التكرار والتحديثات القديمة.

سبب الوجود: إذا تأخّر ردّ الويب هوك (أمر ثقيل مثل ``/allusers``) يعيد
تيليجرام إرسال **نفس** التحديث مرات عديدة. بلا منع تكرار يصبح الأمر
الثقيل حلقة لا نهائية تحجب كل الأوامر التي بعده. وإذا كان التحديث قديماً
جداً (خدمة نائمة ثم استيقظت) لا معنى لتنفيذه متأخراً.
"""

import threading
import time
from collections import OrderedDict

DEFAULT_SEEN_LIMIT = 1000
DEFAULT_STALE_SECONDS = 5 * 60


class SeenUpdates:
    """ذاكرة محدودة لمعرّفات التحديثات التي عولجت.

    ``seen(update_id)`` يعيد True إذا كان التحديث مكرّراً، ويسجّل الجديد.
    """

    def __init__(self, limit=DEFAULT_SEEN_LIMIT):
        self._limit = max(1, int(limit))
        self._items = OrderedDict()
        self._lock = threading.Lock()

    def seen(self, update_id):
        if update_id is None:
            return False
        key = str(update_id)
        with self._lock:
            if key in self._items:
                return True
            self._items[key] = time.time()
            while len(self._items) > self._limit:
                self._items.popitem(last=False)
        return False

    def clear(self):
        with self._lock:
            self._items.clear()

    def __len__(self):
        with self._lock:
            return len(self._items)


def is_stale_message(update, max_age_seconds=DEFAULT_STALE_SECONDS, now=None):
    """هل رسالة هذا التحديث أقدم من المهلة؟

    يخصّ الرسائل العادية فقط (لا أزرار التقييم)، حتى لا يضيع تقييم قديم.
    """
    message = update.get("message")
    if not isinstance(message, dict):
        return False
    sent_at = message.get("date")
    if not isinstance(sent_at, (int, float)):
        return False
    current = time.time() if now is None else now
    return (current - sent_at) > max_age_seconds
