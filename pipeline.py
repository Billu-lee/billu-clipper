from faster_whisper import WhisperModel
from google import genai
from dotenv import load_dotenv

import yt_dlp
import subprocess
import os
import json
import time
import math
import re
import shutil
import tempfile
from types import SimpleNamespace
from pathlib import Path
from urllib.parse import urlparse, parse_qs


# ============================================================
# CONFIG
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

DOWNLOAD_FOLDER = str(BASE_DIR / "downloads")
CLIPS_FOLDER = str(BASE_DIR / "clips")

os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
os.makedirs(CLIPS_FOLDER, exist_ok=True)

GEMINI_MODEL = "gemini-3.8-flash"
RENDER_VERSION = "v5"

OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 1920


class PipelineError(RuntimeError):
    """A deliberately safe message that may be shown in the browser."""
    pass


# ============================================================
# GEMINI
# ============================================================

api_key = os.getenv("GEMINI_API_KEY")

gemini = genai.Client(api_key=api_key, http_options={"timeout": 120000}) if api_key else None
whisper_model = None


def check_dependencies():
    if not api_key:
        raise PipelineError("GEMINI_API_KEY is missing. Add it to .env and restart the application.")
    for binary in ("ffmpeg", "ffprobe", "deno"):
        if not shutil.which(binary):
            raise PipelineError(f"{binary} is missing. Install it and restart the application.")


def validate_url(url):
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError("Please enter a valid YouTube video URL.")
    try:
        parsed = urlparse(url.strip())
        if parsed.scheme not in ("https", "http") or parsed.username or parsed.password or parsed.port:
            raise ValueError()
        host = (parsed.hostname or "").lower()
        if host == "youtu.be":
            video_id = parsed.path.strip("/")
        elif host in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"):
            if parsed.path == "/watch":
                video_id = parse_qs(parsed.query).get("v", [""])[0]
            else:
                match = re.fullmatch(r"/(?:shorts|embed|live)/([A-Za-z0-9_-]{11})/?", parsed.path)
                video_id = match.group(1) if match else ""
        else:
            raise ValueError()
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            raise ValueError()
    except ValueError:
        raise ValueError("Please enter a supported YouTube video URL (watch, Shorts, or youtu.be).") from None
    return video_id, f"https://www.youtube.com/watch?v={video_id}"


def probe_media(path):
    result = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise PipelineError("Video format is unsupported or the media file is incomplete.")
    info = json.loads(result.stdout)
    if not any(stream.get("codec_type") == "video" for stream in info["streams"]):
        raise PipelineError("The media file has no video stream.")
    if float(info["format"].get("duration", 0)) <= 0:
        raise PipelineError("The media file has no usable duration.")
    return info


def valid_media(path, duration=None):
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return False
    try:
        info = probe_media(path)
        return duration is None or abs(float(info["format"]["duration"]) - duration) < 1
    except (RuntimeError, ValueError, KeyError, subprocess.SubprocessError):
        return False


def atomic_json(path, data):
    temporary = str(path) + ".tmp"
    Path(temporary).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def youtube_error(error):
    detail = str(error).lower()
    if "cookie" in detail or "firefox" in detail:
        return "Firefox cookies could not be loaded. Open Firefox and sign into YouTube, then try again."
    if "deno" in detail or "ejs" in detail or "challenge" in detail:
        return "Deno/EJS challenge solving failed. Update yt-dlp with its default extras and check Deno."
    if "bot" in detail or "sign in" in detail or "age" in detail or "account" in detail:
        return "YouTube requires authentication. Sign into an account with access to this video in Firefox."
    if "private" in detail or "unavailable" in detail or "removed" in detail:
        return "Could not access this YouTube video. It may be private, removed, or unavailable."
    if any(word in detail for word in ("network", "timed out", "connection", "resolve", "urlopen")):
        return "YouTube download failed due to a network problem. Check your connection and try again."
    return "YouTube download failed. Check your Firefox YouTube session and update yt-dlp."


# ============================================================
# YT-DLP COMMON OPTIONS
# ============================================================

def youtube_common_options():

    return {
        # Use your logged-in Firefox YouTube session
        "cookiesfrombrowser": ("firefox",),
        "js_runtimes": {"deno": {}},
        "noplaylist": True,
        "socket_timeout": 30,

        # Allow yt-dlp's EJS challenge solver
        "remote_components": {"ejs:npm"},

        "quiet": False,
        "no_warnings": False,
    }


