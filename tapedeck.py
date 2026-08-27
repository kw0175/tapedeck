#!/usr/bin/env python3
"""
tapedeck.py - download audio from any site yt-dlp supports, as MP3 (or other formats).

Wraps yt-dlp (extraction) + ffmpeg (transcode/tagging). Handles single tracks,
playlists/sets, whole user profiles, and private share links.

Examples:
    python tapedeck.py https://soundcloud.com/artist/track-name
    python tapedeck.py URL1 URL2 URL3 -o "D:/Music"
    python tapedeck.py --batch urls.txt --archive done.txt
    python tapedeck.py https://soundcloud.com/artist/sets/my-set --playlist-folder
    python tapedeck.py --format best URL          # keep original file, no transcode
    python tapedeck.py --cookies-from-browser chrome URL   # your own private tracks
"""

import argparse
import os
import shutil
import sys
from pathlib import Path

try:
    import yt_dlp
except ImportError:
    sys.exit("yt-dlp is not installed.  Run:  pip install -r requirements.txt")


DEFAULT_TEMPLATE = "%(uploader)s - %(title)s.%(ext)s"
PLAYLIST_TEMPLATE = "%(playlist)s/%(playlist_index)02d - %(uploader)s - %(title)s.%(ext)s"


def find_ffmpeg():
    """Locate ffmpeg on PATH, falling back to the WinGet install location.

    winget adds ffmpeg to PATH, but shells opened before the install don't see it
    until they're restarted. Without this, yt-dlp silently skips remuxing and
    warns about DASH containers and malformed AAC timestamps.
    """
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    local = os.environ.get("LOCALAPPDATA")
    if local:
        hits = sorted(Path(local).glob(
            "Microsoft/WinGet/Packages/Gyan.FFmpeg*/**/bin/ffmpeg.exe"))
        if hits:
            return str(hits[-1])
    return None


