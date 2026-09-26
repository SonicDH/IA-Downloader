# IA Downloader

A command-line and interactive TUI tool for downloading **original** files
from an [Internet Archive](https://archive.org) item — with resumable,
concurrent, retrying downloads and a live progress display.

"Original" means files IA marks with `source: original` in an item's
metadata: the files actually uploaded by the contributor, as opposed to
IA's own derivative formats (thumbnails, OCR text, re-encoded video, etc.).

## Features

- Interactive TUI **or** scriptable command-line mode
- Downloads only `source: original` files — skips IA-generated derivatives
- Filter by file extension (`.mp4`, `.mkv`, `.mp3`, ...)
- Concurrent downloads (configurable worker count)
- Resumable downloads via HTTP `Range` requests — safe to Ctrl+C and re-run
- Automatically skips files that are already fully downloaded
- Automatic retries with backoff, plus a manual "retry failed files" pass
  from the interactive summary screen
- Live progress display: an overall file-count bar plus one row per
  in-flight download showing bytes, speed, and ETA
- Per-run log file (`ia_downloader.log`) written into the output directory
- Filename sanitization for Windows-incompatible characters (only applied
  on Windows; the file as stored in IA is untouched)

## Requirements

- Python 3.10+
- [`internetarchive`](https://pypi.org/project/internetarchive/)
- [`rich`](https://pypi.org/project/rich/)
- [`requests`](https://pypi.org/project/requests/)

Install dependencies:

```bash
pip install internetarchive rich requests
```

## Usage

### Interactive mode

Run with no arguments to be prompted for an item identifier, output
directory, file types, and worker/retry counts:

```bash
python ia_downloader.py
```

The interactive summary screen lets you retry any failed files again
without starting the whole run over.

### Command-line mode

Pass an item identifier to run non-interactively — useful for scripting
or scheduled jobs:

```bash
# Download everything marked "original" for an item
python ia_downloader.py ITEM_IDENTIFIER

# Choose an output directory
python ia_downloader.py ITEM_IDENTIFIER -o ./downloads

# Only download specific file types
python ia_downloader.py ITEM_IDENTIFIER -t mp4,mkv

# Tune concurrency and retry count
python ia_downloader.py ITEM_IDENTIFIER -t mp3 --workers 8 --retries 3
```

### Options

| Flag             | Description                                   | Default        |
|------------------|------------------------------------------------|----------------|
| `identifier`     | Internet Archive item identifier               | —              |
| `-o`, `--output` | Output directory                               | `./downloads`  |
| `-t`, `--types`  | Comma-separated file extensions (e.g. `mp4,mkv`) | all original files |
| `-w`, `--workers`| Number of concurrent downloads                 | `5`            |
| `-r`, `--retries`| Retry attempts per file                        | `5`            |

`--workers` and `--retries` values below `1` fall back to their defaults
with a warning, rather than failing.

## Resuming and re-running

Downloads write to a `<filename>.part` file while in progress and rename
it to the final name only on success. If a run is interrupted, just run
the same command again:

- Fully downloaded files are detected by size and skipped.
- Partially downloaded `.part` files are resumed with an HTTP `Range`
  request starting from the last byte already on disk. If the server
  doesn't honor the range request, the partial file is discarded and the
  download restarts from scratch.

## Logging

Each run writes a plain-text log to `ia_downloader.log` inside the output
directory (appended across runs), independent of the live terminal
display. It records the item being processed, per-file outcomes
(`OK` / `SKIP` / `RETRY` / `FAIL`), retry passes, and a final summary —
useful for auditing large or unattended runs after the fact.

## Notes

- Only files with `source: original` in IA's metadata are considered;
  run against an item with no such files and the tool will report that
  nothing was found.
- On Windows, filename components are sanitized (forbidden characters
  replaced, reserved device names like `CON`/`NUL` prefixed, trailing
  dots/spaces stripped) to produce a valid local path. This only affects
  the path on disk — the file as archived on IA is never modified.