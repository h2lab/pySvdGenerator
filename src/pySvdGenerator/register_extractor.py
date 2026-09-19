# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Extract register descriptions from a reference manual chapter.

The extraction is strictly sequential:

1. every page of the chapter is read once, its text through :mod:`pypdf` and
   its tables through :mod:`tabula`;
2. each table is located on its page and classified as a register memory map
   or as a bit field table;
3. tables continued over several pages are merged before being parsed;
4. memory maps give the register list, field tables are attached to their
   register and give the bit fields;
5. registers are split per peripheral instance, a single memory map table
   usually covering every instance of the block (``GPT1``, ``GPT2``, ...);
6. the local `ollama` agent consolidates the result.

The agent is a required part of the extraction, not an optional extra. It
classifies the tables the header heuristics could not recognise, decides
which register an orphan field table documents, splits register names whose
instance is not exposed by the mnemonic and repairs field names degraded by
the PDF text extraction. A reachable ollama server is therefore mandatory;
:class:`OllamaUnavailableError` is raised when it cannot be contacted.

Typical use:

.. code-block:: python

    from pySvdGenerator import extract_registers

    data = extract_registers("workspace/Chapter_12_Timers.pdf")
    data["peripherals"]["GPT1"]["registers"]["CR"]["fields"]["EN"]
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import ollama
import pandas as pd
from pypdf import PdfReader

__all__ = [
    "BATCH_SIZE",
    "DEFAULT_MODEL",
    "FALLBACK_MODELS",
    "ChapterRegisters",
    "Field",
    "OllamaAssistant",
    "OllamaUnavailableError",
    "PeripheralRegisters",
    "Register",
    "Table",
    "extract_peripherals",
    "extract_registers",
]

LOGGER = logging.getLogger(__name__)

#: Default ollama model used by the consolidation agent.
DEFAULT_MODEL = "qwen2.5-coder:7b"
#: Models used, in order, when the requested one is not pulled.
FALLBACK_MODELS = ("llama3.2",)
#: Number of pages handed over to tabula in a single call.
BATCH_SIZE = 25

#: Table holding the register list of a peripheral.
MEMORY_MAP = "memory-map"
#: Table holding the bit fields of a register.
FIELDS = "fields"
#: Table that could not be classified.
UNKNOWN = "unknown"


class OllamaUnavailableError(RuntimeError):
    """Raised when the local ollama server cannot be reached."""


def _available_models(listing: Any) -> list[str]:
    """Return the model names published by ``client.list()``.

    The ollama library has used both typed objects and plain dictionaries,
    so both shapes are accepted.
    """
    models = getattr(listing, "models", None)
    if models is None and isinstance(listing, dict):
        models = listing.get("models", [])
    names: list[str] = []
    for entry in models or []:
        name = getattr(entry, "model", None) or getattr(entry, "name", None)
        if name is None and isinstance(entry, dict):
            name = entry.get("model") or entry.get("name")
        if name:
            names.append(str(name))
    return names


def _select_model(wanted: str, available: Sequence[str]) -> str:
    """Return the model to query, falling back when ``wanted`` is missing.

    :param wanted: model asked for by the caller.
    :param available: models pulled on the server.
    :return: ``wanted`` when available, otherwise the first usable fallback.
    :raises OllamaUnavailableError: when no usable model is pulled.
    """
    if not available:
        return wanted  # injected or minimal client, trust the caller
    if any(_same_model(wanted, name) for name in available):
        return wanted
    for fallback in FALLBACK_MODELS:
        match = next((name for name in available if _same_model(fallback, name)), None)
        if match:
            LOGGER.warning("ollama model %r is not pulled, falling back to %r", wanted, match)
            return match
    raise OllamaUnavailableError(
        f"neither {wanted!r} nor any fallback of {list(FALLBACK_MODELS)} is pulled on the "
        f"ollama server, available models: {list(available)}"
    )


def _same_model(wanted: str, candidate: str) -> bool:
    """Compare two model names, an absent tag matching any tag."""
    if wanted == candidate:
        return True
    return ":" not in wanted and candidate.split(":", 1)[0] == wanted


# --------------------------------------------------------------------------
# data model
# --------------------------------------------------------------------------


@dataclass
class Field:
    """A bit field of a register."""

    name: str
    bit_offset: int
    bit_width: int
    description: str = ""
    access: str | None = None
    reset_value: int | None = None


