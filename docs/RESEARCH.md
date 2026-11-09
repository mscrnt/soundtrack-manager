# Phase 1 research: downloads.khinsider.com

Captured 2026-11-09 from a normal, visible Chromium window, with about a dozen page
loads spaced several seconds apart. The trimmed pages are in `tests/fixtures/`,
with session tokens redacted.

## Access

| Client | Result |
|---|---|
| `curl` / httpx (any User-Agent) | **403, Cloudflare "Just a moment..." challenge** on every HTML page |
| Playwright Chromium, **headless** | **403, same challenge** |
| Playwright Chromium, **visible window** with a persistent profile | 200, normal pages. No interaction was needed this time |
| Audio CDN (`vgmtreasurechest.com`) via plain HTTP | **200**, `Content-Length`, `Accept-Ranges: bytes`, `audio/mpeg` |

Consequences:

* **HTML pages** (search, browse, album, track, account) need a real browser
  session, at least from the network tested. Other networks may get plain
  HTTP through, so the app tries HTTP first and switches to the browser only
  when it detects a challenge.
* The owner chose to run **FlareSolverr** (a separate container, on by default in
  `compose.yaml`) for page requests that get the challenge. The app has no
  challenge-solving code of its own: it sends requests through FlareSolverr's
  API, or through a Playwright profile where a person can complete checks. When
  nothing works it reports `AccessBlocked`.
* **Audio files** come from the CDN with plain HTTP. Byte ranges are
  supported, so downloads can resume and be checked against `Content-Length`.
* `robots.txt` has only `Disallow: /unavailable`.

## Search: `GET /search`

Parameters, taken from the site's own filter `onchange` URLs:

| Param | Values |
|---|---|
| `search` | free text |
| `type` | `album` or `song` |
| `sort` | `name`, `timestamp` (date added), `popularity`, `year`, `relevance` |
| `album_type` | `1` Soundtracks, `2` Gamerips, `3` Singles, `4` Remixes, `5` Arrangements, `6` Compilations, `7` Inspired By |
| `album_year` | single year, 1980 to current |
| `album_category` | platform id (69 platforms; see `soundtrack_manager/khinsider/catalog.py`) |

* **All results come on one page.** "final fantasy" returned 533 rows, and `&page=2`
  returned the same page.
* Only **one** platform, **one** type and **one** exact year per request. Year
  ranges and multiple platforms are filtered locally.
* The site's platform `<select>` is wrongly named `album_year`, so the app builds
  URLs directly.
* The result count appears as `Found N matching albums.` / `Found 0 matching results.`
* "resident evil" returns only 10 albums, none of them typed Soundtrack. Many
  franchise albums are no longer listed. The app shows what the site returns and
  adds nothing.

Result table `table.albumList`, with header `[icon] Album | Platform | Type | Year`:

* `td.albumIcon img[src]` holds a thumbnail on `*.vgmtreasurechest.com/.../thumbs_small/...`.
* The title cell has `a[href=/game-soundtracks/album/<slug>]`. An optional
  `<span>[CATALOG-NO]</span>` follows.
* The platform cell holds one `<a>` per platform, using display names (`PS1`, `Genesis / Mega Drive`).
* The type cell uses singular labels (`Remix`, `Arrangement`, `Gamerip`, …).

## Platform browse: `GET /game-soundtracks/<slug>`

* Uses the same `table.albumList` layout. The header adds sort arrows.
* **500 albums per page**, paged with `?page=N&orderby=name&orderdirection=ASC`.
  PS1 reports `Found 4296 albums!` across 9 pages.
* Slugs come from `/console-list` (e.g. PS1 → `playstation`, N64 → `nintendo-64`).

## Album page: `GET /game-soundtracks/album/<slug>`

* `#pageContent h2` holds the title.
* The metadata `<p>` has one `Label: value` per `<br>`. Seen: `Platforms`, `Year`,
  `Published by`, `Number of Files`, `Total Filesize`, `Date Added`, `Album type`,
  `Uploaded by`. The parser keeps every pair in `raw_metadata` and normalises the
  known ones. Missing fields stay `None`.
* `.albumImage a[href]` links to full-size artwork; `thumbs/` holds the thumbnails.
* `a[href=/cp/add_album/<id>]` is labelled "Download all songs at once". It is an
  account feature and gives the numeric album id.
* `table#songlist` has the header `[play] # | Song Name (colspan 2) | MP3 | [dl] | [playlist]`.
  **Format columns are the header cells after "Song Name"**, such as MP3 and FLAC
  when offered. Multi-disc albums add a `CD` column. Each row has a number
  (`1.`), title link, duration, a size per format, and a download icon. The
  `#songlist_footer` row has totals.
* An obfuscated (packed) script also holds the player playlist with direct MP3 URLs.
  The app **does not** use it and parses the plain HTML table instead.

## Track page: `GET /game-soundtracks/album/<slug>/<double-encoded filename>`

* Each available format appears as `<a href="https://vgmtreasurechest.com/...">`
  wrapping `<span class="songDownloadLink">Click here to download as MP3</span>`,
  followed by `(6.26 MB)`.
* The CDN file name is the original file name, e.g. `01 R.E. 1 Save Room- Safe Haven.mp3`.

## Account

* Login form: `/forums/login` (XenForo), POST to `/forums/index.php?login/login`,
  with fields `login`, `password`, `remember`, `_xfToken`. A passkey/WebAuthn
  form is also on the page. **No CAPTCHA was present** in this capture.
* Logged-out pages show `<a href="/forums/login">Log In</a>` in `#leftColumn`.
* Account pages: `/cp/downloads` ("My Profile"), `/cp/history` ("My History"),
  `/cp/favorites`, `/cp/uploads`. Their structure when logged in is **not yet
  inspected**. That is milestone 7, and it needs your session.

## marcus-crane/khinsider (Go)

* MIT licensed, **archived March 2026**, last commit January 2024.
* v3 does not scrape khinsider at all. It downloads a prebuilt third-party index
  (`khindex.utf9k.net`) and per-album JSON. That service is outside our control,
  so we don't depend on it.
* Its v2 code confirms the `table.albumList` and `.pagination-next` era of the
  site layout.
* It has no FLAC download, cover download, rate limiting, concurrency or retries.
  It also has bugs: an unchecked HTTP status writes error pages as `.mp3`, and a
  nil response is dereferenced.
* **Reused:** only concepts, namely file naming that keeps track numbers and
  per-album folders. No code is reused.
