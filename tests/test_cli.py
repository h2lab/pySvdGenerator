# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Tests for command-line option parsing and chapter selection."""

from pathlib import Path

from rich.console import Console

from pySvdGenerator.chapter_splitter import Chapter
from pySvdGenerator.cli import _split, build_parser


def test_llm_model_selects_named_model() -> None:
    """The explicit model option forwards the requested Ollama model name."""
    options = build_parser().parse_args(["--pdf", "manual.pdf", "--llm-model", "llama3.2"])

    assert options.model == "llama3.2"


def test_model_remains_an_alias_for_llm_model() -> None:
    """The previous short model option remains supported."""
    options = build_parser().parse_args(["--pdf", "manual.pdf", "--model", "llama3.2"])

    assert options.model == "llama3.2"


def test_split_passes_selected_filenames_to_splitter(monkeypatch, tmp_path: Path) -> None:
    """Only the selected chapter files are requested from the PDF splitter."""
    selected = [Chapter(title="Timers", first_page=10, last_page=12)]
    calls: list[list[str]] = []

    def fake_split_chapters(*args: object, **kwargs: object) -> list[Path]:
        calls.append(kwargs["select"])
        return [tmp_path / "Timers.pdf"]

    monkeypatch.setattr("pySvdGenerator.cli.split_chapters", fake_split_chapters)

    files = _split(Path("manual.pdf"), tmp_path, selected, Console())

    assert files == [tmp_path / "Timers.pdf"]
    assert calls == [["Timers.pdf"]]
