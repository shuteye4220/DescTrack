# DescTrack

DescTrack puts an audio-described soundtrack onto a video. You give it a video of a film, TV show or documentary (from a DVD, for example) and an MP3 or other audio file of the described version of the same programme. DescTrack works out how far apart the two are, lets you check the sync by ear, and makes a new copy of the video with the described soundtrack in place of the original one.

It was made by and for blind people. The whole program is an accessible web page in its own window, used with the keyboard and a screen reader such as NVDA, JAWS, Narrator, VoiceOver or Orca.

DescTrack runs on Windows, macOS and Linux. Everything happens on your own computer; your files are not sent anywhere.

## What you can do

- Process one video or a whole batch: add one pair (a video and its described audio) per programme, then start them all at once.
- Automatic sync: as soon as both files of a pair are chosen, DescTrack compares the two soundtracks and works out the offset between them. This is a best guess, and you can always adjust it.
- The sync mixer: play a short preview of the video's own sound and the described audio together, at the current offset, so you can hear whether they line up. Nudge the offset with the arrow keys (hold a key down to move in bigger steps) or type an exact value in milliseconds. The preview starts after any silence at the beginning of either file, so there is always something to listen to.
- Keep the original format: by default only the soundtrack is replaced. The picture is copied as it is, so this is quick and nothing is lost.
- Chromecast ready: optionally re-encode the video to H.264 in an MP4 file, which plays on a Chromecast and most other devices. This takes much longer.
- Progress, time remaining and an activity log for each video, a short tone when each one finishes, and a button to open the folder with the finished file.

The new file is saved next to the original video, with `-AD` added to its name. For example `Pride and Prejudice.mkv` becomes `Pride and Prejudice-AD.mkv`.

## What you need

### ffmpeg

DescTrack uses ffmpeg, a free program that reads, cuts and joins audio and video, to do all of its work. It needs both of the ffmpeg programs: `ffmpeg` and `ffprobe`. ffmpeg is not included in DescTrack's downloads; install it as shown below.

DescTrack looks for ffmpeg in this order:

1. In the same folder as DescTrack (or in a folder called `ffmpeg` inside it). On a Mac this is the folder that holds `DescTrack.app`.
2. Anywhere on your PATH, which is where installing ffmpeg normally puts it.
3. On a Mac or Linux, in the usual install folders even when they are not on the PATH: `/opt/homebrew/bin`, `/usr/local/bin`, `/opt/local/bin` and `/snap/bin`. This matters on a Mac, where programs started from the Finder do not see Homebrew's folder otherwise.

If DescTrack cannot find ffmpeg, a message at the top of the page says so.

Any recent ffmpeg works. On Windows it needs the MP3 and AAC encoders, which every ordinary Windows build has.

### The window

DescTrack's page is shown with the web view built into your system:

- Windows: Microsoft Edge WebView2. It is already part of Windows 10 and 11.
- macOS: the system WebKit (the same engine as Safari). Nothing to install.
- Linux: WebKitGTK. Install it as described below. DescTrack uses GTK rather than Qt on Linux because WebKitGTK pages work well with Orca.

## Installing

Download DescTrack for your system from the Releases page of this project.

### Windows

1. Download `DescTrack-windows.zip` and unzip it into a folder of your choice.
2. Install ffmpeg. Either:
   - download a Windows build from https://www.gyan.dev/ffmpeg/builds/ (the "release essentials" zip is enough), open its `bin` folder and copy `ffmpeg.exe` and `ffprobe.exe` into the same folder as `DescTrack.exe`; or
   - install it with `winget install Gyan.FFmpeg` in a command prompt.
3. Start `DescTrack.exe`. The DescTrack window opens.

Windows may warn that the program is from an unknown publisher, because it is not signed. Choose "More info", then "Run anyway".

JAWS users: if you cannot get into the page, route the JAWS cursor to the PC cursor (Insert+Numpad Minus), then do a left mouse click (Numpad Slash). From then on you can use the virtual PC cursor as usual.

### Mac

