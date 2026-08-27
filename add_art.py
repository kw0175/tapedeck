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
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Windows consoles default to cp1252, which cannot encode characters yt-dlp puts
# in filenames - it substitutes U+29F8 BIG SOLIDUS for "/" so the name is legal.
# Printing such a name then raises UnicodeEncodeError and kills the run.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):                             # pragma: no cover
    pass


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


# MusicBrainz asks that clients identify themselves and stay under ~1 req/sec.
MB_UA = "tapedeck/1.0 ( https://github.com/kw0175/tapedeck )"
MB_DELAY = 1.1


def _get_json(url, timeout=25, attempts=4):
    """GET JSON, retrying on throttling.

    MusicBrainz answers bursts with 503. Treating that as "no results" is worse
    than useless - it reports that a release does not exist when the server was
    merely busy - so back off and retry, and let a real failure raise.
    """
    req = urllib.request.Request(url, headers={"User-Agent": MB_UA,
                                               "Accept": "application/json"})
    delay = 1.5
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code in (503, 429) and i < attempts - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise
    return {}


def musicbrainz_releases(artist, album, limit=8):
    """Releases matching an artist/album, best match first."""
    q = urllib.parse.quote(f'artist:"{artist}" AND release:"{album}"' if artist
                           else f'release:"{album}"')
    try:
        data = _get_json(f"https://musicbrainz.org/ws/2/release?query={q}"
                         f"&fmt=json&limit={limit}")
    except Exception as e:                                       # noqa: BLE001
        raise LookupError(f"could not reach MusicBrainz: {e}") from e
    return [(r.get("id"), r.get("title", ""), (r.get("date") or "")[:4])
            for r in data.get("releases", []) if r.get("id")]


def cover_art_url(mbid, kind="release"):
    """Front-cover URL from the Cover Art Archive, or None.

    404 here is the normal answer for a release nobody has uploaded art for -
    most live bootlegs - so it is not worth logging as an error.
    """
    try:
        data = _get_json(f"https://coverartarchive.org/{kind}/{mbid}")
    except Exception:                                            # noqa: BLE001
        return None
    for img in data.get("images", []):
        if img.get("front") and img.get("image"):
            return img["image"].replace("http://", "https://")
    return None




def _norm(t):
    return re.sub(r"[^a-z0-9]+", "", (t or "").lower())


def artist_matches(wanted, candidate):
    """Reject a result whose artist is not the one asked for.

    Searching "Oasis Rock in Rio" on iTunes returns unrelated albums. Picking the
    largest image without checking would embed another band's cover with total
    confidence - worse than falling back to the thumbnail.
    """
    if not wanted:
        return True
    w, c = _norm(wanted), _norm(candidate)
    return bool(w) and bool(c) and (w in c or c in w)


def itunes_covers(artist, album):
    """Apple's public search API. No key, big catalogue, high-res art.

    artworkUrl100 is a thumbnail, but the size is just a path segment - asking
    for 1200x1200 returns the real thing.
    """
    term = urllib.parse.quote(f"{artist} {album}".strip())
    try:
        data = _get_json(f"https://itunes.apple.com/search?term={term}"
                         f"&entity=album&limit=6", attempts=2)
    except Exception:                                            # noqa: BLE001
        return []
    out = []
    for r in data.get("results", []):
        url = r.get("artworkUrl100")
        if url and artist_matches(artist, r.get("artistName")):
            out.append((re.sub(r"/\d+x\d+bb", "/1200x1200bb", url),
                        f"{r.get('artistName','?')} - {r.get('collectionName','?')}"))
    return out


def deezer_covers(artist, album):
    """Deezer's public API. No key. cover_xl is 1000x1000."""
    q = urllib.parse.quote(f"{artist} {album}".strip())
    try:
        data = _get_json(f"https://api.deezer.com/search/album?q={q}&limit=6",
                         attempts=2)
    except Exception:                                            # noqa: BLE001
        return []
    return [(a["cover_xl"], f"{a.get('artist',{}).get('name','?')} - {a.get('title','?')}")
            for a in data.get("data", [])
            if a.get("cover_xl") and artist_matches(artist, a.get("artist", {}).get("name"))]


