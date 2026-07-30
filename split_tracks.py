#!/usr/bin/env python3
"""
split_tracks.py - cut a long concert/DJ-set recording into individual tagged tracks.

Two steps:

  1. DETECT - find the quiet gaps between songs and write a starter cuesheet.
       python split_tracks.py concert.m4a --detect --expect 12 --names tracklist.txt

     Boundaries are guesses. Open the cuesheet, play the file, nudge the times.

  2. SPLIT - cut the file using that cuesheet.
       python split_tracks.py concert.m4a --cue cuesheet.txt -o tracks

Cuesheet format (blank lines and #-comments ignored):

    ALBUM:  Live At Wolverhampton Civic Hall
    ARTIST: Oasis
    DATE:   1994

    0:00     Rock 'n' Roll Star
    5:12     Columbia
    1:02:30  Encore

Each track runs until the next timestamp; the last runs to end of file.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ILLEGAL = str.maketrans({c: "_" for c in '<>:"/\\|?*'})


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
    if not re.fullmatch(r"(\d+:)?(\d+:)?\d+(\.\d+)?", s):
        raise ValueError(f"bad timestamp: {s!r}")
    parts = [float(p) for p in s.split(":")]
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


def fmt_time(sec):
    sec = max(0, int(round(sec)))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def duration_of(path, ffprobe):
    out = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def detect_silences(path, ffmpeg, noise_db, min_dur):
    """Run ffmpeg's silencedetect filter and return [(start, end, duration), ...]."""
    cmd = [ffmpeg, "-hide_banner", "-nostats", "-i", str(path),
           "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}", "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    # silencedetect writes to stderr as: silence_start: 123.4 / silence_end: 126.1 | ...
    starts = [float(m) for m in re.findall(r"silence_start:\s*([\d.]+)", proc.stderr)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*([\d.]+)", proc.stderr)]
    return [(s, e, e - s) for s, e in zip(starts, ends)]


def loudness_envelope(path, ffmpeg):
    """Per-second RMS level in dBFS.

    A live crowd never goes quiet, so absolute silence detection finds nothing
    useful. What DOES happen between songs is a *relative* dip - applause sits
    below the band. This envelope is what lets us find those dips.
    """
    cmd = [ffmpeg, "-hide_banner", "-nostats", "-i", str(path),
           "-af", "aresample=8000,asetnsamples=8000,astats=metadata=1:reset=1,"
                  "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
           "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    vals = []
    for m in re.finditer(r"RMS_level=(-?[\d.]+|-inf)", proc.stdout):
        raw = m.group(1)
        vals.append(-90.0 if raw.startswith("-inf") else float(raw))
    return vals


def _smooth(v, w):
    return [sum(v[max(0, i - w):min(len(v), i + w + 1)]) /
            len(v[max(0, i - w):min(len(v), i + w + 1)]) for i in range(len(v))]


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
        # Ignore the intro and the closing applause - they're always the biggest
        # dips in the file and would otherwise eat two of the slots.
        if i < edge_guard or i > n - edge_guard:
            continue
        lo, hi = max(0, i - window), min(n, i + window + 1)
        local = sm[lo:hi]
        if sm[i] <= min(local):
            cands.append((max(local) - sm[i], i))

    chosen = []
    for prom, i in sorted(cands, key=lambda x: (-x[0], x[1])):
        if all(abs(i - c) >= min_spacing for c in chosen):
            chosen.append(i)
        if len(chosen) >= want:
            break
    return sorted(float(c) for c in chosen)


def pick_boundaries(silences, want, min_spacing):
    """Choose the `want` most promising gaps, keeping them decently far apart.

    Longest silences first (a between-song gap is longer than a mid-song pause),
    but never two boundaries within min_spacing of each other - otherwise one
    ragged gap eats several slots and whole songs get skipped.
    """
    chosen = []
    for start, end, dur in sorted(silences, key=lambda x: -x[2]):
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
    """Return (meta_dict, [(seconds, title), ...])."""
    meta, tracks = {}, []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        ln = raw.strip()
        if not ln or ln.startswith("#"):
            continue
        m = re.match(r"^(ALBUM|ARTIST|DATE|GENRE)\s*:\s*(.+)$", ln, re.I)
        if m:
            meta[m.group(1).upper()] = m.group(2).strip()
            continue
        m = re.match(r"^((?:\d+:)?(?:\d+:)?\d+(?:\.\d+)?)\s+(.+)$", ln)
        if not m:
            print(f"  skipped unparsable line: {ln!r}")
            continue
        tracks.append((parse_time(m.group(1)), m.group(2).strip()))
    tracks.sort(key=lambda t: t[0])
    return meta, tracks


def write_cue(path, times, names, meta):
    lines = [f"# generated by split_tracks.py - CHECK AND ADJUST THESE TIMES",
             f"# boundaries are detected from silence, not from the tracklist",
             ""]
    for k in ("ALBUM", "ARTIST", "DATE"):
        lines.append(f"{k}:{' ' * (7 - len(k))}{meta.get(k, '')}")
    lines.append("")
    for i, t in enumerate(times):
        name = names[i] if i < len(names) else f"Track {i + 1:02d}"
        lines.append(f"{fmt_time(t):<9}{name}")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def do_detect(args, ffmpeg, ffprobe):
    total = duration_of(args.input, ffprobe)
    print(f"Input    : {Path(args.input).name}")
    print(f"Duration : {fmt_time(total)}")

    names = read_names(args.names) if args.names else []

    if args.method == "envelope":
        print("Building loudness envelope and ranking dips by prominence ...")
        env = loudness_envelope(args.input, ffmpeg)
        if not env:
            sys.exit("Could not read a loudness envelope from this file.")
        want = (args.expect - 1) if args.expect else 11
        cuts = envelope_boundaries(env, want, args.min_spacing, args.edge_guard)
        print(f"Scanned {len(env)}s, selected {len(cuts)} boundary point(s).")
    else:
        print(f"Scanning for gaps quieter than {args.noise}dB lasting {args.min_silence}s+ ...")
        silences = detect_silences(args.input, ffmpeg, args.noise, args.min_silence)
        print(f"Found {len(silences)} candidate gap(s).")
        if not silences:
            print("\nNothing detected. Live recordings have constant crowd noise - try\n"
                  "--method envelope instead, which handles that case.")
            return 1
        want = (args.expect - 1) if args.expect else len(silences)
        cuts = pick_boundaries(silences, want, args.min_spacing)

    times = [0.0] + cuts

    if args.expect and len(times) < args.expect:
        print(f"\nOnly {len(times)} of {args.expect} boundaries found. Lower --noise\n"
              f"(e.g. -25) or --min-silence (e.g. 0.4), or add the rest by hand.")

    write_cue(args.cue_out, times, names, {
        "ALBUM": args.album or "", "ARTIST": args.artist or "", "DATE": args.date or ""})

    print(f"\nWrote {args.cue_out} with {len(times)} track(s):\n")
    for i, t in enumerate(times):
        end = times[i + 1] if i + 1 < len(times) else total
        name = names[i] if i < len(names) else f"Track {i + 1:02d}"
        print(f"  {i + 1:2d}. {fmt_time(t):>8} - {fmt_time(end):<8} ({fmt_time(end - t):>6})  {name}")

    print("\nThese are guesses. Play the file, fix any times that are off, then run:\n"
          f"  python split_tracks.py \"{args.input}\" --cue {args.cue_out} -o tracks")
    return 0


def do_split(args, ffmpeg, ffprobe):
    meta, tracks = read_cue(args.cue)
    if not tracks:
        sys.exit(f"No tracks found in {args.cue}")
    total = duration_of(args.input, ffprobe)
    outdir = Path(args.out).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    src_ext = Path(args.input).suffix.lstrip(".")
    ext = src_ext if args.format == "copy" else args.format

    print(f"Input  : {Path(args.input).name}")
    print(f"Output : {outdir}")
    print(f"Mode   : {'stream copy (lossless, fast)' if args.format == 'copy' else f're-encode to {ext}'}")
    print(f"Tracks : {len(tracks)}\n")

    failed = 0
    for i, (start, title) in enumerate(tracks):
        end = tracks[i + 1][0] if i + 1 < len(tracks) else total
        dur = end - start
        if dur <= 0:
            print(f"  {i + 1:2d}. SKIPPED (non-positive length) {title}")
            failed += 1
            continue

        safe = title.translate(ILLEGAL).strip()
        dest = outdir / f"{i + 1:02d} - {safe}.{ext}"

        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(args.input)]
        # -t after -ss but before -i = fast seek; audio frames are small enough
        # that this stays accurate to well under a second.
        cmd += ["-c", "copy"] if args.format == "copy" else \
               ["-b:a", f"{args.bitrate}k"] if ext in ("mp3", "m4a", "opus") else []
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
            mb = dest.stat().st_size / 1024 / 1024
            print(f"  {i + 1:2d}. {fmt_time(dur):>6}  {mb:5.1f} MB  {dest.name}")

    if args.dry_run:
        print("\nDry run - nothing written.")
    else:
        print(f"\nWrote {len(tracks) - failed} track(s) to {outdir}")
    return 1 if failed else 0


def main():
    p = argparse.ArgumentParser(
        description="Split a long recording into individual tagged tracks.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("input", help="the long audio file")
    p.add_argument("--detect", action="store_true",
                   help="find gaps and write a starter cuesheet instead of splitting")
    p.add_argument("--cue", help="cuesheet to split with")
    p.add_argument("--cue-out", default="cuesheet.txt",
                   help="where --detect writes its cuesheet (default: cuesheet.txt)")
    p.add_argument("--names", help="text file of track titles, one per line, in order")
    p.add_argument("--expect", type=int, metavar="N",
                   help="how many tracks the recording should yield")
    p.add_argument("--method", default="envelope", choices=["envelope", "silence"],
                   help="'envelope' ranks relative loudness dips - use this for live "
                        "recordings with crowd noise (default). 'silence' looks for "
                        "true silence - only works on clean studio compilations.")
    p.add_argument("--edge-guard", type=int, default=45, metavar="SEC",
                   help="ignore dips within this many seconds of either end, so the "
                        "intro and closing applause don't steal slots (default: 45)")
    p.add_argument("--noise", type=float, default=-30,
                   help="dB below which counts as silence (default: -30; "
                        "try -25 for noisy live crowds)")
    p.add_argument("--min-silence", type=float, default=0.8,
                   help="seconds of quiet needed to count as a gap (default: 0.8)")
    p.add_argument("--min-spacing", type=float, default=60,
                   help="minimum seconds between chosen boundaries (default: 60)")
    p.add_argument("-o", "--out", default="tracks", help="output folder (default: ./tracks)")
    p.add_argument("-f", "--format", default="copy",
                   choices=["copy", "mp3", "m4a", "flac", "wav", "opus"],
                   help="'copy' cuts losslessly without re-encoding (default)")
    p.add_argument("-q", "--bitrate", type=int, default=256, help="kbps when re-encoding")
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