@dataclass
class Register:
    """A register of a peripheral."""

    name: str
    offset: int | None = None
    address: int | None = None
    width: int = 32
    access: str | None = None
    reset_value: int | None = None
    description: str = ""
    fields: list[Field] = field(default_factory=list)


@dataclass
class PeripheralRegisters:
    """The complete register description of a single peripheral instance."""

    name: str
    base_address: int | None = None
    registers: list[Register] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return the peripheral description as a plain dictionary."""
        return {
            "base_address": self.base_address,
            "registers": {
                register.name: {
                    key: value
                    for key, value in asdict(register).items()
                    if key not in ("name", "fields")
                }
                | {"fields": {item["name"]: item for item in asdict(register)["fields"]}}
                for register in self.registers
            },
        }


@dataclass
class ChapterRegisters:
    """Every peripheral instance described by a chapter."""

    source: str
    peripherals: list[PeripheralRegisters] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return the description as a plain dictionary."""
        return {
            "source": self.source,
            "peripherals": {item.name: item.to_dict() for item in self.peripherals},
        }


# --------------------------------------------------------------------------
# text helpers
# --------------------------------------------------------------------------

_SPACES_RE = re.compile(r"\s+")
_HEX_RE = re.compile(r"^(?:0x([0-9a-fA-F_]+)|([0-9a-fA-F_]+)h)$")
_BITS_RE = re.compile(r"^(\d{1,2})(?:\s*[-:\u2013]\s*(\d{1,2}))?(?=\s|$)")
_PAREN_NAME_RE = re.compile(r"\(([A-Za-z][A-Za-z0-9_]{1,40})\)")
_NAME_RE = re.compile(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*)\b")
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_INSTANCE_RE = re.compile(r"\d+")


