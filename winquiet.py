#!/usr/bin/env python3
"""
winquiet - stop child processes flashing console windows on Windows.

A windowed process (pythonw, or the packaged exe) has no console of its own, so
Windows opens a brand new one for every child it spawns. ffmpeg and ffprobe run
constantly during a job - once per track to split, again to embed art, again to
probe - so a download turned into a stream of black windows appearing and
vanishing.

CREATE_NO_WINDOW tells Windows not to allocate one. Rather than thread that flag
through fifteen call sites, patch it in once here: import this module and every
subprocess call in the process inherits it, including those inside yt-dlp when
it runs in-process.

No effect on other platforms.
"""

import subprocess
import sys

CREATE_NO_WINDOW = 0x08000000


def _install():
    if sys.platform != "win32" or getattr(subprocess, "_winquiet", False):
        return
    _run, _popen = subprocess.run, subprocess.Popen

    def run(*args, **kw):
        kw["creationflags"] = kw.get("creationflags", 0) | CREATE_NO_WINDOW
        return _run(*args, **kw)

    # Popen must stay a class. yt-dlp does `class Popen(subprocess.Popen)`, and
    # swapping in a function makes that subclass raise TypeError at import - the
    # whole downloader fails to load. Subclassing keeps that working, and
    # yt-dlp's own Popen then inherits the flag too.
    class Popen(_popen):
        def __init__(self, *args, **kw):
            kw["creationflags"] = kw.get("creationflags", 0) | CREATE_NO_WINDOW
            super().__init__(*args, **kw)

    subprocess.run = run
    subprocess.Popen = Popen
    subprocess._winquiet = True


_install()
