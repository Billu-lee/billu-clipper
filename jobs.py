"""One local worker: protects CPU/model/caches while Flask stays responsive."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from threading import Lock
from uuid import uuid4
import logging
import pipeline


def now():
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self):
        self.lock = Lock()
        self.jobs = {}
        self.publish_callback = None
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="shorts")

    def create(self, url, publish_options=None):
        with self.lock:
            active = next((j for j in self.jobs.values() if j["status"] in ("queued", "running")), None)
            if active:
                return active["job_id"], False
            # Retain the newest 50 results; runtime media remains on disk.
            while len(self.jobs) >= 50:
                self.jobs.pop(next(iter(self.jobs)))
            job_id = uuid4().hex
            self.jobs[job_id] = dict(job_id=job_id, status="queued", stage="preparing", progress=0,
                                     message="Preparing job...", error=None, title=None, video_id=None,
                                     clips=[], publish_options=publish_options, uploads=[], publishing_error=None, publishing_pending=False, created_at=now(), updated_at=now(), completed_at=None)
        self.worker.submit(self.run, job_id, url)
        return job_id, True

    def get(self, job_id):
        with self.lock:
            return deepcopy(self.jobs.get(job_id))

    def update(self, job_id, **changes):
        with self.lock:
            job = self.jobs[job_id]
            if "progress" in changes:
                changes["progress"] = round(max(job["progress"], min(100, changes["progress"])), 1)
            job.update(changes, updated_at=now())

    def run(self, job_id, url):
        def report(stage, progress, message, **extra):
            self.update(job_id, stage=stage, progress=progress, message=message, **extra)

        stage = "preparing"
        try:
            self.update(job_id, status="running")
            pipeline.check_dependencies()
            stage = "download"
            info, video_id, video_path = pipeline.download_video(url, report)
            self.update(job_id, title=info.get("title", video_id), video_id=video_id)
            stage = "transcription"
            transcript = pipeline.transcribe_video(video_path, video_id, report)
            stage = "analysis"
            clips = pipeline.find_best_clips(transcript, info["duration"], report)
            stage = "render"
            generated = pipeline.create_clips(video_path, video_id, clips, transcript, report)
            self.update(job_id, status="complete", stage="complete", progress=100,
                        message="Shorts ready.", clips=generated, completed_at=now(),
                        publishing_pending=bool(self.get(job_id).get("publish_options") and self.publish_callback))
            options = self.get(job_id).get("publish_options")
            if options and self.publish_callback:
                try:
                    identifiers = self.publish_callback(generated, options)
                    self.update(job_id, uploads=identifiers, publishing_pending=False)
                except Exception:
                    # Publishing failure must never discard successfully generated media.
                    self.update(job_id, publishing_pending=False, publishing_error="Shorts are ready, but automatic publishing could not start. Check your connected accounts.")

        except Exception as error:
            logging.exception("Job %s failed during %s", job_id, stage)
            # Only known, deliberately written messages reach the browser.
            safe = str(error) if isinstance(error, pipeline.PipelineError) else {
                "transcription": "Transcription failed. Check that the video has audible speech.",
                "analysis": "Gemini clip selection failed. Check the terminal for details.",
                "render": "FFmpeg could not render the Shorts. Check the terminal for details.",
                "download": "Could not access this YouTube video.",
            }.get(stage, "Could not prepare the job. Check the terminal for details.")
            self.update(job_id, status="failed", error=safe, message=safe, completed_at=now())
