# مزوّد توكن إثبات الأصل (PO Token) ليوتيوب: صورة رسمية جاهزة تحوي
# Deno + وحدات npm المطلوبة. ننسخ منها وقت البناء بدل بناء canvas محليًا
# (بناء ثقيل ويحتاج مكتبات نظام كثيرة). هذا ما يمنح يوتيوب صيغ الصوت
# من عنوان الخادم بدل صيغ «القصص» فقط.
FROM brainicism/bgutil-ytdlp-pot-provider:2.0.2-deno AS bgutil

FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg unzip curl ca-certificates \
        libcairo2 libpango-1.0-0 libjpeg62-turbo libgif7 librsvg2-2 libpixman-1-0 \
    && rm -rf /var/lib/apt/lists/*

# Deno من صورة المزوّد: نسخة مضمونة التوافق (2.9.5) وتصلح أيضًا
# كبيئة JS التي يطلبها yt-dlp الحديث.
COPY --from=bgutil /usr/bin/deno /usr/local/bin/deno
RUN chmod +x /usr/local/bin/deno && deno --version

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home app

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# سكربت التوليد ووحداته الجاهزة.
# يعمل بالوضع «script»: يستدعيه yt-dlp عند الحاجة فقط.
COPY --from=bgutil /app/src /opt/bgutil/server/src
COPY --from=bgutil /app/node_modules /opt/bgutil/server/node_modules
COPY --from=bgutil /app/package.json /opt/bgutil/server/package.json
COPY --from=bgutil /app/deno.json /opt/bgutil/server/deno.json
RUN mkdir -p /home/app/.cache \
    && chown -R app:app /opt/bgutil /home/app/.cache

COPY --chown=app:app . .

USER app

EXPOSE 10000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:10000/health', timeout=4)"

CMD ["gunicorn", "--timeout", "7200", "--workers", "1", "--worker-tmp-dir", "/tmp", "--bind", "0.0.0.0:10000", "app:app"]
