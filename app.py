from flask import Flask, render_template, request, jsonify
from faster_whisper import WhisperModel
from google import genai
from dotenv import load_dotenv

import yt_dlp
import os
import json
import time


# ============================================================
# CONFIGURATION
# ============================================================

load_dotenv(".env")

app = Flask(__name__)

DOWNLOAD_FOLDER = "downloads"
CLIPS_FOLDER = "clips"

os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
os.makedirs(CLIPS_FOLDER, exist_ok=True)


# ============================================================
# GEMINI SETUP
# ============================================================

api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    raise RuntimeError(
        "GEMINI_API_KEY was not found in .env"
    )

gemini = genai.Client(
    api_key=api_key
)


# ============================================================
# WHISPER SETUP
# ============================================================

print("Loading Whisper...")

whisper_model = WhisperModel(
    "small",
    device="cpu",
    compute_type="int8"
)

print("Whisper loaded.")


# ============================================================
# DOWNLOAD VIDEO
# ============================================================

def download_video(url):

    print("\nChecking video...")

    # First get metadata WITHOUT downloading
    metadata_options = {
        "quiet": True,
        "no_warnings": True,
    }

    with yt_dlp.YoutubeDL(metadata_options) as ydl:
        info = ydl.extract_info(
            url,
            download=False
        )

    video_id = info["id"]

    video_path = os.path.join(
        DOWNLOAD_FOLDER,
        f"{video_id}.mp4"
    )

    # --------------------------------------------------------
    # CACHE CHECK
    # --------------------------------------------------------

    if os.path.exists(video_path):

        print("Video already downloaded.")
        print("Using cached video:")
        print(video_path)

        return info, video_id, video_path

    # --------------------------------------------------------
    # DOWNLOAD
    # --------------------------------------------------------

    print("Video not cached.")
    print("Downloading...")

    ydl_options = {
        "format": "bv*+ba/b",
        "outtmpl":
            f"{DOWNLOAD_FOLDER}/%(id)s.%(ext)s",
        "merge_output_format": "mp4",
        "quiet": False,
    }

    with yt_dlp.YoutubeDL(ydl_options) as ydl:

        info = ydl.extract_info(
            url,
            download=True
        )

    if not os.path.exists(video_path):
        raise RuntimeError(
            f"Download finished but video was not found: "
            f"{video_path}"
        )

    print("\nVideo downloaded:")
    print(video_path)

    return info, video_id, video_path


# ============================================================
# TRANSCRIBE VIDEO
# ============================================================

def transcribe_video(video_path, video_id):

    transcript_path = os.path.join(
        DOWNLOAD_FOLDER,
        f"{video_id}_transcript.json"
    )

    # --------------------------------------------------------
    # CACHE CHECK
    # --------------------------------------------------------

    if os.path.exists(transcript_path):

        print("\nTranscript already exists.")
        print("Loading cached transcript...")

        with open(
            transcript_path,
            "r",
            encoding="utf-8"
        ) as file:

            transcript = json.load(file)

        print(
            f"Loaded {len(transcript)} "
            f"transcript segments."
        )

        return transcript

    # --------------------------------------------------------
    # WHISPER
    # --------------------------------------------------------

    print("\nTranscribing video...")

    segments, info = whisper_model.transcribe(
        video_path,
        beam_size=5
    )

    transcript = []

    for segment in segments:

        text = segment.text.strip()

        if not text:
            continue

        transcript.append({
            "start": round(segment.start, 2),
            "end": round(segment.end, 2),
            "text": text
        })

    print(
        f"Transcription complete. "
        f"Language: {info.language}"
    )

    print(
        f"Segments: {len(transcript)}"
    )

    # --------------------------------------------------------
    # SAVE TRANSCRIPT
    # --------------------------------------------------------

    with open(
        transcript_path,
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            transcript,
            file,
            ensure_ascii=False,
            indent=2
        )

    print("Transcript saved:")
    print(transcript_path)

    return transcript


# ============================================================
# GEMINI
# ============================================================

