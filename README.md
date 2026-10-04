# AI YouTube Auto Clipper

Paste a YouTube video URL and click **Generate Shorts**. This local Flask application downloads the video, transcribes speech, asks Gemini for three complete moments, and renders captioned vertical MP4s with previews and download buttons.

## Features

- Firefox cookies and Deno/EJS support for YouTube access; no cookie exports.
- Whisper `small`, CPU, int8, automatic language detection and word timestamps, including English/Swahili as Whisper supports them.
- Gemini `gemini-3.8-flash` selection, timestamp validation and bounded temporary-error retries.
- 1080×1920, 30 fps H.264/AAC, `yuv420p`, faststart MP4.
- Blurred/darkened background, preserved foreground proportions, subtle zoom, fades, audio normalization (`I=-14:TP=-1.5:LRA=11`).
- Short ASS caption groups, white text and yellow active word, strong outline in a safe lower area.
- Background jobs, real stage/segment/render progress, responsive polling, recovery after refreshing the page.
- Download/transcript caches and versioned, atomically published render caches.

The blurred background layout is a video composition; face/person tracking is not implemented.

## Arch Linux setup

Install system tools and a font:

```sh
sudo pacman -S python ffmpeg deno firefox noto-fonts
```

Open Firefox, visit YouTube, and sign into an account permitted to view the videos you want to process. The application uses yt-dlp's direct browser-cookie reader. It never exports cookies into the repository. Run it as your desktop user so it can read your Firefox profile. Deno and yt-dlp's EJS component solve JavaScript challenges; internet access is required for new downloads and Gemini requests.