def coverartarchive_covers(artist, album):
    """MusicBrainz + Cover Art Archive. Best for genuinely archived releases,
    including some unofficial ones, but thin on live bootlegs."""
    try:
        releases = musicbrainz_releases(artist, album)
    except LookupError:
        return []
    out = []
    for mbid, title, year in releases[:4]:
        time.sleep(MB_DELAY)
        url = cover_art_url(mbid)
        if url:
            out.append((url, f"{title} ({year or '?'})"))
    return out


def search_cover(artist, album, dest, ffprobe, log=print):
    """Find real release artwork. Returns a path or None.

    Tries several catalogues because no single one covers live and unofficial
    recordings well. Candidates are judged on the actual pixels returned, not on
    which source produced them.
    """
    if not album:
        return None
    log(f"Searching for artwork: {artist or '(any artist)'} - {album}")

    best = None                                   # (side, bytes, label, source)
    for name, fn in (("iTunes", itunes_covers),
                     ("Deezer", deezer_covers),
                     ("CoverArtArchive", coverartarchive_covers)):
        try:
            candidates = fn(artist, album)
        except Exception as e:                                   # noqa: BLE001
            log(f"  {name}: unavailable ({e})")
            continue
        if not candidates:
            log(f"  {name}: nothing")
            continue
        for url, label in candidates[:4]:
            try:
                fetch(url, dest)
            except Exception:                                    # noqa: BLE001
                continue
            dim = dimensions(dest, ffprobe) if ffprobe else None
            side = min(dim) if dim else 0
            if not side:
                continue
            log(f"  {name}: {label} - {dim[0]}x{dim[1]}")
            if best is None or side > best[0]:
                best = (side, dest.read_bytes(), label, name)
            if side >= 1000:
                break
        if best and best[0] >= 1000:
            break                                  # good enough; stop querying

    if not best:
        log("  nothing found in any catalogue")
        return None
    dest.write_bytes(best[1])
    log(f"  using {best[3]}: {best[2]} ({best[0]}px)")
    return dest

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


# A real letterbox bar is essentially pure black. Album art routinely has dark
# edges that are part of the picture, and cropdetect cannot tell them apart -
# it flagged 76px off a genuine 1000x1000 sleeve whose background is just dark.
BLACK_MAX_LUMA = 6.0
MIN_TRIM_FRACTION = 0.04


def region_luma(src, ffmpeg, x, y, w, h):
    """Mean brightness of a region, 0 (black) to 255."""
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-loop", "1", "-i", str(src),
         "-vf", f"crop={w}:{h}:{x}:{y},signalstats,"
                f"metadata=print:key=lavfi.signalstats.YAVG",
         "-frames:v", "1", "-f", "null", "-"], capture_output=True, text=True)
    m = re.search(r"YAVG=([\d.]+)", proc.stdout + proc.stderr)
    return float(m.group(1)) if m else None


def content_crop(src, ffmpeg, full_w, full_h):
    """Find real picture bounds, but only when the margins are genuinely black.

    YouTube pads 16:9 thumbnails into a 4:3 frame, and those bars should go. A
    dark photograph should not. So cropdetect only proposes a crop - it is
    accepted only if every strip it wants to remove measures near-black and is
    big enough to be a real bar rather than edge noise.
    """
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-loop", "1", "-i", str(src),
         "-vf", "cropdetect=24:2:0", "-frames:v", "4", "-f", "null", "-"],
        capture_output=True, text=True)
    found = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", proc.stderr)
    if not found:
        return None
    w, h, x, y = (int(v) for v in found[-1])
    if w < 8 or h < 8 or (w == full_w and h == full_h):
        return None

    strips = []
    if y > 0:
        strips.append((0, 0, full_w, y))                        # top
    if y + h < full_h:
        strips.append((0, y + h, full_w, full_h - (y + h)))     # bottom
    if x > 0:
        strips.append((0, 0, x, full_h))                        # left
    if x + w < full_w:
        strips.append((x + w, 0, full_w - (x + w), full_h))     # right
    if not strips:
        return None

    # Too small to be a letterbox bar - almost certainly just dark edges.
    if (full_h - h) < full_h * MIN_TRIM_FRACTION and \
       (full_w - w) < full_w * MIN_TRIM_FRACTION:
        return None

    for sx, sy, sw, sh in strips:
        luma = region_luma(src, ffmpeg, sx, sy, sw, sh)
        if luma is None or luma > BLACK_MAX_LUMA:
            return None                                          # real picture
    return (w, h, x, y)


