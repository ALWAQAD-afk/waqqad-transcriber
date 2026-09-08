import os
import uuid
import time
import shutil
import subprocess
import requests

from flask import Flask, request, jsonify, send_file

app = Flask(__name__)

# ============================================================
# SETTINGS
# ============================================================

CHUNK_MINUTES = 10
MAX_RETRIES = 5

DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"
DEEPGRAM_MODEL = "nova-3"
DEEPGRAM_LANGUAGE = "ar"

# لا نضع API Key داخل الكود
DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY")


# ============================================================
# BASIC ROUTES
# ============================================================

@app.route("/")
def home():
    return "Waqqad Transcriber is alive."


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "waqqad-transcriber"
    })


# ============================================================
# HELPERS
# ============================================================

def run_command(command):
    """
    Run a system command and raise an error if it fails.
    """

    print("\n[CMD]", " ".join(command), flush=True)

    process = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    if process.returncode != 0:
        print(process.stderr, flush=True)

        raise RuntimeError(
            f"Command failed with exit code {process.returncode}"
        )

    return process


def get_duration(file_path):
    """
    Get media duration using ffprobe.
    """

    command = [
        "ffprobe",
        "-v", "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        file_path
    ]

    result = run_command(command)

    return float(result.stdout.strip())


def split_audio(input_file, chunks_dir):
    """
    Split audio into 10-minute mono 16k FLAC chunks.
    """

    os.makedirs(chunks_dir, exist_ok=True)

    output_pattern = os.path.join(
        chunks_dir,
        "chunk_%04d.flac"
    )

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",

        "-i",
        input_file,

        "-vn",

        "-ac", "1",
        "-ar", "16000",

        "-f", "segment",
        "-segment_time", str(CHUNK_MINUTES * 60),
        "-reset_timestamps", "1",

        output_pattern
    ]

    run_command(command)

    chunks = sorted(
        os.path.join(chunks_dir, f)
        for f in os.listdir(chunks_dir)
        if f.endswith(".flac")
    )

    if not chunks:
        raise RuntimeError("FFmpeg created no chunks.")

    return chunks


# ============================================================
# DEEPGRAM
# ============================================================

def transcribe_chunk(chunk_path, index, total):
    """
    Send one audio chunk to Deepgram with retries.
    """

    if not DEEPGRAM_API_KEY:
        raise RuntimeError(
            "DEEPGRAM_API_KEY environment variable is missing."
        )

    print(
        f"\n[DEEPGRAM] Chunk {index}/{total}",
        flush=True
    )

    with open(chunk_path, "rb") as audio_file:
        audio_data = audio_file.read()

    headers = {
        "Authorization": f"Token {DEEPGRAM_API_KEY}",
        "Content-Type": "audio/flac"
    }

    params = {
        "model": DEEPGRAM_MODEL,
        "language": DEEPGRAM_LANGUAGE,
        "smart_format": "true"
    }

    for attempt in range(1, MAX_RETRIES + 1):

        try:

            print(
                f"[DEEPGRAM] Attempt {attempt}/{MAX_RETRIES}",
                flush=True
            )

            response = requests.post(
                DEEPGRAM_URL,
                headers=headers,
                params=params,
                data=audio_data,
                timeout=1800
            )

            print(
                f"[DEEPGRAM] HTTP {response.status_code}",
                flush=True
            )

            if response.status_code == 200:

                data = response.json()

                transcript = (
                    data
                    .get("results", {})
                    .get("channels", [{}])[0]
                    .get("alternatives", [{}])[0]
                    .get("transcript", "")
                )

                return transcript.strip()

            print(
                response.text[:1000],
                flush=True
            )

        except Exception as e:

            print(
                f"[DEEPGRAM] Error: {e}",
                flush=True
            )

        if attempt < MAX_RETRIES:

            wait_time = 2 ** attempt

            print(
                f"[DEEPGRAM] Waiting {wait_time}s...",
                flush=True
            )

            time.sleep(wait_time)

    raise RuntimeError(
        f"Deepgram failed after {MAX_RETRIES} attempts."
    )


# ============================================================
# TRANSCRIBE
# ============================================================

