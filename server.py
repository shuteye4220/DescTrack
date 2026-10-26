"""DescTrack Flask backend.

This module is the entire application's business-logic surface: it is
deliberately kept importable and runnable on its own (``python server.py``)
so it can be developed and tested without the desktop shell (see
desktop.py, which is the pywebview launcher used for the packaged desktop
app). All routes are JSON APIs except the two that serve the frontend and
preview audio clips.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from typing import Optional

from flask import Flask, jsonify, request, send_file, send_from_directory

from services import ffmpeg_locator, media_probe, audio_sync, preview, mux, silence

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# Shown when ffmpeg / ffprobe can't be found next to the app or on PATH.
_FFMPEG_MISSING = "ffmpeg was not found. See README.md."
_FFPROBE_MISSING = "ffprobe was not found. See README.md."
_BOTH_MISSING = "ffmpeg / ffprobe were not found. See README.md."


@dataclass
class JobState:
    status: str = "running"  # running | done | error | cancelled
    percent: int = 0
    elapsed_seconds: float = 0.0
    eta_seconds: Optional[float] = None
    speed: Optional[float] = None
    log: list[str] = field(default_factory=list)
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

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "status": self.status,
                "percent": self.percent,
                "elapsed_seconds": self.elapsed_seconds,
                "eta_seconds": self.eta_seconds,
                "speed": self.speed,
                "log": list(self.log),
                "output_path": self.output_path,
                "error": self.error,
            }


JOBS: dict[str, JobState] = {}
_CURRENT_PREVIEW_CLIPS: dict[str, str] = {}
_PREVIEW_LOCK = threading.Lock()


def create_app() -> Flask:
    app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")

    # -- Frontend -------------------------------------------------------

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    # -- Environment ----------------------------------------------------

    @app.get("/api/status")
    def status():
        ffmpeg_path = ffmpeg_locator.find_ffmpeg()
        ffprobe_path = ffmpeg_locator.find_ffprobe()
        return jsonify({
            "ffmpeg_found": bool(ffmpeg_path),
            "ffprobe_found": bool(ffprobe_path),
            "ffmpeg_path": ffmpeg_path,
            "ffprobe_path": ffprobe_path,
        })

    # -- Probing / sync ---------------------------------------------------

    @app.post("/api/probe")
    def probe():
        data = request.get_json(force=True) or {}
        path = data.get("path", "")
        if not path or not os.path.isfile(path):
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
        data = request.get_json(force=True) or {}
        video_path = data.get("video_path", "")
        audio_path = data.get("audio_path", "")
        if not os.path.isfile(video_path) or not os.path.isfile(audio_path):
            return jsonify({"error": "Both a video file and an audio description file are required."}), 400
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
        data = request.get_json(force=True) or {}
        video_path = data.get("video_path", "")
        audio_path = data.get("audio_path", "")
        if not os.path.isfile(video_path) or not os.path.isfile(audio_path):
            return jsonify({"error": "Both a video file and an audio description file are required."}), 400
        ffmpeg_path = ffmpeg_locator.find_ffmpeg()
        if not ffmpeg_path:
            return jsonify({"error": _FFMPEG_MISSING}), 500
        try:
            video_start = silence.detect_content_start_seconds(ffmpeg_path, video_path)
            ad_start = silence.detect_content_start_seconds(ffmpeg_path, audio_path)
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
        data = request.get_json(force=True) or {}
        video_path = data.get("video_path", "")
        audio_path = data.get("audio_path", "")
        position_s = float(data.get("position_s", 0))
        duration_s = float(data.get("duration_s", 15))
        offset_ms = int(data.get("offset_ms", 0))

        if not os.path.isfile(video_path) or not os.path.isfile(audio_path):
            return jsonify({"error": "Both a video file and an audio description file are required."}), 400
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
        data = request.get_json(force=True) or {}
        video_path = data.get("video_path", "")
        chromecast = bool(data.get("chromecast", False))
        if not video_path:
            return jsonify({"error": "video_path is required."}), 400
        try:
            out_path = mux.build_output_path(video_path, chromecast)
        except mux.MuxError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"output_path": out_path, "exists": os.path.isfile(out_path)})

    @app.post("/api/create")
    def create():
        data = request.get_json(force=True) or {}
        video_path = data.get("video_path", "")
        audio_path = data.get("audio_path", "")
        offset_ms = int(data.get("offset_ms", 0))
        chromecast = bool(data.get("chromecast", False))

        if not os.path.isfile(video_path) or not os.path.isfile(audio_path):
            return jsonify({"error": "Both a video file and an audio description file are required."}), 400

        ffmpeg_path = ffmpeg_locator.find_ffmpeg()
        ffprobe_path = ffmpeg_locator.find_ffprobe()
        if not ffmpeg_path or not ffprobe_path:
            return jsonify({"error": _BOTH_MISSING}), 500

        try:
            out_path = mux.build_output_path(video_path, chromecast)
            duration = media_probe.get_duration_seconds(ffprobe_path, video_path)
        except (mux.MuxError, media_probe.ProbeError) as exc:
            return jsonify({"error": str(exc)}), 400

        job_id = uuid.uuid4().hex
        job = JobState(output_path=out_path)
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
        return jsonify(job.snapshot())

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
        data = request.get_json(force=True) or {}
        path = data.get("path", "")
        if not path or not os.path.isfile(path):
            return jsonify({"error": "File not found."}), 404
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
    app.run(host="127.0.0.1", port=port, threaded=True, debug=debug, use_reloader=False)