def make_square(src, dest, size, ffmpeg, trim=True):
    """Strip letterboxing, centre-crop to a square, then scale."""
    filters, trimmed = [], None
    if trim:
        dim = dimensions(src, find_exe("ffprobe"))
        c = content_crop(src, ffmpeg, *(dim or (0, 0))) if dim else None
        if c:
            w, h, x, y = c
            if x or y:                              # only report an actual trim
                trimmed = (w, h, x, y)
            filters.append(f"crop={w}:{h}:{x}:{y}")
    # Never invent pixels. Upscaling a small cover to hit a target number makes
    # a soft image that merely claims to be 1000px; better to ship what exists.
    dim_now = dimensions(src, find_exe("ffprobe"))
    if dim_now:
        available = min(dim_now)
        if trimmed:
            available = min(trimmed[0], trimmed[1])
        if available and available < size:
            size = available
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
    try:
        tmp.replace(audio)
    except PermissionError:
        # Apple Music keeps handles on files in its own library folder, so the
        # swap fails mid-run and leaves .__art__ files behind unless cleaned up.
        tmp.unlink(missing_ok=True)
        return "file is locked - quit Apple Music (or whatever has it open)"
    except OSError as e:
        tmp.unlink(missing_ok=True)
        return f"could not replace file: {e}"
    return None


def main():
    p = argparse.ArgumentParser(
        description="Embed album artwork into a folder of audio files.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    src = p.add_mutually_exclusive_group()
    src.add_argument("image", nargs="?", help="local image file")
    src.add_argument("--url", help="download the image from here")
    src.add_argument("--from-track", metavar="URL",
                     help="use the artwork of this SoundCloud/YouTube track")
    p.add_argument("--search-artist", metavar="NAME",
                   help="narrow the MusicBrainz search; optional")
    p.add_argument("--search-album", metavar="TITLE", help="album title to search for")
    p.add_argument("--fallback", metavar="FILE",
                   help="image to use only if the search finds nothing")
    p.add_argument("-d", "--dir", required=True, help="folder of audio files")
    p.add_argument("--size", type=int, default=1000,
                   help="output cover size in pixels, square (default: 1000 - "
                        "Apple Music and Spotify both want >=1000)")
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
        got = None

        # A real sleeve beats a video thumbnail: a thumbnail is a 16:9 frame that
        # loses a third of itself when squared, then gets upscaled to fake the size.
        if args.search_album:
            got = search_cover(args.search_artist, args.search_album, raw, ffprobe)

        if got is None and args.image:
            shutil.copyfile(Path(args.image).expanduser().resolve(), raw)
            got = raw
        elif got is None and (args.url or args.from_track):
            url = args.url or thumbnail_of(args.from_track)
            print(f"Fetching {url}")
            fetch(url, raw)
            got = raw
        elif got is None and args.fallback:
            print("Falling back to the supplied image.")
            shutil.copyfile(Path(args.fallback).expanduser().resolve(), raw)
            got = raw

        if got is None:
            sys.exit("No artwork found, and no image or --fallback given.")

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
        # Report what was actually produced. Printing the requested size would
        # claim 1000x1000 for a cover that was capped to the source's real size.
        made = dimensions(art, ffprobe)
        shown = f"{made[0]}x{made[1]}" if made else f"{args.size}x{args.size}"
        note = ""
        if made and made[0] < args.size:
            note = f"  (source was {made[0]}px; not upscaled to {args.size})"
        print(f"Cover      : {shown}  ({art.stat().st_size / 1024:.0f} KB){note}")
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
