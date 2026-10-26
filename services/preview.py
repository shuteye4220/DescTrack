"""Extracts short WAV clips (via ffmpeg) from the video's own audio and from
the AD audio, at whatever positions correspond to the currently chosen sync
offset. The frontend plays them back with the Web Audio API, scheduled to
start at the same instant, so a blind user can listen for drift before
committing to the final render.

Because the extraction already bakes in the offset, no playback-time delay
is needed on the client side - both clips just start together.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import uuid
from dataclasses import dataclass


class PreviewError(RuntimeError):
    pass


@dataclass
class PreviewClips:
    video_clip_path: str
    ad_clip_path: str


def prepare_preview_clips(
    ffmpeg_path: str,
    video_path: str,
    ad_audio_path: str,
    position_seconds: float,
    duration_seconds: float,
    offset_ms: int,
) -> PreviewClips:
    video_start = max(0.0, position_seconds)
    ad_start = max(0.0, position_seconds - offset_ms / 1000.0)

    video_clip = _clip_path("video")
    ad_clip = _clip_path("ad")

    _extract_clip(ffmpeg_path, video_path, video_start, duration_seconds, video_clip)
    _extract_clip(ffmpeg_path, ad_audio_path, ad_start, duration_seconds, ad_clip)

    return PreviewClips(video_clip_path=video_clip, ad_clip_path=ad_clip)


def _clip_path(label: str) -> str:
    return os.path.join(tempfile.gettempdir(), f"desctrack_preview_{label}_{uuid.uuid4().hex}.wav")


def _extract_clip(ffmpeg_path: str, source_path: str, start_seconds: float, duration_seconds: float, output_path: str) -> None:
    args = [
        ffmpeg_path, "-y",
        "-ss", str(start_seconds),
        "-i", source_path,
        "-t", str(duration_seconds),
        "-vn", "-ac", "2", "-ar", "44100",
        "-f", "wav",
        output_path,
    ]
    result = subprocess.run(args, capture_output=True, text=True, errors="replace")
    if result.returncode != 0 or not os.path.isfile(output_path):
        raise PreviewError(f'ffmpeg could not extract a preview clip from "{source_path}":\n{result.stderr}')


def cleanup_clip(path: str | None) -> None:
    if not path:
        return
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass
