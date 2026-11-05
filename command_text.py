"""تطبيع نص الرسالة قبل تحليل أوامر البوت.

سبب الوجود: لوحات المفاتيح العربية قد تكتب محرفًا يشبه الشرطة المائلة
``/`` لكنه ليس إياها (مثل ``٫`` أو ``\\``)، فيبدو الأمر للمستخدم صحيحًا
بينما يراه البوت رسالة عادية تحمل رابطًا — فيُنشئ مهمة تفريغ بدل تنفيذ
الأمر. كذلك تختار قائمة تيليجرام الأمر وحده بلا وسيط، ثم يرسل المستخدم
الرابط في رسالة تالية. هنا نُطبّع المحرف الأول فقط، بلا مساس ببقية النص.
"""

# محارف تبدو كالشرطة المائلة على لوحات مفاتيح مختلفة.
SLASH_LOOKALIKES = ("٫", "／", "∕", "⁄", "÷", "\\")

# أوامر المطوّر التي نقبلها بلا شرطة مائلة (احتياط إضافي).
DEVELOPER_COMMANDS = frozenset({
    "stats",
    "users",
    "allusers",
    "cookies",
    "ytinfo",
    "block",
    "unblock",
    "blocked",
    "msg",
})


def _first_word(text):
    return text.split(None, 1)[0].split("@", 1)[0].lower()


def normalize_command_text(text, developer=False):
    """يعيد النص وقد صار أوله ``/`` إن كان أمرًا واضحًا.

    - ``٫ytinfo ...`` و``\\ytinfo ...`` ← ``/ytinfo ...``
    - ``ytinfo ...`` من المطوّر ← ``/ytinfo ...``
    - أي نص آخر يُعاد كما هو.
    """
    if not text:
        return text

    stripped = text.lstrip()
    if not stripped:
        return text

    if stripped.startswith("/"):
        return text

    first = stripped[:1]
    if first in SLASH_LOOKALIKES:
        candidate = "/" + stripped[1:]
        if _first_word(candidate).lstrip("/") in DEVELOPER_COMMANDS:
            return candidate

    if developer and _first_word(stripped) in DEVELOPER_COMMANDS:
        return "/" + stripped

    return text
