#!/usr/bin/env python3
"""
server.py - local web UI for the downloader.

Paste a URL, pick a destination folder, press Download. The job runs in the
background: fetch best audio -> split on chapters if the upload has them ->
convert to MP3 320 -> embed artwork. The destination folder is created if it
does not exist.

    python server.py
    python server.py --root "C:/Users/Public/AppleMusic" --port 8800
    python server.py --host 0.0.0.0 --token secret123      # exposing it

Security: binds to 127.0.0.1 by default and refuses to write outside --root.
If you put this behind a tunnel, set --token; without one, anyone who reaches
the page can download arbitrary URLs onto your machine.
"""

import argparse
import functools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HERE = Path(__file__).resolve().parent
JOBS = {}
JOBS_LOCK = threading.Lock()
CONFIG = {}

# Python block-buffers stdout when it is redirected to a file, which is how the
# logon launcher runs this - without flushing, the log stays empty.
print = functools.partial(print, flush=True)                     # noqa: A001


# ---------------------------------------------------------------- helpers

CONFIG_FILE = HERE / "config.local.json"


def load_config():
    """Read config.local.json if it's there. Gitignored, so it's a safe home for
    tokens - keeps them off the command line and out of shell history."""
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"!! Ignoring {CONFIG_FILE.name}: {e}\n")
        return {}


def find_exe(name):
    exe = shutil.which(name)
    if exe:
        return exe
    local = os.environ.get("LOCALAPPDATA")
    if local:
        hits = sorted(Path(local).glob(
            f"Microsoft/WinGet/Packages/Gyan.FFmpeg*/**/bin/{name}.exe"))
        if hits:
            return str(hits[-1])
    return None


def find_deno():
    """Locate deno, which YouTube extraction now needs.

    YouTube requires executing JavaScript to solve the signature and n
    challenges. Without a runtime yt-dlp still lists formats, but the media URLs
    it hands back are rejected with HTTP 403 - the failure looks like a download
    problem rather than a missing dependency. winget puts deno on PATH, but a
    shell started before the install won't see it.
    """
    exe = shutil.which("deno")
    if exe:
        return exe
    local = os.environ.get("LOCALAPPDATA")
    if local:
        for pat in ("Microsoft/WinGet/Packages/DenoLand.Deno*/**/deno.exe",
                    "../../.deno/bin/deno.exe"):
            hits = sorted(Path(local).glob(pat))
            if hits:
                return str(hits[-1])
    home = Path.home() / ".deno" / "bin" / "deno.exe"
    return str(home) if home.exists() else None


def safe_target(folder):
    """Resolve a user-supplied folder, refusing anything outside --root.

    The folder box is free text, so without this a request could write to
    C:/Windows or anywhere else the process can reach.
    """
    root = Path(CONFIG["root"]).resolve()
    raw = (folder or "").strip().strip('"')
    if not raw:
        raise ValueError("No destination folder given")
    p = Path(raw)
    target = (root / p).resolve() if not p.is_absolute() else p.resolve()
    if root not in target.parents and target != root:
        raise ValueError(f"Destination must be inside {root}")
    return target


def list_dirs(rel):
    """Directory listing under --root, for the in-page folder browser.

    Everything is expressed relative to root so the browser can't be walked out
    of it, and so the same paths work whether you're at the PC or remote.
    """
    root = Path(CONFIG["root"]).resolve()
    target = safe_target(rel) if (rel or "").strip() else root
    if not target.is_dir():
        raise ValueError(f"Not a folder: {target}")
    dirs = sorted((d.name for d in target.iterdir()
                   if d.is_dir() and not d.name.startswith(".")), key=str.lower)
    rel_here = "" if target == root else target.relative_to(root).as_posix()
    parent = (None if target == root
              else ("" if target.parent == root
                    else target.parent.relative_to(root).as_posix()))
    return {"path": rel_here, "parent": parent, "dirs": dirs,
            "absolute": str(target), "root": str(root)}


