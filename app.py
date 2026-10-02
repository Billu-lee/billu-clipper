from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    send_from_directory
)

from faster_whisper import WhisperModel
from google import genai
from dotenv import load_dotenv

import yt_dlp
import subprocess
import os
import json
import time
import re


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
# GEMINI
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
# WHISPER
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

    metadata_options = {
        "quiet": True,
        "no_warnings": True
    }

    with yt_dlp.YoutubeDL(
        metadata_options
    ) as ydl:

        info = ydl.extract_info(
            url,
            download=False
        )

    video_id = info["id"]

    video_path = os.path.join(
        DOWNLOAD_FOLDER,
        f"{video_id}.mp4"
    )

    if os.path.exists(video_path):

        print("Video already downloaded.")
        print("Using cached video:")
        print(video_path)

        return info, video_id, video_path

    print("Downloading video...")

    options = {

        "format": "bv*+ba/b",

        "outtmpl":
            f"{DOWNLOAD_FOLDER}/%(id)s.%(ext)s",

        "merge_output_format": "mp4",

        "quiet": False
    }

    with yt_dlp.YoutubeDL(
        options
    ) as ydl:

        info = ydl.extract_info(
            url,
            download=True
        )

    if not os.path.exists(video_path):

        raise RuntimeError(
            f"Video not found after download: "
            f"{video_path}"
        )

    return info, video_id, video_path


# ============================================================
# TRANSCRIPTION WITH WORD TIMESTAMPS
# ============================================================

def transcribe_video(
    video_path,
    video_id
):

    transcript_path = os.path.join(
        DOWNLOAD_FOLDER,
        f"{video_id}_words.json"
    )

    # --------------------------------------------------------
    # WORD TRANSCRIPT CACHE
    # --------------------------------------------------------

    if os.path.exists(
        transcript_path
    ):

        print(
            "\nWord transcript already exists."
        )

        print(
            "Loading cached word transcript..."
        )

        with open(
            transcript_path,
            "r",
            encoding="utf-8"
        ) as file:

            transcript = json.load(file)

        print(
            f"Loaded {len(transcript)} segments."
        )

        return transcript

    # --------------------------------------------------------
    # WHISPER
    # --------------------------------------------------------

    print(
        "\nCreating word-level transcription..."
    )

    segments, info = (
        whisper_model.transcribe(
            video_path,
            beam_size=5,
            word_timestamps=True
        )
    )

    transcript = []

    for segment in segments:

        words = []

        if segment.words:

            for word in segment.words:

                if (
                    word.start is None
                    or word.end is None
                ):
                    continue

                words.append({
                    "start": round(
                        word.start,
                        2
                    ),

                    "end": round(
                        word.end,
                        2
                    ),

                    "word":
                        word.word.strip()
                })

        transcript.append({

            "start": round(
                segment.start,
                2
            ),

            "end": round(
                segment.end,
                2
            ),

            "text":
                segment.text.strip(),

            "words":
                words
        })

    print(
        "Transcription complete. "
        f"Language: {info.language}"
    )

    print(
        f"Segments: {len(transcript)}"
    )

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

    print(
        "Word transcript saved:"
    )

    print(
        transcript_path
    )

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

Analyze this timestamped transcript.

Select EXACTLY 3 strong moments for YouTube Shorts,
TikTok, or Instagram Reels.

RULES:

- Each clip should preferably be 25-60 seconds.
- Each should make sense independently.
- Each should have a strong beginning.
- Avoid overlapping clips.
- Use timestamps from the transcript.
- Do not invent dialogue.

Prefer:

- strong hooks
- emotion
- interesting stories
- useful information
- funny moments
- surprising moments
- conflict
- memorable statements
- satisfying conclusions

The transcript may contain transcription mistakes.

The language may be English, Swahili, or mixed.

Return ONLY valid JSON.

