# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Lookup of device tree binding documentation shipped with the kernel."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

__all__ = ["BindingIndex"]

BINDINGS_DIR = Path("Documentation/devicetree/bindings")

_TITLE_RE = re.compile(r"^title:\s*(?:[|>][-+]?\s*)?(.*)$", re.M)
_TXT_TITLE_RE = re.compile(r"^[*#=\s]*(.+?)\s*$", re.M)


@dataclass
class BindingIndex:
    """Map ``compatible`` strings to a human readable description.

    :param titles: title of the binding documenting each compatible.
    :param sources: binding file each compatible was found in.
    """

    titles: dict[str, str] = field(default_factory=dict)
    sources: dict[str, Path] = field(default_factory=dict)

    @classmethod
    def build(cls, kernel_path: Path | str, compatibles: Iterable[str]) -> "BindingIndex":
        """Scan the kernel bindings for the given ``compatible`` strings."""
        index = cls()
        wanted = {compatible for compatible in compatibles if compatible}
        root = Path(kernel_path) / BINDINGS_DIR
        if not wanted or not root.is_dir():
            return index

        for path in sorted(root.rglob("*")):
            if not wanted:
                break
            if path.suffix not in (".yaml", ".txt") or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            found = [compatible for compatible in wanted if compatible in text]
            if not found:
                continue
            title = _extract_title(path, text)
            for compatible in found:
                if title:
                    index.titles[compatible] = title
                index.sources[compatible] = path
            wanted.difference_update(found)
        return index

    def describe(self, compatibles: Iterable[str]) -> str | None:
        """Return the binding title of the first documented compatible."""
        for compatible in compatibles:
            title = self.titles.get(compatible)
            if title:
                return title
        return None


def _extract_title(path: Path, text: str) -> str | None:
    """Return the title of a binding, YAML front matter or first text line."""
    if path.suffix == ".yaml":
        match = _TITLE_RE.search(text)
        if match:
            return match.group(1).strip().strip("\"'") or None
        return None
    for line in text.splitlines():
        cleaned = _TXT_TITLE_RE.match(line)
        if cleaned and cleaned.group(1).strip():
            return cleaned.group(1).strip()
    return None
