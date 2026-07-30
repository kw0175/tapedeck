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
