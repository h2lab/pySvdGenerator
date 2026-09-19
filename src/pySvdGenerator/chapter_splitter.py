# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Split a reference manual PDF into one document per chapter.

Chapters are discovered from the top level entries of the PDF outline
(bookmarks). Each chapter is written as a standalone PDF named after the
chapter title, inside a given workspace directory.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

from pypdf import PdfReader, PdfWriter
from pypdf.generic import Destination

__all__ = ["DEFAULT_WORKSPACE", "Chapter", "list_chapters", "matches", "split_chapters"]

DEFAULT_WORKSPACE = Path("workspace")

_INVALID_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class Chapter:
    """A chapter of a document, as a page range and a title.

    :param title: chapter title, as written in the PDF outline.
    :param first_page: 0 based index of the first page.
    :param last_page: 0 based index of the last page, included.
    """

    title: str
    first_page: int
    last_page: int

    @property
    def filename(self) -> str:
        """Return the sanitized file name (with extension) of the chapter."""
        return f"{sanitize_name(self.title)}.pdf"


def sanitize_name(title: str) -> str:
    """Turn a chapter title into a safe, portable file name stem."""
    # Drop zero-width and other formatting code points kept in PDF bookmarks.
    cleaned = "".join(char for char in title if unicodedata.category(char) != "Cf")
    cleaned = unicodedata.normalize("NFKD", cleaned)
    cleaned = cleaned.encode("ascii", "ignore").decode("ascii")
    cleaned = _INVALID_NAME_CHARS.sub("_", cleaned).strip("._-")
    return cleaned or "chapter"


def _top_level_outline(reader: PdfReader) -> Iterator[Destination]:
    """Iterate over the first level entries of the document outline."""
    for item in reader.outline:
        if isinstance(item, Destination):
            yield item


def list_chapters(document: Path | str) -> list[Chapter]:
    """Return the chapters of ``document`` without writing anything."""
    reader = PdfReader(str(document))
    page_count = len(reader.pages)

    starts: list[tuple[str, int]] = []
    for entry in _top_level_outline(reader):
        page = reader.get_destination_page_number(entry)
        if page is None:
            continue
        starts.append((str(entry.title), page))

    chapters: list[Chapter] = []
    for index, (title, first_page) in enumerate(starts):
        next_start = starts[index + 1][1] if index + 1 < len(starts) else page_count
        last_page = max(first_page, next_start - 1)
        chapters.append(Chapter(title=title, first_page=first_page, last_page=last_page))
    return chapters


def matches(chapter: Chapter, select: Sequence[str]) -> bool:
    """Tell whether a chapter title or file name contains one of ``select``."""
    if not select:
        return True
    haystack = f"{chapter.title} {chapter.filename}".lower()
    return any(needle.lower() in haystack for needle in select)


def split_chapters(
    document: Path | str,
    workspace: Path | str = DEFAULT_WORKSPACE,
    *,
    overwrite: bool = True,
    select: Sequence[str] | None = None,
) -> list[Path]:
    """Split ``document`` into one PDF per chapter inside ``workspace``.

    :param document: path to the source PDF document.
    :param workspace: directory where the chapter documents are written.
    :param overwrite: when False, already existing chapter files are kept.
    :param select: only split the chapters whose title or file name contains
        one of these substrings.
    :return: the list of written (or already present) chapter file paths.
    :raises FileNotFoundError: if ``document`` does not exist.
    :raises ValueError: if no chapter could be found in the document outline.
    """
    source = Path(document)
    if not source.is_file():
        raise FileNotFoundError(f"No such document: {source}")

    chapters = list_chapters(source)
    if not chapters:
        raise ValueError(f"No chapter outline found in {source}")

    target_dir = Path(workspace)
    target_dir.mkdir(parents=True, exist_ok=True)

    reader = PdfReader(str(source))
    written: list[Path] = []
    used: dict[str, int] = {}

    for chapter in chapters:
        name = chapter.filename
        seen = used.get(name, 0)
        used[name] = seen + 1
        if seen:
            name = f"{Path(name).stem}_{seen}.pdf"
        if not matches(chapter, select or ()):
            continue

        output = target_dir / name
        written.append(output)
        if output.exists() and not overwrite:
            continue

        writer = PdfWriter()
        for page in range(chapter.first_page, chapter.last_page + 1):
            writer.add_page(reader.pages[page])
        with output.open("wb") as stream:
            writer.write(stream)
        writer.close()

    return written
