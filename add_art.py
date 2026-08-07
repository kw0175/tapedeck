#!/usr/bin/env python3
"""
add_art.py - embed album artwork into a folder of audio files.

Normalises any image to a square cover and writes it into every track's tags,
without re-encoding the audio.

    python add_art.py cover.jpg -d "Artist - Album"
    python add_art.py --url https://.../artwork.jpg -d "Artist - Album"
    python add_art.py --from-track https://soundcloud.com/user/track -d "Artist - Album"

Non-square input is centre-cropped to the largest square it contains, then scaled
to --size (default 500). Players expect square art; anything else gets letterboxed
or stretched at display time.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

AUDIO_EXT = {".flac", ".m4a", ".mp3", ".ogg", ".opus"}


def find_exe(name):
    """Locate ffmpeg/ffprobe on PATH, falling back to the WinGet install location."""
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


def fetch(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "add_art/1.0"})
    with urllib.request.urlopen(req) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    return dest


def thumbnail_of(track_url):
    """Ask yt-dlp for a track's artwork URL."""
    out = subprocess.run(
        [sys.executable, "-m", "yt_dlp", "--simulate", "--print", "%(thumbnail)s",
         track_url], capture_output=True, text=True)
    for line in reversed(out.stdout.strip().splitlines()):
        if line.startswith("http"):
            return line.strip()
    sys.exit(f"Could not find artwork for {track_url}")


def dimensions(path, ffprobe):
    out = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0:nk=1", str(path)],
        capture_output=True, text=True)
    try:
        w, h = re.findall(r"\d+", out.stdout)[:2]
        return int(w), int(h)
    except (ValueError, IndexError):
        return None


def content_crop(src, ffmpeg):
    """Find the real picture bounds, ignoring letterbox bars.

    YouTube serves thumbnails as 16:9 content padded into a 4:3 frame, so a plain
    centre crop keeps the black bars and bakes them into the cover. cropdetect
    needs several frames even on a still, hence -loop.
    """
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-loop", "1", "-i", str(src),
         "-vf", "cropdetect=24:2:0", "-frames:v", "4", "-f", "null", "-"],
        capture_output=True, text=True)
    found = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", proc.stderr)
    if not found:
        return None
    w, h, x, y = (int(v) for v in found[-1])
    return (w, h, x, y) if w > 8 and h > 8 else None


def make_square(src, dest, size, ffmpeg, trim=True):
    """Strip letterboxing, centre-crop to a square, then scale."""
    filters, trimmed = [], None
    if trim:
        c = content_crop(src, ffmpeg)
        if c:
            w, h, x, y = c
            if x or y:                              # only report an actual trim
                trimmed = (w, h, x, y)
            filters.append(f"crop={w}:{h}:{x}:{y}")
    filters.append("crop='min(iw,ih)':'min(iw,ih)'")
    filters.append(f"scale={size}:{size}:flags=lanczos")
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
         "-vf", ",".join(filters), "-q:v", "2", str(dest)], check=True)
    return dest, trimmed


def embed(audio, art, ffmpeg):
    """Write cover art into one file, stream-copying the audio (no quality loss)."""
    suffix = audio.suffix.lower()
    tmp = audio.with_name(audio.stem + ".__art__" + suffix)
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-i", str(audio), "-i", str(art),
           # Drop any existing artwork by mapping only the audio plus the new image.
           "-map", "0:a", "-map", "1:v", "-c", "copy",
           "-disposition:v:0", "attached_pic",
           "-metadata:s:v", "title=Album cover",
           "-metadata:s:v", "comment=Cover (front)"]
    if suffix == ".mp3":
        cmd += ["-id3v2_version", "3"]
    cmd.append(str(tmp))

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        return proc.stderr.strip()[:200] or "ffmpeg failed"
    tmp.replace(audio)
    return None


def main():
    p = argparse.ArgumentParser(
        description="Embed album artwork into a folder of audio files.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("image", nargs="?", help="local image file")
    src.add_argument("--url", help="download the image from here")
    src.add_argument("--from-track", metavar="URL",
                     help="use the artwork of this SoundCloud/YouTube track")
    p.add_argument("-d", "--dir", required=True, help="folder of audio files")
    p.add_argument("--size", type=int, default=500,
                   help="output cover size in pixels, square (default: 500)")
    p.add_argument("--no-trim", action="store_true",
                   help="keep letterbox bars instead of cropping them off")
    p.add_argument("--no-cover-file", action="store_true",
                   help="skip writing cover.jpg alongside the tracks")
    p.add_argument("--dry-run", action="store_true", help="show what would change")
    args = p.parse_args()

    ffmpeg, ffprobe = find_exe("ffmpeg"), find_exe("ffprobe")
    if not ffmpeg or not ffprobe:
        sys.exit("ffmpeg/ffprobe not found.\n  winget install Gyan.FFmpeg")

    folder = Path(args.dir).expanduser().resolve()
    if not folder.is_dir():
        sys.exit(f"Not a folder: {folder}")
    tracks = sorted(f for f in folder.iterdir()
                    if f.is_file() and f.suffix.lower() in AUDIO_EXT)
    if not tracks:
        sys.exit(f"No audio files in {folder}")

    with tempfile.TemporaryDirectory() as td:
        raw = Path(td) / "raw.img"
        if args.image:
            shutil.copyfile(Path(args.image).expanduser().resolve(), raw)
        else:
            url = args.url or thumbnail_of(args.from_track)
            print(f"Fetching {url}")
            fetch(url, raw)

        dim = dimensions(raw, ffprobe)
        print(f"Source art : {dim[0]}x{dim[1]}" if dim else "Source art : unknown")
        if dim and dim[0] != dim[1]:
            print(f"             not square - centre-cropping to "
                  f"{min(dim)}x{min(dim)} before scaling")

        art, trimmed = make_square(raw, Path(td) / "cover.jpg", args.size, ffmpeg,
                                   trim=not args.no_trim)
        if trimmed:
            w, h, x, y = trimmed
            print(f"             letterboxing detected - trimmed to {w}x{h} "
                  f"(dropped {x}px sides, {y}px top/bottom)")
        print(f"Cover      : {args.size}x{args.size}  "
              f"({art.stat().st_size / 1024:.0f} KB)")
        print(f"Folder     : {folder}")
        print(f"Tracks     : {len(tracks)}\n")

        if args.dry_run:
            for t in tracks:
                print(f"  would embed -> {t.name}")
            print("\nDry run - nothing written.")
            return 0

        failed = 0
        for t in tracks:
            err = embed(t, art, ffmpeg)
            if err:
                print(f"  FAILED  {t.name}\n      {err}")
                failed += 1
            else:
                print(f"  ok      {t.name}")

        if not args.no_cover_file:
            # Plenty of players prefer a folder-level cover.jpg over embedded tags.
            shutil.copyfile(art, folder / "cover.jpg")
            print(f"\nWrote {folder / 'cover.jpg'}")

    print(f"\nEmbedded art in {len(tracks) - failed} of {len(tracks)} track(s).")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