def read_batch(path):
    """One URL per line. Blank lines and #-comments ignored."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


class Progress:
    """Single-line progress reporting + a tally of what succeeded/failed."""

    def __init__(self):
        self.done = []
        self.failed = []
        # Overwriting a line with \r only works on a real terminal. When output is
        # piped or redirected to a log, every refresh would land as its own line
        # (hundreds of KB for one track), so fall back to sparse milestone lines.
        self.tty = sys.stdout.isatty()
        self._last_milestone = -1

    def _line(self, text, transient=False):
        if self.tty:
            print(f"\r{text}", end="" if transient else "\n", flush=True)
        elif not transient:
            print(text, flush=True)

    def hook(self, d):
        status = d.get("status")
        if status == "downloading":
            pct = d.get("_percent_str", "").strip()
            speed = d.get("_speed_str", "").strip()
            name = Path(d.get("filename", "")).name[:60]
            if self.tty:
                self._line(f"  {pct:>6} {speed:>11}  {name}", transient=True)
            else:
                # Non-TTY: one line per 10% so logs stay readable.
                frac = d.get("_percent") or 0
                if not frac and d.get("total_bytes"):
                    frac = 100 * (d.get("downloaded_bytes", 0) / d["total_bytes"])
                milestone = int(frac // 10)
                if milestone > self._last_milestone:
                    self._last_milestone = milestone
                    print(f"  {milestone * 10:>3}%  {name}", flush=True)
        elif status == "finished":
            self._last_milestone = -1
            self._line(f"  {'100%':>6} {'':>11}  downloaded, converting...", transient=True)

    def pp_hook(self, d):
        # Fires after each postprocessor; the MoveFiles one runs last and carries
        # the true final path once the extension has been rewritten to .mp3 etc.
        if d.get("status") == "finished" and d.get("postprocessor") == "MoveFiles":
            info = d.get("info_dict", {})
            final = info.get("filepath") or info.get("_filename")
            if final:
                self._line(f"  {'done':>6} {'':>11}  {Path(final).name}")
                self.done.append(final)


def build_opts(args, progress):
    outdir = Path(args.out).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    template = args.template or (PLAYLIST_TEMPLATE if args.playlist_folder else DEFAULT_TEMPLATE)

    opts = {
        "outtmpl": str(outdir / template),
        "format": "bestaudio/best",
        "windowsfilenames": True,          # strips characters NTFS rejects
        "trim_file_name": 180,             # keep well under MAX_PATH
        "ignoreerrors": True,              # one dead track shouldn't kill a 200-track set
        "quiet": True,
        "noprogress": True,                # we render our own progress line
        "progress_hooks": [progress.hook],
        "postprocessor_hooks": [progress.pp_hook],
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 4,
        "postprocessors": [],
    }

    # Hand yt-dlp the resolved path so it can remux even when ffmpeg isn't on PATH.
    ffmpeg = find_ffmpeg()
    if ffmpeg:
        opts["ffmpeg_location"] = str(Path(ffmpeg).parent)

    if args.archive:
        # yt-dlp records each track id here and silently skips it next run.
        opts["download_archive"] = str(Path(args.archive).expanduser().resolve())

    if args.cookies_from_browser:
        opts["cookiesfrombrowser"] = (args.cookies_from_browser,)
    if args.cookies:
        opts["cookiefile"] = str(Path(args.cookies).expanduser().resolve())

    if args.limit:
        opts["playlistend"] = args.limit

    if args.format != "best":
        opts["postprocessors"].append({
            "key": "FFmpegExtractAudio",
            "preferredcodec": args.format,
            "preferredquality": str(args.bitrate),
        })

    if not args.no_metadata:
        opts["postprocessors"].append({
            "key": "FFmpegMetadata",
            "add_metadata": True,
        })

    if not args.no_cover:
        # writethumbnail pulls the artwork; EmbedThumbnail folds it into the tag
        # and deletes the loose .jpg.
        opts["writethumbnail"] = True
        opts["postprocessors"].append({
            "key": "EmbedThumbnail",
            "already_have_thumbnail": False,
        })

    return opts


def main():
    p = argparse.ArgumentParser(
        description="Download SoundCloud tracks, sets, or profiles as MP3.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("urls", nargs="*", help="SoundCloud track/set/user URLs")
    p.add_argument("-b", "--batch", metavar="FILE",
                   help="text file with one URL per line (# comments ok)")
    p.add_argument("-o", "--out", default="downloads",
                   help="output directory (default: ./downloads)")
    p.add_argument("-f", "--format", default="mp3",
                   choices=["mp3", "m4a", "opus", "flac", "wav", "best"],
                   help="output format; 'best' keeps the original file untouched "
                        "(default: mp3)")
    p.add_argument("-q", "--bitrate", type=int, default=320,
                   help="target bitrate in kbps for lossy formats (default: 320)")
    p.add_argument("--playlist-folder", action="store_true",
                   help="put sets in their own subfolder, numbered in order")
    p.add_argument("--template",
                   help="override the yt-dlp output template entirely")
    p.add_argument("--archive", metavar="FILE",
                   help="track ids already downloaded; skips them on reruns")
    p.add_argument("--limit", type=int, metavar="N",
                   help="only take the first N items of a playlist/profile")
    p.add_argument("--cookies-from-browser", metavar="BROWSER",
                   choices=["chrome", "firefox", "edge", "brave", "opera", "vivaldi", "safari"],
                   help="pull login cookies from a browser (for your own private tracks)")
    p.add_argument("--cookies", metavar="FILE",
                   help="Netscape-format cookies.txt instead of a live browser")
    p.add_argument("--no-cover", action="store_true", help="don't embed artwork")
    p.add_argument("--no-metadata", action="store_true", help="don't write title/artist tags")
    args = p.parse_args()

    urls = list(args.urls)
    if args.batch:
        urls += read_batch(args.batch)
    if not urls:
        p.error("no URLs given (pass them as arguments or use --batch FILE)")

    if args.format != "best" and not find_ffmpeg():
        sys.exit(
            "ffmpeg not found on PATH, and it's required to convert to "
            f"{args.format}.\n"
            "  Windows:  winget install Gyan.FFmpeg     (reopen your terminal after)\n"
            "  macOS:    brew install ffmpeg\n"
            "  Linux:    sudo apt install ffmpeg\n"
            "Or rerun with --format best to skip conversion entirely."
        )

    progress = Progress()
    opts = build_opts(args, progress)

    print(f"Output : {Path(args.out).expanduser().resolve()}")
    print(f"Format : {args.format}" + (f" @ {args.bitrate}kbps" if args.format != "best" else ""))
    print(f"URLs   : {len(urls)}\n")

    with yt_dlp.YoutubeDL(opts) as ydl:
        for i, url in enumerate(urls, 1):
            print(f"[{i}/{len(urls)}] {url}")
            try:
                # ignoreerrors makes yt-dlp return non-zero rather than raise on
                # per-track failures inside a playlist.
                code = ydl.download([url])
                if code != 0:
                    progress.failed.append(url)
            except yt_dlp.utils.DownloadError as e:
                print(f"\r  FAILED  {e}")
                progress.failed.append(url)
            except KeyboardInterrupt:
                print("\n\nInterrupted.")
                break
            print()

    print(f"\nDownloaded {len(progress.done)} file(s).")
    if progress.failed:
        print(f"{len(progress.failed)} URL(s) had failures:")
        for u in progress.failed:
            print(f"  - {u}")
        print("\nCommon causes: the track is private or Go+ only, was deleted, or is "
              "geo-blocked. For your own private tracks try --cookies-from-browser chrome.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
