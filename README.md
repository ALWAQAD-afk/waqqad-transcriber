# مفرّغ الوقّاد

خدمة Flask واحدة تجمع بين:

- **مفرّغ الوقّاد**: تفريغ الملفات الصوتية والمرئية إلى نص عربي.
  - المحرك الأساسي: Cohere.
  - المحرك الاحتياطي: Deepgram عند تعذر Cohere.
  - تنقية النص إملائياً: Gemini.
- **الورّاق**: بوت تيليجرام لأرشفة الملفات + واجهة Mini App.

## المتطلبات

- Python 3.12
- ffmpeg (مثبت داخل Docker)
- قاعدة بيانات PostgreSQL
- مفاتيح API من Cohere و Deepgram و Gemini
- توكن بوت تيليجرام

## التشغيل محلياً

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# املأ القيم داخل .env
export $(grep -v '^#' .env | xargs)
python app.py
```

الخدمة تعمل على `http://localhost:10000`.

## النشر

المشروع مهيأ للنشر على Render عبر `Dockerfile`:

- المنفذ: `10000`
- فحص الصحة: `/health`
- يعمل بمستخدم غير جذري.

## متغيرات البيئة

| المتغير | الوظيفة |
|---|---|
| `DATABASE_URL` | قاعدة بيانات مهام التفريغ |
| `TELEGRAM_BOT_TOKEN` | توكن بوت مفرّغ الوقّاد |
| `DEEPGRAM_API_KEY` | مفتاح Deepgram الاحتياطي |
| `COHERE_API_KEYS` | مفتاح أو مفاتيح Cohere مفصولة بفواصل |
| `COHERE_API_KEY` | بديل لمفتاح واحد |
| `GEMINI_API_KEY` | مفتاح تنقية النص |
| `BASE_URL` | رابط الخدمة العام |
| `TRANSCRIBER_WEBAPP_URL` | رابط صفحة التفريغ |
| `TRANSCRIBER_LOG_CHAT_ID` | محادثة سجلات (اختياري) |
| `TELEGRAM_WEBHOOK_SECRET` | سر ويبهوك بوت التفريغ |
| `WARRAQ_WEBHOOK_SECRET` | سر ويبهوك بوت الورّاق |
| `TELEGRAM_INITDATA_MAX_AGE` | عمر جلسة Mini App بالثواني (افتراضي 86400) |
| `GEMINI_MODEL` | موديل التدقيق (افتراضي gemini-3.5-flash-lite) |
| `REFINE_CHUNK_CHARS` | حجم قطعة التدقيق بالحروف (افتراضي 3500) |
| `REFINE_MIN_CHUNK_CHARS` | أصغر قطعة عند التقسيم التلقائي (افتراضي 500) |
| `REFINE_TIMEOUT` | مهلة طلب التدقيق بالثواني (افتراضي 180) |
| `REFINE_MAX_RETRIES` | عدد محاولات إعادة طلب التدقيق (افتراضي 3) |
| `REFINE_REQUEST_GAP` | فاصل بين قطع التدقيق بالثواني (افتراضي 1.0) |
| `REFINE_THINKING_LEVEL` | مستوى تفكير الموديل (افتراضي minimal) |
| `WARRAQ_BOT_TOKEN` | توكن بوت الورّاق |
| `WARRAQ_STORAGE_CHAT_ID` | محادثة تخزين ملفات الورّاق |
| `WARRAQ_DATABASE_URL` | قاعدة بيانات الورّاق |
| `WARRAQ_MINIAPP_URL` | رابط واجهة الورّاق |

جميع المتغيرات موثقة في `.env.example`.

## أسرار الويبهوك

عند ضبط `TELEGRAM_WEBHOOK_SECRET` و`WARRAQ_WEBHOOK_SECRET` تصبح طلبات الويبهوك مرفوضة (403) ما لم تحمل الترويسة الصحيحة `X-Telegram-Bot-Api-Secret-Token`.

سجّل الويبهوك بالسر مرة واحدة (بعد النشر):

```bash
curl -X POST "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" \
  -d "url=https://<BASE_URL>/telegram/transcriber" \
  -d "secret_token=<TELEGRAM_WEBHOOK_SECRET>"

curl -X POST "https://api.telegram.org/bot<WARRAQ_BOT_TOKEN>/setWebhook" \
  -d "url=https://<BASE_URL>/telegram/warraq" \
  -d "secret_token=<WARRAQ_WEBHOOK_SECRET>"
```

## المصادقة

- صفحة التفريغ تُفتح من داخل بوت تيليجرام فقط، وترسل ترويسة `X-Telegram-Init-Data`.
- الخادم يتحقق من توقيع Telegram ويستخرج رقم المستخدم منه، ولا يعتمد على أي رقم يُرسل في النموذج.
- `/status` و`/download` و`/refine` تعمل لصاحب المهمة فقط.
- واجهة الورّاق تتحقق من `initData` بنفس الطريقة.

## البنية

| الملف | الوظيفة |
|---|---|
| `app.py` | تطبيق Flask ومسارات التفريغ والويبهوك |
| `warraq.py` | منطق بوت الورّاق وقاعدة بياناته |
| `warraq_miniapp.py` | واجهات Mini App للورّاق |
| `warraq.html` | صفحة واجهة الورّاق |
| `refine.py` | تنقية النص عبر Gemini |
| `Dockerfile` | صورة النشر |

## التدقيق التلقائي

بعد كل تفريغ يصل الملف الخام فوراً، ثم يبدأ التدقيق تلقائياً وينتج ملفين إضافيين:

- `<الاسم>_med.txt`: نسخة مصححة إملائياً دون تغيير الكلمات.
- `<الاسم>_well.txt`: نسخة محسّنة للقراءة مع فقرات وعناوين.

إذا رفض المزوّد النص لطوله يُقسَّم تلقائياً ويعاد، وإذا تعذر تدقيق جزء يُرسل تنبيه واضح مع إعادة النص الأصلي لذلك الجزء.

## ملاحظات التشغيل

- نتائج التفريغ تُرسل إلى محادثة المستخدم في تيليجرام، والملف المؤقت في `/tmp` للتنزيل فقط.
- عند إعادة تشغيل الخدمة تُعلَّم المهام غير المكتملة كفاشلة، وتُحذف مجلدات العمل القديمة.
- لا توجد حدود على حجم الملف أو عدد الطلبات؛ حدود تيليجرام/Render نفسها تبقى مطبقة.

## النسخ الاحتياطي

الفرع الاحتياطي بتاريخ 2026-10-30:

```
Backup-stable-30-10-2026
```

وللرجوع إليه:

```bash
git fetch origin
git checkout Backup-stable-30-10-2026
```