def native_pick(initial):
    """Open a real Explorer folder dialog on the machine running this server.

    Only useful when you're sitting at that machine - called remotely, the dialog
    would appear on the host with nobody there to answer it, so the UI only offers
    this for local requests. Runs off-thread with a timeout so a dialog nobody
    closes can't wedge the server.
    """
    try:
        import tkinter
        from tkinter import filedialog
    except Exception as e:                                       # noqa: BLE001
        return None, f"No GUI toolkit available: {e}"

    out = {}

    def show():
        try:
            r = tkinter.Tk()
            r.withdraw()
            r.attributes("-topmost", True)
            out["path"] = filedialog.askdirectory(
                initialdir=initial, title="Choose destination folder", mustexist=False)
            r.destroy()
        except Exception as e:                                   # noqa: BLE001
            out["error"] = str(e)

    t = threading.Thread(target=show, daemon=True)
    t.start()
    t.join(timeout=180)
    if t.is_alive():
        return None, "Dialog timed out - it may still be open on the PC"
    if out.get("error"):
        return None, out["error"]
    return (out.get("path") or ""), None


def log(job, line):
    line = line.rstrip()
    if not line:
        return
    with JOBS_LOCK:
        job["log"].append(line)
        del job["log"][:-400]          # keep the tail, not the whole history


def run(job, cmd, phase):
    """Run a subprocess, streaming its output into the job log."""
    with JOBS_LOCK:
        job["phase"] = phase
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, encoding="utf-8", errors="replace")
    for line in proc.stdout:
        log(job, line)
        m = re.search(r"(\d{1,3}(?:\.\d)?)%", line)
        if m:
            with JOBS_LOCK:
                job["percent"] = min(100, float(m.group(1)))
    proc.wait()
    return proc.returncode


def write_cue(path, chapters, meta):
    """Turn chapter markers into a cuesheet split_tracks.py can consume."""
    def stamp(sec):
        h, rem = divmod(float(sec), 3600)
        m, s = divmod(rem, 60)
        return f"{int(h)}:{int(m):02d}:{s:05.2f}" if h else f"{int(m)}:{s:05.2f}"

    lines = ["# generated by server.py from chapter markers", ""]
    for k in ("ALBUM", "ARTIST", "DATE"):
        lines.append(f"{k}: {meta.get(k, '')}")
    lines.append("")
    for i, c in enumerate(chapters):
        title = (c.get("title") or f"Track {i + 1:02d}").strip()
        lines.append(f"{stamp(c['start_time'])} - {stamp(c['end_time'])}    {title}")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- the job

