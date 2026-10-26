"""End-to-end tests for the DescTrack backend.

These drive the real Flask app and the real ffmpeg, using short media files
generated on the fly, so they check that the whole pipeline (probe, auto
sync, silence detection, preview clips and the final mux) works on the
platform the tests run on. ffmpeg and ffprobe must be installed.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import create_app  # noqa: E402
from services import ffmpeg_locator, mux  # noqa: E402

FFMPEG = ffmpeg_locator.find_ffmpeg()
FFPROBE = ffmpeg_locator.find_ffprobe()

pytestmark = pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not installed")

# The AD track in the test media starts this much later than the video's
# own soundtrack.
AD_DELAY_SECONDS = 1.5


def _ffmpeg(*args: str) -> None:
    subprocess.run([FFMPEG, "-v", "error", "-y", *args], check=True)


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    folder = tmp_path_factory.mktemp("media")
    programme = folder / "programme.wav"
    video = folder / "video.mp4"
    ad = folder / "described.mp3"

    # A short tone then 20 s of pink noise: distinctive enough to correlate.
    _ffmpeg(
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-f", "lavfi", "-i", "anoisesrc=d=20:c=pink:a=0.3:seed=7",
        "-filter_complex", "[0][1]concat=n=2:v=0:a=1[a]", "-map", "[a]",
        str(programme),
    )
    _ffmpeg(
        "-f", "lavfi", "-i", "testsrc=duration=22:size=160x120:rate=10",
        "-i", str(programme),
        "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(video),
    )
    # The "described" version: same soundtrack, but with leading silence.
    _ffmpeg(
        "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={AD_DELAY_SECONDS}",
        "-i", str(programme),
        "-filter_complex", "[0][1]concat=n=2:v=0:a=1[a]", "-map", "[a]",
        "-c:a", "libmp3lame", str(ad),
    )
    return {"video": str(video), "ad": str(ad)}


@pytest.fixture()
def client():
    app = create_app()
    app.testing = True
    return app.test_client()


def test_index_and_status(client):
    assert client.get("/").status_code == 200
    status = client.get("/api/status").get_json()
    assert status["ffmpeg_found"] and status["ffprobe_found"]


def test_probe(client, media):
    data = client.post("/api/probe", json={"path": media["video"]}).get_json()
    assert data["duration_seconds"] == pytest.approx(22, abs=0.5)


def test_probe_missing_file(client):
    response = client.post("/api/probe", json={"path": "/no/such/file.mp4"})
    assert response.status_code == 400


def test_auto_sync_finds_offset(client, media):
    data = client.post("/api/auto-sync", json={"video_path": media["video"], "audio_path": media["ad"]}).get_json()
    # ad_time = video_time - offset, and the AD runs late, so offset is negative.
    assert data["offset_ms"] == pytest.approx(-AD_DELAY_SECONDS * 1000, abs=50)
    assert data["confidence"] > 0.5


def test_detect_start(client, media):
    data = client.post("/api/detect-start", json={"video_path": media["video"], "audio_path": media["ad"]}).get_json()
    assert data["video_start_s"] == pytest.approx(0, abs=0.3)
    assert data["ad_start_s"] == pytest.approx(AD_DELAY_SECONDS, abs=0.3)


def test_preview_clips(client, media):
    data = client.post("/api/preview/prepare", json={
        "video_path": media["video"], "audio_path": media["ad"],
        "position_s": 5, "duration_s": 3, "offset_ms": -1500,
    }).get_json()
    for key in ("video_url", "ad_url"):
        response = client.get(data[key])
        assert response.status_code == 200
        assert response.mimetype == "audio/wav"
        assert response.data[:4] == b"RIFF"
        response.close()


@pytest.mark.parametrize("chromecast", [False, True])
def test_create_output(client, media, chromecast):
    out = client.post("/api/output-path", json={"video_path": media["video"], "chromecast": chromecast}).get_json()
    assert out["output_path"].endswith("video-AD.mp4")

    job_id = client.post("/api/create", json={
        "video_path": media["video"], "audio_path": media["ad"],
        "offset_ms": -1500, "chromecast": chromecast,
    }).get_json()["job_id"]

    deadline = time.time() + 120
    while time.time() < deadline:
        job = client.get(f"/api/job/{job_id}").get_json()
        if job["status"] != "running":
            break
        time.sleep(0.25)
    assert job["status"] == "done", job.get("error")
    assert job["percent"] == 100

    streams = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "stream=codec_type,codec_name", "-of", "csv=p=0", out["output_path"]],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    assert any("video" in s for s in streams) and any("audio" in s for s in streams)
    if chromecast:
        assert "h264,video" in streams


def test_mux_args_offset_direction():
    def args_for(offset_ms):
        return mux._build_args(mux.MuxRequest("ffmpeg", "v.mkv", "a.mp3", "v-AD.mkv", offset_ms, 60.0, False))

    late = args_for(2000)
    assert late[late.index("-itsoffset") + 1] == "2.0"
    early = args_for(-2000)
    assert early[early.index("-ss") + 1] == "2.0"


def test_progress_parsing():
    assert mux._parse_out_time_seconds("out_time=01:02:03.500000") == pytest.approx(3723.5)
    assert mux._parse_out_time_seconds("out_time=N/A") is None
    assert mux._parse_speed("speed=2.5x") == 2.5
    assert mux._parse_speed("speed=N/A") is None


# -- Security ---------------------------------------------------------------


def test_cross_site_text_plain_post_is_rejected(client, media):
    # What any web page can send to 127.0.0.1 without a CORS preflight.
    response = client.post(
        "/api/create",
        data=json.dumps({"video_path": media["video"], "audio_path": media["ad"]}),
        headers={"Content-Type": "text/plain"},
    )
    assert response.status_code == 415


def test_foreign_origin_is_rejected(client):
    response = client.post("/api/output-path", json={"video_path": "/x.mp4"},
                           headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_null_origin_is_rejected(client):
    response = client.post("/api/output-path", json={"video_path": "/x.mp4"}, headers={"Origin": "null"})
    assert response.status_code == 403


def test_dns_rebinding_host_is_rejected(client):
    response = client.get("/api/status", headers={"Host": "evil.example:5891"})
    assert response.status_code == 403


def test_own_origin_is_allowed(client):
    response = client.post("/api/output-path", json={"video_path": "/x.mp4"},
                           headers={"Host": "127.0.0.1:5891", "Origin": "http://127.0.0.1:5891"})
    assert response.status_code == 200


@pytest.mark.parametrize("bad", ["abc", None, True, float("nan"), float("inf"), 10**9])
def test_bad_numbers_are_rejected(client, media, bad):
    body = json.dumps({"video_path": media["video"], "audio_path": media["ad"], "offset_ms": bad},
                      allow_nan=True)
    response = client.post("/api/preview/prepare", data=body, content_type="application/json")
    assert response.status_code == 400


def test_relative_paths_are_rejected(client):
    response = client.post("/api/auto-sync", json={"video_path": "-i", "audio_path": "video.mp4"})
    assert response.status_code == 400


def test_reveal_only_opens_desctrack_outputs(client, media):
    response = client.post("/api/reveal", json={"path": media["video"]})
    assert response.status_code == 404


def test_unexpected_formats_are_refused(tmp_path):
    from services import media_probe

    # A VobSub index disguised as an MP4: without the format whitelist,
    # ffmpeg would hand this to the VobSub demuxer (CVE-2026-64830).
    fake = tmp_path / "movie.mp4"
    fake.write_text("# VobSub index file, v7\n")
    with pytest.raises(media_probe.ProbeError):
        media_probe.get_duration_seconds(FFPROBE, str(fake))

    # An HLS playlist that would make ffmpeg fetch a URL.
    playlist = tmp_path / "playlist.m3u8"
    playlist.write_text("#EXTM3U\n#EXTINF:10,\nhttp://127.0.0.1:9/x.ts\n#EXT-X-ENDLIST\n")
    with pytest.raises(media_probe.ProbeError):
        media_probe.get_duration_seconds(FFPROBE, str(playlist))


def test_failed_render_leaves_no_partial_file(tmp_path, media):
    out = tmp_path / "out-AD.mp4"
    req = mux.MuxRequest(FFMPEG, media["video"], media["ad"], str(out), 0, 22.0, False)
    # Corrupt the request so ffmpeg fails after creating the output.
    req.ad_audio_path = str(tmp_path / "missing.mp3")
    with pytest.raises(mux.MuxError):
        mux.run_mux(req)
    assert not out.exists()


def test_cancelled_render_leaves_no_partial_file(tmp_path, media):
    import threading

    out = tmp_path / "out-AD.mp4"
    cancel = threading.Event()
    cancel.set()
    req = mux.MuxRequest(FFMPEG, media["video"], media["ad"], str(out), 0, 22.0, True)
    with pytest.raises(mux.MuxError, match="Cancelled"):
        mux.run_mux(req, cancel_event=cancel)
    assert not out.exists()


def test_job_poll_omits_log_unless_asked(client, media):
    job_id = client.post("/api/create", json={
        "video_path": media["video"], "audio_path": media["ad"], "offset_ms": 0, "chromecast": False,
    }).get_json()["job_id"]
    assert "log" not in client.get(f"/api/job/{job_id}").get_json()
    assert "log" in client.get(f"/api/job/{job_id}?log=1").get_json()
    deadline = time.time() + 60
    while client.get(f"/api/job/{job_id}").get_json()["status"] == "running" and time.time() < deadline:
        time.sleep(0.25)


def test_ffmpeg_version_is_reported(client):
    status = client.get("/api/status").get_json()
    assert "ffmpeg_outdated" in status and status["ffmpeg_recommended_version"] == "9.0.1"
