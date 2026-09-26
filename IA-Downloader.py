#!/usr/bin/env python3

"""
Internet Archive Original File Downloader

Downloads selected original files from an Internet Archive item.

Features:
    - Interactive TUI
    - File-type selection
    - Original files only
    - Five concurrent downloads by default
    - Live per-file progress
    - Download speed and ETA
    - Resume partially downloaded files
    - Skip completed files
    - Retry failed downloads
    - Windows filename sanitization
    - Retry failed files from the final screen
    - Optional command-line mode

Requirements:

    pip install internetarchive rich requests

Interactive usage:

    python ia_downloader.py

Command-line usage:

    python ia_downloader.py ITEM_IDENTIFIER
    python ia_downloader.py ITEM_IDENTIFIER -o ./downloads
    python ia_downloader.py ITEM_IDENTIFIER -t mp4,mkv
    python ia_downloader.py ITEM_IDENTIFIER -t mp3 --workers 8
"""

from __future__ import annotations

import argparse
import concurrent.futures
import logging
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

import internetarchive
import requests

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.prompt import IntPrompt, Prompt
from rich.table import Table


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_WORKERS = 5
DEFAULT_RETRIES = 5

CHUNK_SIZE = 1024 * 1024  # 1 MiB

# Connection timeout, read timeout.
CONNECT_TIMEOUT = 60
READ_TIMEOUT = 180

LOG_FILENAME = "ia_downloader.log"

console = Console()

log = logging.getLogger("ia_downloader")


def setup_logging(output_dir: Path) -> Path:
    """
    Configure file logging for this run.

    Logs go to a text file inside the output directory (not the
    console, since the console is already used for the Rich UI).
    Returns the path to the log file.
    """

    log_path = output_dir / LOG_FILENAME

    handler = logging.FileHandler(
        log_path,
        encoding="utf-8",
    )

    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    log.setLevel(logging.INFO)

    # Avoid duplicate handlers if setup_logging is ever called twice
    # in the same process.
    log.handlers.clear()
    log.addHandler(handler)

    return log_path


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class DownloadResult:
    filename: str
    success: bool
    skipped: bool = False
    error: str | None = None
    bytes_downloaded: int = 0


@dataclass
class DownloadStats:
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    bytes_downloaded: int = 0

    lock: Lock = None

    def __post_init__(self):
        self.lock = Lock()

    def add_completed(self, amount: int):
        with self.lock:
            self.completed += 1
            self.bytes_downloaded += amount

    def add_skipped(self, amount: int):
        with self.lock:
            self.skipped += 1
            self.completed += 1
            self.bytes_downloaded += amount

    def add_failed(self):
        with self.lock:
            self.failed += 1


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def human_size(value: int | float) -> str:
    """Format bytes as a human-readable size."""

    units = (
        "B",
        "KiB",
        "MiB",
        "GiB",
        "TiB",
        "PiB",
    )

    value = float(value)

    for unit in units:
        if value < 1024:
            return f"{value:.1f} {unit}"

        value /= 1024

    return f"{value:.1f} EiB"


def file_extension(filename: str) -> str:
    """Return a normalized file extension."""

    suffix = Path(filename).suffix.lower()

    if suffix:
        return suffix[1:]

    return "[none]"


def get_file_size(file) -> int:
    """Get the expected file size from an IA File object."""

    try:
        return int(file.size or 0)
    except (AttributeError, TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Windows filename handling
# ---------------------------------------------------------------------------

def sanitize_filename_component(name: str) -> str:
    """
    Make a single filename/directory component safe on Windows.

    Internet Archive's filename is NOT changed. This only affects the
    local filesystem path.

    Windows does not permit:

        < > : " / \\ | ? *

    It also doesn't allow filenames ending in a space or period, or
    reserved device names such as CON, PRN, AUX, NUL, COM1, etc.
    """

    if sys.platform != "win32":
        return name

    # Replace characters forbidden by Windows.
    name = re.sub(r'[<>:"|?*]', "_", name)

    # Windows doesn't permit trailing spaces or periods.
    name = name.rstrip(" .")

    # Empty names aren't useful as path components.
    if not name:
        name = "_"

    # Windows reserved device names.
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }

    stem = Path(name).stem.upper()

    if stem in reserved:
        name = f"_{name}"

    return name