def run_job(job):
    try:
        target = safe_target(job["folder"])
        target.mkdir(parents=True, exist_ok=True)
        with JOBS_LOCK:
            job["output"] = str(target)
        log(job, f"Destination: {target}")

        # Stage everything outside the destination. A watched folder -
        # "Automatically Add to Apple Music" is exactly that - gets swept by
        # Apple Music the instant anything appears, including half-finished
        # downloads and scratch metadata. Only completed tracks may land there.
        staging = Path(tempfile.mkdtemp(prefix="scdl-"))
        work = staging / "work"
        work.mkdir()
        out = staging / "out"
        out.mkdir()

        # 1. download best audio, plus the metadata and thumbnail in one pass
        ytdlp = [sys.executable, "-m", "yt_dlp",
                 "-f", "bestaudio/best",
                 "-o", str(work / "%(title)s.%(ext)s"),
                 "--write-info-json", "--write-thumbnail",
                 "--no-playlist" if not job["playlist"] else "--yes-playlist",
                 "--newline"]
        # YouTube 403s the media URLs unless the JS challenges are solved, which
        # needs a runtime plus the solver script (an opt-in download).
        deno = find_deno()
        if deno:
            ytdlp += ["--js-runtimes", f"deno:{deno}",
                      "--remote-components", "ejs:github"]
        else:
            log(job, "!! deno not found - YouTube downloads will fail with 403.")
            log(job, "   Install it:  winget install DenoLand.Deno")
        rc = run(job, ytdlp + [job["url"]], "Downloading")
        if rc != 0:
            raise RuntimeError("Download failed - see log")

        info_files = sorted(work.glob("*.info.json"))
        if not info_files:
            raise RuntimeError("No metadata written; download may have failed")
        info = json.loads(info_files[0].read_text(encoding="utf-8", errors="replace"))

        audio = next((f for f in sorted(work.iterdir())
                      if f.suffix.lower() in {".m4a", ".webm", ".opus", ".mp3",
                                              ".ogg", ".flac", ".wav"}), None)
        if not audio:
            raise RuntimeError("Downloaded file not found")
        log(job, f"Got: {audio.name}")

        # Fall back to the uploader only when nothing better was given - a YouTube
        # channel name is rarely the artist you want in a library.
        title = info.get("title") or audio.stem
        album = job["album"] or info.get("album") or title
        artist = job["artist"] or info.get("artist") or info.get("uploader") or ""
        date = job["date"] or str(info.get("release_year") or "")
        chapters = info.get("chapters") or []

        # 2. chapters -> split into tracks; otherwise keep it as one file
        if chapters and job["split"]:
            log(job, f"{len(chapters)} chapters found - splitting")
            cue = work / "cue.txt"
            write_cue(cue, chapters, {"ALBUM": album, "ARTIST": artist, "DATE": date})
            rc = run(job, [sys.executable, str(HERE / "split_tracks.py"), str(audio),
                           "--cue", str(cue), "--format", job["format"],
                           "-q", str(job["bitrate"]), "-o", str(out)], "Splitting")
            if rc != 0:
                raise RuntimeError("Split failed - see log")
        else:
            if job["split"] and not chapters:
                log(job, "No chapters on this upload - keeping as a single track")
            ffmpeg = find_exe("ffmpeg")
            if not ffmpeg:
                raise RuntimeError("ffmpeg not found")
            dest = out / f"{re.sub(r'[<>:\"/\\\\|?*]', '_', title)}.{job['format']}"
            with JOBS_LOCK:
                job["phase"] = "Converting"
            cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                   "-i", str(audio), "-map", "0:a"]
            if job["format"] != "flac":
                cmd += ["-b:a", f"{job['bitrate']}k"]
            cmd += ["-metadata", f"title={title}"]
            for tag, val in (("artist", artist), ("album", album), ("date", date)):
                if val:
                    cmd += ["-metadata", f"{tag}={val}"]
            cmd.append(str(dest))
            if subprocess.run(cmd, capture_output=True).returncode != 0:
                raise RuntimeError("Conversion failed")
            log(job, f"Wrote {dest.name}")

        # 3. artwork, from the thumbnail we already pulled down
        thumb = next((f for f in work.iterdir()
                      if f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}), None)
        if thumb:
            rc = run(job, [sys.executable, str(HERE / "add_art.py"), str(thumb),
                           "-d", str(out)], "Artwork")
            if rc != 0:
                log(job, "Artwork step failed - tracks are still fine")
        else:
            log(job, "No thumbnail available; skipping artwork")

        # Move finished files in last, so a watched destination only ever sees
        # complete, tagged tracks - never a partial one it would import broken.
        with JOBS_LOCK:
            job["phase"] = "Moving"
        made = []
        for f in sorted(out.iterdir()):
            if f.is_file():
                shutil.move(str(f), str(target / f.name))
                made.append(f.name)
        shutil.rmtree(staging, ignore_errors=True)
        with JOBS_LOCK:
            job.update(status="done", phase="Done", percent=100,
                       files=made, finished=time.time())
        log(job, f"Finished - {len([f for f in made if not f.endswith('.jpg')])} track(s)")

    except Exception as e:                                  # noqa: BLE001
        with JOBS_LOCK:
            job.update(status="error", phase="Failed", error=str(e),
                       finished=time.time())
        log(job, f"ERROR: {e}")


# ---------------------------------------------------------------- http

class Server(ThreadingHTTPServer):
    """HTTPServer sets allow_reuse_address = 1, which on Windows lets a second
    process bind a port that is already in use. Both servers then live, and
    requests go to whichever the OS picks. That is a security problem here: start
    a tokenless copy, start a tokenised one over it, and the tokenless one may
    still be answering while everything looks protected. Refuse to share a port.
    """
    allow_reuse_address = False