def _clean(value: Any) -> str:
    """Return a cell value as a single line string, empty for missing values."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return _SPACES_RE.sub(" ", str(value)).strip()


def _parse_int(text: str, hexadecimal: bool = False) -> int | None:
    """Parse ``0x1f``, ``1Fh``, ``3066_0008`` or a decimal number.

    :param text: raw cell content.
    :param hexadecimal: read plain digit strings as hexadecimal, which is the
        convention of the address columns of a reference manual.
    :return: the parsed value, or None when the cell holds no number.
    """
    raw = _clean(text)
    cleaned = raw.replace("_", "").rstrip(".")
    if not cleaned:
        return None
    match = _HEX_RE.match(cleaned)
    if match:
        return int(match.group(1) or match.group(2), 16)
    if hexadecimal or "_" in raw:
        try:
            return int(cleaned, 16)
        except ValueError:
            return None
    if cleaned.isdigit():
        return int(cleaned)
    return None


def _parse_bits(text: str) -> tuple[int, int] | None:
    """Parse a bit range such as ``31-16`` into ``(offset, width)``."""
    cleaned = _clean(text)
    if cleaned[:2].lower() in ("0x", "0b"):
        return None
    match = _BITS_RE.match(cleaned)
    if not match:
        return None
    high = int(match.group(1))
    low = int(match.group(2)) if match.group(2) is not None else high
    if low > high:
        high, low = low, high
    if high > 63:
        return None
    return low, high - low + 1


def _identifier(text: str) -> str | None:
    """Extract an upper case mnemonic from a table cell.

    The mnemonic between parentheses wins, as reference manuals write
    ``GPT Control Register (GPT1_CR)``.
    """
    cleaned = _clean(text)
    if not cleaned:
        return None
    parenthesised = _PAREN_NAME_RE.findall(cleaned)
    if parenthesised:
        return str(parenthesised[-1]).upper()
    match = _NAME_RE.search(cleaned.replace(" ", "_"))
    if match:
        return match.group(1).upper()
    return re.sub(r"[^A-Za-z0-9_]+", "_", cleaned).strip("_").upper() or None


def _normalise_access(text: str) -> str | None:
    """Translate a manual access column into the SVD access vocabulary."""
    cleaned = _clean(text).upper()
    if not cleaned:
        return None
    if "RW" in cleaned or ("R" in cleaned and "W" in cleaned):
        return "read-write"
    if cleaned.startswith("R"):
        return "read-only"
    if cleaned.startswith("W"):
        return "write-only"
    return None


# --------------------------------------------------------------------------
# table acquisition
# --------------------------------------------------------------------------


@dataclass
class Table:
    """A table extracted from the document."""

    frame: pd.DataFrame
    page: int
    kind: str = UNKNOWN
    header: list[str] = field(default_factory=list)

    def rows(self) -> list[list[str]]:
        """Return the cleaned data rows of the table."""
        return [[_clean(cell) for cell in row] for row in self.frame.itertuples(index=False)]


def _page_texts(pdf: Path, pages: Sequence[int]) -> dict[int, str]:
    """Return the text of the requested pages, indexed by page number."""
    reader = PdfReader(str(pdf))
    return {number: reader.pages[number - 1].extract_text() or "" for number in pages}


def _read_tables(pdf: Path, pages: Sequence[int], batch_size: int) -> list[Table]:
    """Read every table of the requested pages, in batches, through tabula."""
    from tabula.io import read_pdf

    tables: list[Table] = []
    for start in range(0, len(pages), batch_size):
        batch = pages[start : start + batch_size]
        spec = ",".join(str(number) for number in batch)
        try:
            frames = read_pdf(
                str(pdf),
                pages=spec,
                multiple_tables=True,
                lattice=True,
                pandas_options={"header": None, "dtype": str},
                silent=True,
            )
        except Exception as error:  # tabula raises bare exceptions on bad pages
            LOGGER.warning("tabula failed on pages %s: %s", spec, error)
            continue
        for frame in frames:
            if isinstance(frame, pd.DataFrame) and not frame.empty:
                tables.append(Table(frame=frame, page=batch[0]))
    return tables


def _locate(tables: list[Table], texts: dict[int, str], pages: Sequence[int]) -> None:
    """Assign each table to the page it was extracted from."""
    cursor = 0
    for table in tables:
        signature = next((cell for row in table.rows() for cell in row if len(cell) > 6), "")
        for index in range(cursor, len(pages)):
            if not signature or signature[:40] in texts[pages[index]].replace("\n", " "):
                table.page = pages[index]
                cursor = index
                break
        else:
            table.page = pages[cursor] if pages else 0


# --------------------------------------------------------------------------
# classification and merging
# --------------------------------------------------------------------------

_MEMORY_MAP_HINTS = ("offset", "address")
_FIELD_HINTS = ("field", "bits", "bit")


def _classify(header: Sequence[str]) -> str:
    """Return the kind of a table from the wording of its header row."""
    cells = [cell.lower() for cell in header]
    joined = " ".join(cells)
    if any(hint in joined for hint in _MEMORY_MAP_HINTS) and "register" in joined:
        return MEMORY_MAP
    if any(cell.startswith(_FIELD_HINTS) for cell in cells) and "description" in joined:
        return FIELDS
    return UNKNOWN


def _split_header(table: Table) -> None:
    """Detect the header row of a table and set its kind accordingly."""
    rows = table.rows()
    if not rows:
        return
    kind = _classify(rows[0])
    if kind == UNKNOWN and len(rows) > 1:
        kind = _classify(rows[1])
        if kind != UNKNOWN:
            table.header = rows[1]
            table.frame = table.frame.iloc[2:]
            table.kind = kind
            return
    if kind != UNKNOWN:
        table.header = rows[0]
        table.frame = table.frame.iloc[1:]
    table.kind = kind


def _merge(tables: list[Table]) -> list[Table]:
    """Merge tables continued on the following pages."""
    merged: list[Table] = []
    for table in tables:
        previous = merged[-1] if merged else None
        continuation = (
            previous is not None
            and table.kind == UNKNOWN
            and previous.kind != UNKNOWN
            and not table.header
            and table.frame.shape[1] == previous.frame.shape[1]
            and 0 <= table.page - previous.page <= 1
        )
        same_table = (
            previous is not None
            and table.kind == previous.kind
            and table.header == previous.header
            and 0 <= table.page - previous.page <= 1
        )
        if previous is not None and (continuation or same_table):
            previous.frame = pd.concat([previous.frame, table.frame], ignore_index=True)
            previous.page = table.page
            continue
        merged.append(table)
    return merged


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def _column(header: Sequence[str], *hints: str) -> int | None:
    """Return the index of the first column whose title matches a hint."""
    for index, cell in enumerate(header):
        lowered = cell.lower()
        if any(hint in lowered for hint in hints):
            return index
    return None


def _parse_memory_map(table: Table) -> list[Register]:
    """Turn a register memory map table into a list of registers."""
    header = table.header
    offset_col = _column(header, "offset", "address") or 0
    name_col = _column(header, "register", "name")
    width_col = _column(header, "width", "size")
    access_col = _column(header, "access")
    reset_col = _column(header, "reset")

    registers: list[Register] = []
    for row in table.rows():
        if name_col is None or name_col >= len(row):
            continue
        offset = _parse_int(row[offset_col], hexadecimal=True) if offset_col < len(row) else None
        name = _identifier(row[name_col])
        if name is None or offset is None or len(name) < 2 or name.isdigit():
            continue
        width = (
            _parse_int(row[width_col]) if width_col is not None and width_col < len(row) else None
        )
        registers.append(
            Register(
                name=name,
                address=offset,
                offset=offset,
                width=width or 32,
                access=(
                    _normalise_access(row[access_col])
                    if access_col is not None and access_col < len(row)
                    else None
                ),
                reset_value=(
                    _parse_int(row[reset_col], hexadecimal=True)
                    if reset_col is not None and reset_col < len(row)
                    else None
                ),
                description=_clean(row[name_col]),
            )
        )
    return registers


def _parse_fields(table: Table) -> list[Field]:
    """Turn a bit field table into a list of fields."""
    header = table.header
    bits_col = _column(header, "bits", "bit", "field") or 0
    name_col = _column(header, "name")
    desc_col = _column(header, "description")
    access_col = _column(header, "access", "type")
    reset_col = _column(header, "reset")

    fields: list[Field] = []
    for row in table.rows():
        if bits_col >= len(row):
            continue
        bits = _parse_bits(row[bits_col])
        if bits is None:
            if fields and desc_col is not None and desc_col < len(row):
                fields[-1].description = f"{fields[-1].description} {row[desc_col]}".strip()
            continue
        offset, width = bits
        raw_name = row[name_col] if name_col is not None and name_col < len(row) else ""
        if not raw_name:
            raw_name = _clean(_BITS_RE.sub("", _clean(row[bits_col]), count=1))
        description = _clean(row[desc_col]) if desc_col is not None and desc_col < len(row) else ""
        name = _field_name(raw_name, description, offset)
        fields.append(
            Field(
                name=name,
                bit_offset=offset,
                bit_width=width,
                description=description or _clean(raw_name),
                access=(
                    _normalise_access(row[access_col])
                    if access_col is not None and access_col < len(row)
                    else None
                ),
                reset_value=(
                    _parse_int(row[reset_col])
                    if reset_col is not None and reset_col < len(row)
                    else None
                ),
            )
        )
    return _deduplicate(fields)


def _deduplicate(fields: Iterable[Field]) -> list[Field]:
    """Drop the fields covering an already described bit range."""
    seen: dict[tuple[int, int], Field] = {}
    for item in fields:
        seen.setdefault((item.bit_offset, item.bit_width), item)
    return sorted(seen.values(), key=lambda item: item.bit_offset)


def _field_name(raw_name: str, description: str, offset: int) -> str:
    """Build a usable field name from the cell content and the description."""
    token = _TOKEN_RE.match(_clean(raw_name))
    if token and len(token.group(0)) <= 32:
        return token.group(0).upper()
    candidate = _identifier(" ".join(_clean(description).split()[:3]) or "")
    if candidate and len(candidate) <= 32:
        return candidate
    return f"FIELD_{offset}"


# --------------------------------------------------------------------------
# register / field table association
# --------------------------------------------------------------------------


def _base_name(name: str) -> str:
    """Return an instance agnostic register key (``GPT1_CR`` -> ``GPT_CR``)."""
    return _INSTANCE_RE.sub("", name.upper())


def _owners(table: Table, texts: dict[int, str], registers: dict[str, Register]) -> list[str]:
    """Return the registers documented by a bit field table.

    Reference manuals title the field table with the register mnemonic, using
    a placeholder for the instance index, so a single table describes every
    instance sharing the same base name.
    """
    index: dict[str, list[str]] = {}
    for name in registers:
        index.setdefault(_base_name(name), []).append(name)

    for page in (table.page, table.page - 1):
        text = texts.get(page)
        if not text:
            continue
        for candidate in reversed(_PAREN_NAME_RE.findall(text)):
            owners = index.get(_base_name(candidate))
            if owners:
                return owners
    return []


# --------------------------------------------------------------------------
# local ollama agent
# --------------------------------------------------------------------------


class OllamaAssistant:
    """Local ollama agent consolidating the deterministic extraction.

    The agent answers in strict JSON at temperature 0. It is created once per
    extraction and reused for every request, so the model stays loaded.

    When the requested model is not pulled on the server, the first available
    model of :data:`FALLBACK_MODELS` is used instead.

    :param model: name of the ollama model to query.
    :param host: base URL of the ollama server, the library default when
        omitted.
    :param client: pre-built client, mainly meant for testing.
    :raises OllamaUnavailableError: when the server cannot be reached, or
        when neither the requested model nor a fallback is available.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        host: str | None = None,
        client: Any | None = None,
    ) -> None:
        self._client: Any = client if client is not None else ollama.Client(host=host)
        try:
            available = _available_models(self._client.list())
        except Exception as error:
            raise OllamaUnavailableError(
                f"the ollama server is required but could not be reached: {error}"
            ) from error
        self.model = _select_model(model, available)

    def _ask(self, prompt: str) -> dict[str, Any] | None:
        """Send a prompt and return the parsed JSON answer, None on failure."""
        try:
            response = self._client.chat(
                model=self.model,
                format="json",
                options={"temperature": 0},
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You convert tables extracted from a chip reference "
                            "manual into strict JSON. Answer with JSON only."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
            )
            return dict(json.loads(response["message"]["content"]))
        except Exception as error:
            LOGGER.warning("ollama request failed: %s", error)
            return None

    def classify(self, table: Table) -> str:
        """Return the kind of a table the header heuristics did not recognise.

        :param table: table to classify.
        :return: :data:`MEMORY_MAP`, :data:`FIELDS` or :data:`UNKNOWN`.
        """
        answer = self._ask(
            "Classify the following table as one of "
            f'"{MEMORY_MAP}", "{FIELDS}" or "{UNKNOWN}". '
            'Answer {"kind": "..."}.\n\n' + _render(table)
        )
        kind = str(answer.get("kind", UNKNOWN)) if answer else UNKNOWN
        return kind if kind in (MEMORY_MAP, FIELDS) else UNKNOWN

    def owner(self, table: Table, text: str, known: Sequence[str]) -> str | None:
        """Return the register documented by an orphan bit field table.

        :param table: bit field table whose owner is unknown.
        :param text: text of the page holding the table.
        :param known: register names extracted from the memory maps.
        :return: one of ``known``, or None when the model does not decide.
        """
        answer = self._ask(
            "Given the page text and the bit field table, return the register "
            'it documents as {"register": "NAME"}. Choose among: '
            + ", ".join(known[:200])
            + "\n\nPAGE TEXT:\n"
            + text[:2000]
            + "\n\nTABLE:\n"
            + _render(table)
        )
        if not answer:
            return None
        name = _clean(str(answer.get("register", "")))
        return name.upper() if name.upper() in {item.upper() for item in known} else None

    def split_instances(self, names: Sequence[str], hint: str) -> dict[str, tuple[str, str]]:
        """Split register names into a peripheral instance and a mnemonic.

        A memory map table usually covers every instance of a peripheral
        (``GPT1``, ``GPT2``, ...). The model is queried for the names that do
        not follow the ``<INSTANCE>_<REGISTER>`` convention.

        :param names: register names to split.
        :param hint: name of the peripheral block documented by the chapter.
        :return: mapping of the resolved names to ``(instance, mnemonic)``.
        """
        answer = self._ask(
            "Each name below identifies a register of the "
            f"{hint} peripheral block. Split every name into the peripheral "
            "instance it belongs to and the register mnemonic. Answer "
            '{"NAME": {"peripheral": "...", "register": "..."}} for all names.'
            "\n\nNAMES:\n" + "\n".join(names[:120])
        )
        if not answer:
            return {}
        out: dict[str, tuple[str, str]] = {}
        for name in names:
            entry = answer.get(name)
            if not isinstance(entry, dict):
                continue
            instance = _clean(str(entry.get("peripheral", ""))).upper()
            register = _clean(str(entry.get("register", ""))).upper()
            if instance and register:
                out[name] = (instance, register)
        return out

    def field_names(self, register: str, fields: Sequence[Field]) -> dict[str, str]:
        """Propose proper mnemonics for field names degraded by the PDF.

        :param register: name of the register holding the fields.
        :param fields: fields whose extracted name looks unusable.
        :return: mapping of the current names to the proposed mnemonics.
        """
        described = "\n".join(
            f"{item.name} | bits {item.bit_offset}..{item.bit_offset + item.bit_width - 1} | "
            f"{item.description[:200]}"
            for item in fields[:40]
        )
        answer = self._ask(
            "The following bit fields of register "
            f"{register} carry a name damaged by the PDF extraction. Using the "
            "description, give the mnemonic used by the reference manual, in "
            "upper case, without spaces. Answer "
            '{"CURRENT_NAME": "MNEMONIC"} for every entry.\n\n' + described
        )
        if not answer:
            return {}
        out: dict[str, str] = {}
        for item in fields:
            proposal = _clean(str(answer.get(item.name, "")))
            mnemonic = re.sub(r"[^A-Za-z0-9_]+", "_", proposal).strip("_").upper()
            if mnemonic and len(mnemonic) <= 32:
                out[item.name] = mnemonic
        return out


