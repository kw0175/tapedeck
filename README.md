# tapedeck

Pull a concert, DJ set or single track off the web and file it into your music
library — split into numbered tracks, tagged, with artwork, ready for Apple Music
or anything else that reads a folder of MP3s.

Runs on your own machine. Nothing is uploaded anywhere and there is no service in
the middle.

Built on [yt-dlp](https://github.com/yt-dlp/yt-dlp) for fetching and
[ffmpeg](https://ffmpeg.org/) for audio work.

## Where it can read from

yt-dlp supports around 1,750 sites, and tapedeck passes URLs straight through, so
anything it handles works here. Commonly:

**YouTube** · **SoundCloud** · **Vimeo** · **Dailymotion** · **Bandcamp** ·
**Mixcloud** · **Twitch** · **Internet Archive**

Worth knowing about the last one: the Internet Archive's
[Live Music Archive](https://archive.org/details/etree) holds tens of thousands of
live concert recordings that the artists have explicitly authorised for free
distribution. If you are after live shows, start there — it is the largest source
that is unambiguously yours to keep.

Chapter-based splitting needs the site to expose chapter markers, which in
practice means YouTube. Everywhere else, tapedeck detects track boundaries from
the audio itself.

## What this is for

tapedeck is a personal archiving tool. It is built for material you already have
a right to: your own uploads, artist-authorised downloads, Creative Commons and
public-domain works, and the taper-authorised recordings on the Live Music
Archive.

It does not circumvent DRM, and cannot — yt-dlp decodes nothing that is
encrypted. It has no sharing, hosting or upload features. Everything it produces
stays on the machine that ran it.

Downloading commercial catalogue generally conflicts with the terms of service of
the site it came from, whatever the copyright position where you live. That is
your call and your responsibility. This project does not condone redistributing
licensed material, and is not affiliated with any of the platforms it can read.

## Web UI

Paste a link, name a destination folder, press Download. The job runs in the
background: fetch best audio → split on chapters if the upload has them → convert
→ embed artwork. The destination folder is created if it doesn't exist.

```powershell
python server.py
python server.py --root "C:\Users\Public\AppleMusic" --port 8800
```

Then open <http://localhost:8800>.

Artist / Album / Year boxes override the upload's own tags. Worth filling in for
YouTube, where the "artist" is otherwise the channel name.

### Reaching it from anywhere (Cloudflare Worker + tunnel)

The Worker serves the page and forwards `/api/*` to your PC. It cannot do the work
itself — no Python, no ffmpeg, and no way to write to your music folder — so
`server.py` still has to be running at home.

Quick tunnels get a new hostname every restart, so the backend URL isn't baked in:
`tunnel.py` reads whatever hostname it was handed and POSTs it to the Worker's
`/_register`, which stores it in KV.

One-time setup:

```powershell
cd worker
wrangler kv namespace create STATE      # paste the id into wrangler.toml
wrangler secret put ADMIN_TOKEN         # any long random string
wrangler deploy
```

Then, at home, two windows:

```powershell
python server.py --root "C:\Users\<you>\Music" --token <SERVER_TOKEN>
python tunnel.py --worker https://<name>.workers.dev --admin-token <ADMIN_TOKEN>
```

Open the Worker URL from anywhere. Two separate secrets, deliberately: `ADMIN_TOKEN`
only lets a machine register itself, `SERVER_TOKEN` is what the page asks you for.

If the PC is off or the tunnel is down, the page returns a clear 503 rather than
failing silently.

### Exposing it

`server.py` binds to `127.0.0.1` and refuses to write outside `--root`. Both
matter if you put it behind a Cloudflare Tunnel or similar — an open endpoint that
downloads arbitrary URLs onto your machine is not something to leave unauthenticated.

```powershell
python server.py --host 0.0.0.0 --token "some-long-random-string" --root "C:\Users\Public\AppleMusic"
```

The page prompts for the token once and remembers it. Starting with `--host` set to
anything public and no `--token` prints a warning.

Note this runs on **your** machine, which matters: SoundCloud and YouTube block
datacenter IPs aggressively, so the same code on cloud hosting hits bot checks that
a home connection doesn't.

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
python tapedeck.py https://soundcloud.com/artist/track-name

# several at once, into a specific folder
python tapedeck.py URL1 URL2 URL3 -o "D:\Music"

# a whole set, numbered, in its own subfolder
python tapedeck.py https://soundcloud.com/artist/sets/my-set --playlist-folder

# a list of URLs from a file
python tapedeck.py --batch urls.txt

# skip anything already grabbed on a previous run
python tapedeck.py --batch urls.txt --archive done.txt

# keep the original file, no transcode (fastest, no quality loss)
python tapedeck.py --format best URL

# your own private/unlisted uploads (reads your logged-in browser session)
python tapedeck.py --cookies-from-browser chrome URL
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

### Use chapters when they exist

If the upload has chapter markers — or timestamps in the description that YouTube
parsed into chapters — those boundaries are exact. Always prefer them:

```powershell
python split_tracks.py concert.webm --detect --from-chapters "https://youtu.be/VIDEOID" `
    --artist "Oasis" --album "MTV Unplugged 1996" --date 1996 --cue-out cue.txt
```

Titles come from the chapters too, so no `--names` file is needed. Everything below
is only for recordings with no chapters.

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

## Album artwork

```powershell
python add_art.py cover.jpg -d "Artist - Album"
python add_art.py --from-track https://soundcloud.com/user/track -d "Artist - Album"
```

`--from-track` pulls the artwork straight off the SoundCloud page. Non-square images
are centre-cropped to the largest square they contain, then scaled to `--size`
(default 500). Audio is stream-copied, so embedding costs nothing in quality. A
folder-level `cover.jpg` is written too, since many players prefer that over tags.

## Converting formats

```powershell
python convert_folder.py -d "Artist - Album" --format alac
python convert_folder.py -d "Artist - Album" --format mp3 -q 320 -o "somewhere else"
```

| Target | Type | Notes |
|---|---|---|
| `alac` | lossless | Apple Lossless in `.m4a` |
| `flac` | lossless | |
| `wav` | lossless | uncompressed; no tag support |
| `m4a` | lossy | AAC |
| `mp3` | lossy | most universally supported |

Tags and embedded art carry across. The script reports whether a conversion
preserves audio exactly, and warns on lossy → lossy, which stacks artefacts.

### Getting a bootleg into Apple Music on Windows

Verified the hard way, on Apple Music for Windows 1.1540:

- **FLAC does not work.** Apple has never supported it in iTunes or Apple Music.
- **ALAC did not work either**, despite being Apple's own lossless codec. The app
  silently refused ffmpeg-written `.m4a` files — no error, no import.
- **MP3 works.** `--format mp3 -q 320` is the reliable route.

The app's local-file import is buggy in general: many users report `Import`
missing from the sidebar's `⋯` menu and drag-and-drop failing without any error.
If it fights you, MusicBee or foobar2000 read the original FLAC folders directly,
artwork and tags included, with no conversion at all.

**On quality:** SoundCloud serves 96–160kbps, so that is the ceiling regardless of
what you convert to. MP3 320 gives the encoder far more headroom than the source
ever used, making the loss effectively inaudible. Keep FLAC as the archival copy
and use MP3 for playback.

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
