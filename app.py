import os
import uuid
import json
import time
import threading
import subprocess
from pathlib import Path

import requests
from flask import Flask, request, jsonify, send_file

app = Flask(__name__)

DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY")

CHUNK_MINUTES = 10
MAX_RETRIES = 5
REQUEST_TIMEOUT = 1800

jobs = {}
jobs_lock = threading.Lock()


# ============================================================
# JOB STATUS
# ============================================================

def update_job(job_id, **updates):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(updates)


# ============================================================
# RUN COMMAND
# ============================================================

def run_command(cmd):
    print(f"[CMD] {' '.join(cmd)}", flush=True)

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    if result.returncode != 0:
        print("[CMD ERROR]", flush=True)
        print(result.stderr, flush=True)
        raise RuntimeError(
            f"Command failed with exit code {result.returncode}"
        )

    return result.stdout.strip()


# ============================================================
# GET MEDIA DURATION
# ============================================================

def get_duration(file_path):
    output = run_command([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(file_path)
    ])

    return float(output)


# ============================================================
# SPLIT AUDIO
# ============================================================

def split_audio(input_file, chunks_dir):

    chunks_dir.mkdir(parents=True, exist_ok=True)

    print(
        f"[FFMPEG] Splitting into {CHUNK_MINUTES}-minute chunks...",
        flush=True
    )

    run_command([
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-i", str(input_file),
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-f", "segment",
        "-segment_time", str(CHUNK_MINUTES * 60),
        "-reset_timestamps", "1",
        str(chunks_dir / "chunk_%04d.flac")
    ])

    chunks = sorted(chunks_dir.glob("chunk_*.flac"))

    print(
        f"[FFMPEG] Created {len(chunks)} chunks.",
        flush=True
    )

    return chunks


# ============================================================
# DEEPGRAM
# ============================================================