No markdown.
No ```json.
No additional explanation.

Format:

[
  {{
    "start": 10.0,
    "end": 45.0,
    "title": "Title",
    "reason": "Reason"
  }},
  {{
    "start": 60.0,
    "end": 100.0,
    "title": "Title",
    "reason": "Reason"
  }},
  {{
    "start": 120.0,
    "end": 160.0,
    "title": "Title",
    "reason": "Reason"
  }}
]

TRANSCRIPT:

{transcript_text}
"""

    print(
        "\nGemini is selecting clips..."
    )

    result = None
    max_retries = 5

    for attempt in range(
        max_retries
    ):

        try:

            print(
                f"Gemini request "
                f"(attempt "
                f"{attempt + 1}/"
                f"{max_retries})..."
            )

            interaction = (
                gemini.interactions.create(
                    model="gemini-3.8-flash",
                    input=prompt
                )
            )

            result = (
                interaction
                .output_text
                .strip()
            )

            break

        except Exception as error:

            error_text = str(
                error
            ).lower()

            temporary = (
                "503" in error_text
                or
                "service_unavailable"
                in error_text
                or
                "high demand"
                in error_text
                or
                "temporarily unavailable"
                in error_text
            )

            if (
                temporary
                and
                attempt < max_retries - 1
            ):

                wait_time = (
                    10 * (attempt + 1)
                )

                print(
                    "Gemini busy. "
                    f"Retrying in "
                    f"{wait_time}s..."
                )

                time.sleep(
                    wait_time
                )

                continue

            raise

    if not result:

        raise RuntimeError(
            "Gemini returned no response."
        )

    print(
        "\nRaw Gemini response:"
    )

    print(result)

    if result.startswith(
        "```"
    ):

        result = (
            result
            .replace(
                "```json",
                ""
            )
            .replace(
                "```",
                ""
            )
            .strip()
        )

    try:

        clips = json.loads(
            result
        )

    except json.JSONDecodeError:

        raise RuntimeError(
            "Gemini returned invalid JSON."
        )

    valid_clips = []

    for clip in clips:

        try:

            start = float(
                clip["start"]
            )

            end = float(
                clip["end"]
            )

        except (
            KeyError,
            ValueError,
            TypeError
        ):

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

    print(
        "\nGemini selected:"
    )

    print(
        json.dumps(
            valid_clips,
            indent=2,
            ensure_ascii=False
        )
    )

    return valid_clips


# ============================================================
# ASS SUBTITLE HELPERS
# ============================================================

def ass_time(seconds):

    seconds = max(
        0,
        float(seconds)
    )

    hours = int(
        seconds // 3600
    )

    minutes = int(
        (seconds % 3600) // 60
    )

    secs = (
        seconds % 60
    )

    return (
        f"{hours}:"
        f"{minutes:02d}:"
        f"{secs:05.2f}"
    )


def clean_subtitle_text(text):

    text = (
        text
        .replace("\\", "")
        .replace("{", "")
        .replace("}", "")
    )

    return text.strip()


# ============================================================
# GET WORDS FOR A CLIP
# ============================================================

def get_clip_words(
    transcript,
    clip_start,
    clip_end
):

    words = []

    for segment in transcript:

        for word in segment.get(
            "words",
            []
        ):

            start = float(
                word["start"]
            )

            end = float(
                word["end"]
            )

            # Word falls within clip
            if (
                end >= clip_start
                and
                start <= clip_end
            ):

                text = (
                    word
                    .get(
                        "word",
                        ""
                    )
                    .strip()
                )

                if not text:
                    continue

                words.append({

                    # Convert from original video
                    # time to clip-local time
                    "start": max(
                        0,
                        start - clip_start
                    ),

                    "end": max(
                        0,
                        end - clip_start
                    ),

                    "word":
                        text
                })

    return words


# ============================================================
# CREATE ASS CAPTIONS
# ============================================================

def create_ass_subtitles(
    transcript,
    clip_start,
    clip_end,
    subtitle_path
):

    words = get_clip_words(
        transcript,
        clip_start,
        clip_end
    )

    if not words:

        print(
            "WARNING: No words found "
            "for captions."
        )

        return False

    # --------------------------------------------------------
    # GROUP WORDS
    #
    # Approximately 4 words at a time.
    # --------------------------------------------------------

    groups = []

    current = []

    for word in words:

        current.append(
            word
        )

        # Create short readable phrases
        if len(current) >= 4:

            groups.append(
                current
            )

            current = []

    if current:

        groups.append(
            current
        )

    # --------------------------------------------------------
    # ASS HEADER
    # --------------------------------------------------------

    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Default,Arial,72,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,5,2,2,70,70,360,1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""

    lines = [
        header
    ]

    for group in groups:

        start = group[0][
            "start"
        ]

        end = group[-1][
            "end"
        ]

        # Make sure subtitle remains visible
        # long enough to be readable.
        if end - start < 0.4:

            end = start + 0.4

        text = " ".join(
            word["word"]
            for word in group
        )

        text = clean_subtitle_text(
            text
        )

        line = (
            "Dialogue: 0,"
            f"{ass_time(start)},"
            f"{ass_time(end)},"
            "Default,,0,0,0,,"
            f"{text}\n"
        )

        lines.append(
            line
        )

    with open(
        subtitle_path,
        "w",
        encoding="utf-8"
    ) as file:

        file.writelines(
            lines
        )

    print(
        "Captions created:"
    )

    print(
        subtitle_path
    )

    return True


# ============================================================
# CREATE VERTICAL SHORTS + CAPTIONS
# ============================================================

