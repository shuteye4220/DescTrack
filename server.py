"""DescTrack Flask backend.

This module is the entire application's business-logic surface: it is
deliberately kept importable and runnable on its own (``python server.py``)
so it can be developed and tested without the desktop shell (see
desktop.py, which is the pywebview launcher used for the packaged desktop
app). All routes are JSON APIs except the two that serve the frontend and
preview audio clips.

Security model: the server only listens on 127.0.0.1, but *any* web page
open in any browser on the same computer can also send requests to
127.0.0.1. ``_guard_request`` below stops those pages from driving the API:

* The Host header must name this computer (127.0.0.1 / localhost). This
  defeats DNS-rebinding, where a hostile site points its own domain name at
  127.0.0.1 so the browser treats DescTrack as same-origin.
* POSTs must be ``application/json``. A cross-origin page can only send
  JSON after a CORS preflight, which this server never approves, so it can't
  send any API request with a body.
* If the browser sends an Origin header, it must be DescTrack's own.
"""

from __future__ import annotations

import math
import os
import platform
import subprocess
import sys
import threading
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

from services import ffmpeg_locator, ffmpeg_safety, media_probe, audio_sync, preview, mux, silence

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# Shown when ffmpeg / ffprobe can't be found next to the app or on PATH.
_FFMPEG_MISSING = "ffmpeg was not found. See README.md."
_FFPROBE_MISSING = "ffprobe was not found. See README.md."
_BOTH_MISSING = "ffmpeg / ffprobe were not found. See README.md."
_BOTH_FILES_REQUIRED = "Both a video file and an audio description file are required."

_LOCAL_HOSTNAMES = {"127.0.0.1", "localhost", "::1"}

# Limits on numbers sent by the page (the UI's own limits are the same or
# tighter): +/- 10 minutes of offset, previews of up to a minute.
MAX_OFFSET_MS = 600_000
MAX_PREVIEW_SECONDS = 60.0
MAX_POSITION_SECONDS = 24 * 3600.0

# How much ffmpeg output to keep per job, and how many finished jobs to keep.
MAX_LOG_LINES = 2000
MAX_FINISHED_JOBS = 50


class BadRequest(ValueError):
    """A request field was missing or invalid; message is shown to the user."""


@dataclass
class JobState:
    status: str = "running"  # running | done | error | cancelled
    percent: int = 0
    elapsed_seconds: float = 0.0
    eta_seconds: Optional[float] = None
    speed: Optional[float] = None
    log: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_LOG_LINES))
    output_path: Optional[str] = None
    error: Optional[str] = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def append_log(self, line: str) -> None:
        with self.lock:
            self.log.append(line)

    def set_progress(self, info: mux.ProgressInfo) -> None:
        with self.lock:
            self.percent = info["percent"]
            self.elapsed_seconds = info["elapsed_seconds"]
            self.eta_seconds = info["eta_seconds"]
            self.speed = info["speed"]

    def snapshot(self, include_log: bool = False) -> dict:
        with self.lock:
            snap = {
                "status": self.status,
                "percent": self.percent,
                "elapsed_seconds": self.elapsed_seconds,
                "eta_seconds": self.eta_seconds,
                "speed": self.speed,
                "output_path": self.output_path,
                "error": self.error,
            }
            # The page polls this every 0.7 s and doesn't show the raw ffmpeg
            # log, so only copy it when explicitly asked for (?log=1).
            if include_log:
                snap["log"] = list(self.log)
            return snap


JOBS: dict[str, JobState] = {}
_JOBS_LOCK = threading.Lock()
_CURRENT_PREVIEW_CLIPS: dict[str, str] = {}
_PREVIEW_LOCK = threading.Lock()


def _prune_finished_jobs() -> None:
    with _JOBS_LOCK:
        finished = [job_id for job_id, job in JOBS.items() if job.status != "running"]
        for job_id in finished[: max(0, len(finished) - MAX_FINISHED_JOBS)]:
            del JOBS[job_id]


# -- Request validation -------------------------------------------------------

def _json_body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise BadRequest("Expected a JSON object.")
    return data


