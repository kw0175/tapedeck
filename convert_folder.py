#!/usr/bin/env python3
"""
convert_folder.py - convert an album folder to another format, keeping tags + art.

    python convert_folder.py -d "Artist - Album" --format alac
    python convert_folder.py -d "Artist - Album" --format mp3 -q 320 -o "somewhere else"

Why ALAC: Apple Music and iTunes cannot read FLAC. ALAC is Apple's lossless codec,
so flac -> alac is a lossless-to-lossless transcode - identical audio, readable by
Apple. Everything else Apple supports (aac/mp3) is lossy.

Formats:
    alac   Apple Lossless in .m4a   lossless   <- for Apple Music / iTunes
    flac   FLAC                      lossless
    wav    uncompressed PCM          lossless   (drops tags - the format has none)
    m4a    AAC                       lossy
    mp3    MP3                       lossy

Converting a lossy source to a lossy target stacks compression artefacts; the
script warns when you ask for that.
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Windows consoles default to cp1252, which cannot encode characters yt-dlp puts
# in filenames - it substitutes U+29F8 BIG SOLIDUS for "/" so the name is legal.
# Printing such a name then raises UnicodeEncodeError and kills the run.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):                             # pragma: no cover
    pass


AUDIO_EXT = {".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".aiff"}
LOSSLESS_SRC = {".flac", ".wav", ".aiff"}
# target -> (container extension, ffmpeg codec, is_lossless)
TARGETS = {
    "alac": ("m4a", "alac", True),
    "flac": ("flac", "flac", True),
    "wav": ("wav", "pcm_s16le", True),
    "m4a": ("m4a", "aac", False),
    "mp3": ("mp3", "libmp3lame", False),
}


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


def convert(src, dest, codec, lossless, bitrate, ffmpeg):
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src)]
    if dest.suffix.lower() == ".wav":
        # WAV has no tag or picture support worth relying on - audio only.
        cmd += ["-map", "0:a", "-c:a", codec]
    else:
        # -map 0 carries the embedded cover through; copying it avoids re-encoding
        # the JPEG on every track.
        cmd += ["-map", "0", "-c:a", codec, "-c:v", "copy",
                "-disposition:v:0", "attached_pic"]
        if not lossless:
            cmd += ["-b:a", f"{bitrate}k"]
        if dest.suffix.lower() == ".mp3":
            cmd += ["-id3v2_version", "3"]
    cmd.append(str(dest))

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not dest.exists():
        return proc.stderr.strip()[:250] or "ffmpeg failed"
    return None


def main():
    p = argparse.ArgumentParser(
        description="Convert an album folder to another format, keeping tags and art.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("-d", "--dir", required=True, help="folder of audio files")
    p.add_argument("-f", "--format", required=True, choices=sorted(TARGETS),
                   help="target format")
    p.add_argument("-o", "--out",
                   help="output folder (default: alongside, named '<folder> [FORMAT]')")
    p.add_argument("-q", "--bitrate", type=int, default=320,
                   help="kbps for lossy targets (default: 320)")
    p.add_argument("--dry-run", action="store_true", help="show what would happen")
    args = p.parse_args()

    ffmpeg = find_exe("ffmpeg")
    if not ffmpeg:
        sys.exit("ffmpeg not found.\n  winget install Gyan.FFmpeg")

    folder = Path(args.dir).expanduser().resolve()
    if not folder.is_dir():
        sys.exit(f"Not a folder: {folder}")
    tracks = sorted(f for f in folder.iterdir()
                    if f.is_file() and f.suffix.lower() in AUDIO_EXT)
    if not tracks:
        sys.exit(f"No audio files in {folder}")

    ext, codec, lossless = TARGETS[args.format]
    outdir = (Path(args.out).expanduser().resolve() if args.out
              else folder.with_name(f"{folder.name} [{args.format.upper()}]"))

    src_lossless = all(t.suffix.lower() in LOSSLESS_SRC for t in tracks)
    print(f"Source : {folder}")
    print(f"Output : {outdir}")
    print(f"Format : {args.format}" + ("" if lossless else f" @ {args.bitrate}kbps"))
    if lossless and src_lossless:
        print("         lossless -> lossless: audio is preserved exactly")
    elif not lossless and not src_lossless:
        print("         !! lossy -> lossy: this stacks compression artefacts")
    elif lossless and not src_lossless:
        print("         note: a lossy source stays lossy - this only changes container")
    print(f"Tracks : {len(tracks)}\n")

    if args.dry_run:
        for t in tracks:
            print(f"  would write {outdir.name}\\{t.stem}.{ext}")
        print("\nDry run - nothing written.")
        return 0

    outdir.mkdir(parents=True, exist_ok=True)
    failed = 0
    for i, t in enumerate(tracks, 1):
        dest = outdir / f"{t.stem}.{ext}"
        err = convert(t, dest, codec, lossless, args.bitrate, ffmpeg)
        if err:
            print(f"  {i:2d}. FAILED  {t.name}\n      {err}")
            failed += 1
        else:
            print(f"  {i:2d}. {dest.stat().st_size / 1048576:6.1f} MB  {dest.name}")

    # Carry a folder-level cover across too, if one is there.
    cover = folder / "cover.jpg"
    if cover.exists():
        shutil.copyfile(cover, outdir / "cover.jpg")

    print(f"\nWrote {len(tracks) - failed} of {len(tracks)} track(s) to {outdir}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
