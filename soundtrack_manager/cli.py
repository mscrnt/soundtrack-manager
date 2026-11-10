"""Command-line interface. Run ``python main.py --help``."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys

from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

from . import __version__, queue
from .config import FORMATS
from .discovery import DiscoveryResult, QueryError, SearchQuery
from .khinsider.catalog import ALBUM_TYPES, PLATFORMS, SEARCH_SORTS
from .khinsider.fetch import AccessBlocked, FetchError
from .khinsider.parse import LayoutChangedError, size_to_bytes
from .khinsider.urls import album_slug
from .paths import render_album_dir

console = Console()


def parse_selection(text: str, n: int) -> list[int]:
    """'1,3,5-7' -> [0,2,4,5,6] (0-based, within 1..n). 'all' -> every index."""
    text = text.strip().lower()
    if not text:
        return []
    if text == "all":
        return list(range(n))
    out: list[int] = []
    for part in re.split(r"[,\s]+", text):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            raise ValueError(f"invalid selection: {part!r}")
        a = int(m.group(1))
        b = int(m.group(2) or a)
        if a > b:
            a, b = b, a
        if a < 1 or b > n:
            raise ValueError(f"selection {part} out of range 1-{n}")
        out.extend(i - 1 for i in range(a, b + 1) if i - 1 not in out)
    return out


def _results_table(res: DiscoveryResult) -> Table:
    t = Table(title=f"{res.total} album(s) - page {res.page}/{res.page_count}"
                    + (" (cached)" if res.from_cache else ""), show_lines=False)
    t.add_column("#", justify="right")
    t.add_column("Album")
    t.add_column("Platforms")
    t.add_column("Type")
    t.add_column("Year", justify="right")
    t.add_column("Tracks/Formats")
    start = (res.page - 1) * res.per_page
    for i, a in enumerate(res.page_albums, start=1):
        tf = f"{a['track_count']} · {', '.join(a['formats'] or [])}" if a.get("detail_fetched_at") else "-"
        title = a["title"] + (f" [{a['catalog_number']}]" if a.get("catalog_number") else "")
        t.add_row(str(start + i), title, ", ".join(a["platforms"]), a["album_type"] or "-",
                  str(a["year"] or "-"), tf)
    return t


async def _with_ctx(fn):
    from .runtime import AppContext
    ctx = AppContext()
    try:
        return await fn(ctx)
    finally:
        await ctx.close()


def _run(coro_fn) -> int:
    try:
        return asyncio.run(_with_ctx(coro_fn)) or 0
    except AccessBlocked as exc:
        console.print(f"[yellow]Access check:[/] {exc}")
        return 3
    except LayoutChangedError as exc:
        console.print(f"[red]Site layout not recognised (it may have changed):[/] {exc}")
        return 4
    except (FetchError, QueryError, ValueError) as exc:
        console.print(f"[red]Error:[/] {exc}")
        return 2


# ----------------------------------------------------------------------
def cmd_search(args) -> int:
    query = SearchQuery.build(args.text or "", args.platform, args.type, args.year, args.sort)

    async def go(ctx):
        res = await ctx.discovery.discover(query, page=args.page, per_page=args.per_page, refresh=args.refresh)
        if args.json:
            print(json.dumps({"total": res.total, "site_total": res.site_total, "page": res.page,
                              "page_count": res.page_count, "notes": res.notes,
                              "albums": res.page_albums}, indent=2, default=str))
            return 0
        console.print(_results_table(res))
        for n in res.notes:
            console.print(f"[cyan]note:[/] {n}")
        if args.no_interactive or not sys.stdin.isatty() or res.total == 0:
            return 0
        return _interactive_select(ctx, res)

    return _run(go)


def _interactive_select(ctx, res: DiscoveryResult) -> int:
    page_albums = res.page_albums
    offset = (res.page - 1) * res.per_page
    answer = Prompt.ask(
        "Select albums to queue (e.g. 1,3,5-7 · 'page' = this page · 'all' = all "
        f"{res.total} results · Enter = none)", default="")
    answer = answer.strip()
    if not answer:
        return 0
    if answer.lower() == "all":
        chosen = res.albums
    elif answer.lower() == "page":
        chosen = page_albums
    else:
        idx = parse_selection(answer, res.total)
        chosen = [res.albums[i] for i in idx]
        if any(not offset <= i < offset + len(page_albums) for i in idx):
            console.print("[dim]Note: some selections are on other pages.[/]")
    if not chosen:
        return 0
    fmt = Prompt.ask("Format", choices=list(FORMATS), default=ctx.settings.preferred_format)
    if len(chosen) > 10 and not Confirm.ask(f"Queue {len(chosen)} albums as {fmt.upper()}?", default=False):
        return 0
    r = queue.enqueue(ctx.db, [a["id"] for a in chosen], fmt)
    console.print(f"[green]Queued {len(r.added)}[/]; {len(r.already_queued)} already queued, "
                  f"{len(r.already_completed)} already downloaded.")
    return 0


def cmd_album(args) -> int:
    slug = album_slug(args.album)

    async def go(ctx):
        a = await ctx.discovery.album_detail(slug, refresh=args.refresh)
        if args.json:
            print(json.dumps(a, indent=2, default=str))
            return 0
        console.print(f"[bold]{a['title']}[/]  {a['url']}")
        for label, key in (("Platforms", "platforms"), ("Year", "year"), ("Type", "album_type"),
                           ("Catalog", "catalog_number"), ("Publisher", "publisher"), ("Developer", "developer"),
                           ("Date added", "date_added"), ("Files", "file_count"), ("Total size", "total_size_text"),
                           ("Formats", "formats")):
            v = a.get(key)
            if v:
                console.print(f"  {label}: {', '.join(v) if isinstance(v, list) else v}")
        console.print(f"  Folder: {render_album_dir(ctx.settings.folder_template, a)}")
        t = Table(show_header=True)
        t.add_column("#", justify="right")
        t.add_column("Title")
        t.add_column("Length")
        for f in a.get("formats") or []:
            t.add_column(f, justify="right")
        est: dict[str, int] = {}
        for tr in a["tracks"]:
            t.add_row(str(tr["number"] or ""), tr["title"], tr["duration"] or "",
                      *[tr["sizes"].get(f, "-") for f in a.get("formats") or []])
            for f, s in tr["sizes"].items():
                est[f] = est.get(f, 0) + (size_to_bytes(s) or 0)
        console.print(t)
        for f, b in est.items():
            console.print(f"  Estimated {f}: {b / 1024 / 1024:.1f} MB (sum of listed track sizes)")
        return 0

    return _run(go)


def cmd_queue(args) -> int:
    async def go(ctx):
        if args.action == "list":
            jobs = queue.list_jobs(ctx.db, statuses=tuple(args.status) if args.status else None)
            t = Table(title="Download queue")
            for col in ("ID", "Status", "Format", "Album", "Files", "Error"):
                t.add_column(col)
            for j in jobs:
                t.add_row(str(j["id"]), j["status"], j["format"], j["title"],
                          f"{j['files_done']}/{j['files_total'] or '?'}", j["error"] or "")
            console.print(t)
            console.print(queue.counts(ctx.db))
            return 0
        if args.action == "add":
            ids = []
            for ref in args.refs:
                if ref.isdigit():
                    ids.append(int(ref))
                else:
                    a = await ctx.discovery.album_detail(album_slug(ref))
                    ids.append(a["id"])
            fmt = args.format or ctx.settings.preferred_format
            r = queue.enqueue(ctx.db, ids, fmt)
            console.print(f"Queued {len(r.added)}; already queued {len(r.already_queued)}; "
                          f"already downloaded {len(r.already_completed)}; unknown ids {r.missing}")
            return 0
        fn = {"cancel": queue.cancel, "pause": queue.pause, "resume": queue.resume,
              "retry": queue.retry, "remove": queue.remove}[args.action]
        bad = [j for j in args.ids if not fn(ctx.db, j)]
        if bad:
            console.print(f"[yellow]No change for job(s) {bad} (wrong state or unknown id)[/]")
        return 0

    return _run(go)


def _jobs_live_table(ctx, worker) -> Table:
    live = worker.snapshot()
    t = Table(title="Downloads", expand=True)
    for col in ("ID", "Album", "Status", "Tracks", "Progress", "Speed", "Now / error"):
        t.add_column(col, overflow="fold")
    for j in queue.list_jobs(ctx.db, statuses=("running", "queued", "paused", "failed", "completed"))[:25]:
        pct = (j["bytes_done"] / j["bytes_total"] * 100) if j["bytes_total"] else 0
        info = live.get(j["id"], {})
        t.add_row(str(j["id"]), j["title"], j["status"], f"{j['files_done']}/{j['files_total'] or '?'}",
                  f"{min(pct, 100):.0f}%", info.get("speed_text", ""),
                  info.get("current_file") or j["error"] or j["note"] or "")
    return t


def cmd_download(args) -> int:
    async def go(ctx):
        if args.resume:
            for j in queue.list_jobs(ctx.db, statuses=("paused",)):
                queue.resume(ctx.db, j["id"])
        jobs = queue.list_jobs(ctx.db, statuses=("queued",))
        if not args.dry_run:
            if not jobs:
                console.print("Nothing queued.")
                return 0
            from rich.live import Live

            from .downloader import DownloadWorker
            worker = DownloadWorker(ctx)
            ctx.worker = worker  # ctx.close() stops it gracefully (Ctrl+C included)
            with Live(_jobs_live_table(ctx, worker), console=console, refresh_per_second=2) as live:
                await worker.run_until_idle(on_tick=lambda: live.update(_jobs_live_table(ctx, worker)))
                live.update(_jobs_live_table(ctx, worker))
            c = queue.counts(ctx.db)
            console.print(f"Done: {c['completed']} completed, {c['failed']} failed, {c['paused']} paused.")
            return 0 if c["failed"] == 0 else 1
        total = 0
        for j in jobs:
            a = await ctx.discovery.album_detail(j["slug"])
            fmt = j["format"].upper()
            have = fmt in (a.get("formats") or [])
            use = fmt if have else "MP3"
            size = sum(size_to_bytes(t["sizes"].get(use)) or 0 for t in a["tracks"])
            total += size
            console.print(f"[{j['id']}] {a['title']}: {len(a['tracks'])} tracks as {use}"
                          f"{'' if have else ' (FLAC not offered)'} · ~{size / 1048576:.0f} MB → "
                          f"{ctx.settings.download_dir / str(render_album_dir(ctx.settings.folder_template, a))}")
        console.print(f"{len(jobs)} job(s), ~{total / 1048576:.0f} MB total (dry run: nothing downloaded)")
        return 0

    return _run(go)


def cmd_platforms(args) -> int:
    t = Table(title="Platforms")
    t.add_column("Name")
    t.add_column("Browse slug")
    t.add_column("Search id", justify="right")
    for p in PLATFORMS:
        t.add_row(p.name, p.slug, str(p.category_id))
    console.print(t)
    console.print("Album types: " + ", ".join(f"{a.row_label.lower()} ({a.id})" for a in ALBUM_TYPES))
    console.print("Sorts: " + ", ".join(SEARCH_SORTS))
    return 0


def cmd_browser(args) -> int:
    async def go(ctx):
        if ctx.browser is None:
            raise ValueError("FETCH_MODE=http disables the browser session")
        if args.action == "open":
            if ctx.browser.headless:
                ctx.browser.headless = False
            console.print("A browser window opens. Complete any site check or log in yourself, "
                          "then close the window. The session is saved in the data directory.")
            await ctx.browser.open_interactive()
            return 0
        if args.action == "login":
            ok = await ctx.login()
            console.print("[green]Logged in.[/]" if ok else "[red]Login failed.[/]")
            return 0 if ok else 1
        # status
        html = await ctx.fetcher.get_html("https://downloads.khinsider.com/")
        from .khinsider.parse import login_state
        console.print({**ctx.status.public(), "logged_in": login_state(html)})
        return 0

    return _run(go)


def cmd_serve(args) -> int:
    import uvicorn

    from .config import load_settings
    s = load_settings()
    uvicorn.run("soundtrack_manager.web.app:create_app", factory=True,
                host=args.host or s.web_host, port=args.port or s.web_port,
                log_level=s.log_level.lower(), proxy_headers=False, timeout_graceful_shutdown=20)
    return 0


def cmd_db(args) -> int:
    from .config import load_settings
    from .db import Database
    db = Database(load_settings().db_path)
    applied = db.migrate()
    console.print(f"schema version {db.schema_version()} at {db.path}" + (f"; applied {applied}" if applied else ""))
    return 0


# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="main.py", description="khinsider soundtrack discovery & download manager")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_filters(sp, text_required: bool):
        if text_required:
            sp.add_argument("text", nargs="?", default="", help="game or album name")
        sp.add_argument("--platform", "-p", action="append", default=[], help="platform (repeatable): ps1, snes, n64, pc...")
        sp.add_argument("--type", "-t", help="album type: soundtrack, gamerip, arrangement, remix, single, compilation, 'inspired by'")
        sp.add_argument("--year", "-y", help="year or range: 1998, 1996-2000, 1996-")
        sp.add_argument("--sort", "-s", default="relevance", choices=list(SEARCH_SORTS))
        sp.add_argument("--page", type=int, default=1)
        sp.add_argument("--per-page", type=int, default=50)
        sp.add_argument("--refresh", action="store_true", help="ignore the cache and fetch from the site")
        sp.add_argument("--json", action="store_true", help="print JSON instead of a table")
        sp.add_argument("--no-interactive", action="store_true", help="don't prompt to queue results")

    sp = sub.add_parser("search", help="search albums (text and/or filters)")
    add_filters(sp, True)
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("discover", help="list a platform's albums with filters (no text needed)")
    add_filters(sp, False)
    sp.set_defaults(func=cmd_search, text="")

    sp = sub.add_parser("ask", help="natural-language search via your local LLM (optional; LLM_ENDPOINT)")
    sp.add_argument("request", nargs="+", help='e.g. "PS1 survival horror soundtracks 1996-2000"')
    sp.add_argument("--per-page", type=int, default=50)
    sp.add_argument("--no-interactive", action="store_true")
    sp.set_defaults(func=cmd_ask)

    sp = sub.add_parser("album", help="show album details (tracks, formats, sizes)")
    sp.add_argument("album", help="album URL or slug")
    sp.add_argument("--refresh", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_album)

    sp = sub.add_parser("queue", help="manage the download queue")
    qsub = sp.add_subparsers(dest="action", required=True)
    q = qsub.add_parser("list")
    q.add_argument("--status", action="append", choices=["queued", "running", "paused", "completed", "failed", "cancelled"])
    q = qsub.add_parser("add")
    q.add_argument("refs", nargs="+", help="album ids (from search --json) or album URLs/slugs")
    q.add_argument("--format", "-f", choices=list(FORMATS))
    for action in ("cancel", "pause", "resume", "retry", "remove"):
        q = qsub.add_parser(action)
        q.add_argument("ids", nargs="+", type=int)
    sp.set_defaults(func=cmd_queue)

    sp = sub.add_parser("download", help="process the queue in the foreground (Ctrl+C stops; resumes next time)")
    sp.add_argument("--resume", action="store_true", help="also resume paused jobs")
    sp.add_argument("--dry-run", action="store_true", help="show what would be downloaded")
    sp.set_defaults(func=cmd_download)

    sp = sub.add_parser("platforms", help="list platforms, album types and sorts")
    sp.set_defaults(func=cmd_platforms)

    sp = sub.add_parser("browser", help="browser session: open (interactive), login, status")
    sp.add_argument("action", choices=["open", "login", "status"])
    sp.set_defaults(func=cmd_browser)

    sp = sub.add_parser("serve", help="run the web UI")
    sp.add_argument("--host")
    sp.add_argument("--port", type=int)
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("db", help="apply database migrations")
    sp.add_argument("action", choices=["migrate"])
    sp.set_defaults(func=cmd_db)

    sp = sub.add_parser("history", help="import albums from your khinsider account pages (needs login)")
    sp.add_argument("action", choices=["sync"])
    sp.add_argument("--source", choices=["downloads", "history", "all"], default="all")
    sp.set_defaults(func=cmd_history)

    sp = sub.add_parser("library", help="scan/verify local folders, duplicates, missing albums, recovery")
    lsub = sp.add_subparsers(dest="action", required=True)
    q = lsub.add_parser("scan", help="scan a folder (default: download dir); read-only")
    q.add_argument("path", nargs="?")
    q.add_argument("--deep", action="store_true", help="hash every file (SHA-256)")
    q = lsub.add_parser("list")
    q.add_argument("--status", choices=["ok", "unverified", "incomplete", "unmatched"])
    q.add_argument("--search", default="")
    lsub.add_parser("duplicates")
    lsub.add_parser("missing")
    q = lsub.add_parser("recover", help="queue missing/damaged albums again")
    q.add_argument("ids", nargs="*", type=int, help="album ids from `library missing` (default: all)")
    q.add_argument("--format", "-f", choices=list(FORMATS))
    q.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    sp.set_defaults(func=cmd_library)

    sp = sub.add_parser("import", help="import a shopping list (.txt) or CSV, match it, queue the matches")
    isub = sp.add_subparsers(dest="action", required=True)
    for name, hlp in (("check", "check a list file without importing (exit 1 if rows have errors)"),
                      ("convert", "convert a list file to the import CSV format"),
                      ("add", "import a list file and match it against the site")):
        q = isub.add_parser(name, help=hlp)
        q.add_argument("file")
        q.add_argument("--read-as", choices=["auto", "text", "csv"], default="auto")
        if name == "convert":
            q.add_argument("--output", "-o", help="default: same name with .csv")
        if name == "add":
            q.add_argument("--format", "-f", choices=list(FORMATS))
            q.add_argument("--name")
            q.add_argument("--include-done", action="store_true", help="also import entries checked off with [x]")
    isub.add_parser("lists", help="list imported lists")
    q = isub.add_parser("resume", help="continue matching a list")
    q.add_argument("id", type=int)
    q = isub.add_parser("show", help="show a list's entries")
    q.add_argument("id", type=int)
    q.add_argument("--status", choices=["pending", "matched", "ambiguous", "not_found", "error", "skipped", "queued",
                                        "review", "no_format"])
    q = isub.add_parser("choose", help="set the album for an entry")
    q.add_argument("id", type=int, help="list id")
    q.add_argument("entry", type=int, help="entry id (first column of `import show`)")
    q.add_argument("album", help="album id (from the candidates) or album URL")
    q = isub.add_parser("queue", help="queue the matched albums in priority order")
    q.add_argument("id", type=int)
    q.add_argument("--include-no-format", action="store_true", help="also queue albums without the format (MP3)")
    q.add_argument("--yes", action="store_true")
    q = isub.add_parser("export", help="write match results as CSV")
    q.add_argument("id", type=int)
    q.add_argument("--output", "-o")
    sp.set_defaults(func=cmd_import)
    return p


def cmd_ask(args) -> int:
    from .ai import AiError, plan_from_request

    async def go(ctx):
        s = ctx.settings
        try:
            plan = await plan_from_request(" ".join(args.request), endpoint=s.llm_endpoint or "", model=s.llm_model,
                                           api_key=ctx.base_settings.llm_api_key.get())
        except AiError as exc:
            console.print(f"[red]AI:[/] {exc}")
            return 2
        console.print(f"[cyan]Interpreted as[/] {plan.describe()}")
        for w in plan.warnings:
            console.print(f"[yellow]{w}[/]")
        params = dict(plan.query_params())
        query = SearchQuery.build(params.get("q", ""), plan.platforms, plan.album_type, plan.year, plan.sort)
        res = await ctx.discovery.discover(query, per_page=args.per_page)
        console.print(_results_table(res))
        for n in res.notes:
            console.print(f"[cyan]note:[/] {n}")
        if args.no_interactive or not sys.stdin.isatty() or res.total == 0:
            return 0
        return _interactive_select(ctx, res)

    return _run(go)


def cmd_history(args) -> int:
    from .library import Library

    async def go(ctx):
        sources = None if args.source == "all" else [args.source]
        counts = await Library(ctx.db).sync_history(ctx.client, sources)
        console.print(f"Imported: {counts}. Next: `python main.py library scan` then `library missing`.")
        return 0

    return _run(go)


def cmd_library(args) -> int:
    from pathlib import Path

    from .library import Library

    async def go(ctx):
        lib = Library(ctx.db)
        if args.action == "scan":
            root = Path(args.path) if args.path else ctx.settings.download_dir
            with console.status(f"Scanning {root}…") as st:
                s = await asyncio.to_thread(lib.scan, root, deep=args.deep,
                                            progress=lambda rel: st.update(f"Scanning {rel}"))
            console.print(f"{s.folders} folder(s): {s.matched} matched, {s.unmatched} unmatched, "
                          f"{s.incomplete} incomplete · {s.files} files · {s.bytes / 1048576:.0f} MB")
            for e in s.errors[:10]:
                console.print(f"[red]{e}[/]")
            return 0
        if args.action == "list":
            t = Table(title="Library")
            for col in ("ID", "Album", "Folder", "Status", "Files", "Missing", "Damaged"):
                t.add_column(col)
            for a in lib.albums(args.search, args.status):
                t.add_row(str(a["id"]), a["album_title"] or a["title_guess"], a["rel_path"], a["status"],
                          str(a["file_count"]), str(a["missing_count"]), str(a["mismatch_count"]))
            console.print(t)
            console.print(lib.stats())
            return 0
        if args.action == "duplicates":
            groups = lib.duplicates()
            for g in groups:
                console.print(f"[yellow]{g['kind']}[/]: {g['label']}")
                for p in g["paths"]:
                    console.print(f"   {p}")
            console.print(f"{len(groups)} group(s). Nothing was changed.")
            return 0
        missing = lib.missing()
        if args.action == "missing":
            t = Table(title="Missing or damaged albums")
            for col in ("Album ID", "Album", "Why"):
                t.add_column(col)
            for m in missing:
                t.add_row(str(m["id"]), m["title"], m["reason"])
            console.print(t)
            return 0
        # recover
        ids = args.ids or [m["id"] for m in missing]
        if not ids:
            console.print("Nothing to recover.")
            return 0
        fmt = args.format or ctx.settings.preferred_format
        if not args.yes and not Confirm.ask(f"Queue {len(ids)} album(s) as {fmt.upper()} for recovery?", default=False):
            return 0
        r = lib.recover(ids, fmt)
        console.print(f"Queued {len(r.added)}; already queued {len(r.already_queued)}. Run `python main.py download`.")
        return 0

    return _run(go)


def _print_check(parsed, path) -> None:
    s = parsed.summary()
    pri = ", ".join(f"{'no priority' if k == 'none' else 'P' + k}: {v}" for k, v in sorted(s["by_priority"].items()))
    console.print(f"[bold]{path}[/] reads as {'CSV' if s['source_format'] == 'csv' else 'a text list'}: "
                  f"{s['entries']} entries ({pri})" + (f", {s['done']} checked off" if s["done"] else "")
                  + (f", {s['with_album_url']} with an album URL" if s["with_album_url"] else ""))
    colors = {"error": "red", "warning": "yellow", "info": "dim"}
    for i in parsed.issues:
        console.print(f"[{colors[i.level]}]{i.level}[/] " + (f"line {i.line}: " if i.line else "") + i.message)


def cmd_import(args) -> int:
    from pathlib import Path

    from .imports import ImportFormatError, parse_import, to_csv

    def load():
        p = Path(args.file)
        try:
            data = p.read_bytes()
        except OSError as exc:
            raise ValueError(f"can't read {p}: {exc}") from exc
        return parse_import(data, p.name, source_format=None if args.read_as == "auto" else args.read_as), p

    if args.action in ("check", "convert"):
        try:
            parsed, p = load()
        except (ImportFormatError, ValueError) as exc:
            console.print(f"[red]Can't import this file:[/] {exc}")
            return 2
        _print_check(parsed, p)
        if args.action == "check":
            return 1 if parsed.errors else 0
        out = Path(args.output) if args.output else p.with_suffix(".csv")
        if out.resolve() == p.resolve():
            console.print("[red]Error:[/] the output would overwrite the input; pass another path with -o")
            return 2
        out.write_text(to_csv(parsed), encoding="utf-8", newline="")
        console.print(f"[green]Wrote {len(parsed.entries)} entries to {out}[/]")
        return 0

    async def go(ctx):
        imp = ctx.imports
        if args.action == "add":
            try:
                parsed, p = load()
            except ImportFormatError as exc:
                console.print(f"[red]Can't import this file:[/] {exc}")
                return 2
            _print_check(parsed, p)
            fmt = args.format or ctx.settings.preferred_format
            lid = imp.create(parsed, name=args.name or p.name, fmt=fmt, include_done=args.include_done)
            console.print(f"Created import list {lid} ({fmt.upper()}). Matching… "
                          f"(Ctrl+C stops; `import resume {lid}` continues)")
            return await _match_live(ctx, lid)
        if args.action == "lists":
            t = Table(title="Import lists")
            for col in ("ID", "Name", "Status", "Format", "Matched", "Review", "Queued", "Skipped", "Pending"):
                t.add_column(col)
            for row in imp.lists():
                n = row["counts"]
                t.add_row(str(row["id"]), row["name"], row["status"], row["format"], str(n["matched"]),
                          str(n["ambiguous"] + n["not_found"] + n["error"]), str(n["queued"]), str(n["skipped"]),
                          str(n["pending"]))
            console.print(t)
            return 0
        info = imp.get(args.id)
        if info is None:
            raise ValueError(f"unknown import list {args.id}")
        if args.action == "resume":
            imp._set_list(args.id, status="matching", error=None)
            return await _match_live(ctx, args.id)
        if args.action == "show":
            t = Table(title=f"{info['name']} ({info['status']})")
            for col in ("Entry", "#", "P", "From your list", "Status", "Site album / candidates", "Score", "Formats"):
                t.add_column(col, overflow="fold")
            for e in imp.entries(args.id, args.status):
                album = e["album_title"] or "; ".join(f"[{c['album_id']}] {c['title']}" for c in e["candidates"][:3])
                no_fmt = f" (no {info['format'].upper()})" if e["has_format"] == 0 else ""
                t.add_row(str(e["id"]), str(e["position"] + 1), str(e["priority"] or ""), e["title"], e["status"],
                          album, f"{e['score']:.2f}" if e["score"] is not None else "",
                          " ".join(e["formats"] or []) + no_fmt)
            console.print(t)
            console.print(info["counts"])
            return 0
        if args.action == "choose":
            e = (await imp.choose(args.entry, int(args.album)) if args.album.isdigit()
                 else await imp.choose_url(args.entry, args.album))
            console.print(f"Entry {args.entry}: {e['title']} → {e['album_title']}")
            return 0
        if args.action == "queue":
            extra = f" (plus {info['no_format']} without it)" if args.include_no_format and info["no_format"] else ""
            if not args.yes and not Confirm.ask(
                    f"Queue {info['queueable']} matched album(s) as {info['format'].upper()}{extra}?", default=False):
                return 0
            r = imp.queue(args.id, include_without_format=args.include_no_format)
            console.print(f"Queued {r['queued']}; already queued {r['already_queued']}; already downloaded "
                          f"{r['already_downloaded']}; skipped (format not offered) {r['skipped_no_format']}. "
                          "Run `python main.py download` or use the web UI.")
            return 0
        # export
        out = Path(args.output) if args.output else Path(f"import-{args.id}-results.csv")
        out.write_text(imp.export_csv(args.id), encoding="utf-8", newline="")
        console.print(f"Wrote {out}")
        return 0

    return _run(go)


async def _match_live(ctx, lid: int) -> int:
    imp = ctx.imports
    task = asyncio.create_task(imp.match_list(lid))
    with console.status("Matching…") as st:
        while not task.done():
            info = imp.get(lid)
            n = info["counts"]
            st.update(f"Matching {info['done']}/{info['total']} · {n['matched']} matched · "
                      f"{n['ambiguous']} to choose · {n['not_found']} not found")
            await asyncio.wait({task}, timeout=1)
    task.result()
    info = imp.get(lid)
    n = info["counts"]
    console.print(f"[green]Done:[/] {n['matched']} matched ({info['no_format']} without {info['format'].upper()}), "
                  f"{n['ambiguous']} to choose, {n['not_found']} not found, {n['error']} error(s), {n['skipped']} skipped.")
    console.print(f"Review: `python main.py import show {lid} --status review` (or the web UI), "
                  f"then `python main.py import queue {lid}`")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except QueryError as exc:
        console.print(f"[red]Error:[/] {exc}")
        return 2
    except KeyboardInterrupt:
        return 130
