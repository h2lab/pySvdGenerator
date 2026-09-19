# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Command line entry point chaining the whole SVD generation pipeline.

The ``pysvdgen`` command runs the four stages of the generator in order:

1. the device tree of the SoC is parsed and turned into a CMSIS-SVD skeleton,
   or an existing SVD file is taken as the starting point;
2. the reference manual is split into one document per chapter;
3. the register tables of the selected chapters are extracted;
4. the registers and bit fields are injected into the SVD file.

Starting from an existing SVD, with ``--svd``, completes it chapter after
chapter: the peripherals already documented are left untouched.

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
#: Returned when the arguments are inconsistent.
EXIT_USAGE = 3


@dataclass
class Options:
    """Resolved command line options.

    :param kernel: root of the Linux kernel sources, None with ``svd``.
    :param dtsi: DTSI name without extension, None with ``svd``.
    :param svd: existing SVD file to complete, None when generating one.
    :param pdf: reference manual to read.
    :param output: SVD file to write.
    :param workspace: working directory kept on exit, None for a temporary one.
    :param chapters: chapter name filters, empty for the whole manual.
    :param model: ollama model used by the extraction agent.
    :param host: base URL of the ollama server, None for the default one.
    :param use_llm: query the ollama agent, enabled by default.
    :param vendor: vendor name written in the SVD.
    :param verbose: show the module logs.
    """

    kernel: Path | None
    dtsi: str | None
    svd: Path | None
    pdf: Path
    output: Path
    workspace: Path | None
    chapters: list[str]
    model: str
    host: str | None
    use_llm: bool
    vendor: str | None
    verbose: bool

    @classmethod
    def from_namespace(cls, args: argparse.Namespace) -> "Options":
        """Build the options from a parsed command line."""
        return cls(
            kernel=args.kernel,
            dtsi=args.dtsi,
            svd=args.svd,
            pdf=args.pdf,
            output=args.output or args.svd,
            workspace=args.workspace,
            chapters=args.chapters,
            model=args.model,
            host=args.ollama_host,
            use_llm=not args.no_llm,
            vendor=args.vendor,
            verbose=args.verbose,
        )


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser of the ``pysvdgen`` command."""
    parser = argparse.ArgumentParser(
        prog="pysvdgen",
        description=(
            "Generate a CMSIS-SVD file from the Linux device tree of a SoC and "
            "enrich it with the register tables of its reference manual, or "
            "complete an existing SVD file chapter after chapter. The "
            "extraction queries a local ollama server unless --no-llm is given."
        ),
    )
    parser.add_argument("-k", "--kernel", type=Path, help="Linux kernel sources")
    parser.add_argument("-d", "--dtsi", help="DTSI name, without extension")
    parser.add_argument(
        "-s",
        "--svd",
        type=Path,
        help="existing SVD file to complete, instead of generating one from --kernel/--dtsi",
    )
    parser.add_argument("-p", "--pdf", required=True, type=Path, help="reference manual PDF")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="SVD file to write, required unless --svd is given and updated in place",
    )
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
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="do not query the ollama agent, the extraction is then degraded",
    )
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
                data = extract_registers(
                    path, model=options.model, host=options.host, use_llm=options.use_llm
                )
                target.write_text(json.dumps(data, indent=1))
                dictionaries.append(data)
            progress.advance(task)
    total = sum(len(item.get("peripherals", {})) for item in dictionaries)
    console.print(f"{total} peripheral(s) described by the manual")
    return dictionaries


def _check(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject the argument combinations the pipeline cannot honour."""
    if args.svd:
        if args.kernel or args.dtsi:
            parser.error("--svd cannot be combined with --kernel or --dtsi")
    elif not (args.kernel and args.dtsi):
        parser.error("--kernel and --dtsi are required, unless --svd is given")
    if not args.output and not args.svd:
        parser.error("--output is required, unless --svd is updated in place")


def run(argv: Sequence[str] | None = None) -> int:
    """Run the complete pipeline.

    Either ``--kernel``/``--dtsi`` generate the SVD skeleton from the device
    tree, or ``--svd`` completes an existing file.

    :param argv: command line arguments, :data:`sys.argv` when omitted.
    :return: 0 on success, 2 when no chapter matches the selection, 3 on a
        usage error, 1 on any other error.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        _check(parser, args)
    except SystemExit:
        return EXIT_USAGE
    options = Options.from_namespace(args)
    console = Console()
    logging.basicConfig(
        level=logging.INFO if options.verbose else logging.ERROR,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False, rich_tracebacks=True)],
    )

    origin = f"kernel : {options.kernel}" if options.svd is None else f"svd    : {options.svd}"
    label = options.dtsi if options.svd is None else options.svd.name
    console.print(
        Panel.fit(
            f"[bold]{label}[/bold]\n"
            f"{origin}\n"
            f"manual : {options.pdf}\n"
            f"output : {options.output}\n"
            f"model  : {options.model if options.use_llm else 'disabled (--no-llm)'}",
            title="pySvdGenerator",
            border_style="cyan",
        )
    )

    try:
        with _workspace(options.workspace, console) as workspace:
            if options.svd is not None:
                console.rule("[bold]1/4 Existing SVD")
                svd = options.svd
                if not svd.is_file():
                    raise FileNotFoundError(f"No such SVD file: {svd}")
                console.print(f"Completing: [green]{svd}[/green]")
            else:
                console.rule("[bold]1/4 Device tree to SVD")
                svd = generate_svd(
                    options.kernel or Path(),
                    options.dtsi or "",
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