def safe_destination(
    output_dir: Path,
    filename: str,
) -> Path:
    """
    Convert an Internet Archive filename into a safe local path.

    Directory structure from IA is preserved.
    """

    path = Path(filename)

    safe_parts = [
        sanitize_filename_component(part)
        for part in path.parts
    ]

    destination = output_dir.joinpath(*safe_parts)

    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    return destination


# ---------------------------------------------------------------------------
# Internet Archive metadata
# ---------------------------------------------------------------------------

def get_original_files(identifier: str):
    """
    Retrieve only files marked source=original.
    """

    item = internetarchive.get_item(identifier)

    files = list(item.get_files())

    return [
        file
        for file in files
        if getattr(file, "source", None) == "original"
    ]


# ---------------------------------------------------------------------------
# File type selection
# ---------------------------------------------------------------------------

def group_by_extension(files):
    """Group files by extension."""

    groups = defaultdict(list)

    for file in files:
        groups[file_extension(file.name)].append(file)

    return dict(groups)


def print_file_types(files):
    """Display available original file types."""

    groups = group_by_extension(files)

    table = Table(
        title="Original File Types",
        show_lines=False,
    )

    table.add_column("#", justify="right")
    table.add_column("Type")
    table.add_column("Files", justify="right")
    table.add_column("Size", justify="right")

    sorted_groups = sorted(
        groups.items(),
        key=lambda item: sum(
            get_file_size(file)
            for file in item[1]
        ),
        reverse=True,
    )

    for number, (extension, group) in enumerate(
        sorted_groups,
        start=1,
    ):

        total_size = sum(
            get_file_size(file)
            for file in group
        )

        table.add_row(
            str(number),
            extension,
            f"{len(group):,}",
            human_size(total_size),
        )

    console.print(table)

    return sorted_groups


def select_file_types(files):
    """Interactively select extensions."""

    groups = group_by_extension(files)

    sorted_groups = sorted(
        groups.items(),
        key=lambda item: sum(
            get_file_size(file)
            for file in item[1]
        ),
        reverse=True,
    )

    console.print()

    print_file_types(files)

    console.print()

    console.print(
        "[dim]Enter extensions separated by commas, "
        "or 'all' for every original file.[/dim]"
    )

    console.print(
        "[dim]Example: mp4,mkv[/dim]"
    )

    console.print()

    while True:

        selection = Prompt.ask(
            "[bold]File types[/bold]"
        ).strip().lower()

        if selection == "all":
            return files

        requested = {
            item.strip().lstrip(".")
            for item in selection.split(",")
            if item.strip()
        }

        if not requested:

            console.print(
                "[yellow]"
                "Please enter at least one extension."
                "[/yellow]"
            )

            continue

        unknown = requested - set(groups)

        if unknown:

            console.print(
                "[yellow]Unknown file type(s):[/yellow] "
                + ", ".join(sorted(unknown))
            )

            console.print(
                "[dim]Available types: "
                + ", ".join(
                    extension
                    for extension, _ in sorted_groups
                )
                + "[/dim]"
            )

            continue

        return [
            file
            for file in files
            if file_extension(file.name) in requested
        ]


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download_file(
    file,
    output_dir: Path,
    file_progress: Progress,
    overall_progress: Progress,
    overall_task_id: int,
    stats: DownloadStats,
    retries: int,
) -> DownloadResult:

    filename = file.name

    destination = safe_destination(
        output_dir,
        filename,
    )

    partial = destination.with_name(
        destination.name + ".part"
    )

    expected_size = get_file_size(file)

    # -----------------------------------------------------------------
    # Per-file progress row.
    #
    # This task is created only once a worker actually starts on this
    # file (rather than one row for every file up front), and is
    # always removed again in the `finally` below. That keeps the
    # number of live rows bounded by the worker count instead of by
    # the total file count, which is what was causing the display to
    # stall/flicker on large batches: Rich's Live can't do in-place
    # updates once the rendered block is taller than the terminal, so
    # it was redrawing hundreds of rows on every refresh.
    #
    # This lives on its own `file_progress` Progress instance, with
    # byte-oriented columns (DownloadColumn, TransferSpeedColumn,
    # TimeRemainingColumn). The overall counter below is a *file*
    # count, not a byte count, so it gets its own Progress instance
    # with count-oriented columns -- otherwise Rich renders "40/40
    # bytes" and "102 bytes/s" for what is actually a file tally.
    # -----------------------------------------------------------------

    task_id = file_progress.add_task(
        filename,
        total=expected_size,
    )

    try:
        return _download_file_inner(
            file,
            filename,
            destination,
            partial,
            expected_size,
            file_progress,
            task_id,
            stats,
            retries,
        )
    finally:
        file_progress.remove_task(task_id)
        overall_progress.advance(overall_task_id, 1)


