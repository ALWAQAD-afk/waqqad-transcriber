# نشر الوقّاد على Modal مع Render كطوارئ

## 1. المعمارية

```
Telegram / Web
      │
      ▼
Modal web (ASGI / Flask)
      │  رد سريع فقط
      ▼
Modal process_job (دالة خلفية مستقلة)
      │  تنزيل + ffmpeg + تفريغ + تنقيح + إرسال
      ▼
PostgreSQL + Telegram
```

- **`modal_app.py::web`**: نقطة `modal.asgi_app` تستضيف Flask كما هو. لا تنفّذ ffmpeg ولا تفريغًا.
- **`modal_app.py::process_job`**: دالة `modal.Function` مستقلة، مهلة 6 ساعات، 2 CPU و4GB RAM افتراضيًا. تستقبل:
  - `upload`: بايتات ملف الويب.
  - `bot`: بيانات مرفق تيليجرام، وتنزّله بنفسها.
  - `url`: رابط، وتنزّله بنفسها.
  - `deepgram_finalize`: نتيجة callback جاهزة.
- **`app.py::dispatch_heavy_job`**: الموزّع الوحيد. يحاول Modal أولًا. لا يرسل شيئًا إلى Render ما دام استدعاء Modal قد نجح، بلا أي شرط على مدة الملف.
- **`app.py::/internal/render-fallback`**: مستقبل Render المحمي بسر. يضع المهمة في طابور Render المحلي ولا يعيدها إلى Modal.

## 2. إعداد Modal

### 2.1 إنشاء السر

كل متغيرات البيئة الحساسة تعيش في Modal Secret، لا في الصورة ولا المستودع:

```bash
modal secret create waqqad-transcriber-env \
  DATABASE_URL='postgresql://...' \
  TELEGRAM_BOT_TOKEN='...' \
  COHERE_API_KEYS='key1,key2' \
  DEEPGRAM_API_KEY='...' \
  GEMINI_API_KEY='...' \
  TELEGRAM_WEBHOOK_SECRET='...' \
  WARRAQ_WEBHOOK_SECRET='...' \
  WARRAQ_BOT_TOKEN='...' \
  WARRAQ_STORAGE_CHAT_ID='...' \
  WARRAQ_DATABASE_URL='postgresql://...' \
  BASE_URL='https://<workspace>--waqqad-transcriber-web.modal.run' \
  TRANSCRIBER_WEBAPP_URL='https://alwaqad-afk.github.io/waqqad-transcriber-web/' \
  TRANSCRIBER_LOG_CHAT_ID='...' \
  DEVELOPER_CHAT_ID='...' \
  RENDER_FALLBACK_URL='https://<render-service>.onrender.com' \
  RENDER_FALLBACK_ENABLED='1' \
  RENDER_FALLBACK_SECRET='<long-random-secret>'
```

يمكن تغيير اسم السر عبر `MODAL_SECRET_NAMES`، ويقبل أسماء متعددة مفصولة بفواصل.

### 2.2 النشر اليدوي

```bash
pip install "modal>=1.0,<2.0"
modal deploy modal_app.py
```

بعد النشر:

```bash
modal app list
modal app logs waqqad-transcriber
```

### 2.3 النشر التلقائي

`.github/workflows/deploy.yml` يعمل عند:

- push إلى `main`
- `workflow_dispatch`

أضف أسرار المستودع:

| الاسم | القيمة |
|---|---|
| `MODAL_TOKEN_ID` | من `modal token new` |
| `MODAL_TOKEN_SECRET` | من `modal token new` |

اختياريًا أضف Repository variables:

- `MODAL_SECRET_NAMES` (افتراضي `waqqad-transcriber-env`)
- `MODAL_APP_NAME` (افتراضي `waqqad-transcriber`)

### 2.4 تسجيل الويبهوك

```bash
curl -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" \
  -d "url=https://<workspace>--waqqad-transcriber-web.modal.run/telegram/transcriber" \
  -d "secret_token=$TELEGRAM_WEBHOOK_SECRET" \
  -d 'allowed_updates=["message","callback_query"]'
```

لويبهوك الورّاق استخدم `WARRAQ_BOT_TOKEN` والمسار `/telegram/warraq`.

## 3. Render كطوارئ أخيرة

### 3.1 إعداد خدمة Render

انشر نفس المستودع على Render مع:

- نفس `DATABASE_URL` (ضروري للحالة والبحث والتنزيل والتنقيح).
- `RENDER_FALLBACK_SECRET` (نفس قيمة Modal).
- `WAQQAD_RENDER_FALLBACK_MODE=1` حتى لا يعلّم إقلاع Render مهام Modal النشطة بالفشل.
- `WAQQAD_DISABLE_MODAL_DISPATCH=1` حتى لا يحاول Render إعادة الإرسال إلى Modal.
- `RENDER_FALLBACK_ENABLED=0` على Render نفسه (هو مستقبل لا مرسل).

### 3.2 إعداد Modal

على سر Modal:

```
RENDER_FALLBACK_URL=https://<render-service>.onrender.com
RENDER_FALLBACK_ENABLED=1
RENDER_FALLBACK_SECRET=<same-long-random-secret>
RENDER_FALLBACK_TIMEOUT=180
```

### 3.3 متى يعمل الاحتياطي؟

فقط عند استثناء في:

- استيراد/تهيئة عميل Modal.
- `process_job.spawn(...)` (اتصال، مهلة، فشل تشغيل الدالة).
- تعطّل نقطة الويب نفسها: هنا يلزم تحويل ويبهوك تيليجرام يدويًا إلى Render.