@app.route("/transcribe", methods=["POST"])
def transcribe():

    print("\n========================================", flush=True)
    print("[TRANSCRIBE] New request", flush=True)
    print("========================================", flush=True)

    # --------------------------------------------------------
    # Check upload
    # --------------------------------------------------------

    if "file" not in request.files:

        return jsonify({
            "error": "No file uploaded."
        }), 400

    uploaded_file = request.files["file"]

    if uploaded_file.filename == "":

        return jsonify({
            "error": "Empty filename."
        }), 400

    # --------------------------------------------------------
    # Create job directory
    # --------------------------------------------------------

    job_id = str(uuid.uuid4())

    work_dir = os.path.join(
        "/tmp",
        f"waqqad_{job_id}"
    )

    os.makedirs(work_dir, exist_ok=True)

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

        # ----------------------------------------------------
        # Save uploaded file
        # ----------------------------------------------------

        print(
            f"[UPLOAD] Saving: {uploaded_file.filename}",
            flush=True
        )

        uploaded_file.save(original_file)

        file_size = os.path.getsize(original_file)

        print(
            f"[UPLOAD] Size: {file_size / (1024 * 1024):.2f} MB",
            flush=True
        )

        # ----------------------------------------------------
        # Duration
        # ----------------------------------------------------

        duration = get_duration(original_file)

        print(
            f"[MEDIA] Duration: {duration / 60:.2f} minutes",
            flush=True
        )

        # ----------------------------------------------------
        # Split
        # ----------------------------------------------------

        print(
            f"[FFMPEG] Splitting into {CHUNK_MINUTES}-minute chunks...",
            flush=True
        )

        chunks = split_audio(
            original_file,
            chunks_dir
        )

        total_chunks = len(chunks)

        print(
            f"[FFMPEG] Created {total_chunks} chunks.",
            flush=True
        )

        # ----------------------------------------------------
        # Transcribe chunks
        # ----------------------------------------------------

        for index, chunk_path in enumerate(chunks, start=1):

            checkpoint_file = os.path.join(
                texts_dir,
                f"chunk_{index:04d}.txt"
            )

            # -----------------------------------------------
            # Checkpoint
            # -----------------------------------------------

            if os.path.exists(checkpoint_file):

                print(
                    f"[CHECKPOINT] Chunk {index}/{total_chunks} "
                    f"already exists. Skipping.",
                    flush=True
                )

                continue

            # -----------------------------------------------
            # Transcribe
            # -----------------------------------------------

            transcript = transcribe_chunk(
                chunk_path,
                index,
                total_chunks
            )

            # -----------------------------------------------
            # Save checkpoint
            # -----------------------------------------------

            with open(
                checkpoint_file,
                "w",
                encoding="utf-8"
            ) as text_file:

                text_file.write(transcript)

            print(
                f"[CHECKPOINT] Chunk {index}/{total_chunks} saved.",
                flush=True
            )

        # ----------------------------------------------------
        # Merge
        # ----------------------------------------------------

        print(
            "\n[MERGE] Combining transcripts...",
            flush=True
        )

        final_text = []

        for index in range(1, total_chunks + 1):

            checkpoint_file = os.path.join(
                texts_dir,
                f"chunk_{index:04d}.txt"
            )

            if not os.path.exists(checkpoint_file):

                raise RuntimeError(
                    f"Missing checkpoint for chunk {index}."
                )

            with open(
                checkpoint_file,
                "r",
                encoding="utf-8"
            ) as text_file:

                text = text_file.read().strip()

            if text:
                final_text.append(text)

        merged_text = "\n\n".join(final_text)

        # ----------------------------------------------------
        # Save result
        # ----------------------------------------------------

        result_file = os.path.join(
            work_dir,
            "result.txt"
        )

        with open(
            result_file,
            "w",
            encoding="utf-8"
        ) as text_file:

            text_file.write(merged_text)

        print(
            "\n========================================",
            flush=True
        )

        print(
            "[DONE] Transcription completed.",
            flush=True
        )

        print(
            f"[DONE] Job: {job_id}",
            flush=True
        )

        print(
            f"[DONE] Chunks: {total_chunks}",
            flush=True
        )

        print(
            "========================================\n",
            flush=True
        )

        # ----------------------------------------------------
        # Return TXT
        # ----------------------------------------------------

        response = send_file(
            result_file,
            as_attachment=True,
            download_name="transcription.txt",
            mimetype="text/plain; charset=utf-8"
        )

        # Delete after response closes
        @response.call_on_close
        def cleanup():

            try:
                shutil.rmtree(work_dir)

                print(
                    f"[CLEANUP] Removed {work_dir}",
                    flush=True
                )

            except Exception as e:

                print(
                    f"[CLEANUP] Failed: {e}",
                    flush=True
                )

        return response

    except Exception as e:

        print(
            "\n[ERROR]",
            str(e),
            flush=True
        )

        # Keep the work directory on failure.
        # This allows us to inspect checkpoints/logs
        # before the next test.

        return jsonify({
            "error": str(e),
            "job_id": job_id
        }), 500


# ============================================================
# MAIN
# ============================================================

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
