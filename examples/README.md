# Import list templates

Copy one of these, replace the example rows with your albums, then import it:
**Import** page in the web UI, `python main.py import add my-list.csv`, or `POST /api/v1/imports`.

| File | Use it when |
|---|---|
| [import-template.txt](import-template.txt) | You keep a hand-written checklist. Numbered `[ ]` lines, `PRIORITY N` headings, a note after `  - `, and an optional search or album URL on the next line. |
| [import-template.csv](import-template.csv) | You make the list in a spreadsheet or another program. |

## CSV columns

A header row is required. Column names are case-insensitive. Only `title` or `url` is needed on each row.

| Column | Meaning |
|---|---|
| `title` | The album as you'd describe it. It is matched against site titles; generic words like "Original Soundtrack" and "OST" don't count. |
| `search` | The site search to run. If empty, it is derived from the title. |
| `url` | A khinsider album URL skips matching. A search URL (`/search?search=...`) sets `search`. |
| `platform` | A hint, e.g. `PS1`, `SNES`, `NES` (see `python main.py platforms`). |
| `year` | A hint, e.g. `1998`. |
| `format` | `flac` or `mp3` for this row only. If empty, the list's format is used. |
| `priority` | A number; 1 downloads first. |
| `section` | A free-text group name. |
| `note` | Your note; it is shown next to the entry. |
| `done` | `x`, `yes` or `1` means you already have it. The row is skipped unless you choose to include checked entries. |

Comma, semicolon and tab delimiters are all detected, and Excel's UTF-8 CSV export (with BOM) works.
To convert a text list to CSV, run `python main.py import convert my-list.txt`.
To check a file without importing it, run `python main.py import check my-list.csv`.