def transcribe_chunk(chunk_path):

    url = "https://api.deepgram.com/v1/listen"

    params = {
        "model": "nova-3",
        "language": "ar",
        "smart_format": "true"
    }

    headers = {
        "Authorization": f"Token {DEEPGRAM_API_KEY}",
        "Content-Type": "audio/flac"
    }

    with open(chunk_path, "rb") as audio:

        response = requests.post(
            url,
            params=params,
            headers=headers,
            data=audio,
            timeout=REQUEST_TIMEOUT
        )

    print(
        f"[DEEPGRAM] HTTP {response.status_code}",
        flush=True
    )

    if response.status_code != 200:
        print(response.text[:1000], flush=True)

        raise RuntimeError(
            f"Deepgram HTTP {response.status_code}"
        )

    data = response.json()

    try:
        return data["results"]["channels"][0]["alternatives"][0]["transcript"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(
            "Unexpected Deepgram response format"
        )


# ============================================================
# PROCESS JOB
# ============================================================

def process_job(job_id, input_file):

    work_dir = input_file.parent.parent

    chunks_dir = work_dir / "chunks"
    texts_dir = work_dir / "texts"

    texts_dir.mkdir(parents=True, exist_ok=True)

    try:

        update_job(
            job_id,
            status="processing",
            stage="duration"
        )

        duration = get_duration(input_file)

        print(
            f"[MEDIA] Duration: {duration / 60:.2f} minutes",
            flush=True
        )

        update_job(
            job_id,
            duration_minutes=round(duration / 60, 2),
            stage="splitting"
        )

        chunks = split_audio(
            input_file,
            chunks_dir
        )

        total_chunks = len(chunks)

        update_job(
            job_id,
            total_chunks=total_chunks,
            completed_chunks=0,
            stage="transcription"
        )

        transcripts = []

        # ----------------------------------------------------
        # TRANSCRIBE CHUNKS
        # ----------------------------------------------------

        for index, chunk in enumerate(chunks, start=1):

            text_file = texts_dir / f"chunk_{index:04d}.txt"

            update_job(
                job_id,
                current_chunk=index
            )

            print(
                f"[DEEPGRAM] Chunk {index}/{total_chunks}",
                flush=True
            )

            # Checkpoint
            if text_file.exists():

                print(
                    f"[CHECKPOINT] Chunk {index}/{total_chunks} "
                    f"already exists. Skipping.",
                    flush=True
                )

                text = text_file.read_text(
                    encoding="utf-8"
                )

            else:

                text = None

                for attempt in range(1, MAX_RETRIES + 1):

                    try:

                        print(
                            f"[DEEPGRAM] Attempt "
                            f"{attempt}/{MAX_RETRIES}",
                            flush=True
                        )

                        text = transcribe_chunk(chunk)

                        break

                    except Exception as e:

                        print(
                            f"[DEEPGRAM ERROR] {e}",
                            flush=True
                        )

                        if attempt == MAX_RETRIES:
                            raise

                        wait_time = 2 ** attempt

                        print(
                            f"[RETRY] Waiting "
                            f"{wait_time}s...",
                            flush=True
                        )

                        time.sleep(wait_time)

                text_file.write_text(
                    text,
                    encoding="utf-8"
                )

                print(
                    f"[CHECKPOINT] Chunk "
                    f"{index}/{total_chunks} saved.",
                    flush=True
                )

            transcripts.append(text)

            update_job(
                job_id,
                completed_chunks=index
            )

        # ----------------------------------------------------
        # MERGE
        # ----------------------------------------------------

        update_job(
            job_id,
            stage="merging"
        )

        print(
            "[MERGE] Combining transcripts...",
            flush=True
        )

        final_text = "\n\n".join(transcripts)

        output_file = work_dir / "transcription.txt"

        output_file.write_text(
            final_text,
            encoding="utf-8"
        )

        # ----------------------------------------------------
        # DONE
        # ----------------------------------------------------

        update_job(
            job_id,
            status="completed",
            stage="done",
            output_file=str(output_file)
        )

        print("========================================", flush=True)
        print("[DONE] Transcription completed.", flush=True)
        print(f"[DONE] Job: {job_id}", flush=True)
        print(f"[DONE] Chunks: {total_chunks}", flush=True)
        print("========================================", flush=True)

    except Exception as e:

        print("========================================", flush=True)
        print("[ERROR] Job failed.", flush=True)
        print(f"[ERROR] Job: {job_id}", flush=True)
        print(f"[ERROR] {e}", flush=True)
        print("========================================", flush=True)

        update_job(
            job_id,
            status="failed",
            stage="error",
            error=str(e)
        )


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():
    return jsonify({
        "service": "Waqqad Transcriber",
        "status": "online"
    })


# ============================================================
# HEALTH
# ============================================================

@app.route("/health")
def health():
    return jsonify({
        "status": "ok"
    })


# ============================================================
# START TRANSCRIPTION JOB
# ============================================================

@app.route("/transcribe", methods=["POST"])
def transcribe():

    print("========================================", flush=True)
    print("[TRANSCRIBE] New request", flush=True)
    print("========================================", flush=True)

    if "file" not in request.files:
        return jsonify({
            "error": "No file provided"
        }), 400

    uploaded_file = request.files["file"]

    if not uploaded_file.filename:
        return jsonify({
            "error": "Empty filename"
        }), 400

    # --------------------------------------------------------
    # JOB ID
    # --------------------------------------------------------

    job_id = str(uuid.uuid4())

    work_dir = Path("/tmp") / f"waqqad_{job_id}"

    original_dir = work_dir / "original"

    original_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    input_file = original_dir / uploaded_file.filename

    # --------------------------------------------------------
    # SAVE UPLOAD
    # --------------------------------------------------------

    print(
        f"[UPLOAD] Saving: {uploaded_file.filename}",
        flush=True
    )

    uploaded_file.save(input_file)

    file_size = input_file.stat().st_size

    print(
        f"[UPLOAD] Size: "
        f"{file_size / (1024 * 1024):.2f} MB",
        flush=True
    )

    # --------------------------------------------------------
    # REGISTER JOB
    # --------------------------------------------------------

    with jobs_lock:

        jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "stage": "queued",
            "current_chunk": 0,
            "completed_chunks": 0,
            "total_chunks": 0,
            "duration_minutes": None,
            "error": None,
            "output_file": None
        }

    # --------------------------------------------------------
    # BACKGROUND THREAD
    # --------------------------------------------------------

    thread = threading.Thread(
        target=process_job,
        args=(job_id, input_file),
        daemon=True
    )

    thread.start()

    # --------------------------------------------------------
    # RESPOND IMMEDIATELY
    # --------------------------------------------------------

    return jsonify({
        "job_id": job_id,
        "status": "queued",
        "message": "Transcription job started."
    }), 202


# ============================================================
# JOB STATUS
# ============================================================

@app.route("/status/<job_id>", methods=["GET"])
def job_status(job_id):

    with jobs_lock:
        job = jobs.get(job_id)

    if job is None:
        return jsonify({
            "error": "Job not found"
        }), 404

    return jsonify(job)


# ============================================================
# DOWNLOAD RESULT
# ============================================================

@app.route("/download/<job_id>", methods=["GET"])
def download_result(job_id):

    with jobs_lock:
        job = jobs.get(job_id)

    if job is None:
        return jsonify({
            "error": "Job not found"
        }), 404

    if job["status"] != "completed":
        return jsonify({
            "error": "Job is not completed yet",
            "status": job["status"]
        }), 409

    output_file = job.get("output_file")

    if not output_file or not os.path.exists(output_file):
        return jsonify({
            "error": "Output file not found"
        }), 404

    return send_file(
        output_file,
        as_attachment=True,
        download_name="transcription.txt",
        mimetype="text/plain"
    )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=10000
    )
