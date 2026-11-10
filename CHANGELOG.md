# What's new

Plain-language notes for each release. To update, see [Updating](README.md#updating).

## 0.2.0 — 2026-11-10

### Import a whole shopping list

You can now hand the app a list of albums and let it fetch them all. There's a new **Import** page,
and the same thing works from the command line with `python main.py import add my-list.txt`.

- **Use the list you already have.** A numbered checklist in a text file works:
  `[ ]  12. Chrono Cross Original Soundtrack`, with `PRIORITY 1` headings, notes after a dash, and a
  khinsider link underneath. So does a CSV from a spreadsheet. Entries you've ticked off (`[x]`) are skipped.
- **It finds the right album, not just a similar one.** Each entry is searched on the site, and the
  results are compared with the title you wrote. "Original Soundtrack" and "OST" are ignored, numbers
  have to agree (Final Fantasy IX is not X), and `(1996)` or `(NES)` in a title helps pick the right
  version. When your list says "Original Soundtrack", the official release beats a game rip; an
  arrangement album only wins if you asked for one.
- **It checks for FLAC before downloading.** Albums that only come in MP3 are flagged and left out,
  unless you choose to take them anyway.
- **You decide the close calls.** When two albums fit equally well, for example the two *Prepare to
  Die* releases of Dark Souls, or Radiant Silvergun's SOUNDTRACK+ and SOUNDTRACK-, the entry waits
  under **Needs review**. Pick one, paste a link, search again with other words, or skip it.
- **Priority 1 downloads first.** Queueing follows your list's priorities, and albums you queue by
  hand still go ahead of the list.
- **It's safe to stop.** Matching continues after a restart. You can pause it, queue what's matched
  so far, and export the results as a CSV.
- **Check a file first.** **Check file** (or `import check`) points out problems line by line without
  importing anything. `import convert` turns a text list into a CSV.
- **Templates:** `examples/import-template.txt` and `examples/import-template.csv`, also downloadable
  from the Import page.

In a test against a real 20-album list, half matched automatically. The rest were real choices (two
editions of the same album) or albums the site no longer has.

### An API, with docs you can click through

Turn on `API_ENABLED=true` to get a JSON API at `/api/v1` for search, queue, jobs, import lists and
the library. Interactive Swagger docs are at `/api/v1/docs`, so you can try every call from the browser.

### Let AI agents drive it (MCP)

Turn on `MCP_ENABLED=true` and the app serves an MCP server at `/mcp`. Claude Code and other agents
can then search, import a list, sort out the review entries and queue downloads. The tools mirror the
API one for one. If you work in this repo, `.mcp.json` sets Claude Code up for you.

**No token needed at home.** Without `API_TOKEN`, the API and MCP server accept your web login, or
only connections from this machine if you haven't set a web password. Requests from other websites
and DNS-rebinding tricks are blocked either way. Set `API_TOKEN` if other machines or containers need access.

### Smaller things

- The Settings page shows whether the API and MCP server are on, who can reach them, and the exact
  command to add the MCP server to Claude Code.
- Database migrations are sturdier: a semicolon in an SQL comment no longer breaks them.
- Updated FastAPI, Starlette, Uvicorn and python-multipart. FastMCP is new.

### Updating from 0.1.x

Run `git pull`, `docker compose pull` and `docker compose up -d`. The database upgrades itself on the
first start. Nothing you already have changes, and the API and MCP server stay off until you turn them
on. New optional settings are in `.env.example` under *REST API and MCP server*.

## 0.1.2 — 2026-11-09

- **AI search is fast with "thinking" models.** Reasoning models such as Qwen3 on vLLM used to think
  for minutes before answering. Thinking is now switched off for these requests, so answers take about
  a second, and stray `<think>` text is ignored.
- **Your password stays out of FlareSolverr's logs.** Compose now runs FlareSolverr at `warning` level.
  At `info` it logged every request body, including the khinsider login.
- **Secret files are more forgiving.** An empty secret file no longer hides the value in `.env`, and
  files saved by Notepad (with a byte-order mark) are read correctly.

## 0.1.1 — 2026-11-09

- The project is now MIT-licensed.
- Fixed a timing bug that could make Windows send requests slightly faster than the configured delay.

## 0.1.0 — 2026-11-09

First release: search and filter khinsider albums, a download queue that survives restarts and resumes
broken downloads, verified FLAC/MP3 downloads with cover art, library scanning with duplicate and
missing-album checks, khinsider login through FlareSolverr, optional AI-assisted search, a web UI and a CLI.