# ============================================================
# DOWNLOAD VIDEO
# ============================================================

def download_video(url, report=lambda *args, **kwargs: None):
    video_id, url = validate_url(url)
    video_path = os.path.join(DOWNLOAD_FOLDER, f"{video_id}.mp4")
    metadata_path = os.path.join(DOWNLOAD_FOLDER, f"{video_id}_metadata.json")
    report("download", 5, "Checking YouTube video...")
    if valid_media(video_path):
        try:
            info = json.loads(Path(metadata_path).read_text(encoding="utf-8"))
            if not isinstance(info, dict) or info.get("id") != video_id or not isinstance(info.get("title"), str):
                raise ValueError("Invalid metadata cache")
        except (OSError, ValueError):
            info = {"id": video_id, "title": "Cached YouTube video"}
        info["duration"] = float(probe_media(video_path)["format"]["duration"])
        report("download", 15, "Using cached video.", video_id=video_id, title=info.get("title", video_id))
        return info, video_id, video_path

    if os.path.exists(video_path):
        os.replace(video_path, video_path + f".invalid-{int(time.time())}")

    def hook(data):
        if data["status"] == "downloading":
            # A download may consist of multiple streams; reserve completion for merge/probe.
            report("download", 7, "Downloading video...")
        elif data["status"] == "finished":
            report("download", 12, "Merging downloaded video and audio...")

    options = youtube_common_options()
    options.update({
        "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
        "outtmpl": os.path.join(DOWNLOAD_FOLDER, "%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
        "progress_hooks": [hook],
        "postprocessors": [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}],
    })
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as error:
        print("yt-dlp error:", error)
        raise PipelineError(youtube_error(error)) from error
    if not valid_media(video_path):
        raise PipelineError("yt-dlp finished but no complete usable MP4 was found.")
    metadata = {"id": video_id, "title": info.get("title", video_id), "duration": float(probe_media(video_path)["format"]["duration"])}
    atomic_json(metadata_path, metadata)
    report("download", 15, "Download ready.", video_id=video_id, title=metadata["title"])
    return metadata, video_id, video_path


# ============================================================
# TRANSCRIPTION
# ============================================================

def transcribe_video(video_path, video_id, report=lambda *args, **kwargs: None):
    global whisper_model
    report("transcription", 15, "Checking cached transcript...")

    transcript_path = os.path.join(
        DOWNLOAD_FOLDER,
        f"{video_id}_words.json"
    )

    # --------------------------------------------------------
    # CACHE
    # --------------------------------------------------------

    if os.path.exists(transcript_path):
        try:
            transcript = json.loads(Path(transcript_path).read_text(encoding="utf-8"))
            if valid_transcript(transcript):
                report("transcription", 45, "Using cached word transcript.")
                return transcript
        except (OSError, ValueError):
            pass

    # --------------------------------------------------------
    # WHISPER
    # --------------------------------------------------------

    print("\n" + "=" * 60)
    print("WHISPER")
    print("=" * 60)

    print("Transcribing video...")

    report("transcription", 15, "Loading Whisper small on CPU...")
    if whisper_model is None:
        whisper_model = WhisperModel("small", device="cpu", compute_type="int8")
    segments, info = whisper_model.transcribe(
        video_path,
        beam_size=5,
        word_timestamps=True
    )

    transcript = []

    for segment in segments:
        report("transcription", 15 + 30 * min(1, segment.end / max(info.duration, 1)), "Transcribing audio...")

        words = []

        if segment.words:

            for word in segment.words:

                if word.start is None or word.end is None:
                    continue

                text = word.word.strip()

                if not text:
                    continue

                words.append({
                    "start": round(word.start, 3),
                    "end": round(word.end, 3),
                    "word": text
                })

        transcript.append({
            "start": round(segment.start, 3),
            "end": round(segment.end, 3),
            "text": segment.text.strip(),
            "words": words
        })

    if not valid_transcript(transcript):
        if any(segment["words"] for segment in transcript):
            print("Whisper transcript validation failed: inconsistent segment or word timestamps.")
            raise PipelineError("Whisper returned inconsistent timestamps. Please retry transcription.")
        raise PipelineError("Whisper did not recognize any words. The source may have no audible speech or lyrics.")
    atomic_json(transcript_path, transcript)
    report("transcription", 45, "Transcription complete.")

    print("Transcription complete.")
    print(f"Language: {info.language}")
    print(f"Segments: {len(transcript)}")

    return transcript