def create_clips(
    video_path,
    video_id,
    clips,
    transcript
):

    generated_clips = []

    print(
        "\nCreating captioned vertical clips..."
    )

    for index, clip in enumerate(
        clips,
        start=1
    ):

        start = float(
            clip["start"]
        )

        end = float(
            clip["end"]
        )

        duration = (
            end - start
        )

        # ----------------------------------------------------
        # UNIQUE TIMESTAMP FILENAME
        # ----------------------------------------------------

        start_tag = (
            f"{start:.2f}"
            .replace(
                ".",
                "-"
            )
        )

        end_tag = (
            f"{end:.2f}"
            .replace(
                ".",
                "-"
            )
        )

        base_name = (
            f"{video_id}_"
            f"{start_tag}_"
            f"{end_tag}"
        )

        output_filename = (
            f"{base_name}_captioned.mp4"
        )

        output_path = os.path.join(
            CLIPS_FOLDER,
            output_filename
        )

        subtitle_filename = (
            f"{base_name}.ass"
        )

        subtitle_path = os.path.join(
            CLIPS_FOLDER,
            subtitle_filename
        )

        print(
            "\n"
            + "-" * 50
        )

        print(
            f"Creating clip "
            f"{index}/{len(clips)}"
        )

        print(
            f"{start:.2f}s "
            f"→ {end:.2f}s"
        )

        # ----------------------------------------------------
        # CACHE
        # ----------------------------------------------------

        if os.path.exists(
            output_path
        ):

            print(
                "Captioned clip already exists."
            )

            print(
                output_path
            )

        else:

            # ------------------------------------------------
            # CREATE SUBTITLE FILE
            # ------------------------------------------------

            has_captions = (
                create_ass_subtitles(
                    transcript,
                    start,
                    end,
                    subtitle_path
                )
            )

            # ------------------------------------------------
            # VIDEO FILTER
            # ------------------------------------------------

            vertical_filter = (
                "scale=1080:1920:"
                "force_original_aspect_ratio=increase,"
                "crop=1080:1920"
            )

            if has_captions:

                # FFmpeg filter paths can be sensitive
                # to special characters.
                escaped_subtitle_path = (
                    subtitle_path
                    .replace(
                        "\\",
                        "\\\\"
                    )
                    .replace(
                        ":",
                        "\\:"
                    )
                    .replace(
                        "'",
                        "\\'"
                    )
                )

                video_filter = (
                    vertical_filter
                    +
                    ",ass='"
                    +
                    escaped_subtitle_path
                    +
                    "'"
                )

            else:

                video_filter = (
                    vertical_filter
                )

            # ------------------------------------------------
            # FFMPEG
            # ------------------------------------------------

            command = [

                "ffmpeg",

                "-y",

                "-ss",
                str(start),

                "-i",
                video_path,

                "-t",
                str(duration),

                "-vf",
                video_filter,

                "-c:v",
                "libx264",

                "-crf",
                "20",

                "-preset",
                "veryfast",

                "-c:a",
                "aac",

                "-b:a",
                "192k",

                "-movflags",
                "+faststart",

                output_path
            ]

            print(
                "Running FFmpeg..."
            )

            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )

            if result.returncode != 0:

                print(
                    "\nFFmpeg ERROR:"
                )

                print(
                    result.stderr
                )

                raise RuntimeError(
                    f"FFmpeg failed on "
                    f"clip {index}."
                )

            print(
                "Created:"
            )

            print(
                output_path
            )

        generated_clips.append({

            **clip,

            "filename":
                output_filename,

            "path":
                output_path,

            "url":
                f"/clips/{output_filename}"
        })

    print(
        "\n"
        + "=" * 50
    )

    print(
        f"Created "
        f"{len(generated_clips)} "
        "captioned clips."
    )

    print(
        "=" * 50
    )

    return generated_clips


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():

    return render_template(
        "index.html"
    )


# ============================================================
# SERVE CLIPS
# ============================================================

@app.route(
    "/clips/<filename>"
)
def serve_clip(filename):

    return send_from_directory(
        CLIPS_FOLDER,
        filename
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

        print(
            "\n\n"
            + "=" * 60
        )

        print(
            "NEW JOB"
        )

        print(
            "=" * 60
        )

        # ----------------------------------------------------
        # VIDEO
        # ----------------------------------------------------

        (
            info,
            video_id,
            video_path
        ) = download_video(
            url
        )

        # ----------------------------------------------------
        # WORD-LEVEL WHISPER
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
        # FFMPEG + CAPTIONS
        # ----------------------------------------------------

        generated_clips = create_clips(
            video_path,
            video_id,
            clips,
            transcript
        )

        # ----------------------------------------------------
        # DONE
        # ----------------------------------------------------

        print(
            "\nJob complete."
        )

        return jsonify({

            "success": True,

            "title":
                info.get(
                    "title"
                ),

            "video_id":
                video_id,

            "clips":
                generated_clips
        })

    except Exception as error:

        print(
            "\nERROR:"
        )

        print(
            error
        )

        return jsonify({

            "success": False,

            "error":
                str(error)

        }), 500


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    app.run(
        debug=True
    )