class Handler(BaseHTTPRequestHandler):
    server_version = "tapedeck"

    def log_message(self, *a):
        pass                                                # quiet console

    def _send(self, code, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _is_local(self):
        """True only if the browser is on this machine.

        The peer address is not enough: cloudflared runs here and connects over
        loopback, so a request tunnelled from anywhere in the world arrives from
        127.0.0.1. Cloudflare stamps these headers on the way through, so their
        presence means the request came from outside.
        """
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            return False
        return not any(self.headers.get(h) for h in
                       ("CF-Connecting-IP", "CF-Ray", "X-Forwarded-For",
                        "X-Forwarded-Host", "X-Real-IP"))

    def _authed(self):
        token = CONFIG.get("token")
        if not token:
            return True
        sent = (self.headers.get("X-Token") or "").strip()
        if sent == token:
            return True
        self._send(401, json.dumps({"error": "bad or missing token"}))
        return False

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            page = HERE / "web" / "index.html"
            if not page.exists():
                return self._send(500, "web/index.html is missing", "text/plain")
            return self._send(200, page.read_bytes(), "text/html; charset=utf-8")

        if path == "/api/config":
            return self._send(200, json.dumps({
                "root": CONFIG["root"],
                "needsToken": bool(CONFIG.get("token")),
                "formats": ["mp3", "m4a", "flac"],
                # A native dialog opens on the machine running this server, so it
                # is only offered when the browser is on that same machine.
                "canNativePick": self._is_local(),
            }))

        if path == "/api/browse":
            if not self._authed():
                return
            rel = (parse_qs(urlparse(self.path).query).get("path") or [""])[0]
            try:
                return self._send(200, json.dumps(list_dirs(rel)))
            except ValueError as e:
                return self._send(400, json.dumps({"error": str(e)}))

        if path.startswith("/api/jobs"):
            if not self._authed():
                return
            jid = path.rsplit("/", 1)[-1]
            with JOBS_LOCK:
                if jid and jid != "jobs":
                    job = JOBS.get(jid)
                    if not job:
                        return self._send(404, json.dumps({"error": "no such job"}))
                    return self._send(200, json.dumps(job))
                recent = sorted(JOBS.values(), key=lambda j: -j["started"])[:20]
                return self._send(200, json.dumps([
                    {k: v for k, v in j.items() if k != "log"} for j in recent]))

        self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        path = self.path.split("?")[0]
        if path not in ("/api/jobs", "/api/mkdir", "/api/pick"):
            return self._send(404, json.dumps({"error": "not found"}))
        if not self._authed():
            return

        if path == "/api/mkdir":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                b = json.loads(self.rfile.read(n) or b"{}")
                name = re.sub(r'[<>:"/\\|?*]', "_", (b.get("name") or "").strip())
                if not name:
                    raise ValueError("Give the folder a name")
                target = safe_target(f"{(b.get('path') or '').strip()}/{name}".strip("/"))
                target.mkdir(parents=True, exist_ok=True)
                root = Path(CONFIG["root"]).resolve()
                return self._send(200, json.dumps(
                    {"path": target.relative_to(root).as_posix()}))
            except (ValueError, json.JSONDecodeError) as e:
                return self._send(400, json.dumps({"error": str(e)}))
            except OSError as e:
                return self._send(400, json.dumps({"error": f"Could not create: {e}"}))

        if path == "/api/pick":
            if not self._is_local():
                return self._send(400, json.dumps({
                    "error": "A system dialog only works when you're at the PC. "
                             "Use Browse instead."}))
            chosen, err = native_pick(CONFIG["root"])
            if err:
                return self._send(500, json.dumps({"error": err}))
            if not chosen:
                return self._send(200, json.dumps({"cancelled": True}))
            try:
                target = safe_target(chosen)
            except ValueError as e:
                return self._send(400, json.dumps({"error": str(e)}))
            root = Path(CONFIG["root"]).resolve()
            rel = "" if target == root else target.relative_to(root).as_posix()
            return self._send(200, json.dumps({"path": rel, "absolute": str(target)}))
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._send(400, json.dumps({"error": "bad JSON"}))

        url = (body.get("url") or "").strip()
        if not re.match(r"^https?://", url):
            return self._send(400, json.dumps({"error": "Enter a http(s) URL"}))
        try:
            safe_target(body.get("folder"))                 # validate before queueing
        except ValueError as e:
            return self._send(400, json.dumps({"error": str(e)}))

        job = {
            "id": uuid.uuid4().hex[:12],
            "url": url,
            "folder": (body.get("folder") or "").strip(),
            "artist": (body.get("artist") or "").strip(),
            "album": (body.get("album") or "").strip(),
            "date": (body.get("date") or "").strip(),
            "format": body.get("format") if body.get("format") in
                      ("mp3", "m4a", "flac") else "mp3",
            "bitrate": int(body.get("bitrate") or 320),
            "split": bool(body.get("split", True)),
            "playlist": bool(body.get("playlist", False)),
            "status": "running", "phase": "Starting", "percent": 0.0,
            "log": [], "files": [], "output": "", "error": "",
            "started": time.time(), "finished": 0,
        }
        with JOBS_LOCK:
            JOBS[job["id"]] = job
        threading.Thread(target=run_job, args=(job,), daemon=True).start()
        return self._send(200, json.dumps({"id": job["id"]}))


def main():
    cfg = load_config()
    p = argparse.ArgumentParser(
        description="Local web UI for the downloader.",
        epilog="Values default to config.local.json when present; flags override it.")
    p.add_argument("--host", default=cfg.get("host", "127.0.0.1"),
                   help="bind address (default: 127.0.0.1, local only)")
    p.add_argument("--port", type=int, default=int(cfg.get("port", 8800)))
    p.add_argument("--root", default=cfg.get("root") or str(Path.home() / "Music"),
                   help="destination folders must live under this (default: ~/Music)")
    p.add_argument("--token", default=cfg.get("token"),
                   help="require this token in an X-Token header")
    args = p.parse_args()

    root = Path(args.root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    CONFIG.update(root=str(root), token=args.token)

    if not find_exe("ffmpeg"):
        print("!! ffmpeg not found - conversion will fail.\n"
              "   winget install Gyan.FFmpeg\n")
    if args.host not in ("127.0.0.1", "::1") and not args.token:
        # A warning is not enough. This endpoint downloads arbitrary URLs onto
        # the host and writes files under --root; unauthenticated on a reachable
        # interface is not a state anyone should arrive at by accident.
        sys.exit(f"\n!! Refusing to bind {args.host} with no --token.\n"
                 f"   Anyone who could reach this page would be able to download\n"
                 f"   arbitrary URLs onto this machine and write files under --root.\n\n"
                 f"   Give it one:  python server.py --host {args.host} --token <SECRET>\n"
                 f"   Or leave it on 127.0.0.1 and put a tunnel in front (see README).\n")

    # Bind before printing anything reassuring. A silent bind failure is genuinely
    # dangerous here: an older instance keeps serving on the port, and if that one
    # was started without --token you end up believing a tokenless server is
    # protected.
    try:
        httpd = Server((args.host, args.port), Handler)
    except OSError as e:
        sys.exit(f"\n!! Could not bind {args.host}:{args.port} - {e}\n"
                 f"   Something else is already listening, most likely an older\n"
                 f"   copy of this server. That one keeps answering, with whatever\n"
                 f"   token settings it was started with. Find and stop it:\n\n"
                 f"     Get-NetTCPConnection -LocalPort {args.port} -State Listen\n"
                 f"     Stop-Process -Id <pid> -Force\n\n"
                 f"   Or start this one on a different --port.\n")

    print(f"  Root  : {root}")
    print(f"  Auth  : {'token required' if args.token else 'NONE - anyone who can reach this can use it'}")
    print(f"  Open  : http://{'localhost' if args.host == '127.0.0.1' else args.host}"
          f":{args.port}\n  Ctrl+C to stop.\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
