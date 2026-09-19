# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Command line entry point chaining the whole SVD generation pipeline.

The ``pysvdgen`` command runs the four stages of the generator in order:

1. the device tree of the SoC is parsed and turned into a CMSIS-SVD skeleton;
2. the reference manual is split into one document per chapter;
3. the register tables of the selected chapters are extracted;
4. the registers and bit fields are injected into the SVD file.

Every message is rendered with :mod:`rich`. Anything the pipeline could not
resolve, peripherals missing on either side and registers landing outside the
device tree address blocks, is reported at the end of the run.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from .chapter_splitter import Chapter, list_chapters, split_chapters
from .register_extractor import DEFAULT_MODEL, extract_registers
from .svd import generate_svd
from .svd_enricher import EnrichmentReport, enrich_svd

__all__ = ["Options", "build_parser", "run"]

#: Returned when the whole pipeline succeeded.
EXIT_SUCCESS = 0
#: Returned when a stage raised an error.
EXIT_FAILURE = 1
#: Returned when no chapter matches the requested selection.
EXIT_NO_CHAPTER = 2


@dataclass
class Options:
    """Resolved command line options.

    :param kernel: root of the Linux kernel sources.
    :param dtsi: DTSI name, without extension.
    :param pdf: reference manual to read.
    :param output: SVD file to write.
    :param workspace: working directory kept on exit, None for a temporary one.
    :param chapters: chapter name filters, empty for the whole manual.
    :param model: ollama model used by the extraction agent.
    :param host: base URL of the ollama server, None for the default one.
    :param vendor: vendor name written in the SVD.
    :param verbose: show the module logs.
    """

    kernel: Path
    dtsi: str
    pdf: Path
    output: Path
    workspace: Path | None
    chapters: list[str]
    model: str
    host: str | None
    vendor: str | None
    verbose: bool

    @classmethod
    def from_namespace(cls, args: argparse.Namespace) -> "Options":
        """Build the options from a parsed command line."""
        return cls(
            kernel=args.kernel,
            dtsi=args.dtsi,
            pdf=args.pdf,
            output=args.output,
            workspace=args.workspace,
            chapters=args.chapters,
            model=args.model,
            host=args.ollama_host,
            vendor=args.vendor,
            verbose=args.verbose,
        )


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser of the ``pysvdgen`` command."""
    parser = argparse.ArgumentParser(
        prog="pysvdgen",
        description=(
            "Generate a CMSIS-SVD file from the Linux device tree of a SoC and "
            "enrich it with the register tables of its reference manual. A "
            "local ollama server is required by the extraction stage."
        ),
    )
    parser.add_argument("-k", "--kernel", required=True, type=Path, help="Linux kernel sources")
    parser.add_argument("-d", "--dtsi", required=True, help="DTSI name, without extension")
    parser.add_argument("-p", "--pdf", required=True, type=Path, help="reference manual PDF")
    parser.add_argument("-o", "--output", required=True, type=Path, help="SVD file to write")
    parser.add_argument(
        "-w",
        "--workspace",
        type=Path,
        default=None,
        help="working directory, kept on exit; a temporary one is used and removed when omitted",
    )
    parser.add_argument(
        "-c",
        "--chapter",
        dest="chapters",
        action="append",
        default=[],
        metavar="NAME",
        help="only process the chapters whose name contains NAME (repeatable)",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="ollama model used for extraction")
    parser.add_argument("--ollama-host", default=None, help="base URL of the ollama server")
    parser.add_argument("--vendor", default=None, help="vendor name written in the SVD")
    parser.add_argument("-v", "--verbose", action="store_true", help="show module logs")
    return parser


@contextmanager
def _workspace(path: Path | None, console: Console) -> Iterator[Path]:
    """Yield the working directory, removing it when it is a temporary one."""
    if path is not None:
        path.mkdir(parents=True, exist_ok=True)
        console.print(f"[dim]Working directory kept:[/dim] {path}")
        yield path
        return
    temporary = Path(tempfile.mkdtemp(prefix="pysvdgen-"))
    console.print(f"[dim]Temporary working directory:[/dim] {temporary}")
    try:
        yield temporary
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
        console.print("[dim]Temporary working directory removed.[/dim]")


def _selected(chapters: Sequence[Chapter], filters: Sequence[str]) -> list[Chapter]:
    """Return the chapters matching the ``--chapter`` filters."""
    if not filters:
        return list(chapters)
    lowered = [item.lower() for item in filters]
    return [
        chapter
        for chapter in chapters
        if any(
            needle in chapter.title.lower() or needle in chapter.filename.lower()
            for needle in lowered
        )
    ]


def _progress(console: Console) -> Progress:
    """Return the progress bar used by the long running stages."""
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    )


def _summary(console: Console, report: EnrichmentReport) -> None:
    """Print the enrichment table and every reported anomaly."""
    table = Table(title="Enriched peripherals", header_style="bold")
    table.add_column("Peripheral")
    table.add_column("Registers", justify="right")
    table.add_column("Fields", justify="right")
    table.add_column("Out of range", justify="right", style="yellow")
    table.add_column("Source", overflow="fold", style="dim")
    for match in report.matches:
        table.add_row(
            match.peripheral,
            str(match.registers),
            str(match.fields),
            str(len(match.rejected)),
            Path(match.source).name,
        )
    console.print(table)

    if report.unmatched_svd:
        console.print(
            Panel(
                ", ".join(report.unmatched_svd),
                title="Device tree peripherals without register description",
                border_style="yellow",
            )
        )
    if report.unused_sources:
        console.print(
            Panel(
                ", ".join(sorted(set(report.unused_sources))),
                title="Reference manual peripherals missing from the device tree",
                border_style="yellow",
            )
        )
    rejected = {match.peripheral: match.rejected for match in report.matches if match.rejected}
    if rejected:
        console.print(
            Panel(
                "\n".join(
                    f"[bold]{name}[/bold]: {', '.join(items)}" for name, items in rejected.items()
                ),
                title="Registers dropped, outside the device tree address blocks",
                border_style="yellow",
            )
        )


def _split(pdf: Path, workspace: Path, chapters: Sequence[Chapter], console: Console) -> list[Path]:
    """Split the selected chapters into the workspace and return their paths."""
    with _progress(console) as progress:
        task = progress.add_task("Splitting", total=len(chapters))
        files: list[Path] = []
        for chapter in chapters:
            progress.update(task, description=f"Splitting {chapter.title}")
            files.extend(
                split_chapters(pdf, workspace / "chapters", overwrite=False, select=[chapter.title])
            )
            progress.advance(task)
    console.print(f"{len(files)} chapter(s) split")
    return files


def _extract(
    files: Sequence[Path], workspace: Path, options: Options, console: Console
) -> list[dict[str, Any]]:
    """Extract the register dictionaries, caching them in the workspace."""
    cache = workspace / "registers"
    cache.mkdir(parents=True, exist_ok=True)
    dictionaries: list[dict[str, Any]] = []
    with _progress(console) as progress:
        task = progress.add_task("Chapters", total=len(files))
        for path in files:
            progress.update(task, description=f"Extracting {path.stem}")
            target = cache / f"{path.stem}.json"
            if target.is_file():
                dictionaries.append(json.loads(target.read_text()))
            else:
                data = extract_registers(path, model=options.model, host=options.host)
                target.write_text(json.dumps(data, indent=1))
                dictionaries.append(data)
            progress.advance(task)
    total = sum(len(item.get("peripherals", {})) for item in dictionaries)
    console.print(f"{total} peripheral(s) described by the manual")
    return dictionaries


def run(argv: Sequence[str] | None = None) -> int:
    """Run the complete pipeline.

    :param argv: command line arguments, :data:`sys.argv` when omitted.
    :return: 0 on success, 2 when no chapter matches the selection, 1 on error.
    """
    options = Options.from_namespace(build_parser().parse_args(argv))
    console = Console()
    logging.basicConfig(
        level=logging.INFO if options.verbose else logging.ERROR,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False, rich_tracebacks=True)],
    )

    console.print(
        Panel.fit(
            f"[bold]{options.dtsi}[/bold]\n"
            f"kernel : {options.kernel}\n"
            f"manual : {options.pdf}\n"
            f"output : {options.output}\n"
            f"model  : {options.model}",
            title="pySvdGenerator",
            border_style="cyan",
        )
    )

    try:
        with _workspace(options.workspace, console) as workspace:
            console.rule("[bold]1/4 Device tree to SVD")
            svd = generate_svd(
                options.kernel,
                options.dtsi,
                workspace / f"{options.dtsi}.svd",
                vendor=options.vendor,
            )
            console.print(f"Intermediate SVD: [green]{svd}[/green]")

            console.rule("[bold]2/4 Reference manual splitting")
            chapters = _selected(list_chapters(options.pdf), options.chapters)
            if not chapters:
                console.print("[red]No chapter matches the selection.[/red]")
                return EXIT_NO_CHAPTER
            files = _split(options.pdf, workspace, chapters, console)

            console.rule("[bold]3/4 Register extraction")
            dictionaries = _extract(files, workspace, options, console)

            console.rule("[bold]4/4 SVD enrichment")
            report = enrich_svd(svd, dictionaries, options.output)
    except Exception as error:
        console.print(Panel(str(error), title="Failed", border_style="red"))
        if options.verbose:
            console.print_exception()
        return EXIT_FAILURE

    _summary(console, report)
    console.print(
        f"[bold green]SVD written:[/bold green] {report.output} "
        f"({report.registers} registers, {report.fields} fields)"
    )
    return EXIT_SUCCESS