# ============================================================
# GEMINI CLIP SELECTION
# ============================================================

def find_best_clips(transcript, duration=None, report=lambda *args, **kwargs: None):
    report("analysis", 45, "AI is finding the best moments...")

    print("\n" + "=" * 60)
    print("GEMINI")
    print("=" * 60)

    transcript_text = "\n".join(

        f"[{item['start']:.2f} - {item['end']:.2f}] "
        f"{item['text']}"

        for item in transcript
    )

    prompt = f"""
You are an expert short-form video editor.

Analyze this timestamped video transcript.

Choose EXACTLY 3 sections that have the strongest potential
for YouTube Shorts, TikTok, and Instagram Reels.

Each clip should preferably be 25-60 seconds.

IMPORTANT:

- Use timestamps from the transcript.
- Do not invent dialogue.
- Avoid overlapping clips.
- Avoid starting in the middle of an important sentence.
- Avoid ending before the thought is completed.
- Each clip should make sense without watching the original video.
- Prefer clips with strong hooks.

Look for:

- emotional moments
- funny moments
- surprising statements
- controversial or unexpected statements
- memorable quotes
- useful information
- interesting stories
- satisfying conclusions
- strong reactions
- catchy musical moments
- relatable moments

The transcript may contain English, Swahili, or both.

Return ONLY valid JSON.

Format:

[
  {{
    "start": 10.0,
    "end": 45.0,
    "title": "Short title",
    "reason": "Why this is a strong short"
  }},
  {{
    "start": 60.0,
    "end": 100.0,
    "title": "Short title",
    "reason": "Why this is a strong short"
  }},
  {{
    "start": 120.0,
    "end": 160.0,
    "title": "Short title",
    "reason": "Why this is a strong short"
  }}
]

TRANSCRIPT:

{transcript_text}
"""

    result = None

    for attempt in range(5):

        try:

            print(
                f"Gemini request {attempt + 1}/5..."
            )

            interaction = gemini.interactions.create(
                model=GEMINI_MODEL,
                input=prompt
            )

            result = interaction.output_text

            if not result:

                raise PipelineError(
                    "Gemini returned an empty response."
                )

            break

        except Exception as error:

            error_text = str(error).lower()

            temporary = (
                any(code in error_text for code in ("429", "500", "502", "503", "504"))
                or "timeout" in error_text
                or "timed out" in error_text
                or "high demand" in error_text
                or "service_unavailable" in error_text
                or "temporarily unavailable" in error_text
            )

            if temporary and attempt < 4:

                wait = 10 * (attempt + 1)

                print(
                    f"Gemini busy. Retrying in {wait}s..."
                )

                report("analysis", 45, f"Gemini is busy. Retrying in {wait} seconds...")
                time.sleep(wait)

                continue

            print("Gemini error:", error)
            raise PipelineError("Gemini is temporarily unavailable. Please try again later." if temporary else "Gemini clip selection failed. Check the API key and model access in the terminal.") from error

    if not result:

        raise PipelineError(
            "Gemini failed to select clips."
        )

    result = (
        result
        .replace("```json", "")
        .replace("```", "")
        .strip()
    )

    print("\nGemini response:")
    print(result)

    try:

        clips = json.loads(result)

    except json.JSONDecodeError as error:

        raise PipelineError(
            "Gemini returned invalid JSON."
        ) from error

    if not isinstance(clips, list):

        raise PipelineError(
            "Gemini response was not a list."
        )

    validated = validate_clips(clips, duration or max(item["end"] for item in transcript))
    report("analysis", 55, "AI selected three moments.")
    return validated


def valid_transcript(transcript):
    try:
        if not isinstance(transcript, list) or not transcript:
            return False
        count = 0
        for segment in transcript:
            if not isinstance(segment["text"], str) or not 0 <= float(segment["start"]) < float(segment["end"]) < math.inf:
                return False
            for word in segment["words"]:
                if not isinstance(word["word"], str) or not word["word"].strip() or not 0 <= float(word["start"]) <= float(word["end"]) < math.inf:
                    return False
                count += 1
        return count > 0
    except (KeyError, TypeError, ValueError):
        return False


