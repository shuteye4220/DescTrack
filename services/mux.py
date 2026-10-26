"""Final ffmpeg mux/transcode: replaces a video's audio with an AD track at
a chosen offset, either preserving the original format or re-encoding to
H.264/MP4 for Chromecast.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional, TypedDict

from .ffmpeg_safety import input_args


class MuxError(RuntimeError):
    pass


@dataclass
class MuxRequest:
    ffmpeg_path: str
    video_path: str
    ad_audio_path: str
    output_path: str
    offset_ms: int
    video_duration_seconds: float
    chromecast_ready: bool


def build_output_path(video_path: str, chromecast_ready: bool) -> str:
    """Same folder as the source video, '-AD' appended before the
    extension; extension forced to .mp4 when re-encoding for Chromecast."""
    directory = os.path.dirname(os.path.abspath(video_path))
    if not directory:
        raise MuxError("Could not determine the source video's folder.")
    name_no_ext = os.path.splitext(os.path.basename(video_path))[0]
    if chromecast_ready:
        ext = ".mp4"
    else:
        ext = os.path.splitext(video_path)[1] or ".mp4"
    return os.path.join(directory, f"{name_no_ext}-AD{ext}")


def _audio_codec_args(output_path: str) -> list[str]:
    ext = os.path.splitext(output_path)[1].lower()
    if ext == ".avi":
        return ["-c:a", "libmp3lame", "-b:a", "192k"]
    if ext == ".webm":
        return ["-c:a", "libopus", "-b:a", "160k"]
    return ["-c:a", "aac", "-b:a", "192k"]


def _build_args(req: MuxRequest) -> list[str]:
    if req.offset_ms >= 0:
        ad_input_args = ["-itsoffset", str(req.offset_ms / 1000.0), *input_args(req.ad_audio_path)]
    else:
        ad_input_args = ["-ss", str(-req.offset_ms / 1000.0), *input_args(req.ad_audio_path)]

    if req.chromecast_ready:
        video_codec_args = ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p"]
    else:
        video_codec_args = ["-c:v", "copy"]

    audio_codec_args = _audio_codec_args(req.output_path)
    faststart = ["-movflags", "+faststart"] if req.chromecast_ready else []

    return (
        ["-nostdin", "-y", "-progress", "pipe:1", "-nostats", *input_args(req.video_path)]
        + ad_input_args
        + ["-map", "0:v:0", "-map", "1:a:0"]
        + video_codec_args
        + audio_codec_args
        + ["-af", "apad", "-t", str(req.video_duration_seconds)]
        + faststart
        + [os.path.abspath(req.output_path)]
    )


_OUT_TIME_RE = re.compile(r"^out_time=(.+)$")
_SPEED_RE = re.compile(r"^speed=\s*([\d.]+)x\s*$")


class ProgressInfo(TypedDict):
    percent: int
    elapsed_seconds: float
    eta_seconds: Optional[float]
    speed: Optional[float]


def _parse_out_time_seconds(line: str) -> Optional[float]:
    match = _OUT_TIME_RE.match(line)
    if not match:
        return None
    value = match.group(1).strip()
    parts = value.split(":")
    if len(parts) != 3:
        return None
    try:
        hours, minutes, seconds = float(parts[0]), float(parts[1]), float(parts[2])
    except ValueError:
        return None
    return hours * 3600 + minutes * 60 + seconds


def _parse_speed(line: str) -> Optional[float]:
    match = _SPEED_RE.match(line)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def run_mux(
    req: MuxRequest,
    log_cb: Optional[Callable[[str], None]] = None,
    progress_cb: Optional[Callable[[ProgressInfo], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> None:
    args = [req.ffmpeg_path] + _build_args(req)
    if log_cb:
        log_cb("Running: " + " ".join(args))

    process = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        bufsize=1,
    )

    # The last few lines ffmpeg printed, to explain a failure.
    stderr_tail: deque[str] = deque(maxlen=8)

    def pump_stderr():
        assert process.stderr is not None
        for line in process.stderr:
            line = line.rstrip()
            if not line:
                continue
            stderr_tail.append(line)
            if log_cb:
                log_cb(line)

    stderr_thread = threading.Thread(target=pump_stderr, daemon=True)
    stderr_thread.start()

    start_time = time.monotonic()
    last_out_time_seconds = 0.0
    last_speed = None

    def report_progress() -> None:
        if progress_cb is None or req.video_duration_seconds <= 0:
            return
        pct = max(0, min(100, int(last_out_time_seconds / req.video_duration_seconds * 100)))
        eta_seconds = None
        if last_speed and last_speed > 0:
            remaining_source_seconds = max(0.0, req.video_duration_seconds - last_out_time_seconds)
            eta_seconds = remaining_source_seconds / last_speed
        progress_cb({
            "percent": pct,
            "elapsed_seconds": time.monotonic() - start_time,
            "eta_seconds": eta_seconds,
            "speed": last_speed,
        })

    succeeded = False
    try:
        assert process.stdout is not None
        for raw_line in process.stdout:
            if cancel_event is not None and cancel_event.is_set():
                raise MuxError("Cancelled.")

            line = raw_line.strip()

            out_time = _parse_out_time_seconds(line)
            if out_time is not None:
                last_out_time_seconds = out_time
                continue

            speed = _parse_speed(line)
            if speed is not None:
                last_speed = speed
                continue

            # ffmpeg's -progress output arrives in blocks of key=value lines,
            # each block terminated by "progress=continue" (or "progress=end"
            # for the last one). Report once per block, after out_time and
            # speed for that block have both been read.
            if line.startswith("progress="):
                report_progress()

        process.wait()
        stderr_thread.join(timeout=2)

        if process.returncode != 0:
            details = "\n".join(stderr_tail)
            raise MuxError(
                f"ffmpeg exited with code {process.returncode}."
                + (f" Its last messages were:\n{details}" if details else "")
            )
        succeeded = True
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if not succeeded:
            # Don't leave a truncated "-AD" file behind: it would look like a
            # finished output and trigger an "already exists" warning next time.
            try:
                if os.path.isfile(req.output_path):
                    os.remove(req.output_path)
            except OSError:
                pass

    last_out_time_seconds = req.video_duration_seconds
    if progress_cb:
        progress_cb({
            "percent": 100,
            "elapsed_seconds": time.monotonic() - start_time,
            "eta_seconds": 0.0,
            "speed": last_speed,
        })
