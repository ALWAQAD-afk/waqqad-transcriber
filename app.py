import os
import subprocess
import tempfile
import uuid
import time

import requests
from flask import Flask, jsonify, request, send_file

app = Flask(__name__)

DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY")
DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"

CHUNK_MINUTES = 10
MAX_RETRIES = 5


@app.route("/")
def home():
    return "Waqqad Transcriber is alive! 🚀"


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "waqqad-transcriber"
    })


@app.route("/transcribe", methods=["POST"])
def transcribe():

    if not DEEPGRAM_API_KEY:
        return jsonify({
            "error": "DEEPGRAM_API_KEY is not configured"
        }), 500

    if "file" not in request.files:
        return jsonify({
            "error": "No file uploaded. Use field name: file"
        }), 400

    uploaded_file = request.files["file"]

    if not uploaded_file.filename:
        return jsonify({
            "error": "Empty filename"
        }), 400

    job_id = uuid.uuid4().hex

    work_dir = os.path.join(
        tempfile.gettempdir(),
        f"waqqad_{job_id}"
    )

    chunks_dir = os.path.join(work_dir, "chunks")
    texts_dir = os.path.join(work_dir, "texts")

    os.makedirs(chunks_dir)
    os.makedirs(texts_dir)

    try:

        # ==================================================
        # 1. حفظ الملف
        # ==================================================

        original_name = uploaded_file.filename

        input_path = os.path.join(
            work_dir,
            original_name
        )

        uploaded_file.save(input_path)

        # ==================================================
        # 2. تقسيم الملف بواسطة FFmpeg
        # ==================================================

        output_pattern = os.path.join(
            chunks_dir,
            "chunk_%04d.flac"
        )

        ffmpeg_command = [
            "ffmpeg",
            "-y",
            "-i",
            input_path,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "flac",
            "-f",
            "segment",
            "-segment_time",
            str(CHUNK_MINUTES * 60),
            output_pattern
        ]

        process = subprocess.run(
            ffmpeg_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        if process.returncode != 0:
            return jsonify({
                "error": "FFmpeg failed",
                "details": process.stderr[-4000:]
            }), 500

        chunks = sorted(
            os.path.join(chunks_dir, filename)
            for filename in os.listdir(chunks_dir)
            if filename.endswith(".flac")
        )

        if not chunks:
            return jsonify({
                "error": "FFmpeg produced no chunks"
            }), 500

        # ==================================================
        # 3. تفريغ الأجزاء مع Retry + Checkpoint
        # ==================================================

        transcripts = []

        for index, chunk_path in enumerate(chunks):

            text_path = os.path.join(
                texts_dir,
                f"chunk_{index:04d}.txt"
            )

            # ----------------------------------------------
            # إذا كان النص موجودًا من محاولة سابقة:
            # استخدمه مباشرة
            # ----------------------------------------------

            if os.path.exists(text_path):

                with open(
                    text_path,
                    "r",
                    encoding="utf-8"
                ) as text_file:

                    transcript = text_file.read()

                transcripts.append(transcript)

                continue

            # ----------------------------------------------
            # محاولة التفريغ
            # ----------------------------------------------

            success = False
            transcript = ""

            for attempt in range(1, MAX_RETRIES + 1):

                try:

                    with open(
                        chunk_path,
                        "rb"
                    ) as audio_file:

                        response = requests.post(
                            DEEPGRAM_URL,
                            params={
                                "model": "nova-3",
                                "language": "ar",
                                "smart_format": "true"
                            },
                            headers={
                                "Authorization":
                                    f"Token {DEEPGRAM_API_KEY}",
                                "Content-Type":
                                    "audio/flac"
                            },
                            data=audio_file,
                            timeout=1800
                        )

                    if response.status_code == 200:

                        result = response.json()

                        transcript = (
                            result
                            .get("results", {})
                            .get("channels", [{}])[0]
                            .get("alternatives", [{}])[0]
                            .get("transcript", "")
                        )

                        # ----------------------------------
                        # حفظ النتيجة فور نجاحها
                        # ----------------------------------

                        with open(
                            text_path,
                            "w",
                            encoding="utf-8"
                        ) as text_file:

                            text_file.write(transcript)

                        success = True

                        break

                    else:

                        print(
                            f"Chunk {index + 1}: "
                            f"attempt {attempt}/{MAX_RETRIES} "
                            f"failed: HTTP {response.status_code}"
                        )

                except requests.RequestException as e:

                    print(
                        f"Chunk {index + 1}: "
                        f"attempt {attempt}/{MAX_RETRIES} "
                        f"network error: {e}"
                    )

                # ------------------------------------------
                # انتظار تصاعدي بين المحاولات
                # 2s → 4s → 8s → 16s
                # ------------------------------------------

                if attempt < MAX_RETRIES:

                    time.sleep(2 ** attempt)

            # ----------------------------------------------
            # فشل الجزء بعد جميع المحاولات
            # ----------------------------------------------

            if not success:

                return jsonify({
                    "error": "Failed to transcribe chunk",
                    "chunk": index + 1,
                    "total_chunks": len(chunks),
                    "message":
                        f"Chunk failed after {MAX_RETRIES} attempts"
                }), 502

            transcripts.append(transcript)

        # ==================================================
        # 4. دمج النصوص
        # ==================================================

        output_txt = os.path.join(
            work_dir,
            os.path.splitext(original_name)[0] + ".txt"
        )

        with open(
            output_txt,
            "w",
            encoding="utf-8"
        ) as final_file:

            for transcript in transcripts:

                if transcript.strip():

                    final_file.write(
                        transcript.strip()
                    )

                    final_file.write("\n\n")

        # ==================================================
        # 5. إرسال TXT
        # ==================================================

        return send_file(
            output_txt,
            as_attachment=True,
            download_name=os.path.basename(output_txt),
            mimetype="text/plain; charset=utf-8"
        )

    except Exception as e:

        return jsonify({
            "error": "Unexpected server error",
            "details": str(e)
        }), 500


if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            10000
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
