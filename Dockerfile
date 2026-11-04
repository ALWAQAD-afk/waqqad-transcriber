FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg unzip curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# بيئة JS (Deno) يطلبها yt-dlp الحديث لتفريغ يوتيوب. تركيب «بأفضل جهد»:
# المصدر الأول إصدار GitHub الأحدث؛ ثم سكربت deno.land الرسمي.
# إن فشلا معًا يبقى البناء ناجحًا لكن يوتيوب قد تعطي صيغًا ناقصة.
RUN (python -c "import urllib.request, zipfile, io; data = urllib.request.urlopen('https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip', timeout=120).read(); zipfile.ZipFile(io.BytesIO(data)).extract('deno', '/usr/local/bin')" \
        && chmod +x /usr/local/bin/deno) \
    || (curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s -- -y --no-modify-path) \
    || echo "WARN: deno install failed — YouTube may return no-audio formats" \
    && deno --version || echo "[WARN] deno NOT available at build end"

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home app

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app . .

USER app

EXPOSE 10000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:10000/health', timeout=4)"

CMD ["gunicorn", "--timeout", "7200", "--workers", "1", "--worker-tmp-dir", "/tmp", "--bind", "0.0.0.0:10000", "app:app"]
