#!/usr/bin/env python3
"""
app.py - tapedeck in a desktop window.

Same server and same page as `python server.py`, wrapped in a native window
instead of a browser tab. Nothing is exposed: the server binds to loopback on a
random free port, so there is no fixed address to reach and no token to type.

    python app.py
"""

import socket
import sys
import threading
import webbrowser
from pathlib import Path

import server


def free_port():
    """Ask the OS for an unused port rather than guessing 8800 and colliding
    with a copy of server.py the user already has running."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def missing_tools():
    """Report anything the jobs will need, so it is visible at launch rather
    than surfacing later as a cryptic download failure."""
    missing = []
    if not server.find_exe("ffmpeg"):
        missing.append(("ffmpeg", "winget install Gyan.FFmpeg",
                        "required to convert or split anything"))
    if not server.find_deno():
        missing.append(("deno", "winget install DenoLand.Deno",
                        "required for YouTube, which 403s without it"))
    return missing


def main():
    cfg = server.load_config()
    root = Path(cfg.get("root") or (Path.home() / "Music")).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    # No token. The listener is on 127.0.0.1 on an ephemeral port, so nothing
    # off this machine can reach it and there is no shared secret to protect.
    server.CONFIG.update(root=str(root), token=None,
                         cookiesFromBrowser=cfg.get("cookiesFromBrowser"))

    port = free_port()
    httpd = server.Server(("127.0.0.1", port), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"

    for name, cmd, why in missing_tools():
        print(f"!! {name} not found - {why}\n   {cmd}")

    try:
        import webview
    except ImportError:
        # Still usable without the GUI toolkit; fall back rather than dying.
        print(f"pywebview not installed - opening in your browser instead.\n  {url}")
        webbrowser.open(url)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        return 0

    webview.create_window("tapedeck", url, width=980, height=880,
                          min_size=(720, 600))
    webview.start()          # blocks until the window is closed
    httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