def _download_file_inner(
    file,
    filename,
    destination,
    partial,
    expected_size,
    progress: Progress,
    task_id: int,
    stats: DownloadStats,
    retries: int,
) -> DownloadResult:

    # ---------------------------------------------------------------
    # Already complete
    # ---------------------------------------------------------------

    if destination.exists():

        existing_size = destination.stat().st_size

        if (
            expected_size == 0
            or existing_size == expected_size
        ):

            progress.update(
                task_id,
                total=expected_size or existing_size,
                completed=expected_size or existing_size,
            )

            stats.add_skipped(existing_size)

            log.info(
                "SKIP  %s (already complete, %s)",
                filename,
                human_size(existing_size),
            )

            return DownloadResult(
                filename=filename,
                success=True,
                skipped=True,
                bytes_downloaded=existing_size,
            )

    # ---------------------------------------------------------------
    # Existing partial download
    # ---------------------------------------------------------------

    existing_size = 0

    if partial.exists():
        existing_size = partial.stat().st_size

    progress.update(
        task_id,
        total=expected_size,
        completed=existing_size,
    )

    # ---------------------------------------------------------------
    # Retry loop
    # ---------------------------------------------------------------

    for attempt in range(1, retries + 1):

        response = None

        try:

            headers = {}

            if existing_size > 0:
                headers["Range"] = f"bytes={existing_size}-"

            response = requests.get(
                file.url,
                headers=headers,
                stream=True,
                timeout=(
                    CONNECT_TIMEOUT,
                    READ_TIMEOUT,
                ),
            )

            # -------------------------------------------------------
            # IA/server ignored Range request.
            #
            # If we have a partial file but the server responds with
            # 200 rather than 206, discard the partial and start over.
            # -------------------------------------------------------

            if (
                existing_size > 0
                and response.status_code == 200
            ):

                response.close()
                response = None

                existing_size = 0

                if partial.exists():
                    partial.unlink()

                response = requests.get(
                    file.url,
                    stream=True,
                    timeout=(
                        CONNECT_TIMEOUT,
                        READ_TIMEOUT,
                    ),
                )

            response.raise_for_status()

            # -------------------------------------------------------
            # Determine total size
            # -------------------------------------------------------

            content_length = response.headers.get(
                "Content-Length"
            )

            if content_length:

                content_length = int(content_length)

                if response.status_code == 206:
                    total_size = (
                        existing_size
                        + content_length
                    )
                else:
                    total_size = content_length

                if expected_size == 0:
                    expected_size = total_size

                progress.update(
                    task_id,
                    total=expected_size,
                )

            # -------------------------------------------------------
            # Write response
            # -------------------------------------------------------

            mode = (
                "ab"
                if existing_size > 0
                else "wb"
            )

            downloaded = existing_size

            with open(partial, mode) as output:

                for chunk in response.iter_content(
                    chunk_size=CHUNK_SIZE
                ):

                    if not chunk:
                        continue

                    output.write(chunk)

                    downloaded += len(chunk)

                    progress.update(
                        task_id,
                        completed=downloaded,
                    )

            response.close()
            response = None

            # -------------------------------------------------------
            # Verify size
            # -------------------------------------------------------

            actual_size = partial.stat().st_size

            if (
                expected_size
                and actual_size != expected_size
            ):

                raise IOError(
                    "incomplete download: "
                    f"{actual_size:,} / "
                    f"{expected_size:,} bytes"
                )

            # -------------------------------------------------------
            # Finalize
            # -------------------------------------------------------

            partial.replace(destination)

            stats.add_completed(actual_size)

            log.info(
                "OK    %s (%s, attempt %d/%d)",
                filename,
                human_size(actual_size),
                attempt,
                retries,
            )

            return DownloadResult(
                filename=filename,
                success=True,
                bytes_downloaded=actual_size,
            )

        except Exception as exc:

            if response is not None:
                response.close()

            log.warning(
                "RETRY %s (attempt %d/%d): %s",
                filename,
                attempt,
                retries,
                exc,
            )

            if attempt >= retries:

                stats.add_failed()

                log.error(
                    "FAIL  %s: %s",
                    filename,
                    exc,
                )

                return DownloadResult(
                    filename=filename,
                    success=False,
                    error=str(exc),
                    bytes_downloaded=existing_size,
                )

            # -------------------------------------------------------
            # Exponential-ish backoff:
            #
            # 5, 10, 15, 20, 25...
            # capped at 30 seconds.
            # -------------------------------------------------------

            delay = min(attempt * 5, 30)

            time.sleep(delay)

            # Recalculate resume point after a failed attempt.
            if partial.exists():
                existing_size = partial.stat().st_size

    stats.add_failed()

    log.error("FAIL  %s: unknown error", filename)

    return DownloadResult(
        filename=filename,
        success=False,
        error="unknown error",
    )