def _render(table: Table, limit: int = 30) -> str:
    """Render a table as a pipe separated text block for the prompts."""
    rows = [table.header] if table.header else []
    rows += table.rows()[:limit]
    return "\n".join(" | ".join(cell[:80] for cell in row) for row in rows)


_SUSPICIOUS_NAME_RE = re.compile(r"^(?:FIELD_\d+|RESERVED\w*|\w{25,})$")


def _consolidate(peripherals: Sequence[PeripheralRegisters], assistant: OllamaAssistant) -> None:
    """Ask the agent to repair the field names the parsing could not read."""
    for peripheral in peripherals:
        for register in peripheral.registers:
            damaged = [
                item
                for item in register.fields
                if _SUSPICIOUS_NAME_RE.match(item.name) and not item.name.startswith("RESERVED")
            ]
            if not damaged:
                continue
            proposals = assistant.field_names(register.name, damaged)
            taken = {item.name for item in register.fields}
            for item in damaged:
                proposal = proposals.get(item.name)
                if proposal and proposal not in taken:
                    taken.add(proposal)
                    item.name = proposal


# --------------------------------------------------------------------------
# instance separation
# --------------------------------------------------------------------------

_INSTANCE_SPLIT_RE = re.compile(r"^([A-Z][A-Z0-9]*?\d+)_(.+)$")


