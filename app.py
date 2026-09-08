import os
import requests

from flask import Flask, jsonify, request

app = Flask(__name__)

DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY")
DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"


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

    try:

        audio_data = uploaded_file.read()

        response = requests.post(
            DEEPGRAM_URL,
            params={
                "model": "nova-3",
                "language": "ar",
                "smart_format": "true"
            },
            headers={
                "Authorization": f"Token {DEEPGRAM_API_KEY}",
                "Content-Type": (
                    uploaded_file.content_type
                    or "application/octet-stream"
                )
            },
            data=audio_data,
            timeout=1800
        )

        if response.status_code != 200:
            return jsonify({
                "error": "Deepgram request failed",
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

        return jsonify({
            "status": "success",
            "filename": uploaded_file.filename,
            "transcript": transcript
        })

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