def validate_clips(clips, duration):
    if not isinstance(clips, list) or len(clips) != 3:
        raise PipelineError("Gemini must select three usable moments. Try a longer video with more speech.")
    validated = []
    for clip in clips:
        try:
            if isinstance(clip["start"], bool) or isinstance(clip["end"], bool):
                raise ValueError()
            start, end = float(clip["start"]), float(clip["end"])
            if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end <= duration and 10 <= end - start <= 65):
                raise ValueError()
            if not isinstance(clip["title"], str) or not isinstance(clip["reason"], str) or not clip["title"].strip() or not clip["reason"].strip():
                raise ValueError()
        except (KeyError, ValueError, TypeError):
            raise PipelineError("Gemini returned invalid clip timestamps or descriptions. Please try again.") from None
        if any(min(end, other["end"]) > max(start, other["start"]) for other in validated):
            raise PipelineError("Gemini selected overlapping moments. Please try again.")
        validated.append({"start": start, "end": end, "duration": round(end-start, 2), "title": clip["title"][:200], "reason": clip["reason"][:1000]})
    return validated


# ============================================================
# ASS HELPERS
# ============================================================

def ass_time(seconds):
    # Round before splitting so 59.999 seconds becomes 0:01:00.00.
    centiseconds = max(0, round(float(seconds) * 100))
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    whole_seconds, fraction = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{fraction:02d}"


def clean_ass_text(text):

    return (
        text
        .replace("\\", "")
        .replace("{", "")
        .replace("}", "")
        .replace("\n", " ")
        .strip()
    )


# ============================================================
# EXTRACT WORDS FOR CLIP
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

            original_start = float(
                word["start"]
            )

            original_end = float(
                word["end"]
            )

            if original_end < clip_start:
                continue

            if original_start > clip_end:
                continue

            text = (
                word
                .get("word", "")
                .strip()
            )

            if not text:
                continue

            local_start = max(
                0,
                original_start - clip_start
            )

            local_end = min(
                clip_end - clip_start,
                original_end - clip_start
            )

            if local_end <= local_start:
                local_end = min(clip_end - clip_start, local_start + 0.01)
                if local_end <= local_start:
                    continue

            words.append({

                "start": local_start,

                "end": local_end,

                "word": text
            })

    return sorted(words, key=lambda word: (word["start"], word["end"]))


# ============================================================
# CAPTION GROUPING
# ============================================================

def group_caption_words(words):

    groups = []
    current = []

    for word in words:

        if current:

            previous = current[-1]

            gap = (
                word["start"]
                -
                previous["end"]
            )

            if gap > 0.60:

                groups.append(current)

                current = []

        current.append(word)

        # Short TikTok-style captions
        if len(current) >= 4:

            groups.append(current)

            current = []

    if current:
        groups.append(current)

    return groups


# ============================================================
# KARAOKE SUBTITLES
# ============================================================