**لا يوجد** أي شرط مثل «الملف أقل من دقيقة» أو «الحجم صغير». كل المهام تذهب إلى Modal أولًا.

### 3.4 تحويل الويبهوك عند تعطل Modal

```bash
curl -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" \
  -d "url=https://<render-service>.onrender.com/telegram/transcriber" \
  -d "secret_token=$TELEGRAM_WEBHOOK_SECRET" \
  -d 'allowed_updates=["message","callback_query"]'
```

بعد عودة Modal، أعد الويبهوك إلى رابط Modal. لا تحتاج إلى حذف أي بيانات؛ قاعدة البيانات مشتركة.

## 4. التحقق بعد النشر

1. `GET https://<modal-web>/health` يعيد `{"status":"ok"}`.
2. أرسل `/start` للبوت وتأكد من وصول الرد عبر Modal.
3. ارفع ملفًا قصيرًا (أقل من دقيقة) وتأكد أن `processing_backend` في جدول `jobs` يساوي `modal` — هذا يثبت أن Render لم يُستخدم.
4. راقب `modal app logs waqqad-transcriber` لرؤية `[MODAL DISPATCH]` و`[MODAL WORKER]`.
5. تأكد من وصول TXT ثم نسختي التنقيح، ثم اختبر `/find`.
6. اختبر `/download/<job_id>` من صفحة الويب؛ يجب أن يعمل من قاعدة البيانات حتى لو كانت حاوية الويب مختلفة عن حاوية العامل.
7. لاختبار الطوارئ: أوقف تطبيق Modal مؤقتًا (`modal app stop waqqad-transcriber`) وأرسل مهمة. يجب أن يظهر `[RENDER FALLBACK]` في سجل Modal/Render وتعمل المهمة على Render. ثم أعد تشغيل Modal.

## 5. خطة التراجع

### 5.1 تراجع Git

العمل كله على فرع مستقل:

```bash
git checkout feat/modal-primary-render-fallback-2026-11-26
git log --oneline
```

للتراجع عن الالتزام كاملًا:

```bash
git revert <commit-sha>
```

أو حذف الملفات المضافة فقط:

```bash
rm -f modal_app.py docs/modal-deployment.md .github/workflows/deploy.yml
git checkout -- app.py .env.example README.md
```

### 5.2 تراجع Modal

```bash
modal app stop waqqad-transcriber
```

ثم أعد ويبهوك تيليجرام إلى Render:

```bash
curl -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" \
  -d "url=https://<render-service>.onrender.com/telegram/transcriber" \
  -d "secret_token=$TELEGRAM_WEBHOOK_SECRET"
```

Render يعمل بالكود الأساسي نفسه، ولا يحتاج تغييرًا في قاعدة البيانات.

### 5.3 تعطيل الاحتياطي فورًا

على Modal:

```
RENDER_FALLBACK_ENABLED=0
```

أو احذف `RENDER_FALLBACK_URL`. لن يُرسل أي شيء إلى Render بعدها، وستظهر رسالة فشل واضحة عند تعطل Modal.

## 6. الأمان والتنظيف

- **عزل الأخطاء**: كل دالة عامل تلتقط الاستثناء، تعلّم المهمة فاشلة إن لم تكن انتهت، وتعيد رفعه للوغ Modal. لا تُسقط مهامًا أخرى.
- **تنظيف إلزامي**: `finally` في `process_job` يستدعي `cleanup_job_workdir(job_id)`. كذلك `process_transcription_job` و`run_bot_job` و`run_url_job` تنظّف عند الفشل.
- **عدم تراكم الملفات**: حاوية الويب تحذف نسختها المحلية بعد نجاح تسليم المهمة إلى Modal/Render.
- **لا حلقات احتياطية**: نقطة `/internal/render-fallback` لا تستدعي Modal. وRender الاحتياطي عليه `WAQQAD_DISABLE_MODAL_DISPATCH=1`.
- **حماية النقطة**: `Authorization: Bearer <RENDER_FALLBACK_SECRET>` مع مقارنة ثابتة الزمن. بدون السر تعيد 503.
- **الأسرار**: لا تُكتب في الصورة أو `.env.example` أو Git. تُدار عبر Modal Secret وRender Environment وGitHub Secrets.
- **الملفات المؤقتة**: على Modal كل حاوية لها `/tmp` خاص؛ لا تُشارك المسارات بين الويب والعامل. الملفات الكبيرة تُنقل كبايتات مرة واحدة إلى العامل، أو ينزّلها العامل بنفسه من تيليجرام/الرابط.

## 7. ملاحظات وحدود

- رابط Modal يتغير بين البيئات/المساحات؛ حدّث `BASE_URL` بعد النشر.
- يوتيوب على Modal يحتاج أحيانًا كوكيز أو `YTDLP_PROXY` كما على Render. الصورة تتضمن `yt-dlp` من `requirements.txt` وffmpeg، لكنها لا تتضمن Deno/bgutil تلقائيًا. إن ظهرت صيغ «القصص» فقط، أرسل `cookies.txt` للبوت أو اضبط `YTDLP_PROXY`.
- حجم الحمولة إلى دالة Modal يشمل بايتات ملف الويب (حد الرفع 20MB افتراضيًا). للملفات الأكبر استخدم مسار الرابط أو ارفعها على تخزين خارجي.
- لا تفعّل أكثر من نسخة ويب واحدة بنفس `TELEGRAM_WEBHOOK_SECRET` على خدمتين في الوقت نفسه؛ Telegram يرسل إلى ويبهوك واحد. استخدم التحويل اليدوي عند الطوارئ.