1. Install ffmpeg. The easiest way is Homebrew (https://brew.sh): `brew install ffmpeg`. Or download `ffmpeg` and `ffprobe` from https://evermeet.cx/ffmpeg/ and put them in the same folder as `DescTrack.app`.
2. Download `DescTrack-mac.zip` and unzip it. Move `DescTrack.app` wherever you like, for example to Applications.
3. Open `DescTrack.app`. The first time, macOS will say it cannot verify the developer. Choose Done, then open System Settings, Privacy and Security, and near the bottom choose "Open Anyway" next to the message about DescTrack. After that it opens normally. If you downloaded ffmpeg and ffprobe by hand, you may need to do the same for each of them the first time DescTrack uses them.

The download is built for Macs with Apple silicon (M1 or newer). On an Intel Mac, run DescTrack from source; see Building from source.

### Linux

1. Install ffmpeg and WebKitGTK from your distribution:
   - Arch Linux: `sudo pacman -S ffmpeg webkit2gtk-4.1`
   - Debian and Ubuntu: `sudo apt install ffmpeg gir1.2-webkit2-4.1`
   - Fedora: `sudo dnf install ffmpeg webkit2gtk4.1` (ffmpeg is in RPM Fusion)
2. Download `DescTrack-linux-x86_64.tar.gz`, unpack it, and run `./DescTrack`. Or build it yourself; see Building from source.

## Using it

The page is divided by headings, so you can move between sections with your screen reader's heading keys.

1. Under "Video / AD audio pairs", choose "Choose video file" and pick the video, then "Choose audio description file" and pick the described audio. DescTrack syncs the pair automatically and reports the offset it found, and how confident it is, in the activity log (which screen readers announce as it updates). To add more videos to the batch, choose "Add another pair".
2. To check a pair by ear, choose "Open sync mixer". "Play preview with current offset" plays the two soundtracks together; if the described version sounds early or late, Tab to the offset control and nudge it with the Left and Right arrow keys, or type an exact offset, and play the preview again. "Auto-sync" makes a fresh best guess. Press Enter to keep the offset, or Escape to close the mixer.
3. Under "Output", check "Chromecast ready" if you want H.264 MP4 files.
4. Choose "Start process". Progress for each video and the activity log are under "Render progress". When it is done, "Open last output's folder" opens the folder with the new file.

About the offset: a positive offset delays the described audio relative to the video; a negative offset trims the start of the described audio so it begins earlier. If the described file has an extra 1.5 seconds of silence or introduction at the start, the offset is -1500 ms.

## Building from source

You need Python 3.10 or newer (the downloads are built with Python 3.14) and ffmpeg.

On Linux you also need WebKitGTK and the GObject introspection headers to install PyGObject:

- Arch Linux: `sudo pacman -S webkit2gtk-4.1 gobject-introspection cairo pkgconf`
- Debian and Ubuntu (24.04 or newer): `sudo apt install gir1.2-webkit2-4.1 gir1.2-girepository-3.0 libgirepository-2.0-dev libcairo2-dev pkg-config`

Then, in the DescTrack folder:

```
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

python desktop.py                  # run DescTrack from source
python -m pytest                   # run the tests (needs ffmpeg)
pyinstaller desctrack.spec         # build the app into dist/
```

`pyinstaller desctrack.spec` makes `dist/DescTrack.exe` on Windows, `dist/DescTrack` on Linux and `dist/DescTrack.app` on a Mac. PyInstaller can only build for the system it runs on, so each one must be built on that system.

Every push to GitHub runs the tests and builds all three on GitHub's Windows, Mac and Linux machines, and checks that each built app starts. Pushing a tag such as `v1.2.1` also attaches the three downloads to a new release.

### Testing in a browser

`python server.py` starts just the part of DescTrack that does the work, at http://127.0.0.1:5891, without its window, so you can use the page in an ordinary browser (`python server.py --debug` turns on Flask's debug mode; set the `PORT` environment variable to use another port). A browser does not tell a web page where a chosen file is on disk, so in a browser the file buttons are only a best effort; the page has a test hook, `window.__setPairsForTesting([{video: "/path/to/video.mp4", audio: "/path/to/described.mp3"}])`, for filling in real paths from the developer console or an automated test.

The tests in `tests/` make short test media with ffmpeg and run the whole process through the real program: probing, auto-sync, silence detection, preview clips, and making the final file in both modes.

## How it works

- `desktop.py` starts the program and opens its window with pywebview. It also provides the native file dialogs, which give the page the real location of each file so the result can be saved next to the video.
- `server.py` is a small Flask web server that the page talks to. All of the work happens in the `services` folder:
  - `audio_sync.py` takes the first 90 seconds of both soundtracks at low quality and finds the offset where they match best (cross-correlation with numpy), within 20 seconds either way.
  - `silence.py` uses ffmpeg's silence detection to find where the sound starts in each file, so previews start somewhere audible.
  - `preview.py` cuts matching short clips from both files at the chosen offset for the sync mixer.
  - `mux.py` runs ffmpeg to make the final file, and reports its progress.
  - `media_probe.py` and `ffmpeg_locator.py` find each file's length and the ffmpeg programs.
- The page itself is in the `static` folder.

## Licence and credits

DescTrack is released under the MIT licence; see the LICENSE file.

DescTrack is built with:

- [ffmpeg](https://ffmpeg.org), which does all the audio and video work (LGPL or GPL licence depending on the build), installed separately.
- [pywebview](https://pywebview.flowrl.com) for the window (BSD licence).
- [Flask](https://flask.palletsprojects.com) for the built-in web server (BSD licence).
- [NumPy](https://numpy.org) for the automatic sync (BSD licence).
- [Requests](https://requests.readthedocs.io) (Apache 2.0 licence).
- [PyGObject](https://pygobject.gnome.org) and WebKitGTK on Linux (LGPL licence).
- [PyInstaller](https://pyinstaller.org) to build the downloads (GPL licence with an exception that allows building programs under any licence).
