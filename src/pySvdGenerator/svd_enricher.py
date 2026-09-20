# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Enrich a device tree generated SVD with register level information.

The SVD produced from the device tree holds the memory map (base addresses,
address blocks) and the interrupts. The dictionaries produced by
:mod:`pySvdGenerator.register_extractor` hold the registers and bit fields
read from the reference manual. This module merges both: peripherals are
paired by base address, then by name, then by address containment, and every
register is kept only when it falls inside an address block declared by the
device tree ``reg`` property. Whatever could not be merged is reported
through :class:`EnrichmentReport` instead of being silently dropped.
"""

from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

__all__ = ["EnrichmentReport", "PeripheralMatch", "enrich_svd", "load_dictionaries"]

LOGGER = logging.getLogger(__name__)

Dictionary = Mapping[str, Any]


@dataclass
class PeripheralMatch:
    """Outcome of the pairing of one SVD peripheral with the manual.

    :param peripheral: name of the SVD peripheral.
    :param source: document the register description comes from.
    :param registers: number of registers injected.
    :param fields: number of bit fields injected.
    :param rejected: registers dropped because their address falls outside
        the address blocks declared by the device tree.
    """

    peripheral: str
    source: str
    registers: int = 0
    fields: int = 0
    rejected: list[str] = field(default_factory=list)


@dataclass
class EnrichmentReport:
    """Summary of an enrichment run.

    :param output: SVD file that has been written.
    :param matches: one entry per enriched peripheral.
    :param unmatched_svd: device tree peripherals left without registers.
    :param unused_sources: manual peripherals absent from the device tree.
    """

    output: Path
    matches: list[PeripheralMatch] = field(default_factory=list)
    unmatched_svd: list[str] = field(default_factory=list)
    unused_sources: list[str] = field(default_factory=list)

    @property
    def registers(self) -> int:
        """Return the total number of registers injected."""
        return sum(match.registers for match in self.matches)

    @property
    def fields(self) -> int:
        """Return the total number of bit fields injected."""
        return sum(match.fields for match in self.matches)


# --------------------------------------------------------------------------
# inputs
# --------------------------------------------------------------------------


@dataclass
class _Source:
    """A peripheral description coming from the reference manual."""

    name: str
    base_address: int | None
    registers: Dictionary
    origin: str

    @property
    def key(self) -> str:
        """Return a unique key identifying the source peripheral."""
        return f"{self.origin}:{self.name}"


def load_dictionaries(
    data: Dictionary | Sequence[Dictionary | Path | str] | Path | str,
) -> list[Dictionary]:
    """Normalise the accepted inputs into a list of chapter dictionaries.

    :param data: a dictionary, a sequence of dictionaries or paths, the path
        of a JSON file, or a directory holding JSON files.
    :return: the chapter dictionaries, in the order they were given.
    """
    if isinstance(data, (str, Path)):
        path = Path(data)
        if path.is_dir():
            return [json.loads(item.read_text()) for item in sorted(path.glob("*.json"))]
        return [json.loads(path.read_text())]
    if isinstance(data, Mapping):
        return [data]
    out: list[Dictionary] = []
    for item in data:
        out.extend(load_dictionaries(item))
    return out


def _sources(chapters: Iterable[Dictionary]) -> list[_Source]:
    """Flatten the chapter dictionaries into one entry per peripheral."""
    sources: list[_Source] = []
    for chapter in chapters:
        origin = str(chapter.get("source", "?"))
        for name, body in chapter.get("peripherals", {}).items():
            sources.append(
                _Source(
                    name=str(name).upper(),
                    base_address=body.get("base_address"),
                    registers=body.get("registers", {}),
                    origin=origin,
                )
            )
    return sources


# --------------------------------------------------------------------------
# SVD helpers
# --------------------------------------------------------------------------


def _text(element: ET.Element | None) -> str:
    """Return the stripped text of an element, empty when it is missing."""
    return (element.text or "").strip() if element is not None else ""


def _number(element: ET.Element | None) -> int | None:
    """Return the element text read as an integer, None when unusable."""
    raw = _text(element)
    if not raw:
        return None
    try:
        return int(raw, 0)
    except ValueError:
        return None


def _windows(peripheral: ET.Element) -> list[tuple[int, int]]:
    """Return the ``(start, end)`` address blocks of an SVD peripheral."""
    base = _number(peripheral.find("baseAddress"))
    if base is None:
        return []
    blocks: list[tuple[int, int]] = []
    for block in peripheral.findall("addressBlock"):
        offset = _number(block.find("offset")) or 0
        size = _number(block.find("size")) or 0
        if size > 0:
            blocks.append((base + offset, base + offset + size))
    return blocks or [(base, base + 0x1000)]


def _pair(peripherals: Sequence[ET.Element], sources: Sequence[_Source]) -> dict[int, _Source]:
    """Pair SVD peripherals with manual descriptions, strongest match first."""
    pairs: dict[int, _Source] = {}
    taken: set[str] = set()

    def assign(peripheral: ET.Element, source: _Source) -> None:
        pairs[id(peripheral)] = source
        taken.add(source.key)

    def pending() -> list[ET.Element]:
        return [item for item in peripherals if id(item) not in pairs]

    def free() -> list[_Source]:
        return [item for item in sources if item.key not in taken]

    for peripheral in pending():
        base = _number(peripheral.find("baseAddress"))
        for source in free():
            if source.base_address is not None and source.base_address == base:
                assign(peripheral, source)
                break

    for peripheral in pending():
        name = _text(peripheral.find("name")).upper()
        for source in free():
            if source.name == name:
                assign(peripheral, source)
                break

    for peripheral in pending():
        windows = [(start, end) for start, end in _windows(peripheral) if end - start <= 0x10000]
        for source in free():
            if source.base_address is not None and any(
                start <= source.base_address < end for start, end in windows
            ):
                assign(peripheral, source)
                break

    return pairs


def _address(base: int | None, body: Dictionary) -> int | None:
    """Return the absolute address of a register from the manual entry."""
    address = body.get("address")
    if isinstance(address, int):
        return address
    offset = body.get("offset")
    if isinstance(offset, int) and base is not None:
        return base + offset
    return None


def _sub(parent: ET.Element, tag: str, text: str) -> ET.Element:
    """Append a child element carrying ``text`` and return it."""
    element = ET.SubElement(parent, tag)
    element.text = text
    return element


def _append_fields(register: ET.Element, fields: Dictionary) -> int:
    """Append the ``<fields>`` section of a register and return its size."""
    if not fields:
        return 0
    container = ET.SubElement(register, "fields")
    count = 0
    for name, body in sorted(fields.items(), key=lambda item: item[1].get("bit_offset", 0)):
        element = ET.SubElement(container, "field")
        _sub(element, "name", str(name))
        description = str(body.get("description") or "").strip()
        if description:
            _sub(element, "description", description)
        _sub(element, "bitOffset", str(body.get("bit_offset", 0)))
        _sub(element, "bitWidth", str(body.get("bit_width", 1)))
        access = body.get("access")
        if access:
            _sub(element, "access", str(access))
        count += 1
    return count


def _enrich_peripheral(peripheral: ET.Element, source: _Source) -> PeripheralMatch:
    """Write the registers of ``source`` into an SVD peripheral element.

    Registers whose address falls outside the address blocks declared by the
    device tree are skipped and listed in the returned match.
    """
    match = PeripheralMatch(peripheral=_text(peripheral.find("name")), source=source.origin)
    base = _number(peripheral.find("baseAddress"))
    windows = _windows(peripheral)

    existing = peripheral.find("registers")
    if existing is not None:
        peripheral.remove(existing)
    container = ET.Element("registers")

    for name, body in source.registers.items():
        address = _address(source.base_address, body)
        if address is None or not any(start <= address < end for start, end in windows):
            match.rejected.append(str(name))
            continue
        register = ET.SubElement(container, "register")
        _sub(register, "name", str(name))
        description = str(body.get("description") or "").strip()
        if description:
            _sub(register, "description", description)
        _sub(register, "addressOffset", f"0x{address - (base or 0):X}")
        _sub(register, "size", str(body.get("width") or 32))
        access = body.get("access")
        if access:
            _sub(register, "access", str(access))
        reset = body.get("reset_value")
        if isinstance(reset, int):
            _sub(register, "resetValue", f"0x{reset:X}")
        match.fields += _append_fields(register, body.get("fields", {}))
        match.registers += 1

    if match.registers:
        peripheral.append(container)
    if match.rejected:
        LOGGER.info(
            "%s: %d registers outside the device tree address blocks",
            match.peripheral,
            len(match.rejected),
        )
    return match


def _new_peripheral(source: _Source) -> ET.Element | None:
    """Build the SVD shell for a peripheral found only in the manual."""
    if source.base_address is None:
        return None

    addresses = [
        address
        for body in source.registers.values()
        if (address := _address(source.base_address, body)) is not None
    ]
    max_offset = max((address - source.base_address for address in addresses), default=0)
    block_size = max(0x1000, ((max_offset + 4 + 0xFFF) // 0x1000) * 0x1000)

    peripheral = ET.Element("peripheral")
    _sub(peripheral, "name", source.name)
    _sub(peripheral, "groupName", re.sub(r"\d+$", "", source.name) or source.name)
    _sub(peripheral, "baseAddress", f"0x{source.base_address:X}")
    block = ET.SubElement(peripheral, "addressBlock")
    _sub(block, "offset", "0x0")
    _sub(block, "size", f"0x{block_size:X}")
    _sub(block, "usage", "registers")
    return peripheral


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------


def enrich_svd(
    svd: Path | str,
    registers: Dictionary | Sequence[Dictionary | Path | str] | Path | str,
    output: Path | str | None = None,
    *,
    peripherals: Sequence[str] | None = None,
    allow_new: bool = False,
) -> EnrichmentReport:
    """Inject the manual register descriptions into an SVD file.

    :param svd: SVD file generated from the device tree.
    :param registers: dictionary produced by
        :func:`pySvdGenerator.extract_registers`, a sequence of such
        dictionaries, or the path of a JSON file or directory holding them.
    :param output: destination file, defaults to updating ``svd`` in place.
    :param peripherals: names of the SVD and manual peripherals to process;
        when omitted, all peripherals are processed.
    :param allow_new: append selected manual peripherals missing from the SVD.
    :return: a report describing what has been merged.

    Peripherals already holding a ``<registers>`` section and not covered by
    the given dictionaries are left untouched and are not reported as
    unmatched, so an SVD file can be completed one chapter at a time.
    """
    source_path = Path(svd)
    if not source_path.is_file():
        raise FileNotFoundError(f"No such SVD file: {source_path}")

    tree = ET.parse(source_path)
    root = tree.getroot()
    sources = _sources(load_dictionaries(registers))
    wanted = {name.upper() for name in peripherals} if peripherals else None
    if wanted is not None:
        sources = [item for item in sources if item.name in wanted]
    all_peripherals = root.findall("./peripherals/peripheral")
    selected_peripherals = (
        [item for item in all_peripherals if _text(item.find("name")).upper() in wanted]
        if wanted is not None
        else all_peripherals
    )
    pairs = _pair(selected_peripherals, sources)
    used: set[str] = set()

    report = EnrichmentReport(output=Path(output) if output else source_path)
    for peripheral in selected_peripherals:
        candidate = pairs.get(id(peripheral))
        documented = peripheral.find("registers") is not None
        if candidate is None:
            if not documented:
                report.unmatched_svd.append(_text(peripheral.find("name")))
            continue
        used.add(candidate.key)
        match = _enrich_peripheral(peripheral, candidate)
        if match.registers:
            report.matches.append(match)
        elif not documented:
            report.unmatched_svd.append(match.peripheral)

    parent = root.find("./peripherals")
    if parent is not None and allow_new:
        for source in sources:
            if source.key in used:
                continue
            new_peripheral = _new_peripheral(source)
            if new_peripheral is None:
                continue
            parent.append(new_peripheral)
            match = _enrich_peripheral(new_peripheral, source)
            used.add(source.key)
            if match.registers:
                report.matches.append(match)

    report.unused_sources = [item.name for item in sources if item.key not in used]

    ET.indent(tree, space="  ")
    report.output.parent.mkdir(parents=True, exist_ok=True)
    tree.write(report.output, encoding="utf-8", xml_declaration=True)
    return report
