import os
import subprocess
import tempfile
import uuid

import requests
from flask import Flask, jsonify, request, send_file

app = Flask(__name__)

DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY")
DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"

CHUNK_MINUTES = 10


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
    work_dir = os.path.join(tempfile.gettempdir(), f"waqqad_{job_id}")
    chunks_dir = os.path.join(work_dir, "chunks")
    texts_dir = os.path.join(work_dir, "texts")

    os.makedirs(chunks_dir)
    os.makedirs(texts_dir)

    try:
        # --------------------------------------------------
        # 1. Save uploaded file
        # --------------------------------------------------

        original_name = uploaded_file.filename
        input_path = os.path.join(work_dir, original_name)

        uploaded_file.save(input_path)

        # --------------------------------------------------
        # 2. Split audio into 10-minute FLAC chunks
        # --------------------------------------------------

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

        # --------------------------------------------------
        # 3. Transcribe every chunk with Deepgram
        # --------------------------------------------------

        transcripts = []

        for index, chunk_path in enumerate(chunks):

            with open(chunk_path, "rb") as audio_file:
                audio_data = audio_file.read()

            response = requests.post(
                DEEPGRAM_URL,
                params={
                    "model": "nova-3",
                    "language": "ar",
                    "smart_format": "true"
                },
                headers={
                    "Authorization": f"Token {DEEPGRAM_API_KEY}",
                    "Content-Type": "audio/flac"
                },
                data=audio_data,
                timeout=1800
            )

            if response.status_code != 200:
                return jsonify({
                    "error": "Deepgram request failed",
                    "chunk": index + 1,
                    "status_code": response.status_code,
                    "details": response.text
                }), 502

            result = response.json()

            transcript = (
                result
                .get("results", {})
                .get("channels", [{}])[0]
                .get("alternatives", [{}])[0]
                .get("transcript", "")
            )

            transcripts.append(transcript)

            # Save each chunk transcript
            text_path = os.path.join(
                texts_dir,
                f"chunk_{index:04d}.txt"
            )

            with open(text_path, "w", encoding="utf-8") as text_file:
                text_file.write(transcript)

        # --------------------------------------------------
        # 4. Merge transcripts
        # --------------------------------------------------

        output_txt = os.path.join(
            work_dir,
            os.path.splitext(original_name)[0] + ".txt"
        )

        with open(output_txt, "w", encoding="utf-8") as final_file:

            for transcript in transcripts:

                if transcript.strip():
                    final_file.write(transcript.strip())
                    final_file.write("\n\n")

        # --------------------------------------------------
        # 5. Return TXT file
        # --------------------------------------------------

        return send_file(
            output_txt,
            as_attachment=True,
            download_name=os.path.basename(output_txt),
            mimetype="text/plain; charset=utf-8"
        )

    except requests.RequestException as e:

        return jsonify({
            "error": "Network error while contacting Deepgram",
            "details": str(e)
        }), 502

    except Exception as e:

        return jsonify({
            "error": "Unexpected server error",
            "details": str(e)
        }), 500


if __name__ == "__main__":

    port = int(os.environ.get("PORT", 10000))

    app.run(
        host="0.0.0.0",
        port=port
    )
