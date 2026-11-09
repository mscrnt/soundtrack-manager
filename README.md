# Soundtrack Manager

[![CI](https://github.com/mscrnt/soundtrack-manager/actions/workflows/ci.yml/badge.svg)](https://github.com/mscrnt/soundtrack-manager/actions/workflows/ci.yml)
[![Release](https://github.com/mscrnt/soundtrack-manager/actions/workflows/release.yml/badge.svg)](https://github.com/mscrnt/soundtrack-manager/actions/workflows/release.yml)

A self-hosted download manager for video game soundtracks from
[downloads.khinsider.com](https://downloads.khinsider.com). Search and filter albums,
queue them, download resumably and with verification, keep your library in order, and
optionally ask a local LLM to search for you. It runs in Docker on Windows, Linux,
macOS and Unraid, with a web UI and a CLI.

* **Discover:** search by name, platform, album type, year or year range, and sort. You can
  search several titles at once (`Resident Evil | Silent Hill`). Results show cover art,
  metadata, track lists, formats and estimated sizes.
* **Downloads:** a persistent queue that survives restarts. Downloads resume after
  interruptions, files are checked against the server's sizes and get SHA-256 checksums,
  and FLAC is used when offered (MP3 otherwise). Original file names are kept. Each album
  gets `cover.*` and `album_info.json`. Pause, resume, retry and cancel are available,
  along with live per-track progress and speed.
* **Library:** scan and deep-verify folders, find duplicates and missing or damaged albums,
  import your khinsider account history, and queue recoveries. **Nothing is ever deleted,
  and existing files are only overwritten after you confirm.**
* **Optional AI:** turns a request like "PS1 survival horror soundtracks 1996–2000" into
  normal searches through any OpenAI-compatible local server (Ollama, LM Studio, …).

## Quick start (Docker)

```sh
git clone https://github.com/mscrnt/soundtrack-manager && cd soundtrack-manager
cp .env.example .env            # Windows PowerShell: Copy-Item .env.example .env
# edit .env: set DOWNLOAD_DIR (where your soundtracks should go)
mkdir -p data downloads         # Linux: create before first start so ownership is right
docker compose pull && docker compose up -d     # released image
# or: docker compose up -d --build              # build from source
```

Open <http://127.0.0.1:8080>. Downloads continue after you close the browser. The
queue, settings, browser/FlareSolverr session and logs are kept in `DATA_DIR`, so
restarts and upgrades lose nothing. Pin a version with `APP_VERSION=0.9.0` in `.env`.

* **Windows (Docker Desktop):** use forward slashes in `.env`, e.g. `DOWNLOAD_DIR=D:/Music/Soundtracks`.
* **Unraid:** `PUID=99`, `PGID=100`, `DATA_DIR=/mnt/user/appdata/soundtrack-manager`,
  `DOWNLOAD_DIR=/mnt/user/media/soundtracks`.
* **LAN access:** set `WEB_BIND=0.0.0.0` and a password in `secrets/web_password`. The app
  refuses to start when exposed without one. Use a reverse proxy with HTTPS beyond your home network.
* **An existing collection:** mount it read-only (see the comment in `compose.yaml`) and set
  `LIBRARY_EXTRA_DIRS=/library`.

Images: `ghcr.io/mscrnt/soundtrack-manager:<version>` / `:latest`, for linux/amd64 and linux/arm64.

### Logging in to khinsider (optional; needed for history import)

Put `KHINSIDER_USERNAME=you` in `.env` and the password in `secrets/khinsider_password`
(one line, git-ignored, mounted read-only; setting `KHINSIDER_PASSWORD` in `.env` also works).
Then use **Settings → Log in now** or `python main.py browser login`. The app submits the
site's normal login form through FlareSolverr (or the browser profile) and reuses that
session. The password is never logged or displayed. If the form shows a CAPTCHA, the app
stops and asks you to log in yourself in a browser.

## Cloudflare and FlareSolverr

When this was built, khinsider answered plain HTTP and headless browsers with a Cloudflare
check, while audio files on its CDN downloaded normally. Page requests try, in order
(`FETCH_MODE=auto`):

1. **Plain HTTP**, whenever the site allows it.
2. **[FlareSolverr](https://github.com/FlareSolverr/FlareSolverr)**, **on by default**.
   `docker compose up` starts `ghcr.io/flaresolverr/flaresolverr:v3.5.2`, which only the app
   can reach. One FlareSolverr session keeps cookies and your login. The request delay still applies.
3. **Playwright browser profile**, if FlareSolverr can't be reached. On a desktop,
   `python main.py browser open` lets you complete checks or log in yourself.

If nothing gets through, the UI/CLI shows **"Access check: …"** and stops. Nothing loops.
The badge in the top-right corner shows which backend is in use.

* Disable FlareSolverr: `FLARESOLVERR_URL=` (empty) in `.env`.
* Native runs default to `http://127.0.0.1:8191`:
  `docker run -d --name flaresolverr -p 127.0.0.1:8191:8191 ghcr.io/flaresolverr/flaresolverr:v3.5.2`.
* Keep FlareSolverr at `LOG_LEVEL=info`. Debug logging can include request bodies, such as the login form.
* If you see "FlareSolverr returned the site's check page", update its image tag in `compose.yaml`.

## Native setup (Windows / Linux / macOS)

Requires Python 3.12+.

```sh
python -m venv .venv
# Windows: .venv\Scripts\activate     Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt
python -m playwright install chromium   # Linux: add --with-deps
python main.py serve                    # http://127.0.0.1:8080 (runs the download worker too)
```

Configuration comes from environment variables (the same names as `.env.example`).
Defaults: `./data` and `./downloads`.

## CLI

```sh
# discover
python main.py search "Final Fantasy" --platform ps1
python main.py search "Final Fantasy" -p ps1 -p ps2 --type soundtrack --year 1996-2000 --sort year
python main.py search "Resident Evil | Silent Hill | Parasite Eve" -p ps1
python main.py discover --platform n64 --year 1998          # no text: platform listing + local filters
python main.py ask "PlayStation 1 survival horror soundtracks from 1996 to 2000"   # needs LLM_ENDPOINT
python main.py album <album-url-or-slug>                     # tracks, formats, sizes, target folder
python main.py platforms

# queue and download
python main.py queue list [--status failed]
python main.py queue add <album-id|url|slug>... [--format flac|mp3]
python main.py queue pause|resume|cancel|retry|remove <job-id>...
python main.py download [--resume]          # foreground worker with live progress; Ctrl+C stops, resumes next run
python main.py download --dry-run           # plan only

# library
python main.py history sync [--source downloads|history|all]   # needs a logged-in session
python main.py library scan [PATH] [--deep]                     # read-only; --deep hashes every file
python main.py library list [--status incomplete] [--search mario]
python main.py library duplicates
python main.py library missing
python main.py library recover [ALBUM_ID...] [--format flac] [--yes]

# session / admin
python main.py browser open|login|status
python main.py serve [--host 127.0.0.1] [--port 8080]
python main.py db migrate
```

After `search`, `discover` or `ask`, a prompt lets you queue results: `1,3,5-7`, `page`, or
`all` (every result). Queueing more than 10 albums asks for confirmation. Albums already
queued or downloaded are skipped, so overlapping searches are safe.

Exit codes: `0` ok · `1` some downloads failed · `2` bad input or fetch error · `3` site access check · `4` site layout not recognised.

## How downloads work

1. The worker reads the album page, then each track page, to find the file links the site
   offers. Track pages are fetched politely through the shared rate limiter, and the link
   is stored so it isn't fetched twice.
2. Files go to `<DOWNLOAD_DIR>/<FOLDER_TEMPLATE>/<original file name>`, written as `*.part`
   and resumed with HTTP Range after a crash, pause or restart.
3. Each file's size is checked against the server's, then it's hashed (SHA-256) and renamed into place.
4. A file that already exists with the server's size is **kept** (skipped). One with a
   different size is a **conflict**: it is kept, and you can choose **Overwrite conflicts…**
   to replace it.
5. `album_info.json` records the album metadata and every file's size and checksum. The
   library scan uses it to verify the album later.

Folder template fields: `{platform}` (first platform), `{platforms}`, `{album}`, `{year}`,
`{type}`, `{catalog}`, `{slug}`. Example: `{platform}/{album} ({year})` →
`PS1/Resident Evil_ Save Room Compilation (2023)`. Characters that are invalid on Windows are
replaced, and paths cannot leave the download folder.

## Releases

Versions follow [SemVer](https://semver.org). The single source is
`soundtrack_manager/__init__.py`. To release:

```sh
# bump __version__, commit, then:
git tag -a v0.9.1 -m "v0.9.1" && git push origin v0.9.1
```

The **Release** workflow runs the tests, checks that the tag matches `__version__`, builds
and pushes the multi-arch image (`:0.9.1`, `:0.9`, `:0`, `:latest`; pre-releases like
`v1.0.0-rc.1` skip `:latest`), and creates a GitHub Release with generated notes. **CI** runs
the tests on Linux and Windows, plus a Docker build, for every push and pull request.

## Tests

```sh
pytest
```

The tests use saved HTML from the real site (`tests/fixtures/`, session tokens removed), a
mocked CDN with Range support, and mocked FlareSolverr and LLM servers. They never touch the network.

## Limitations

* Page access depends on FlareSolverr keeping up with Cloudflare. The FlareSolverr, login and
  account-history code is tested against mocked services. The logged-in account pages were
  never available to inspect, so history import uses a deliberately generic parser.
* Search results reflect what the site currently lists. Some franchises (e.g. Resident Evil) have few albums left.
* Metadata is only what the site shows. Missing fields stay empty. Size estimates sum the
  site's rounded per-track sizes.
* FLAC parsing is tested on a modified real page, because no FLAC album was captured.
* The AI feature only produces search terms and filters. Results always come from the site.

Use this tool only for downloads your account is entitled to, and keep the default request
delay. khinsider is run by volunteers.
