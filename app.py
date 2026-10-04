"""Local Flask interface; all expensive work belongs to the background worker."""
import argparse
import logging
import os
import secrets
import time
import re
from flask import Flask, jsonify, render_template, request, send_from_directory, redirect, session
import pipeline
from jobs import JobStore
from publishing.manager import Publisher
from publishing.providers import PublishError

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8192
jobs = JobStore()
app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  TRUSTED_HOSTS=["localhost", "127.0.0.1", "[::1]"])
publisher = Publisher(pipeline.BASE_DIR / ".runtime", pipeline.CLIPS_FOLDER)
jobs.publish_callback = publisher.automatic


@app.get("/")
def home():
    return render_template("index.html")


@app.post("/generate")
def generate():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(success=False, error="Please send a YouTube URL as JSON."), 400
    try:
        _, url = pipeline.validate_url(data.get("url"))
    except ValueError as error:
        return jsonify(success=False, error=str(error)), 400
    publishing_options = None
    if data.get("publish") is not None:
        try:
            publishing_options = publisher.options(data["publish"])
        except PublishError as error:
            return jsonify(success=False, error=str(error)), 400
    job_id, created = jobs.create(url, publishing_options)
    if not created:
        return jsonify(success=False, job_id=job_id, error="A video is already processing. Following its progress."), 409
    return jsonify(success=True, job_id=job_id), 202


@app.get("/status/<job_id>")
def status(job_id):
    job = jobs.get(job_id)
    if job is None:
        return jsonify(success=False, error="Job not found. The server may have restarted; generate again to reuse cached work."), 404
    response = jsonify(success=True, **job)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/clips/<path:filename>")
def serve_clip(filename):
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.mp4", filename):
        return jsonify(success=False, error="Short not found."), 404
    return send_from_directory(pipeline.CLIPS_FOLDER, filename, conditional=True,
                               as_attachment=request.args.get("download") == "1")


@app.before_request
def publishing_origin_check():
    if request.method == "POST" and request.path.startswith(("/publish", "/accounts")):
        origin = request.headers.get("Origin")
        if (origin and origin != request.host_url.rstrip("/")) or not request.is_json:
            return jsonify(success=False, error="Use the publishing controls in this application."), 403


@app.get("/accounts")
def accounts():
    response = jsonify(success=True, accounts=publisher.accounts())
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/accounts/youtube/connect")
def youtube_connect():
    try:
        url, state, verifier = publisher.providers['youtube'].authorize()
        session['youtube_oauth'] = dict(state=state, verifier=verifier, created_at=time.time())
        return redirect(url)
    except PublishError as error:
        return render_template('connection.html', message=str(error)), 400


@app.get("/accounts/youtube/callback")
def youtube_callback():
    pending = session.pop('youtube_oauth', None)
    state = request.args.get('state','')
    if not pending or time.time()-pending['created_at'] > 600 or not secrets.compare_digest(pending['state'],state):
        return render_template('connection.html', message='Connection expired or could not be verified. Start Connect YouTube again.'), 400
    if request.args.get('error') or not request.args.get('code'):
        return render_template('connection.html', message='Google account connection was cancelled.'), 400
    try:
        publisher.providers['youtube'].connect(request.args['code'],pending['verifier'])
        return redirect('/?connected=youtube')
    except PublishError as error:
        return render_template('connection.html', message=str(error)), 400
    except Exception:
        return render_template('connection.html', message='Could not connect YouTube. Check the OAuth settings and try again.'), 400


@app.post("/accounts/instagram/connect")
def instagram_connect():
    try:
        publisher.providers['instagram'].connect()
        return jsonify(success=True)
    except PublishError as error:
        return jsonify(success=False,error=str(error)), 400


@app.post("/accounts/<platform>/disconnect")
def disconnect(platform):
    if platform not in publisher.providers:
        return jsonify(success=False,error='Unknown platform.'), 404
    publisher.store.disconnect(platform)
    return jsonify(success=True)


@app.post("/publish")
def publish():
    data = request.get_json(silent=True)
    if not isinstance(data,dict):
        return jsonify(success=False,error='Choose a completed Short to publish.'), 400
    job = jobs.get(data.get('job_id','')) if isinstance(data.get('job_id',''),str) else None
    index = data.get('clip_index')
    if not job or job['status'] != 'complete' or type(index) is not int or not 0 <= index < len(job['clips']):
        return jsonify(success=False,error='Choose a completed Short from your results.'), 400
    try:
        options = publisher.options(data)
        identifiers = publisher.enqueue(job['clips'][index],options)
        return jsonify(success=True,upload_ids=identifiers), 202
    except PublishError as error:
        return jsonify(success=False,error=str(error)), 400


@app.post("/publish/resolve/<upload_id>")
def resolve_upload(upload_id):
    data = request.get_json(silent=True)
    upload = publisher.get(upload_id)
    if not isinstance(data,dict) or data.get('not_posted') is not True:
        return jsonify(success=False,error='Check the destination account before confirming no post exists.'), 400
    if not upload or upload['status'] != 'uncertain' or upload.get('remote_id'):
        return jsonify(success=False,error='This upload cannot be reset. A confirmed post must not be uploaded again.'), 400
    publisher.store.update(upload_id,status='failed',error=None,
                           message='Confirmed no post exists. Use Publish on this Short to retry.')
    return jsonify(success=True)


@app.get("/publish/status/<upload_id>")
def publish_status(upload_id):
    upload = publisher.get(upload_id)
    if not upload:
        return jsonify(success=False,error='Upload not found.'), 404
    response=jsonify(success=True,**upload)
    response.headers['Cache-Control']='no-store'
    return response


@app.errorhandler(413)
def too_large(error):
    return jsonify(success=False, error="Request is too large. Please send only a YouTube URL."), 413


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        pipeline.check_dependencies()
    except RuntimeError as error:
        raise SystemExit(str(error))
    print(f"AI Auto Clipper — http://127.0.0.1:{args.port} — Firefox + Deno/EJS — Whisper small CPU — Gemini")
    # A single process owns the in-memory jobs. The debug reloader would lose them.
    app.run(host="127.0.0.1", port=args.port, threaded=True, debug=False, use_reloader=False)
