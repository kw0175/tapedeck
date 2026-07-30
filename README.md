# soundcloud DL

Command-line downloader that turns SoundCloud URLs into tagged MP3s (or m4a/opus/flac/wav).
Wraps [yt-dlp](https://github.com/yt-dlp/yt-dlp) for extraction and ffmpeg for conversion.

Works on single tracks, sets/playlists, whole user profiles, likes pages, and
private share links (`?s-...`).

> Intended for your own uploads, tracks the artist has marked downloadable, and
> Creative Commons material. Downloading paid or protected catalog is against
> SoundCloud's Terms of Service.

## Setup

**1. Python packages**

```powershell
pip install -r requirements.txt
```

**2. ffmpeg** — required for anything except `--format best`.

```powershell
winget install Gyan.FFmpeg
```

Close and reopen your terminal afterward so `ffmpeg` lands on `PATH`. Verify with
`ffmpeg -version`.

## Usage

```powershell
# one track -> ./downloads/Artist - Title.mp3
python soundcloud_dl.py https://soundcloud.com/artist/track-name

# several at once, into a specific folder
python soundcloud_dl.py URL1 URL2 URL3 -o "D:\Music"

# a whole set, numbered, in its own subfolder
python soundcloud_dl.py https://soundcloud.com/artist/sets/my-set --playlist-folder

# a list of URLs from a file
python soundcloud_dl.py --batch urls.txt

# skip anything already grabbed on a previous run
python soundcloud_dl.py --batch urls.txt --archive done.txt

# keep the original file, no transcode (fastest, no quality loss)
python soundcloud_dl.py --format best URL

# your own private/unlisted uploads (reads your logged-in browser session)
python soundcloud_dl.py --cookies-from-browser chrome URL
```

### Options

| Flag | Default | What it does |
|---|---|---|
| `-o, --out` | `./downloads` | Output directory |
| `-f, --format` | `mp3` | `mp3`, `m4a`, `opus`, `flac`, `wav`, or `best` (no conversion) |
| `-q, --bitrate` | `320` | kbps for lossy formats |
| `-b, --batch` | — | Text file, one URL per line, `#` comments allowed |
| `--playlist-folder` | off | Sets go in their own numbered subfolder |
| `--template` | — | Raw yt-dlp output template override |
| `--archive` | — | File of already-downloaded ids; skipped on reruns |
| `--limit N` | — | Only the first N items of a set/profile |
| `--cookies-from-browser` | — | `chrome`, `firefox`, `edge`, `brave`, `opera`, `vivaldi`, `safari` |
| `--cookies` | — | Netscape `cookies.txt` instead of a live browser |
| `--no-cover` | off | Skip embedding artwork |
| `--no-metadata` | off | Skip writing title/artist tags |

Title, artist, and cover art are embedded into the file automatically unless you
opt out.

## Splitting a long recording

`split_tracks.py` cuts a concert, DJ set, or mixtape into individual tagged tracks.
It runs in two steps so you can correct the boundaries before committing.

```powershell
# 1. find the boundaries -> writes an editable cuesheet
python split_tracks.py concert.m4a --detect --expect 12 --names tracklist.txt `
    --artist "Oasis" --album "Live At Wolverhampton 1994" --date 1994 --cue-out cue.txt

# 2. check/adjust the times in cue.txt, then cut
python split_tracks.py concert.m4a --cue cue.txt -o tracks
```

`--names` takes a plain text file of song titles, one per line, in playing order.

### Detection methods

`--method auto` (the default) tries these in order:

| Method | How it works | Best for |
|---|---|---|
| `gaps` | Finds the short digital silences that joined uploads use as separators, and **trims them out** so no dead air survives into a track | Uploads assembled from separate song files — very common, and exact when it applies |
| `envelope` | Builds a per-second loudness envelope and ranks *relative* dips by prominence | True live recordings, where constant crowd noise defeats silence detection |
| `silence` | Absolute silence threshold | Clean studio compilations |

Segments shorter than `--min-track` (default 45s) are discarded, which drops spoken
intros and closing applause instead of turning them into tracks.

### Split options

| Flag | Default | What it does |
|---|---|---|
| `--detect` | — | Find boundaries and write a cuesheet |
| `--cue FILE` | — | Cuesheet to split with |
| `--names FILE` | — | Track titles, one per line |
| `--expect N` | — | How many tracks to expect |
| `--method` | `auto` | `auto`, `gaps`, `envelope`, `silence` |
| `--min-track` | `45` | Segments shorter than this aren't songs |
| `--gap-noise` | `-50` | dB threshold for a separator |
| `--gap-min` | `0.6` | Minimum separator length, seconds |
| `--fade MS` | `24` | Fade in/out at each cut; kills clicks (forces re-encode) |
| `-f, --format` | `copy` | `copy`, `mp3`, `m4a`, `flac`, `wav`, `opus` |
| `--dry-run` | off | Show the cuts, write nothing |

### Cuesheet format

```
ALBUM:  Live At Wolverhampton Civic Hall
ARTIST: Oasis
DATE:   1994

0:44 - 5:22.18    Rock 'n' Roll Star     <- explicit end: gap trimmed out
5:24.16           Columbia               <- no end: runs to the next start
```

### About `--fade` and quality

Concatenated uploads often have a click exactly where one song was joined to the
next, and a cut lands right on it — you hear a blip before the music starts. A few
milliseconds of fade removes it.

Fading needs a filter, and filters need decoded audio, so **any fade forces a
re-encode**. Two ways to avoid a second generation of lossy compression:

- `--format flac` — lossless, so the re-encode costs nothing in quality (bigger files)
- `--fade 0` — true lossless stream copy, but you keep the clicks

The default (`--fade 24` into the source format at 256kbps) is a middle ground:
audibly transparent from a 128–160kbps SoundCloud source, and small.

## Notes

- **`--format best` vs `mp3`:** SoundCloud serves most streams as ~128kbps
  Opus/AAC. Transcoding that to 320kbps MP3 does not add quality — it just makes a
  bigger file that plays everywhere. Use `best` if your player handles Opus.
- **Failures on a big set** don't stop the run; anything that failed is listed at
  the end.
- **Common failure causes:** track is Go+ only, private, deleted, or geo-blocked.
- **Keep yt-dlp current** — SoundCloud changes their API periodically and yt-dlp
  ships fixes fast:
  ```powershell
  pip install -U yt-dlp
  ```