def _media_path(data: dict, key: str) -> str:
    """An existing file, given as an absolute path (the native file dialogs
    always return one). Returned normalised, so it can't start with "-"."""
    value = data.get(key, "")
    if not isinstance(value, str) or not value or "\0" in value:
        raise BadRequest(_BOTH_FILES_REQUIRED)
    if not os.path.isabs(value) or not os.path.isfile(value):
        raise BadRequest(_BOTH_FILES_REQUIRED)
    return os.path.abspath(value)


def _number(data: dict, key: str, default: float, low: float, high: float) -> float:
    value = data.get(key, default)
    if isinstance(value, bool):
        raise BadRequest(f"{key} must be a number.")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise BadRequest(f"{key} must be a number.")
    if not math.isfinite(number) or not (low <= number <= high):
        raise BadRequest(f"{key} must be between {low:g} and {high:g}.")
    return number


def _bool(data: dict, key: str) -> bool:
    return data.get(key, False) is True


def create_app() -> Flask:
    app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")
    # Request bodies here are a few hundred bytes of JSON.
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024

    @app.before_request
    def _guard_request():
        # 1. Host must be this computer (defeats DNS rebinding).
        host = request.host or ""
        if urlsplit("//" + host).hostname not in _LOCAL_HOSTNAMES:
            abort(403)
        # 2. A browser-supplied Origin must be DescTrack's own page.
        origin = request.headers.get("Origin")
        if origin is not None:
            parts = urlsplit(origin)
            if parts.scheme != "http" or parts.netloc.lower() != host.lower():
                abort(403)
        # 3. Bodies must be JSON, which cross-origin pages can't send
        #    without a CORS preflight (never granted here).
        if request.method not in ("GET", "HEAD", "OPTIONS") and not request.is_json:
            abort(415)

    @app.after_request
    def _security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    @app.errorhandler(BadRequest)
    def _bad_request(exc):
        return jsonify({"error": str(exc)}), 400

    @app.errorhandler(403)
    def _forbidden(_exc):
        return jsonify({"error": "Forbidden."}), 403

    @app.errorhandler(415)
    def _unsupported(_exc):
        return jsonify({"error": "Requests must be sent as JSON."}), 415

    # -- Frontend -------------------------------------------------------

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    # -- Environment ----------------------------------------------------

    @app.get("/api/status")
    def status():
        ffmpeg_path = ffmpeg_locator.find_ffmpeg()
        ffprobe_path = ffmpeg_locator.find_ffprobe()
        version = ffmpeg_safety.get_version(ffmpeg_path) if ffmpeg_path else None
        return jsonify({
            "ffmpeg_found": bool(ffmpeg_path),
            "ffprobe_found": bool(ffprobe_path),
            "ffmpeg_path": ffmpeg_path,
            "ffprobe_path": ffprobe_path,
            "ffmpeg_version": ".".join(map(str, version)) if version else None,
            "ffmpeg_outdated": bool(ffmpeg_path) and ffmpeg_safety.is_outdated(ffmpeg_path),
            "ffmpeg_recommended_version": ".".join(map(str, ffmpeg_safety.RECOMMENDED_MIN_VERSION)),
        })

    # -- Probing / sync ---------------------------------------------------

    @app.post("/api/probe")
    def probe():
        data = _json_body()
        path = data.get("path", "")
        if not isinstance(path, str) or not os.path.isabs(path) or not os.path.isfile(path):
            return jsonify({"error": f'File not found: "{path}"'}), 400
        ffprobe_path = ffmpeg_locator.find_ffprobe()
        if not ffprobe_path:
            return jsonify({"error": _FFPROBE_MISSING}), 500
        try:
            duration = media_probe.get_duration_seconds(ffprobe_path, path)
        except media_probe.ProbeError as exc:
            return jsonify({"error": str(exc)}), 500
        return jsonify({"duration_seconds": duration})

    @app.post("/api/auto-sync")
    def auto_sync():
        data = _json_body()
        video_path = _media_path(data, "video_path")
        audio_path = _media_path(data, "audio_path")
        ffmpeg_path = ffmpeg_locator.find_ffmpeg()
        if not ffmpeg_path:
            return jsonify({"error": _FFMPEG_MISSING}), 500
        try:
            result = audio_sync.estimate_offset(ffmpeg_path, video_path, audio_path)
        except audio_sync.AudioSyncError as exc:
            return jsonify({"error": str(exc)}), 500
        return jsonify({"offset_ms": result.offset_ms, "confidence": result.confidence})

    @app.post("/api/detect-start")
    def detect_start():
        data = _json_body()
        video_path = _media_path(data, "video_path")
        audio_path = _media_path(data, "audio_path")
        ffmpeg_path = ffmpeg_locator.find_ffmpeg()
        if not ffmpeg_path:
            return jsonify({"error": _FFMPEG_MISSING}), 500
        try:
            # Scan both files at the same time - each is a separate ffmpeg process.
            with ThreadPoolExecutor(max_workers=2) as pool:
                video_job = pool.submit(silence.detect_content_start_seconds, ffmpeg_path, video_path)
                ad_job = pool.submit(silence.detect_content_start_seconds, ffmpeg_path, audio_path)
                video_start = video_job.result()
                ad_start = ad_job.result()
        except Exception as exc:
            return jsonify({"error": f"Silence detection failed: {exc}"}), 500
        # Start the preview where *both* files have audible content.
        return jsonify({
            "video_start_s": video_start,
            "ad_start_s": ad_start,
            "suggested_position_s": max(video_start, ad_start),
        })

    # -- Sync preview ---------------------------------------------------

    @app.post("/api/preview/prepare")
    def preview_prepare():
        data = _json_body()
        video_path = _media_path(data, "video_path")
        audio_path = _media_path(data, "audio_path")
        position_s = _number(data, "position_s", 0, 0, MAX_POSITION_SECONDS)
        duration_s = _number(data, "duration_s", 15, 0.5, MAX_PREVIEW_SECONDS)
        offset_ms = int(_number(data, "offset_ms", 0, -MAX_OFFSET_MS, MAX_OFFSET_MS))

        ffmpeg_path = ffmpeg_locator.find_ffmpeg()
        if not ffmpeg_path:
            return jsonify({"error": _FFMPEG_MISSING}), 500
        try:
            clips = preview.prepare_preview_clips(
                ffmpeg_path, video_path, audio_path, position_s, duration_s, offset_ms
            )
        except preview.PreviewError as exc:
            return jsonify({"error": str(exc)}), 500

        token = uuid.uuid4().hex
        with _PREVIEW_LOCK:
            # Only one preview is ever "live" at a time; drop the previous
            # pair of temp files before registering the new ones.
            for old_path in _CURRENT_PREVIEW_CLIPS.values():
                preview.cleanup_clip(old_path)
            _CURRENT_PREVIEW_CLIPS.clear()
            _CURRENT_PREVIEW_CLIPS[f"{token}:video"] = clips.video_clip_path
            _CURRENT_PREVIEW_CLIPS[f"{token}:ad"] = clips.ad_clip_path

        return jsonify({
            "token": token,
            "video_url": f"/api/preview/clip/{token}/video",
            "ad_url": f"/api/preview/clip/{token}/ad",
        })

    @app.get("/api/preview/clip/<token>/<which>")
    def preview_clip(token: str, which: str):
        key = f"{token}:{which}"
        with _PREVIEW_LOCK:
            path = _CURRENT_PREVIEW_CLIPS.get(key)
        if not path or not os.path.isfile(path):
            return jsonify({"error": "Preview clip not found or expired."}), 404
        response = send_file(path, mimetype="audio/wav", conditional=False)
        response.headers["Cache-Control"] = "no-store"
        return response

    # -- Render ---------------------------------------------------------

    @app.post("/api/output-path")
    def output_path():
        data = _json_body()
        video_path = data.get("video_path", "")
        chromecast = _bool(data, "chromecast")
        if not isinstance(video_path, str) or not video_path or not os.path.isabs(video_path):
            return jsonify({"error": "video_path is required."}), 400
        try:
            out_path = mux.build_output_path(video_path, chromecast)
        except mux.MuxError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"output_path": out_path, "exists": os.path.isfile(out_path)})

    @app.post("/api/create")
    def create():
        data = _json_body()
        video_path = _media_path(data, "video_path")
        audio_path = _media_path(data, "audio_path")
        offset_ms = int(_number(data, "offset_ms", 0, -MAX_OFFSET_MS, MAX_OFFSET_MS))
        chromecast = _bool(data, "chromecast")

        ffmpeg_path = ffmpeg_locator.find_ffmpeg()
        ffprobe_path = ffmpeg_locator.find_ffprobe()
        if not ffmpeg_path or not ffprobe_path:
            return jsonify({"error": _BOTH_MISSING}), 500

        try:
            out_path = mux.build_output_path(video_path, chromecast)
            duration = media_probe.get_duration_seconds(ffprobe_path, video_path)
        except (mux.MuxError, media_probe.ProbeError) as exc:
            return jsonify({"error": str(exc)}), 400

        # Never write over either of the inputs.
        if os.path.normcase(out_path) in {os.path.normcase(video_path), os.path.normcase(audio_path)}:
            return jsonify({"error": "The output file would replace one of the input files."}), 400

        _prune_finished_jobs()
        job_id = uuid.uuid4().hex
        job = JobState(output_path=out_path)
        with _JOBS_LOCK:
            JOBS[job_id] = job

        req = mux.MuxRequest(
            ffmpeg_path=ffmpeg_path,
            video_path=video_path,
            ad_audio_path=audio_path,
            output_path=out_path,
            offset_ms=offset_ms,
            video_duration_seconds=duration,
            chromecast_ready=chromecast,
        )

        def run_job():
            try:
                mux.run_mux(
                    req,
                    log_cb=job.append_log,
                    progress_cb=job.set_progress,
                    cancel_event=job.cancel_event,
                )
                with job.lock:
                    job.status = "done"
            except mux.MuxError as exc:
                with job.lock:
                    job.status = "cancelled" if job.cancel_event.is_set() else "error"
                    job.error = str(exc)
            except Exception as exc:
                with job.lock:
                    job.status = "error"
                    job.error = f"Unexpected error: {exc}"

        thread = threading.Thread(target=run_job, daemon=True)
        thread.start()

        return jsonify({"job_id": job_id})

    @app.get("/api/job/<job_id>")
    def job_status(job_id: str):
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Unknown job id."}), 404
        return jsonify(job.snapshot(include_log=request.args.get("log") == "1"))

    @app.post("/api/job/<job_id>/cancel")
    def job_cancel(job_id: str):
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "Unknown job id."}), 404
        job.cancel_event.set()
        return jsonify({"ok": True})

    # -- Desktop integration ----------------------------------------------

    @app.post("/api/reveal")
    def reveal():
        data = _json_body()
        path = data.get("path", "")
        if not isinstance(path, str) or not path or not os.path.isfile(path):
            return jsonify({"error": "File not found."}), 404
        # Only ever open the folder of a file DescTrack itself produced.
        with _JOBS_LOCK:
            known_outputs = {os.path.normcase(j.output_path) for j in JOBS.values()
                             if j.status == "done" and j.output_path}
        if os.path.normcase(os.path.abspath(path)) not in known_outputs:
            return jsonify({"error": "File not found."}), 404
        path = os.path.abspath(path)
        try:
            if platform.system() == "Windows":
                subprocess.Popen(["explorer.exe", f"/select,{path}"])
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", "-R", path])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
        except OSError as exc:
            return jsonify({"error": f"Could not open the output folder: {exc}"}), 500
        return jsonify({"ok": True})

    return app


if __name__ == "__main__":
    app = create_app()
    port = int(os.environ.get("PORT", "5891"))
    debug = "--debug" in sys.argv
    if debug:
        print("WARNING: --debug enables Werkzeug's interactive debugger. Use it for development only.")
    app.run(host="127.0.0.1", port=port, threaded=True, debug=debug, use_reloader=False)
