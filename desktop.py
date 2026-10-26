"""Desktop launcher: runs the Flask app in a background thread and shows it
in a native window via pywebview. This is the only module that assumes it's
running on an end-user's desktop (Windows, macOS or Linux) - server.py has
no dependency on this file and can be developed and tested entirely on its
own.

Native file-open dialogs are exposed to the page's JavaScript as
``window.pywebview.api.pick_video()`` / ``pick_audio()``, which return a
real filesystem path (unlike a plain browser's <input type=file>, which
only exposes a filename). That's what lets the output file be auto-saved
next to the source video without asking the user to separately confirm a
folder.
"""

from __future__ import annotations

import os
import threading

import webview
from werkzeug.serving import make_server

from server import create_app

HOST = "127.0.0.1"

VIDEO_FILE_TYPES = ("Video files (*.mp4;*.mkv;*.mov;*.avi;*.webm;*.m4v;*.wmv)", "All files (*.*)")
AUDIO_FILE_TYPES = ("Audio files (*.wav;*.mp3;*.aac;*.flac;*.m4a;*.ogg;*.wma)", "All files (*.*)")


class Api:
    """Methods here are callable from the page as window.pywebview.api.<name>(...)."""

    def _pick(self, file_types):
        window = webview.windows[0]
        # pywebview 5+ uses webview.FileDialog.OPEN; older releases only
        # have the (now deprecated) webview.OPEN_DIALOG constant.
        file_dialog = getattr(webview, "FileDialog", None)
        open_dialog = file_dialog.OPEN if file_dialog else webview.OPEN_DIALOG
        result = window.create_file_dialog(open_dialog, allow_multiple=False, file_types=file_types)
        if not result:
            return None
        return result[0]

    def pick_video(self):
        return self._pick(VIDEO_FILE_TYPES)

    def pick_audio(self):
        return self._pick(AUDIO_FILE_TYPES)


def _start_server():
    """Starts the Flask app on a free local port and returns the server.

    The listening socket is bound before this returns, so the window can load
    the page straight away (no polling). Letting the OS pick the port means
    DescTrack never collides with - or is impersonated by - another program
    that happens to be using a fixed port. Set DESCTRACK_PORT to force one.
    """
    port = int(os.environ.get("DESCTRACK_PORT", "0"))
    server = make_server(HOST, port, create_app(), threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> None:
    server = _start_server()
    base_url = f"http://{HOST}:{server.server_port}"
    print(f"DescTrack is running at {base_url}", flush=True)

    api = Api()
    webview.create_window(
        "DescTrack - Audio Description Muxer",
        base_url,
        js_api=api,
        width=900,
        height=800,
        min_size=(720, 640),
    )
    try:
        webview.start()
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