def _split_instance(name: str) -> tuple[str, str] | None:
    """Split ``GPT1_CR`` into ``("GPT1", "CR")``, None when not applicable."""
    match = _INSTANCE_SPLIT_RE.match(name.upper())
    if match:
        return match.group(1), match.group(2)
    return None


def _group_by_instance(
    registers: Iterable[Register],
    fallback: str,
    assistant: OllamaAssistant,
) -> list[PeripheralRegisters]:
    """Split a flat register list into one entry per peripheral instance."""
    items = list(registers)
    resolved: dict[str, tuple[str, str]] = {}
    unresolved: list[str] = []
    for register in items:
        split = _split_instance(register.name)
        if split:
            resolved[register.name] = split
        else:
            unresolved.append(register.name)

    if unresolved:
        resolved.update(assistant.split_instances(unresolved, fallback))

    groups: dict[str, PeripheralRegisters] = {}
    leftovers: list[Register] = []
    for register in items:
        split = resolved.get(register.name)
        if split is None:
            leftovers.append(register)
            continue
        instance, mnemonic = split
        groups.setdefault(instance, PeripheralRegisters(name=instance)).registers.append(
            _renamed(register, mnemonic)
        )

    _set_base_addresses(groups.values())
    for register in leftovers:
        instance = _closest_instance(groups.values(), register.address) or fallback
        groups.setdefault(instance, PeripheralRegisters(name=instance)).registers.append(
            _renamed(register, register.name)
        )

    _set_base_addresses(groups.values())
    for peripheral in groups.values():
        peripheral.registers.sort(key=lambda item: (item.offset or 0, item.name))
    return sorted(groups.values(), key=lambda item: (item.base_address or 0, item.name))