# ---------------------------------------------------------------------------
# Download manager
# ---------------------------------------------------------------------------

def download_all(
    files,
    output_dir: Path,
    workers: int,
    retries: int,
):

    stats = DownloadStats()

    overall_progress = Progress(
        TextColumn(
            "[bold]{task.description}"
        ),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn(
            "{task.percentage:>5.1f}%"
        ),
        TimeElapsedColumn(),
        console=console,
    )

    file_progress = Progress(
        SpinnerColumn(),
        TextColumn(
            "[progress.description]{task.description}"
        ),
        BarColumn(),
        TextColumn(
            "{task.percentage:>5.1f}%"
        ),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
    )

    progress_group = Group(
        overall_progress,
        file_progress,
    )

    failures = []

    with Live(
        progress_group,
        console=console,
        refresh_per_second=10,
    ):

        overall_task_id = overall_progress.add_task(
            f"Overall ({workers} workers)",
            total=len(files),
        )

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=workers
        ) as executor:

            futures = {}

            for file in files:

                future = executor.submit(
                    download_file,
                    file,
                    output_dir,
                    file_progress,
                    overall_progress,
                    overall_task_id,
                    stats,
                    retries,
                )

                futures[future] = file

            for future in concurrent.futures.as_completed(
                futures
            ):

                result = future.result()

                if not result.success:
                    failures.append(result)

    return stats, failures


# ---------------------------------------------------------------------------
# Failure reporting
# ---------------------------------------------------------------------------