def create_karaoke_subtitles(
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

        print("No word timestamps available.")

        return False

    groups = group_caption_words(words)

    header = """[Script Info]
Title: AI Auto Clipper
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Viral,Noto Sans,76,&H00FFFFFF,&H0000FFFF,&H00000000,&H70000000,-1,0,0,0,100,100,1,0,1,6,3,2,80,80,360,1
Style: Hook,Noto Sans,88,&H00FFFFFF,&H0000FFFF,&H00000000,&H70000000,-1,0,0,0,100,100,1,0,1,7,4,2,70,70,360,1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""

    lines = [header]

    for group in groups:

        if not group:
            continue

        group_start = float(
            group[0]["start"]
        )

        group_end = float(
            group[-1]["end"]
        )

        if group_end <= group_start:
            continue

        # Each event highlights exactly one word until the next word starts.
        # Absolute clip-local times avoid accumulating centisecond rounding errors.
        style = "Hook" if group_start < 3 else "Viral"
        for index, word in enumerate(group):
            event_start = max(group_start, word["start"])
            event_end = group[index + 1]["start"] if index + 1 < len(group) else word["end"]
            event_end = min(group_end, max(event_start + 0.01, event_end))
            if event_end <= event_start:
                continue
            parts = []
            for j, item in enumerate(group):
                color = "&H0000FFFF&" if j == index else "&H00FFFFFF&"
                parts.append("{\\c" + color + "}" + clean_ass_text(item["word"]).upper())
            lines.append(f"Dialogue: 0,{ass_time(event_start)},{ass_time(event_end)},{style},,0,0,0,," + " ".join(parts) + "\n")

    with open(
        subtitle_path,
        "w",
        encoding="utf-8"
    ) as file:

        file.writelines(lines)

    print("Karaoke captions created:")
    print(subtitle_path)

    return True


# ============================================================
# FFMPEG SUBTITLE PATH
# ============================================================

def escape_filter_path(path):

    absolute = os.path.abspath(path)

    return (
        absolute
        .replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
    )


# ============================================================
# VIDEO FILTER
# ============================================================

def build_video_filter(
    subtitle_path,
    duration,
    has_captions,
    start=0
):

    fade_duration = 0.20

    fade_out_start = max(
        0,
        duration - fade_duration
    )

    filters = [

        # ----------------------------------------------------
        # SPLIT
        # ----------------------------------------------------

        f"[0:v]trim=start={start}:duration={duration},setpts=PTS-STARTPTS,fps=30,"
        "split=2"
        "[background]"
        "[foreground]",

        # ----------------------------------------------------
        # BLURRED 9:16 BACKGROUND
        # ----------------------------------------------------

        "[background]"
        "scale="
        "1080:1920:"
        "force_original_aspect_ratio=increase,"
        "crop=1080:1920,setsar=1,"
        "boxblur="
        "luma_radius=25:"
        "luma_power=2,"
        "eq="
        "brightness=-0.13:"
        "saturation=0.80"
        "[bg]",

        # ----------------------------------------------------
        # PRESERVE ORIGINAL FOREGROUND
        # ----------------------------------------------------

        "[foreground]"
        "scale="
        "1080:1150:"
        "force_original_aspect_ratio=decrease,"
        "setsar=1"
        "[fg]",

        # ----------------------------------------------------
        # CENTER FOREGROUND
        # ----------------------------------------------------

        "[bg][fg]"
        "overlay="
        "(W-w)/2:"
        "(H-h)/2"
        "[combined]"
    ]

    # --------------------------------------------------------
    # SUBTITLES
    # --------------------------------------------------------

    if has_captions:

        subtitle = escape_filter_path(
            subtitle_path
        )

        filters.append(

            "[combined]"
            f"ass='{subtitle}'"
            "[captioned]"
        )

        previous = "captioned"

    else:

        previous = "combined"

    # --------------------------------------------------------
    # SUBTLE PUNCH EFFECT
    # --------------------------------------------------------

    filters.append(

        f"[{previous}]"

        "zoompan="

        "z='"
        "1.01+0.010*sin(2*PI*on/240)"
        "':"

        "x='"
        "iw/2-(iw/zoom/2)"
        "':"

        "y='"
        "ih/2-(ih/zoom/2)"
        "':"

        "d=1:"

        "s=1080x1920:"

        "fps=30"

        "[zoomed]"
    )

    # --------------------------------------------------------
    # VIDEO FADES
    # --------------------------------------------------------

    filters.append(

        "[zoomed]"

        "fade="
        "t=in:"
        "st=0:"
        f"d={fade_duration},"

        "fade="
        "t=out:"
        f"st={fade_out_start}:"
        f"d={fade_duration},"

        "format=yuv420p"

        "[vout]"
    )

    return ";".join(filters)


# ============================================================
# RENDER SHORT
# ============================================================

def render_short(
    video_path,
    output_path,
    subtitle_path,
    start,
    end,
    has_captions,
    on_progress=None
):

    duration = end - start

    video_filter = build_video_filter(
        subtitle_path,
        duration,
        has_captions,
        start
    )

    audio_fade = 0.15

    audio_fade_out = max(
        0,
        duration - audio_fade
    )

    audio_filter = (
        f"atrim=start={start}:duration={duration},asetpts=PTS-STARTPTS,"

        "loudnorm="
        "I=-14:"
        "TP=-1.5:"
        "LRA=11,"

        "afade="
        "t=in:"
        "st=0:"
        f"d={audio_fade},"

        "afade="
        "t=out:"
        f"st={audio_fade_out}:"
        f"d={audio_fade}"
    )

    # Decode before trimming (accurate seek). Both streams are trimmed and reset
    # inside their filters, BEFORE ASS evaluates clip-local caption timestamps.
    # Output -ss alone would discard frames after captions had already run.
    command = [

        "ffmpeg",
        "-y",
        "-filter_complex_threads", "2",
        "-threads", "2",
        "-i",
        video_path,

        "-t",
        str(duration),

        "-filter_complex",
        video_filter,

        "-map",
        "[vout]",

        "-map",
        "0:a:0?",

        "-c:v",
        "libx264",

        "-threads", "2",
        "-preset",
        "veryfast",

        "-crf",
        "20",

        "-profile:v",
        "high",

        "-level",
        "4.1",

        "-af",
        audio_filter,

        "-c:a",
        "aac",

        "-b:a",
        "192k",

        "-ar",
        "48000",

        "-movflags",
        "+faststart",

        "-shortest",

        output_path
    ]

    temporary_output = output_path + ".rendering.mp4"
    command[-1] = temporary_output
    print("\nRendering Short...")

    if on_progress is None:
        result = subprocess.run(command, capture_output=True, text=True)
    else:
        # Drain progress stdout continuously; stderr goes to a temporary file so
        # FFmpeg cannot deadlock on a full pipe while the browser polls status.
        command[1:1] = ["-progress", "pipe:1", "-nostats"]
        with tempfile.TemporaryFile(mode="w+") as errors:
            with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors, text=True) as process:
                for line in process.stdout:
                    key, _, value = line.strip().partition("=")
                    if key == "out_time_us":
                        try:
                            on_progress(min(0.99, max(0, int(value) / 1_000_000 / duration)))
                        except ValueError:
                            pass
                code = process.wait()
            errors.seek(0)
            result = SimpleNamespace(returncode=code, stderr=errors.read())

    if result.returncode != 0:

        Path(temporary_output).unlink(missing_ok=True)
        print("\nFFMPEG ERROR")
        print("=" * 60)
        print(result.stderr)

        raise PipelineError(
            "FFmpeg failed while rendering the Short."
        )


    if not valid_media(temporary_output, duration):
        Path(temporary_output).unlink(missing_ok=True)
        raise PipelineError("FFmpeg produced an incomplete Short.")
    os.replace(temporary_output, output_path)


# ============================================================
# CREATE ALL SHORTS
# ============================================================

def create_clips(video_path, video_id, clips, transcript,
                 report=lambda *args, **kwargs: None):
    prepared = []
    report("captions", 55, "Preparing captions...")
    for index, clip in enumerate(clips, start=1):
        start, end = float(clip["start"]), float(clip["end"])
        base = f"{video_id}_{start:.2f}_{end:.2f}".replace(".", "-")
        output_filename = f"{base}_viral_{RENDER_VERSION}.mp4"
        subtitle_path = os.path.join(CLIPS_FOLDER, f"{base}_karaoke_{RENDER_VERSION}.ass")
        output_path = os.path.join(CLIPS_FOLDER, output_filename)
        cached = valid_media(output_path, end - start)
        has_captions = False
        if not cached:
            has_captions = create_karaoke_subtitles(transcript, start, end, subtitle_path)
        prepared.append((clip, output_filename, output_path, subtitle_path, cached, has_captions))
        report("captions", 55 + 10 * index / len(clips), f"Captions prepared for Short {index} of {len(clips)}.")

    generated = []
    for index, (clip, filename, output, subtitles, cached, has_captions) in enumerate(prepared, start=1):
        report("render", 65 + (index - 1) * 10, f"Rendering Short {index} of {len(clips)}...")
        if not cached:
            try:
                render_short(video_path, output, subtitles, clip["start"], clip["end"], has_captions,
                             lambda fraction: report("render", 65 + (index - 1) * 10 + 9 * fraction,
                                                     f"Rendering Short {index} of {len(clips)}..."))
            except Exception as error:
                raise PipelineError(f"FFmpeg could not render Short {index}. Check the terminal for details.") from error
        report("render", 65 + index * 10, f"Short {index} of {len(clips)} ready.")
        generated.append({**clip, "filename": filename, "url": f"/clips/{filename}"})
    return generated
