#!/usr/bin/env python3
"""
split_tracks.py - cut a long concert/DJ-set recording into individual tagged tracks.

Two steps:

  1. DETECT - find the song boundaries and write a starter cuesheet.
       python split_tracks.py concert.m4a --detect --expect 12 --names tracklist.txt

  2. SPLIT - cut the file using that cuesheet.
       python split_tracks.py concert.m4a --cue cuesheet.txt -o tracks

Three detection methods, tried in this order by default:

  gaps      (default) Many uploads are assembled from separate song files with a
            short digital silence between each. Those separators are exact, so
            when they exist this is perfect - and the silence gets trimmed OUT,
            leaving no dead air at track edges.
  envelope  No separators? Rank *relative* loudness dips by prominence. Works on
            true live recordings where the crowd never goes quiet.
  silence   Absolute silence threshold. Only for clean studio compilations.

Cuesheet format (blank lines and #-comments ignored):

    ALBUM:  Live At Wolverhampton Civic Hall
    ARTIST: Oasis
    DATE:   1994

    0:44 - 5:22.18    Rock 'n' Roll Star     <- explicit end: gap trimmed out
    5:24.16           Columbia               <- no end: runs to the next start
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ILLEGAL = str.maketrans({c: "_" for c in '<>:"/\\|?*'})
TIME_RE = r"(?:\d+:)?(?:\d+:)?\d+(?:\.\d+)?"


def find_exe(name):
    """Locate ffmpeg/ffprobe on PATH, falling back to the WinGet install location.

    winget adds ffmpeg to PATH, but shells opened before the install don't see it
    until they're restarted - so look in the package directory too.
    """
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


def parse_time(s):
    """'5:12' / '1:02:30' / '312' / '5:12.5' -> seconds (float)."""
    s = s.strip()
    if not re.fullmatch(TIME_RE, s):
        raise ValueError(f"bad timestamp: {s!r}")
    total = 0.0
    for p in (float(x) for x in s.split(":")):
        total = total * 60 + p
    return total


def fmt_time(sec, precise=False):
    sec = max(0.0, float(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if precise:
        return f"{int(h)}:{int(m):02d}:{s:05.2f}" if h else f"{int(m)}:{s:05.2f}"
    s = int(round(s))
    return f"{int(h)}:{int(m):02d}:{s:02d}" if h else f"{int(m)}:{s:02d}"


def duration_of(path, ffprobe):
    out = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def detect_silences(path, ffmpeg, noise_db, min_dur):
    """Run ffmpeg's silencedetect filter -> [(start, end, duration), ...]."""
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostats", "-i", str(path),
         "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}", "-f", "null", "-"],
        capture_output=True, text=True)
    # silencedetect logs to stderr: silence_start: 123.4 / silence_end: 126.1 | ...
    starts = [float(m) for m in re.findall(r"silence_start:\s*([\d.]+)", proc.stderr)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*([\d.]+)", proc.stderr)]
    return [(s, e, e - s) for s, e in zip(starts, ends)]


def segments_from_gaps(silences, total, min_track):
    """Treat each silence as a separator and return the audio BETWEEN them.

    Returns [(start, end), ...] with the silences excluded, so no dead air
    survives into the output. Segments shorter than min_track are dropped -
    that removes intros, outros and inter-song banter without hand-editing.
    """
    segs, cursor = [], 0.0
    for start, end, _ in sorted(silences):
        if start > cursor:
            segs.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < total:
        segs.append((cursor, total))
    return [(a, b) for a, b in segs if (b - a) >= min_track]


def chapters_of(url):
    """Read a video's chapter list -> [(start, end, title), ...].

    When an uploader has marked chapters (or written timestamps YouTube parsed
    into them), those boundaries are exact. Always prefer them over anything
    detected from the audio.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "yt_dlp", "--simulate", "--print", "%(chapters)j", url],
        capture_output=True, text=True)
    for line in reversed(proc.stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("["):
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            return [(float(c["start_time"]), float(c["end_time"]),
                     str(c.get("title") or "").strip()) for c in data]
    return []


def loudness_envelope(path, ffmpeg):
    """Per-second RMS level in dBFS.

    A live crowd never goes quiet, so absolute silence detection finds nothing
    useful. What DOES happen between songs is a *relative* dip - applause sits
    below the band. This envelope is what lets us find those dips.
    """
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostats", "-i", str(path),
         "-af", "aresample=8000,asetnsamples=8000,astats=metadata=1:reset=1,"
                "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
         "-f", "null", "-"], capture_output=True, text=True)
    return [-90.0 if m.group(1).startswith("-inf") else float(m.group(1))
            for m in re.finditer(r"RMS_level=(-?[\d.]+|-inf)", proc.stdout)]


def _smooth(v, w):
    return [sum(v[max(0, i - w):i + w + 1]) / len(v[max(0, i - w):i + w + 1])
            for i in range(len(v))]


def envelope_boundaries(env, want, min_spacing, edge_guard, window=25):
    """Find the most pronounced loudness dips, ranked by prominence.

    Prominence = how far the dip sits below the loudest point around it. A gap
    between songs dips hard against loud music on both sides; a quiet passage
    inside a song doesn't. Ranking by that beats ranking by absolute level.
    """
    if not env:
        return []
    sm = _smooth(env, 3)
    n = len(sm)
    cands = []
    for i in range(n):
        # Ignore intro and closing applause - always the biggest dips in a
        # concert file, and they'd otherwise steal two slots.
        if i < edge_guard or i > n - edge_guard:
            continue
        lo, hi = max(0, i - window), min(n, i + window + 1)
        local = sm[lo:hi]
        if sm[i] <= min(local):
            cands.append((max(local) - sm[i], i))

    chosen = []
    for _, i in sorted(cands, key=lambda x: (-x[0], x[1])):
        if all(abs(i - c) >= min_spacing for c in chosen):
            chosen.append(i)
        if len(chosen) >= want:
            break
    return sorted(float(c) for c in chosen)


def pick_boundaries(silences, want, min_spacing):
    """Choose the `want` most promising gaps, keeping them decently far apart."""
    chosen = []
    for start, _, dur in sorted(silences, key=lambda x: -x[2]):
        mid = start + dur / 2
        if all(abs(mid - c) >= min_spacing for c in chosen):
            chosen.append(mid)
        if len(chosen) >= want:
            break
    return sorted(chosen)


def read_names(path):
    out = []
    for ln in Path(path).read_text(encoding="utf-8").splitlines():
        ln = ln.strip().lstrip("-").strip()
        if ln and not ln.startswith("#"):
            out.append(ln)
    return out


def read_cue(path):
    """Return (meta, [(start, end_or_None, title), ...])."""
    meta, tracks = {}, []
    line_re = re.compile(rf"^({TIME_RE})(?:\s*-\s*({TIME_RE}))?\s+(.+)$")
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        ln = raw.strip()
        if not ln or ln.startswith("#"):
            continue
        m = re.match(r"^(ALBUM|ARTIST|DATE|GENRE)\s*:\s*(.+)$", ln, re.I)
        if m:
            meta[m.group(1).upper()] = m.group(2).strip()
            continue
        m = line_re.match(ln)
        if not m:
            print(f"  skipped unparsable line: {ln!r}")
            continue
        end = parse_time(m.group(2)) if m.group(2) else None
        tracks.append((parse_time(m.group(1)), end, m.group(3).strip()))
    tracks.sort(key=lambda t: t[0])
    return meta, tracks


def write_cue(path, rows, meta, explicit_ends):
    lines = ["# generated by split_tracks.py - check these times before splitting", ""]
    for k in ("ALBUM", "ARTIST", "DATE"):
        lines.append(f"{k}:{' ' * (7 - len(k))}{meta.get(k, '')}")
    lines.append("")
    for start, end, name in rows:
        stamp = (f"{fmt_time(start, True)} - {fmt_time(end, True)}"
                 if explicit_ends and end is not None else fmt_time(start, True))
        lines.append(f"{stamp:<22}{name}")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def do_detect(args, ffmpeg, ffprobe):
    total = duration_of(args.input, ffprobe)
    print(f"Input    : {Path(args.input).name}")
    print(f"Duration : {fmt_time(total)}")

    names = read_names(args.names) if args.names else []
    method = args.method
    rows, explicit_ends = [], False

    if args.from_chapters:
        chaps = chapters_of(args.from_chapters)
        if not chaps:
            sys.exit(f"No chapters found on {args.from_chapters}.\n"
                     "Drop --from-chapters to detect boundaries from the audio instead.")
        # Chapter titles win over --names; they came from the same source as the
        # timings. --names only fills gaps where a chapter had no title.
        rows = [(s, e, t or (names[i] if i < len(names) else f"Track {i + 1:02d}"))
                for i, (s, e, t) in enumerate(chaps)]
        explicit_ends = True
        method = "chapters"

    elif method in ("auto", "gaps"):
        sil = detect_silences(args.input, ffmpeg, args.gap_noise, args.gap_min)
        segs = segments_from_gaps(sil, total, args.min_track)
        enough = len(segs) >= (args.expect or 2)
        if enough:
            print(f"Found {len(sil)} separator gap(s) -> {len(segs)} segment(s) "
                  f"longer than {int(args.min_track)}s.")
            if args.expect and len(segs) > args.expect:
                # Extra segments are almost always a spoken intro or the closing
                # applause. The real songs are the longest ones; keep those, but
                # restore playing order afterwards.
                keep = sorted(sorted(segs, key=lambda s: -(s[1] - s[0]))[:args.expect])
                print(f"Dropping {len(segs) - args.expect} short non-song segment(s) "
                      f"(intro/outro).")
                segs = keep
            rows = [(a, b, names[i] if i < len(names) else f"Track {i + 1:02d}")
                    for i, (a, b) in enumerate(segs)]
            explicit_ends = True
            method = "gaps"
        elif args.method == "gaps":
            sys.exit(f"Only {len(segs)} segment(s) found. This file has no silent "
                     f"separators - try --method envelope.")
        else:
            print("No usable separator gaps; falling back to loudness envelope.")
            method = "envelope"

    if method == "envelope":
        print("Building loudness envelope and ranking dips by prominence ...")
        env = loudness_envelope(args.input, ffmpeg)
        if not env:
            sys.exit("Could not read a loudness envelope from this file.")
        if args.expect:
            want = args.expect - 1
        else:
            # Guessing a fixed count is worse than useless - it silently produces
            # merged or chopped tracks. Estimate from duration and say so loudly.
            want = max(1, int(total // 270) - 1)
            print(f"!! No --expect given. Estimating {want + 1} tracks from duration "
                  f"(~4.5min/song). Pass --expect N for an accurate split.")
        cuts = envelope_boundaries(env, want, args.min_spacing, args.edge_guard)
        print(f"Scanned {len(env)}s, selected {len(cuts)} boundary point(s).")
        starts = [0.0] + cuts
        rows = [(t, None, names[i] if i < len(names) else f"Track {i + 1:02d}")
                for i, t in enumerate(starts)]

    elif method == "silence":
        sil = detect_silences(args.input, ffmpeg, args.noise, args.min_silence)
        print(f"Found {len(sil)} candidate gap(s).")
        if not sil:
            sys.exit("Nothing detected. Try --method envelope.")
        cuts = pick_boundaries(sil, (args.expect - 1) if args.expect else len(sil),
                               args.min_spacing)
        starts = [0.0] + cuts
        rows = [(t, None, names[i] if i < len(names) else f"Track {i + 1:02d}")
                for i, t in enumerate(starts)]

    if args.expect and len(rows) != args.expect:
        print(f"\n!! Got {len(rows)} track(s), expected {args.expect}. "
              f"Adjust the cuesheet by hand before splitting.")

    write_cue(args.cue_out, rows, {"ALBUM": args.album or "",
                                   "ARTIST": args.artist or "",
                                   "DATE": args.date or ""}, explicit_ends)

    print(f"\nWrote {args.cue_out} with {len(rows)} track(s) [method: {method}]:\n")
    for i, (start, end, name) in enumerate(rows):
        stop = end if end is not None else (
            rows[i + 1][0] if i + 1 < len(rows) else total)
        print(f"  {i + 1:2d}. {fmt_time(start):>8} - {fmt_time(stop):<8} "
              f"({fmt_time(stop - start):>6})  {name}")

    if explicit_ends:
        print("\nSeparator silences are excluded, so tracks won't have dead air at "
              "their edges.")
    print(f"\nThen run:\n  python split_tracks.py \"{args.input}\" "
          f"--cue {args.cue_out} -o tracks")
    return 0


def do_split(args, ffmpeg, ffprobe):
    meta, tracks = read_cue(args.cue)
    if not tracks:
        sys.exit(f"No tracks found in {args.cue}")
    total = duration_of(args.input, ffprobe)
    outdir = Path(args.out).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    ext = Path(args.input).suffix.lstrip(".") if args.format == "copy" else args.format
    fade = max(0.0, args.fade / 1000.0)
    # A fade needs a filter, and filters need decoded audio - so any fade forces a
    # re-encode. Joined uploads often have a click exactly at each join, and cuts
    # land right on it; a few ms of fade removes that without audible loss.
    reencode = args.format != "copy" or fade > 0

    print(f"Input  : {Path(args.input).name}")
    print(f"Output : {outdir}")
    if not reencode:
        print("Mode   : stream copy (lossless, fast)")
    else:
        why = f" [forced by --fade {args.fade}ms]" if args.format == "copy" else ""
        print(f"Mode   : re-encode to {ext}{why}")
        if ext == "flac":
            print("         flac is lossless, so this adds no quality loss")
    if fade > 0:
        print(f"Fade   : {args.fade}ms in/out (removes clicks at cut points)")
    print(f"Tracks : {len(tracks)}\n")

    failed = 0
    for i, (start, end, title) in enumerate(tracks):
        if end is None:
            end = tracks[i + 1][0] if i + 1 < len(tracks) else total
        dur = end - start
        if dur <= 0:
            print(f"  {i + 1:2d}. SKIPPED (non-positive length) {title}")
            failed += 1
            continue

        # Trailing '_' from a stripped '?' (e.g. "...What I Mean?") looks like a
        # typo, and Windows rejects trailing dots/spaces outright.
        safe = title.translate(ILLEGAL).strip().rstrip(" ._") or f"Track {i + 1:02d}"
        dest = outdir / f"{i + 1:02d} - {safe}.{ext}"
        # -ss/-t before -i. When re-encoding this is still sample-accurate
        # (-accurate_seek is on by default, so ffmpeg decodes from an earlier
        # keyframe and discards), AND it rebases output timestamps to zero -
        # which the fade filter below depends on. With -ss as an OUTPUT option
        # the filter still sees source timestamps, so afade=t=out fires before
        # the segment starts and silences the whole track.
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(args.input)]
        if reencode:
            if fade > 0:
                out_at = max(0.0, dur - fade)
                cmd += ["-af", f"afade=t=in:st=0:d={fade:.3f},"
                               f"afade=t=out:st={out_at:.3f}:d={fade:.3f}"]
            if ext in ("mp3", "m4a", "opus"):
                cmd += ["-b:a", f"{args.bitrate}k"]
        else:
            cmd += ["-c", "copy"]
        cmd += ["-metadata", f"title={title}", "-metadata", f"track={i + 1}/{len(tracks)}"]
        for tag, key in (("artist", "ARTIST"), ("album", "ALBUM"), ("date", "DATE")):
            if meta.get(key):
                cmd += ["-metadata", f"{tag}={meta[key]}"]
        cmd.append(str(dest))

        if args.dry_run:
            print(f"  {i + 1:2d}. {fmt_time(start)} +{fmt_time(dur)}  ->  {dest.name}")
            continue

        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            print(f"  {i + 1:2d}. FAILED  {title}\n      {proc.stderr.strip()[:300]}")
            failed += 1
        else:
            print(f"  {i + 1:2d}. {fmt_time(dur):>6}  "
                  f"{dest.stat().st_size / 1048576:5.1f} MB  {dest.name}")

    print("\nDry run - nothing written." if args.dry_run
          else f"\nWrote {len(tracks) - failed} track(s) to {outdir}")
    return 1 if failed else 0


def main():
    p = argparse.ArgumentParser(
        description="Split a long recording into individual tagged tracks.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("input", help="the long audio file")
    p.add_argument("--detect", action="store_true",
                   help="find boundaries and write a starter cuesheet")
    p.add_argument("--cue", help="cuesheet to split with")
    p.add_argument("--cue-out", default="cuesheet.txt",
                   help="where --detect writes its cuesheet (default: cuesheet.txt)")
    p.add_argument("--names", help="text file of track titles, one per line, in order")
    p.add_argument("--expect", type=int, metavar="N", help="how many tracks to expect")
    p.add_argument("--from-chapters", metavar="URL",
                   help="build the cuesheet from a video's chapter markers instead "
                        "of detecting boundaries. Exact when available - always "
                        "prefer this. Titles come from the chapters themselves.")
    p.add_argument("--method", default="auto",
                   choices=["auto", "gaps", "envelope", "silence"],
                   help="'auto' tries gaps then falls back to envelope (default)")

    g = p.add_argument_group("gaps method")
    g.add_argument("--gap-noise", type=float, default=-50,
                   help="dB below which counts as a separator (default: -50)")
    g.add_argument("--gap-min", type=float, default=0.6,
                   help="minimum separator length in seconds (default: 0.6)")
    g.add_argument("--min-track", type=float, default=45,
                   help="segments shorter than this are dropped as intro/outro/"
                        "banter rather than songs (default: 45)")

    e = p.add_argument_group("envelope / silence methods")
    e.add_argument("--edge-guard", type=int, default=45, metavar="SEC",
                   help="ignore dips this close to either end (default: 45)")
    e.add_argument("--noise", type=float, default=-30,
                   help="silence threshold in dB (default: -30)")
    e.add_argument("--min-silence", type=float, default=0.8,
                   help="seconds of quiet to count as a gap (default: 0.8)")
    e.add_argument("--min-spacing", type=float, default=60,
                   help="minimum seconds between boundaries (default: 60)")

    p.add_argument("-o", "--out", default="tracks", help="output folder (default: ./tracks)")
    p.add_argument("-f", "--format", default="copy",
                   choices=["copy", "mp3", "m4a", "flac", "wav", "opus"],
                   help="'copy' cuts losslessly without re-encoding (default)")
    p.add_argument("-q", "--bitrate", type=int, default=256, help="kbps when re-encoding")
    p.add_argument("--fade", type=float, default=24, metavar="MS",
                   help="fade in/out at each cut, in milliseconds (default: 24). "
                        "Removes the click you get when a cut lands on a join in a "
                        "concatenated upload. Forces a re-encode - use --fade 0 to "
                        "keep a true lossless stream copy.")
    p.add_argument("--album"), p.add_argument("--artist"), p.add_argument("--date")
    p.add_argument("--dry-run", action="store_true", help="show the cuts, write nothing")
    args = p.parse_args()

    if not Path(args.input).exists():
        sys.exit(f"File not found: {args.input}")

    ffmpeg, ffprobe = find_exe("ffmpeg"), find_exe("ffprobe")
    if not ffmpeg or not ffprobe:
        sys.exit("ffmpeg/ffprobe not found.\n"
                 "  winget install Gyan.FFmpeg      (then reopen your terminal)")

    if args.detect:
        return do_detect(args, ffmpeg, ffprobe)
    if not args.cue:
        sys.exit("Give me either --detect (to find boundaries) or --cue FILE (to split).")
    return do_split(args, ffmpeg, ffprobe)


if __name__ == "__main__":
    sys.exit(main())