def print_failures(failures):
    """Display failed downloads."""

    if not failures:
        return

    console.print()

    console.print(
        "[bold red]Failed files:[/bold red]"
    )

    for failure in failures:

        console.print(
            f"  [red]✗[/red] {failure.filename}"
        )

        console.print(
            f"      {failure.error}"
        )


# ---------------------------------------------------------------------------
# Retry failed files
# ---------------------------------------------------------------------------

def download_with_failure_retry(
    failed_files,
    output_dir,
    workers,
    retries,
):
    """
    Retry failed IA File objects.

    This is intentionally a separate pass so the user can inspect the
    failures before deciding whether to try them again.
    """

    return download_all(
        failed_files,
        output_dir,
        workers,
        retries,
    )


# ---------------------------------------------------------------------------
# Interactive mode
# ---------------------------------------------------------------------------

def interactive():

    console.clear()

    console.print(
        Panel.fit(
            "[bold cyan]Internet Archive Downloader[/bold cyan]\n"
            "Original files, selected by file type",
            border_style="cyan",
        )
    )

    console.print()

    # ---------------------------------------------------------------
    # Item
    # ---------------------------------------------------------------

    identifier = Prompt.ask(
        "[bold]Internet Archive identifier[/bold]"
    ).strip()

    if not identifier:

        console.print(
            "[red]No identifier supplied.[/red]"
        )

        return 1

    # ---------------------------------------------------------------
    # Output
    # ---------------------------------------------------------------

    output = Prompt.ask(
        "[bold]Output directory[/bold]",
        default="./downloads",
    )

    output_dir = (
        Path(output)
        .expanduser()
        .resolve()
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    log_path = setup_logging(output_dir)

    log.info("Session started for item: %s", identifier)

    console.print(
        f"[dim]Logging to {log_path}[/dim]"
    )

    # ---------------------------------------------------------------
    # Workers
    # ---------------------------------------------------------------

    workers = IntPrompt.ask(
        "[bold]Concurrent downloads[/bold]",
        default=DEFAULT_WORKERS,
    )

    if workers < 1:
        workers = DEFAULT_WORKERS

    # ---------------------------------------------------------------
    # Retries
    # ---------------------------------------------------------------

    retries = IntPrompt.ask(
        "[bold]Retries per file[/bold]",
        default=DEFAULT_RETRIES,
    )

    if retries < 1:
        retries = DEFAULT_RETRIES

    # ---------------------------------------------------------------
    # Metadata
    # ---------------------------------------------------------------

    console.print()

    console.print(
        "[cyan]Reading Internet Archive metadata...[/cyan]"
    )

    try:

        files = get_original_files(
            identifier
        )

    except Exception as exc:

        log.error("Unable to retrieve item %s: %s", identifier, exc)

        console.print(
            f"\n[red]Unable to retrieve item:[/red] "
            f"{exc}"
        )

        return 1

    if not files:

        log.warning("No original files found for %s", identifier)

        console.print(
            "\n[yellow]"
            "No files marked as original were found."
            "[/yellow]"
        )

        return 0

    log.info("Found %d original files for %s", len(files), identifier)

    console.print()

    # ---------------------------------------------------------------
    # File selection
    # ---------------------------------------------------------------

    selected = select_file_types(files)

    if not selected:

        console.print(
            "[yellow]No files selected.[/yellow]"
        )

        return 0

    log.info("Selected %d files to download", len(selected))

    # ---------------------------------------------------------------
    # Selection summary
    # ---------------------------------------------------------------

    selected_size = sum(
        get_file_size(file)
        for file in selected
    )

    selected_types = sorted(
        {
            file_extension(file.name)
            for file in selected
        }
    )

    console.print()

    console.print(
        Panel(
            f"[bold]Types:[/bold] "
            f"{', '.join(selected_types)}\n"
            f"[bold]Files:[/bold] "
            f"{len(selected):,}\n"
            f"[bold]Size:[/bold] "
            f"{human_size(selected_size)}\n"
            f"[bold]Output:[/bold] "
            f"{output_dir}\n"
            f"[bold]Workers:[/bold] "
            f"{workers}\n"
            f"[bold]Retries:[/bold] "
            f"{retries}",
            title="Download Selection",
            border_style="green",
        )
    )

    console.print()

    answer = Prompt.ask(
        "Start downloading?",
        choices=["y", "n"],
        default="y",
    )

    if answer != "y":

        console.print(
            "[yellow]Cancelled.[/yellow]"
        )

        return 0

    console.print()

    # ---------------------------------------------------------------
    # Main download
    # ---------------------------------------------------------------

    stats, failures = download_all(
        selected,
        output_dir,
        workers,
        retries,
    )

    # ---------------------------------------------------------------
    # Retry loop
    #
    # Keep the actual IA File objects corresponding to the failures.
    # ---------------------------------------------------------------

    failed_names = {
        failure.filename
        for failure in failures
    }

    failed_files = [
        file
        for file in selected
        if file.name in failed_names
    ]

    while failures:

        console.print()

        console.rule(
            "[bold red]Download Failures[/bold red]"
        )

        print_failures(failures)

        console.print()

        console.print(
            f"[bold]Completed:[/bold] "
            f"{stats.completed:,}"
        )

        console.print(
            f"[bold]Failed:[/bold] "
            f"{stats.failed:,}"
        )

        console.print()

        choice = Prompt.ask(
            "What would you like to do?",
            choices=[
                "retry",
                "quit",
            ],
            default="retry",
        )

        if choice == "quit":
            log.info("User declined further retries; stopping.")
            break

        console.print()

        console.print(
            f"[cyan]Retrying {len(failed_files):,} "
            f"failed file(s)...[/cyan]"
        )

        log.info("Retrying %d failed file(s)", len(failed_files))

        retry_stats, retry_failures = (
            download_with_failure_retry(
                failed_files,
                output_dir,
                workers,
                retries,
            )
        )

        # -----------------------------------------------------------
        # Merge statistics for display.
        # -----------------------------------------------------------

        stats.completed += retry_stats.completed
        stats.skipped += retry_stats.skipped
        stats.failed = retry_stats.failed
        stats.bytes_downloaded += (
            retry_stats.bytes_downloaded
        )

        failures = retry_failures

        failed_names = {
            failure.filename
            for failure in failures
        }

        failed_files = [
            file
            for file in selected
            if file.name in failed_names
        ]

    # ---------------------------------------------------------------
    # Final summary
    # ---------------------------------------------------------------

    console.print()

    table = Table(
        title="Download Summary",
        show_header=False,
    )

    table.add_row(
        "Completed",
        f"[green]{stats.completed:,}[/green]",
    )

    table.add_row(
        "Skipped",
        f"[cyan]{stats.skipped:,}[/cyan]",
    )

    table.add_row(
        "Failed",
        (
            f"[red]{len(failures):,}[/red]"
            if failures
            else "[green]0[/green]"
        ),
    )

    table.add_row(
        "Downloaded",
        human_size(stats.bytes_downloaded),
    )

    console.print(table)

    if failures:

        console.print()

        print_failures(failures)

        console.print()

        console.print(
            "[yellow]"
            "Partial .part files were preserved. "
            "You can run the downloader again to resume them."
            "[/yellow]"
        )

        log.warning(
            "Run finished with %d failure(s): %s",
            len(failures),
            ", ".join(sorted(failed_names)),
        )

        return 1

    console.print()

    console.print(
        "[bold green]"
        "All selected files downloaded successfully."
        "[/bold green]"
    )

    log.info(
        "Run finished: %d completed, %d skipped, %s downloaded",
        stats.completed,
        stats.skipped,
        human_size(stats.bytes_downloaded),
    )

    return 0


# ---------------------------------------------------------------------------
# Command-line mode
# ---------------------------------------------------------------------------

def command_line(args):

    output_dir = (
        Path(args.output)
        .expanduser()
        .resolve()
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    log_path = setup_logging(output_dir)

    log.info("Session started for item: %s", args.identifier)

    console.print(
        f"[dim]Logging to {log_path}[/dim]"
    )

    # Interactive mode has always fallen back to the defaults for
    # non-positive values; the CLI path didn't, which let
    # `--workers 0` crash the executor and `--retries 0` silently
    # skip every download. Apply the same guard here.

    if args.workers < 1:
        log.warning(
            "--workers %d is invalid; using default (%d)",
            args.workers,
            DEFAULT_WORKERS,
        )
        args.workers = DEFAULT_WORKERS

    if args.retries < 1:
        log.warning(
            "--retries %d is invalid; using default (%d)",
            args.retries,
            DEFAULT_RETRIES,
        )
        args.retries = DEFAULT_RETRIES

    console.print(
        f"[cyan]Reading metadata for "
        f"{args.identifier}...[/cyan]"
    )

    try:

        files = get_original_files(
            args.identifier
        )

    except Exception as exc:

        log.error(
            "Unable to retrieve item %s: %s",
            args.identifier,
            exc,
        )

        console.print(
            f"[red]Unable to retrieve item:[/red] "
            f"{exc}"
        )

        return 1

    if not files:

        log.warning(
            "No original files found for %s",
            args.identifier,
        )

        console.print(
            "[yellow]No original files found.[/yellow]"
        )

        return 0

    # ---------------------------------------------------------------
    # Extension filter
    # ---------------------------------------------------------------

    if args.types:

        requested = {
            item.strip().lower().lstrip(".")
            for item in args.types.split(",")
            if item.strip()
        }

        files = [
            file
            for file in files
            if file_extension(file.name)
            in requested
        ]

    if not files:

        log.warning(
            "No original files matched requested types: %s",
            args.types,
        )

        console.print(
            "[yellow]"
            "No original files matched the requested "
            "file types."
            "[/yellow]"
        )

        return 0

    total_size = sum(
        get_file_size(file)
        for file in files
    )

    log.info(
        "Downloading %d files (%s)",
        len(files),
        human_size(total_size),
    )

    console.print(
        f"Found [bold]{len(files):,}[/bold] "
        f"matching original files "
        f"({human_size(total_size)})."
    )

    stats, failures = download_all(
        files,
        output_dir,
        args.workers,
        args.retries,
    )

    console.print()

    console.print(
        f"Completed: [green]{stats.completed:,}[/green]"
    )

    console.print(
        f"Skipped:   [cyan]{stats.skipped:,}[/cyan]"
    )

    console.print(
        f"Failed:    [red]{stats.failed:,}[/red]"
    )

    if failures:

        console.print()

        print_failures(failures)

    log.info(
        "Run finished: %d completed, %d skipped, %d failed, %s downloaded",
        stats.completed,
        stats.skipped,
        stats.failed,
        human_size(stats.bytes_downloaded),
    )

    return 1 if failures else 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():

    parser = argparse.ArgumentParser(
        description=(
            "Download selected original files "
            "from Internet Archive."
        )
    )

    parser.add_argument(
        "identifier",
        nargs="?",
        help="Internet Archive item identifier",
    )

    parser.add_argument(
        "-o",
        "--output",
        default="./downloads",
        help="Output directory",
    )

    parser.add_argument(
        "-t",
        "--types",
        help=(
            "Comma-separated file extensions, "
            "e.g. mp4,mkv"
        ),
    )

    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help="Number of concurrent downloads",
    )

    parser.add_argument(
        "-r",
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help="Retries per file",
    )

    args = parser.parse_args()

    if args.identifier:
        return command_line(args)

    return interactive()


if __name__ == "__main__":
    sys.exit(main())