def _renamed(register: Register, mnemonic: str) -> Register:
    """Return a copy of a register carrying another name."""
    return Register(
        name=mnemonic,
        address=register.address,
        offset=register.offset,
        width=register.width,
        access=register.access,
        reset_value=register.reset_value,
        description=register.description,
        fields=register.fields,
    )


def _set_base_addresses(peripherals: Iterable[PeripheralRegisters]) -> None:
    """Set the base address of each instance and rebase its register offsets."""
    for peripheral in peripherals:
        addresses = [item.address for item in peripheral.registers if item.address is not None]
        peripheral.base_address = min(addresses) if addresses else None
        for item in peripheral.registers:
            if item.address is not None and peripheral.base_address is not None:
                item.offset = item.address - peripheral.base_address


def _closest_instance(
    peripherals: Iterable[PeripheralRegisters],
    address: int | None,
    window: int = 0x10000,
) -> str | None:
    """Return the instance whose address window contains ``address``."""
    if address is None:
        return None
    candidates = [
        (address - item.base_address, item.name)
        for item in peripherals
        if item.base_address is not None and 0 <= address - item.base_address < window
    ]
    return min(candidates)[1] if candidates else None


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------


def extract_peripherals(
    pdf: Path | str,
    *,
    peripheral: str | None = None,
    pages: Sequence[int] | None = None,
    model: str = DEFAULT_MODEL,
    host: str | None = None,
    assistant: OllamaAssistant | None = None,
    batch_size: int = BATCH_SIZE,
) -> ChapterRegisters:
    """Extract the registers and bit fields described by a chapter PDF.

    :param pdf: chapter document, as produced by
        :func:`pySvdGenerator.split_chapters`.
    :param peripheral: name of the peripheral block, deduced from the first
        page when omitted.
    :param pages: 1 based page numbers to read, the whole document by default.
    :param model: ollama model used by the consolidation agent.
    :param host: base URL of the ollama server.
    :param assistant: already built agent, created from ``model`` and ``host``
        when omitted.
    :param batch_size: number of pages read by a single tabula call.
    :return: the peripheral instances described by the chapter.
    :raises FileNotFoundError: if ``pdf`` does not exist.
    :raises OllamaUnavailableError: if the ollama server cannot be reached.
    """
    source = Path(pdf)
    if not source.is_file():
        raise FileNotFoundError(f"No such document: {source}")

    agent = assistant if assistant is not None else OllamaAssistant(model, host)

    reader = PdfReader(str(source))
    page_numbers = list(pages) if pages else list(range(1, len(reader.pages) + 1))
    texts = _page_texts(source, page_numbers)

    tables = _read_tables(source, page_numbers, batch_size)
    _locate(tables, texts, page_numbers)
    for table in tables:
        _split_header(table)

    for table in tables:
        if table.kind == UNKNOWN and table.frame.shape[1] >= 2:
            table.kind = agent.classify(table)

    tables = _merge(tables)

    registers: dict[str, Register] = {}
    for table in (item for item in tables if item.kind == MEMORY_MAP):
        for register in _parse_memory_map(table):
            registers.setdefault(register.name, register)

    for table in (item for item in tables if item.kind == FIELDS):
        fields = _parse_fields(table)
        if not fields:
            continue
        owners = _owners(table, texts, registers)
        if not owners:
            answer = agent.owner(table, texts.get(table.page, ""), list(registers))
            owners = [answer] if answer else []
        for owner in owners:
            registers[owner].fields = _deduplicate([*registers[owner].fields, *fields])

    name = peripheral or _peripheral_name(source, texts, page_numbers)
    grouped = _group_by_instance(registers.values(), name, agent)
    _consolidate(grouped, agent)
    return ChapterRegisters(source=str(source), peripherals=grouped)


def extract_registers(pdf: Path | str, **kwargs: Any) -> dict[str, Any]:
    """Extract a chapter PDF and return the peripheral register dictionary.

    Convenience wrapper over :func:`extract_peripherals` returning the plain
    dictionary consumed by :func:`pySvdGenerator.enrich_svd`.

    :param pdf: chapter document to read.
    :param kwargs: forwarded to :func:`extract_peripherals`.
    :return: ``{"source": ..., "peripherals": {name: {...}}}``.
    """
    return extract_peripherals(pdf, **kwargs).to_dict()


def _peripheral_name(source: Path, texts: dict[int, str], pages: Sequence[int]) -> str:
    """Guess the peripheral block name from the first page of the chapter."""
    first = texts.get(pages[0], "") if pages else ""
    match = _PAREN_NAME_RE.search(first)
    if match:
        return match.group(1).upper()
    return re.sub(r"[^A-Za-z0-9_]+", "_", source.stem).strip("_").upper()
