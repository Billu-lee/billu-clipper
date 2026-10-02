from flask import Flask, render_template, request, jsonify
import yt_dlp
import os

from faster_whisper import WhisperModel


app = Flask(__name__)

# -----------------------------
# FOLDERS
# -----------------------------

DOWNLOAD_FOLDER = "downloads"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)


# -----------------------------
# LOAD WHISPER AI MODEL
# -----------------------------

whisper_model = WhisperModel(
    "small",
    device="cpu",
    compute_type="int8"
)


# -----------------------------
# TRANSCRIPTION FUNCTION
# -----------------------------

def transcribe_video(video_path):

    segments, info = whisper_model.transcribe(
        video_path,
        beam_size=5
    )

    transcript = []

    for segment in segments:

        transcript.append({
            "start": round(segment.start, 2),
            "end": round(segment.end, 2),
            "text": segment.text.strip()
        })

    return transcript


# -----------------------------
# HOME PAGE
# -----------------------------

@app.route("/")
def home():
    return render_template("index.html")


# -----------------------------
# GENERATE SHORTS
# -----------------------------

@app.route("/generate", methods=["POST"])
def generate():

    data = request.get_json()
    url = data.get("url", "").strip()

    if not url:
        return jsonify({
            "error": "Please enter a YouTube URL"
        }), 400

    try:

        # -------------------------
        # DOWNLOAD YOUTUBE VIDEO
        # -------------------------

        ydl_opts = {
            "format": "bv*+ba/b",
            "outtmpl": "downloads/%(id)s.%(ext)s",
            "merge_output_format": "mp4",
            "quiet": False,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:

            info = ydl.extract_info(
                url,
                download=True
            )

        video_id = info.get("id")

        video_path = os.path.join(
            DOWNLOAD_FOLDER,
            f"{video_id}.mp4"
        )

        print("Video downloaded:")
        print(video_path)


        # -------------------------
        # TRANSCRIBE WITH WHISPER
        # -------------------------

        print("Starting transcription...")

        transcript = transcribe_video(
            video_path
        )

        print("Transcription finished!")

        # Print transcript in terminal
        for segment in transcript:
            print(
                segment["start"],
                "-",
                segment["end"],
                segment["text"]
            )


        # -------------------------
        # SEND RESULT TO BROWSER
        # -------------------------

        return jsonify({
            "success": True,
            "title": info.get("title"),
            "video_id": video_id,
            "transcript": transcript,
            "message":
                "Video downloaded and transcribed successfully"
        })


    except Exception as e:

        print("ERROR:", e)

        return jsonify({
            "error": str(e)
        }), 500


# -----------------------------
# START SERVER
# -----------------------------

if __name__ == "__main__":
    app.run(debug=True)