def find_best_clips(transcript):

    transcript_text = "\n".join(
        f"[{item['start']:.2f} - "
        f"{item['end']:.2f}] "
        f"{item['text']}"
        for item in transcript
    )

    prompt = f"""
You are an expert short-form video editor.

Analyze the timestamped transcript below.

Select EXACTLY 3 of the strongest moments that could become
engaging YouTube Shorts, TikTok videos, or Instagram Reels.

CLIP RULES:

- Each clip should preferably be 25 to 60 seconds.
- Each clip must have a clear beginning and ending.
- Each clip should make sense without watching the entire video.
- Do not overlap clips.

Prefer moments containing:

- strong hooks
- surprising statements
- interesting stories
- emotional moments
- conflict or tension
- useful information
- funny moments
- controversial or thought-provoking statements
- satisfying conclusions

IMPORTANT:

- Use timestamps from the transcript.
- Do not invent dialogue.
- The transcript may contain transcription mistakes.
- The language may be English, Swahili, or mixed.
- Judge the meaning even when transcription is imperfect.

Return ONLY valid JSON.

Do NOT return markdown.

Do NOT use ```json.

Do NOT include explanations outside the JSON.

Return exactly this structure:

[
  {{
    "start": 120.5,
    "end": 165.2,
    "title": "Short descriptive title",
    "reason": "Why this section would make a strong short"
  }},
  {{
    "start": 300.0,
    "end": 345.0,
    "title": "Another title",
    "reason": "Why this section would make a strong short"
  }},
  {{
    "start": 500.0,
    "end": 550.0,
    "title": "Third title",
    "reason": "Why this section would make a strong short"
  }}
]

TRANSCRIPT:

{transcript_text}
"""

    print("\nGemini is selecting clips...")

    max_retries = 5

    result = None

    # --------------------------------------------------------
    # GEMINI RETRY LOOP
    # --------------------------------------------------------

    for attempt in range(max_retries):

        try:

            print(
                f"Gemini request "
                f"(attempt {attempt + 1}/"
                f"{max_retries})..."
            )

            interaction = (
                gemini.interactions.create(
                    model="gemini-3.8-flash",
                    input=prompt
                )
            )

            result = interaction.output_text

            if not result:
                raise RuntimeError(
                    "Gemini returned an empty response."
                )

            result = result.strip()

            break

        except Exception as error:

            error_text = str(error)

            temporary_error = (
                "503" in error_text
                or
                "service_unavailable"
                in error_text.lower()
                or
                "high demand"
                in error_text.lower()
                or
                "temporarily unavailable"
                in error_text.lower()
            )

            if temporary_error:

                if attempt == max_retries - 1:
                    raise

                wait_time = 10 * (
                    attempt + 1
                )

                print(
                    "Gemini is currently busy."
                )

                print(
                    f"Retrying in "
                    f"{wait_time} seconds..."
                )

                time.sleep(wait_time)

            else:

                raise

    # --------------------------------------------------------
    # CLEAN RESPONSE
    # --------------------------------------------------------

    print("\nRaw Gemini response:")
    print(result)

    if result.startswith("```"):

        result = result.replace(
            "```json",
            ""
        )

        result = result.replace(
            "```",
            ""
        )

        result = result.strip()

    # --------------------------------------------------------
    # PARSE JSON
    # --------------------------------------------------------

    try:

        clips = json.loads(result)

    except json.JSONDecodeError as error:

        print("\nGemini returned invalid JSON.")

        raise RuntimeError(
            "Could not parse Gemini response "
            "as JSON."
        ) from error

    # --------------------------------------------------------
    # VALIDATE
    # --------------------------------------------------------

    if not isinstance(clips, list):

        raise RuntimeError(
            "Gemini response must be a list."
        )

    if len(clips) == 0:

        raise RuntimeError(
            "Gemini did not select any clips."
        )

    valid_clips = []

    for clip in clips:

        if not isinstance(clip, dict):
            continue

        if (
            "start" not in clip
            or
            "end" not in clip
        ):
            continue

        try:

            start = float(
                clip["start"]
            )

            end = float(
                clip["end"]
            )

        except (ValueError, TypeError):

            continue

        if end <= start:
            continue

        valid_clips.append({
            "start": start,
            "end": end,
            "duration": round(
                end - start,
                2
            ),
            "title": clip.get(
                "title",
                "Untitled Clip"
            ),
            "reason": clip.get(
                "reason",
                ""
            )
        })

    if not valid_clips:

        raise RuntimeError(
            "Gemini returned no valid clips."
        )

    print("\nGemini selected:")

    print(
        json.dumps(
            valid_clips,
            indent=2,
            ensure_ascii=False
        )
    )

    return valid_clips


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():

    return render_template(
        "index.html"
    )


# ============================================================
# GENERATE
# ============================================================

@app.route(
    "/generate",
    methods=["POST"]
)
def generate():

    try:

        # ----------------------------------------------------
        # GET URL
        # ----------------------------------------------------

        data = request.get_json(
            silent=True
        ) or {}

        url = data.get(
            "url",
            ""
        ).strip()

        if not url:

            return jsonify({
                "success": False,
                "error":
                    "Please enter a YouTube URL."
            }), 400

        print("\n")
        print("=" * 60)
        print("NEW JOB")
        print("=" * 60)

        # ----------------------------------------------------
        # VIDEO
        # ----------------------------------------------------

        info, video_id, video_path = (
            download_video(url)
        )

        # ----------------------------------------------------
        # TRANSCRIPT
        # ----------------------------------------------------

        transcript = transcribe_video(
            video_path,
            video_id
        )

        # ----------------------------------------------------
        # GEMINI
        # ----------------------------------------------------

        clips = find_best_clips(
            transcript
        )

        # ----------------------------------------------------
        # RESPONSE
        # ----------------------------------------------------

        print("\nJob complete.")

        return jsonify({
            "success": True,

            "title": info.get(
                "title"
            ),

            "video_id": video_id,

            "video_path": video_path,

            "clips": clips
        })

    except Exception as error:

        print("\nERROR:")
        print(error)

        return jsonify({
            "success": False,
            "error": str(error)
        }), 500


# ============================================================
# START SERVER
# ============================================================

if __name__ == "__main__":

    app.run(
        debug=True
    )