Create a virtual environment (or keep using this project's existing `.venv`):

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

These Python versions are pinned to the installed environment used for this build. `yt-dlp[default]` includes EJS support; the working `ejs:npm` remote component remains enabled as a fallback. FFmpeg/FFprobe need libx264, AAC and libass support. Whisper's first use downloads the `small` model; subsequent uses load the local model cache. Model loading is lazy so the web server starts without waiting for Whisper.

Create `.env` (or use your existing file):

```dotenv
GEMINI_API_KEY=your_key_here
```

The key stays on the backend. A missing key or FFmpeg/FFprobe/Deno produces a clear startup error. Ensure your Gemini account has access to `gemini-3.8-flash`; the application preserves this configured model and does not silently switch models.

Start from the repository:

```sh
.venv/bin/python app.py
```

Open http://127.0.0.1:5000. If port 5000 is busy:

```sh
.venv/bin/python app.py --port 5001
```

## Architecture

```text
youtube-auto-clipper/
├── app.py                 # Flask validation, job status, safe MP4 routes
├── jobs.py                # locked job store and one background worker
├── pipeline.py            # existing download/transcribe/AI/caption/render pipeline
├── templates/index.html
├── static/css/style.css
├── static/js/app.js        # status polling and refresh recovery
├── tests/test_app.py       # offline regression suite
├── requirements.txt
├── .env.example
├── downloads/             # private video, metadata, word transcript caches
└── clips/                 # private ASS and versioned MP4 renders
```

`POST /generate` accepts `{"url":"https://youtu.be/VIDEO_ID"}` and returns HTTP 202 with `success` and `job_id`. `GET /status/<job_id>` returns status, stage, progress, message, error, title, video ID, timestamps and completed clip metadata. `GET /clips/<filename>.mp4` supports video range requests; adding `?download=1` sends an attachment.

One background worker processes one video at a time to avoid competing Whisper/FFmpeg operations and cache writes. An active second submission returns HTTP 409 with the active job ID; the browser follows that job. Progress reflects completed stages, Whisper segments and FFmpeg output timestamps. It can remain stationary during model loading, AI requests and decoding before an individual render; there is no timer-based percentage. Rendering completes each step only after FFmpeg succeeds and FFprobe validates the output. The newest 50 jobs are retained in memory.

Use one Flask process with the reloader disabled. Restarting the server loses in-memory job state but keeps disk caches; generate again to reuse them. Refreshing the page in the same browser restores the saved job, including completed results. This is a local application, bound to loopback; accounts, persistent jobs, and remote deployment are outside this build.

## Caption timing and cache policy

Every word is converted from source time to clip-local time (`90.50 − 88.96 = 1.54`). FFmpeg decodes before trimming and resets both video/audio timestamps **inside the filters before ASS**. This is an accurate trim equivalent to decoding before output seeking; output-only `-ss` would not reset timestamps before subtitle evaluation. There is no arbitrary caption offset.

Highlights run from a word's start to the next word's start, with the final word ending at its own end. Absolute ASS event times avoid accumulating karaoke rounding drift. Groups contain at most four words and split after pauses greater than 0.6 seconds. Whisper occasionally emits zero-duration words; the cache remains valid and the caption builder handles them safely.

Downloads are reused only if FFprobe finds a usable video and positive duration. `.part` files are never considered ready. Word caches are parsed and checked before reuse. Render filenames contain source ID, start/end and `RENDER_VERSION = "v5"`; increase this version when changing rendering behavior. New renders go to temporary files and only replace final MP4s after success. Generated media, `.env`, cookies and virtual environments are gitignored.

Gemini must return three non-overlapping moments with finite timestamps inside the source duration, titles and reasons. Preferred duration is 25–60 seconds; validation allows 10–65 seconds for complete shorter moments. If the video cannot provide three usable moments, the job reports an error instead of inventing clips. Silent video rendering is supported, but the automatic selection pipeline requires transcribable speech/lyrics.

## Testing

```sh
.venv/bin/python -m py_compile app.py jobs.py pipeline.py
.venv/bin/python -m unittest discover -s tests -v
node --check static/js/app.js  # optional; Node is not an application dependency
node tests/test_frontend.cjs  # optional frontend polling regression test
node tests/test_publishing_frontend.cjs  # optional publishing UI regression test
```

Offline tests mock downloads, model inference, API failures and render failures to verify job behavior without private credentials or paid requests. They check URL validation, cached/fresh transcription flow, clip bounds/overlap, caption-local timestamps, retries, safe errors, status polling, 100% completion, responsiveness, refresh requests, and downloads. Actual inference, downloading and media playback should additionally be checked on your machine with Firefox logged in and Gemini available.

Verified on this machine during development: a fresh Firefox/Deno download, cached media and transcripts, fresh Whisper inference on a speech sample, live Gemini selection, three full Shorts reaching 100%, FFmpeg frame progress, AAC audio decoding, silent-video rendering, and real Firefox playback of all three outputs. The original generation regression tests and frontend polling/reconnect/refresh test pass; publishing adds its own offline tests described below.

## Troubleshooting

| Symptom | Action |
|---|---|
| Missing API key | Add `GEMINI_API_KEY` to `.env` and restart. |
| Firefox cookies cannot be loaded | Open Firefox and sign into YouTube as the same desktop user running the app. Close Firefox if its database is locked. |
| YouTube asks for authentication | Confirm the Firefox account can watch this specific video, including age/private restrictions. |
| Deno/EJS or bot challenge fails | Check `deno --version`; update with `.venv/bin/python -m pip install -U 'yt-dlp[default]'`. The setup follows yt-dlp's [EJS documentation](https://github.com/yt-dlp/yt-dlp/wiki/EJS). |
| Network/download failure | Check connectivity and try again. Valid cached videos do not need another download. |
| Gemini temporarily unavailable | The worker retries transient failures with bounded backoff. If exhausted, try again later. Permanent key/model errors are not retried. |
| Invalid Gemini moments | Try again or choose a longer video with several complete stories. |
| Transcription fails | Confirm audible speech and that the Whisper model can download/load. Music recognition quality varies. Restart Flask after code updates: the reloader is disabled, so an existing process keeps its old validation logic. |
| FFmpeg render fails | Read terminal diagnostics; check libass/libx264 support, available disk space, and a valid source file. Failed files are never published as completed Shorts. |
| Captions seem wrong | Whisper timing can be imperfect. Check `downloads/VIDEO_ID_words.json`; timestamps are used without a global offset. |
| Progress holds still | Model loading, AI calls and CPU rendering take time. Progress advances from actual processed segments/frames and completed stages. |
| Connection interrupted | Polling reconnects automatically. Keep the Flask process running. |
| Job not found after restart | Start generation again; video and transcript caches remain. |
| Port already in use | Start with `--port 5001`. |

Detailed failures stay in the terminal; browser errors use deliberately safe messages. Never commit `.env`, browser cookie databases, transcripts, downloads or generated clips.

## Publishing to YouTube and Instagram

Publishing is optional. Generating/downloading Shorts does not require social account credentials. Expand **Publish to social media** in the app, connect accounts, and select destinations. Use **Publish this Short** on a result to edit its title/caption and publish, or check **Publish all three Shorts automatically after generation** before submitting the video URL. Automatic publishing uses the selected account, audience and visibility settings. Instagram publishes to the account's feed/Reels; YouTube defaults to private. Uploads run in a separate background worker with their own progress display.

### YouTube: one-time setup

1. Create a project in [Google Cloud Console](https://console.cloud.google.com/) and enable **YouTube Data API v3**.
2. Configure the OAuth consent screen. While testing, add your Google account as a test user.
3. Create an OAuth client of type **Web application**. Add this exact authorized redirect URI if you run Flask on port 5001: `http://127.0.0.1:5001/accounts/youtube/callback`. Match the host and port you actually use.
4. Put the client ID and client secret in your private `.env`:

   ```dotenv
   YOUTUBE_CLIENT_ID=your_client_id
   YOUTUBE_CLIENT_SECRET=your_client_secret
   YOUTUBE_REDIRECT_URI=http://127.0.0.1:5001/accounts/youtube/callback
   ```

5. Restart Flask and click **Connect YouTube**. Choose the Google account/channel to receive Shorts and approve upload access and read access (used to identify the connected channel).

Uploads use Google's resumable video API, with acknowledged byte progress. Credentials are refreshed when needed. OAuth uses an expiring, browser-bound state and PKCE. Account connection is separate from the Firefox cookies used to download source videos. See [Google's authorization guide](https://developers.google.com/youtube/v3/guides/auth/server-side-web-apps) and [upload API](https://developers.google.com/youtube/v3/docs/videos/insert).

Google restricts uploads from qualifying unverified API projects to private viewing until the project passes its YouTube API audit. Selecting Public in the app cannot override that platform restriction. The upload result displays the visibility returned by YouTube. A successfully uploaded video may still need processing in YouTube Studio before it is playable.

### Instagram: one-time setup

This integration uses **Instagram API with Facebook Login**, with a Page-linked professional Instagram account, and local binary upload. Personal Instagram accounts are not supported by this publishing integration.

1. Use a professional Instagram Business/Creator account linked to a Facebook Page you manage.
2. Create a Meta developer app configured for **Facebook Login for Business** and the Instagram publishing use case. Enable the required permissions/access for the account you will use. Development access is limited to eligible app roles/assets; publishing for other users may require Meta App Review.
3. Obtain an authorized Facebook access token for that app and account. It needs the applicable `instagram_basic`, `instagram_content_publish` and `pages_read_engagement` permissions; `pages_show_list` is needed if you use the API to discover your Pages. Follow your app's authorization flow and Meta's permission requirements. Do not paste access tokens into chat or commit them.
4. Find the **Instagram professional account ID**, not the Facebook Page ID or username (the Page's `instagram_business_account` field identifies the linked Instagram account).
5. Add to `.env`:

   ```dotenv
   INSTAGRAM_ACCOUNT_ID=your_numeric_instagram_account_id
   INSTAGRAM_ACCESS_TOKEN=your_authorized_meta_token
   META_GRAPH_VERSION=v24.0
   ```

6. Restart Flask and click **Connect Instagram** to validate the token/account and show its username. If the token expires or permissions change, update `.env`, restart and reconnect. Unlike YouTube, this initial integration does not implement automatic Meta token renewal or a Facebook OAuth sign-in screen.

The configured default API version is `v24.0`; use a supported version for your Meta app. No public hosting of the clips folder or tunnel is required: the backend creates a resumable Reel container, uploads local bytes to Meta, polls processing and then publishes. It prepares a private upload-only MP4 with 128 kbps AAC and no edit lists while preserving the original rendered Short. See [Meta's official local upload sample](https://github.com/fbsamples/reels_publishing_apis/tree/main/insta_reels_publishing_api_sample) and [publishing documentation](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-facebook-login/content-publishing).

### Upload status and retries

- `.runtime/publishing.sqlite3` stores private account tokens and upload results; the directory has mode 700 and database mode 600 and is gitignored. Tokens are stored locally, unencrypted, and never returned to frontend JavaScript. Keep the directory private.
- Identical rendered bytes are uploaded once per connected platform/account. Repeated clicks return the existing upload, including after restarting Flask. Titles/captions/visibility are fixed when submitted; repeat clicks on a completed clip do not edit or repost it.
- A confirmed rejection can be retried with **Publish** after fixing the account/settings. Ambiguous connection/server failures are marked **uncertain**. Check the destination account first; if no post exists, use **I checked: no post exists**, then **Publish** to retry. A returned remote post ID prevents this reset.
- Restarted uploads are marked uncertain rather than submitted a second time. This version does not resume saved upload sessions after a restart.
- Disconnecting an account removes its locally saved token; already started remote requests may still finish. You can also revoke the app in the platform account settings. Reconnecting to a different account during generation cancels automatic publishing to avoid posting to an unintended account.
- Upload failure leaves all completed Shorts available for preview/download. You can regenerate after a restart to recover video/transcript/render caches; generation jobs are still in memory.
- Do not launch multiple app processes against the same runtime directory.

The current adapters support YouTube and Instagram. TikTok, Facebook Reels, and other networks need their own developer applications, account permissions and API-specific integrations; they are not enabled in this build.

Publishing tests use mocked platform responses and temporary private stores. They cover OAuth state/PKCE, token refresh, chunk acknowledgements, Instagram processing, durable duplicate protection, uncertain outcomes, account changes and generation/publishing isolation. Live account publishing requires your developer credentials and explicit account connection and has not been tested on your accounts.
