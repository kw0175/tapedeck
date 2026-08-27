#!/usr/bin/env python3
"""
refresh_art.py - re-run the artwork search across a whole music library.

Reads each album's own artist/album tags and looks the cover up again, so you
never have to search by hand. Useful after adding a Discogs token, or when the
matching has improved.

    python refresh_art.py -d "C:/Users/you/Music"
    python refresh_art.py -d "C:/Users/you/Music" --only-thumbnails
    python refresh_art.py -d "C:/Users/you/Music" --dry-run

An album is any folder containing audio files. Albums where nothing is found are
left exactly as they are - a wrong sleeve is worse than the one already there.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import winquiet  # noqa: F401  (patches subprocess on import)
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

HERE = Path(__file__).resolve().parent
AUDIO = {".mp3", ".m4a", ".flac", ".ogg", ".opus"}


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


def tags_of(track, ffprobe):
    out = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format_tags", "-of", "json",
         str(track)], capture_output=True, text=True)
    try:
        t = json.loads(out.stdout or "{}").get("format", {}).get("tags", {})
    except json.JSONDecodeError:
        return {}
    return {k.lower(): v for k, v in t.items()}


def cover_side(track, ffprobe):
    """Smaller dimension of the embedded cover, 0 if there is none."""
    out = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "csv=p=0:nk=1", str(track)],
        capture_output=True, text=True)
    nums = [int(n) for n in out.stdout.replace("x", ",").split(",") if n.strip().isdigit()]
    return min(nums) if len(nums) >= 2 else 0


def albums_under(root):
    for folder in sorted(p for p in root.rglob("*") if p.is_dir()):
        if any(f.suffix.lower() in AUDIO for f in folder.iterdir() if f.is_file()):
            yield folder


def main():
    p = argparse.ArgumentParser(
        description="Refresh cover art across a library using each album's tags.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("-d", "--dir", required=True, help="library root to walk")
    p.add_argument("--size", type=int, default=1000)
    p.add_argument("--only-thumbnails", action="store_true",
                   help="skip albums that already have art above --good-size")
    p.add_argument("--good-size", type=int, default=800,
                   help="art at least this big counts as already good (default: 800)")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    ffprobe = find_exe("ffprobe")
    if not ffprobe:
        sys.exit("ffprobe not found.\n  winget install Gyan.FFmpeg")

    root = Path(args.dir).expanduser().resolve()
    if not root.is_dir():
        sys.exit(f"Not a folder: {root}")

    folders = list(albums_under(root))
    print(f"Scanning {root}\nFound {len(folders)} album folder(s)\n")

    changed = skipped = failed = 0
    for folder in folders:
        tracks = sorted(f for f in folder.iterdir()
                        if f.is_file() and f.suffix.lower() in AUDIO)
        if not tracks:
            continue
        tags = tags_of(tracks[0], ffprobe)
        artist = tags.get("album_artist") or tags.get("artist") or ""
        album = tags.get("album") or folder.name
        have = cover_side(tracks[0], ffprobe)

        label = f"{artist or '?'} - {album}"
        if args.only_thumbnails and have >= args.good_size:
            print(f"  skip   {label}  (already {have}px)")
            skipped += 1
            continue

        print(f"  ---    {label}  (currently {have or 'no'}px)")
        if args.dry_run:
            continue

        # Never trade down: a refresh that replaces a 1000px sleeve with a
        # 599px one has made the library worse, not better.
        cmd = [sys.executable, str(HERE / "add_art.py"), "-d", str(folder),
               "--size", str(args.size), "--search-album", album,
               "--min-size", str(have + 1)]
        if artist:
            cmd += ["--search-artist", artist]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        out = (r.stdout or "") + (r.stderr or "")
        used = [l for l in out.splitlines() if "using " in l]
        if r.returncode == 0 and used:
            print(f"         {used[-1].strip()}")
            changed += 1
        elif "nothing found" in out or "smaller than" in out:
            why = "nothing matched" if "nothing found" in out else "only found something smaller"
            print(f"         {why} - left as is")
            skipped += 1
        else:
            first = next((l for l in out.splitlines() if "FAILED" in l or "locked" in l), "")
            print(f"         not updated{(' - ' + first.strip()) if first else ''}")
            failed += 1

    print(f"\nUpdated {changed}, left alone {skipped}, problems {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
