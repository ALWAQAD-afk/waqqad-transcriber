import os
import uuid
import time
import shutil
import subprocess
import requests

from flask import Flask, request, send_file, jsonify

app = Flask(__name__)

DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY")

CHUNK_MINUTES = 10
MAX_RETRIES = 5
REQUEST_TIMEOUT = 1800


@app.route("/")
def home():
    return "Waqqad Transcriber is alive."


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "deepgram_key": bool(DEEPGRAM_API_KEY)
    })


@app.route("/transcribe", methods=["POST"])
def transcribe():

    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    if not DEEPGRAM_API_KEY:
        return jsonify({"error": "DEEPGRAM_API_KEY is not configured"}), 500

    uploaded_file = request.files["file"]

    if not uploaded_file.filename:
        return jsonify({"error": "Empty filename"}), 400

    job_id = str(uuid.uuid4())
    work_dir = f"/tmp/waqqad_{job_id}"

    original_dir = os.path.join(work_dir, "original")
    chunks_dir = os.path.join(work_dir, "chunks")
    texts_dir = os.path.join(work_dir, "texts")

    os.makedirs(original_dir, exist_ok=True)
    os.makedirs(chunks_dir, exist_ok=True)
    os.makedirs(texts_dir, exist_ok=True)

    original_file = os.path.join(
        original_dir,
        uploaded_file.filename
    )

    try:

        # --------------------------------------------------
        # 1. Save uploaded file
        # --------------------------------------------------

        print(f"[1/5] Saving upload: {uploaded_file.filename}")

        uploaded_file.save(original_file)

        file_size = os.path.getsize(original_file)

        print(
            f"[UPLOAD] Saved {file_size / (1024 * 1024):.2f} MB"
        )

        # --------------------------------------------------
        # 2. Get duration
        # --------------------------------------------------

        print("[2/5] Detecting duration...")

        duration_cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            original_file
        ]

        duration_result = subprocess.run(
            duration_cmd,
            capture_output=True,
            text=True,
            timeout=300
        )

        if duration_result.returncode != 0:
            return jsonify({
                "error": "Could not detect audio duration",
                "details": duration_result.stderr
            }), 500

        duration = float(duration_result.stdout.strip())

        print(
            f"[DURATION] {duration / 3600:.2f} hours "
            f"({duration / 60:.1f} minutes)"
        )

        # --------------------------------------------------
        # 3. Split into 10-minute FLAC chunks
        # --------------------------------------------------

        print("[3/5] Splitting audio...")

        chunk_pattern = os.path.join(
            chunks_dir,
            "chunk_%04d.flac"
        )

        ffmpeg_cmd = [
            "ffmpeg",
            "-y",
            "-i",
            original_file,
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
            "-reset_timestamps",
            "1",
            chunk_pattern
        ]

        ffmpeg_result = subprocess.run(
            ffmpeg_cmd,
            capture_output=True,
            text=True,
            timeout=7200
        )

        if ffmpeg_result.returncode != 0:
            return jsonify({
                "error": "FFmpeg failed",
                "details": ffmpeg_result.stderr[-4000:]
            }), 500

        chunks = sorted(
            os.path.join(chunks_dir, f)
            for f in os.listdir(chunks_dir)
            if f.endswith(".flac")
        )

        if not chunks:
            return jsonify({
                "error": "No audio chunks were created"
            }), 500

        print(
            f"[CHUNKS] Created {len(chunks)} chunks"
        )

        # --------------------------------------------------
        # 4. Transcribe chunks
        # --------------------------------------------------

        print("[4/5] Starting Deepgram transcription...")

        headers = {
            "Authorization": f"Token {DEEPGRAM_API_KEY}",
            "Content-Type": "audio/flac"
        }

        texts = []

        for index, chunk_path in enumerate(chunks):

            chunk_number = index + 1

            checkpoint = os.path.join(
                texts_dir,
                f"chunk_{chunk_number:04d}.txt"
            )

            # ----------------------------------------------
            # Checkpoint
            # ----------------------------------------------

            if os.path.exists(checkpoint):

                print(
                    f"[{chunk_number}/{len(chunks)}] "
                    f"Checkpoint found — skipping"
                )

                with open(
                    checkpoint,
                    "r",
                    encoding="utf-8"
                ) as f:
                    text = f.read()

                texts.append(text)

                continue

            print(
                f"[{chunk_number}/{len(chunks)}] "
                f"Sending to Deepgram..."
            )

            success = False
            text = ""

            for attempt in range(1, MAX_RETRIES + 1):

                try:

                    start_time = time.time()

                    with open(
                        chunk_path,
                        "rb"
                    ) as audio:

                        response = requests.post(
                            "https://api.deepgram.com/v1/listen",
                            headers=headers,
                            params={
                                "model": "nova-3",
                                "language": "ar",
                                "smart_format": "true"
                            },
                            data=audio,
                            timeout=REQUEST_TIMEOUT
                        )

                    elapsed = time.time() - start_time

                    print(
                        f"[{chunk_number}] "
                        f"Attempt {attempt}: "
                        f"HTTP {response.status_code} "
                        f"in {elapsed:.1f}s"
                    )

                    if response.status_code == 200:

                        data = response.json()

                        text = (
                            data
                            .get("results", {})
                            .get("channels", [{}])[0]
                            .get("alternatives", [{}])[0]
                            .get("transcript", "")
                        )

                        with open(
                            checkpoint,
                            "w",
                            encoding="utf-8"
                        ) as f:
                            f.write(text)

                        print(
                            f"[{chunk_number}] "
                            f"Saved checkpoint."
                        )

                        success = True
                        break

                    else:

                        print(
                            f"[{chunk_number}] "
                            f"Deepgram error: "
                            f"{response.text[:1000]}"
                        )

                except Exception as e:

                    print(
                        f"[{chunk_number}] "
                        f"Attempt {attempt} failed: {e}"
                    )

                if attempt < MAX_RETRIES:

                    wait_time = 2 ** attempt

                    print(
                        f"[{chunk_number}] "
                        f"Retrying in {wait_time}s..."
                    )

                    time.sleep(wait_time)

            if not success:

                print(
                    f"[ERROR] Chunk {chunk_number} "
                    f"failed permanently."
                )

                return jsonify({
                    "error": "Transcription failed",
                    "chunk": chunk_number,
                    "total_chunks": len(chunks)
                }), 500

            texts.append(text)

        # --------------------------------------------------
        # 5. Merge
        # --------------------------------------------------

        print("[5/5] Merging transcript...")

        output_file = os.path.join(
            work_dir,
            "result.txt"
        )

        with open(
            output_file,
            "w",
            encoding="utf-8"
        ) as output:

            for text in texts:

                output.write(text.strip())
                output.write("\n\n")

        print("[DONE] Transcription completed.")

        return send_file(
            output_file,
            as_attachment=True,
            download_name="result.txt",
            mimetype="text/plain; charset=utf-8"
        )

    except subprocess.TimeoutExpired:

        return jsonify({
            "error": "Processing timeout"
        }), 504

    except Exception as e:

        print(f"[FATAL] {e}")

        return jsonify({
            "error": "Internal server error",
            "details": str(e)
        }), 500
    
