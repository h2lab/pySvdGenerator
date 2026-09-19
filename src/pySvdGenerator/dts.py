# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Self contained device tree source (DTS/DTSI) reader.

The module implements the small subset of the C preprocessor used by the
Linux device tree sources (``#include``, object like and function like
``#define``) and a DTS parser producing a navigable node tree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Sequence

__all__ = [
    "Cells",
    "DeviceTree",
    "DtsError",
    "Node",
    "Phandle",
    "Preprocessor",
    "Property",
    "parse_dts",
    "read_dts",
]


class DtsError(Exception):
    """Raised when a device tree source cannot be read or parsed."""


# --------------------------------------------------------------------------
# preprocessor
# --------------------------------------------------------------------------

_COMMENT_RE = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)
_INCLUDE_RE = re.compile(r"^\s*#\s*include\s+(?:\"([^\"]+)\"|<([^>]+)>)")
_DTS_INCLUDE_RE = re.compile(r"^\s*/include/\s+\"([^\"]+)\"\s*;?\s*$")
_DEFINE_RE = re.compile(r"^\s*#\s*define\s+(\w+)(\([^)]*\))?[ \t]*(.*)$")
_UNDEF_RE = re.compile(r"^\s*#\s*undef\s+(\w+)")
_DIRECTIVE_RE = re.compile(
    r"^\s*#\s*(include|define|undef|if|ifdef|ifndef|else|elif|endif|error|warning|pragma|line)\b"
)


@dataclass(frozen=True)
class Macro:
    """A preprocessor macro definition.

    :param name: macro name.
    :param body: replacement text.
    :param params: parameter names, None for an object like macro.
    """

    name: str
    body: str
    params: tuple[str, ...] | None = None


def _strip_comments(text: str) -> str:
    """Remove the C comments, keeping the line numbering intact."""
    return _COMMENT_RE.sub(lambda m: "\n" * m.group(0).count("\n"), text)


class Preprocessor:
    """Expand ``#include`` directives and collect ``#define`` macros.

    A file is included only once, which mirrors the include guards of the
    ``dt-bindings`` headers and keeps the DTSI tree finite.

    :param include_dirs: directories searched for angle bracket includes.
    """

    def __init__(self, include_dirs: Sequence[Path | str]) -> None:
        self.include_dirs = [Path(directory) for directory in include_dirs]
        self.macros: dict[str, Macro] = {}
        self._included: set[Path] = set()

    def process(self, path: Path | str) -> str:
        """Return the fully included content of ``path``."""
        return "\n".join(self._process_file(Path(path)))

    def _resolve(self, name: str, current: Path) -> Path | None:
        """Locate an included file, relative to ``current`` then to the dirs."""
        candidates = [current.parent / name]
        candidates += [directory / name for directory in self.include_dirs]
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()
        return None

    def _process_file(self, path: Path) -> list[str]:
        """Return the preprocessed lines of a file, includes expanded."""
        resolved = path.resolve()
        if resolved in self._included:
            return []
        if not resolved.is_file():
            raise DtsError(f"No such device tree source: {path}")
        self._included.add(resolved)

        text = _strip_comments(resolved.read_text(encoding="utf-8", errors="replace"))
        text = text.replace("\\\n", " ")

        out: list[str] = []
        for line in text.splitlines():
            include = _INCLUDE_RE.match(line) or _DTS_INCLUDE_RE.match(line)
            if include:
                name = next(group for group in include.groups() if group)
                target = self._resolve(name, resolved)
                if target is not None:
                    out.extend(self._process_file(target))
                continue

            define = _DEFINE_RE.match(line)
            if define:
                name, params, body = define.groups()
                parsed = (
                    tuple(part.strip() for part in params[1:-1].split(",") if part.strip())
                    if params
                    else None
                )
                self.macros[name] = Macro(name=name, body=body.strip(), params=parsed)
                continue

            undef = _UNDEF_RE.match(line)
            if undef:
                self.macros.pop(undef.group(1), None)
                continue

            if _DIRECTIVE_RE.match(line):
                continue

            out.append(line)
        return out


# --------------------------------------------------------------------------
# macro expansion and integer expression evaluation
# --------------------------------------------------------------------------

_IDENT_RE = re.compile(r"[A-Za-z_]\w*")
_NUMBER_RE = re.compile(r"0[xX][0-9a-fA-F]+|\d+")


def _split_args(text: str) -> list[str]:
    """Split the arguments of a function like macro invocation."""
    args: list[str] = []
    depth = 0
    current = ""
    for char in text:
        if char == "," and depth == 0:
            args.append(current.strip())
            current = ""
            continue
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        current += char
    if current.strip() or args:
        args.append(current.strip())
    return args


def _split_parts(text: str) -> list[str]:
    """Split a property value on the commas separating its parts."""
    parts: list[str] = []
    depth = 0
    in_string = False
    current = ""
    index = 0
    while index < len(text):
        char = text[index]
        index += 1
        if in_string:
            current += char
            if char == "\\" and index < len(text):
                current += text[index]
                index += 1
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "<[(":
            depth += 1
        elif char in ">])":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(current.strip())
            current = ""
            continue
        current += char
    if current.strip():
        parts.append(current.strip())
    return parts


class MacroExpander:
    """Textual expansion of preprocessor macros.

    :param macros: macros collected by a :class:`Preprocessor`.
    """

    def __init__(self, macros: dict[str, Macro]) -> None:
        self.macros = macros

    def expand(self, text: str, depth: int = 0) -> str:
        """Recursively expand every known macro found in ``text``."""
        if depth > 32:
            return text

        out = ""
        pos = 0
        changed = False
        while pos < len(text):
            match = _IDENT_RE.search(text, pos)
            if match is None:
                out += text[pos:]
                break
            out += text[pos : match.start()]
            name = match.group(0)
            macro = self.macros.get(name)
            pos = match.end()

            if macro is None:
                out += name
                continue

            if macro.params is None:
                out += f"({macro.body})" if macro.body else "0"
                changed = True
                continue

            rest = text[pos:]
            stripped = rest.lstrip()
            if not stripped.startswith("("):
                out += name
                continue
            offset = len(rest) - len(stripped)
            end = _matching_paren(stripped)
            if end < 0:
                out += name
                continue
            args = _split_args(stripped[1:end])
            body = macro.body
            for param, value in zip(macro.params, args):
                body = re.sub(rf"\b{re.escape(param)}\b", f"({value})", body)
            out += f"({body})"
            pos += offset + end + 1
            changed = True

        return self.expand(out, depth + 1) if changed else out


def _matching_paren(text: str) -> int:
    """Return the index of the parenthesis closing the first one, -1 if none."""
    depth = 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    return -1


_BINARY_LEVELS: tuple[tuple[str, ...], ...] = (
    ("||",),
    ("&&",),
    ("|",),
    ("^",),
    ("&",),
    ("==", "!="),
    ("<=", ">=", "<", ">"),
    ("<<", ">>"),
    ("+", "-"),
    ("*", "/", "%"),
)

_OPERATORS = sorted(
    {operator for level in _BINARY_LEVELS for operator in level} | {"~", "!", "(", ")"},
    key=len,
    reverse=True,
)


def _tokenize_expression(text: str) -> list[str]:
    """Split a fully expanded expression into numbers and operators.

    :raises DtsError: when a symbol remains unexpanded or is unsupported.
    """
    tokens: list[str] = []
    pos = 0
    while pos < len(text):
        if text[pos].isspace():
            pos += 1
            continue
        number = _NUMBER_RE.match(text, pos)
        if number:
            tokens.append(number.group(0))
            pos = number.end()
            while pos < len(text) and text[pos] in "uUlL":
                pos += 1
            continue
        identifier = _IDENT_RE.match(text, pos)
        if identifier:
            raise DtsError(f"Unresolved symbol {identifier.group(0)!r}")
        for operator in _OPERATORS:
            if text.startswith(operator, pos):
                tokens.append(operator)
                pos += len(operator)
                break
        else:
            raise DtsError(f"Unexpected character {text[pos]!r} in expression")
    return tokens


class _ExpressionParser:
    """Precedence climbing parser for C integer expressions."""

    def __init__(self, tokens: list[str]) -> None:
        self.tokens = tokens
        self.pos = 0

    def parse(self) -> int:
        """Evaluate the whole token list."""
        value = self._binary(0)
        if self.pos != len(self.tokens):
            raise DtsError("Trailing tokens in expression")
        return value

    def _peek(self) -> str | None:
        """Return the current token without consuming it."""
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _binary(self, level: int) -> int:
        """Evaluate the binary operators of the given precedence level."""
        if level >= len(_BINARY_LEVELS):
            return self._unary()
        value = self._binary(level + 1)
        while (operator := self._peek()) in _BINARY_LEVELS[level]:
            self.pos += 1
            right = self._binary(level + 1)
            value = _apply(str(operator), value, right)
        return value

    def _unary(self) -> int:
        """Evaluate a unary operator, a parenthesis or a literal."""
        token = self._peek()
        if token in ("-", "+", "~", "!"):
            self.pos += 1
            value = self._unary()
            if token == "-":
                return -value
            if token == "~":
                return ~value
            if token == "!":
                return int(not value)
            return value
        if token == "(":
            self.pos += 1
            value = self._binary(0)
            if self._peek() != ")":
                raise DtsError("Unbalanced parenthesis in expression")
            self.pos += 1
            return value
        if token is None:
            raise DtsError("Truncated expression")
        self.pos += 1
        return int(token, 0)


def _apply(operator: str, left: int, right: int) -> int:
    """Apply a binary operator to two integers."""
    match operator:
        case "+":
            return left + right
        case "-":
            return left - right
        case "*":
            return left * right
        case "/":
            return left // right if right else 0
        case "%":
            return left % right if right else 0
        case "<<":
            return left << right
        case ">>":
            return left >> right
        case "&":
            return left & right
        case "|":
            return left | right
        case "^":
            return left ^ right
        case "&&":
            return int(bool(left) and bool(right))
        case "||":
            return int(bool(left) or bool(right))
        case "==":
            return int(left == right)
        case "!=":
            return int(left != right)
        case "<":
            return int(left < right)
        case ">":
            return int(left > right)
        case "<=":
            return int(left <= right)
        case ">=":
            return int(left >= right)
    raise DtsError(f"Unsupported operator {operator!r}")


def evaluate(text: str, expander: MacroExpander) -> int:
    """Expand and evaluate an integer expression coming from a DTS cell.

    :param text: raw cell text, macros included.
    :param expander: expander holding the macros of the parsed sources.
    :return: the integer value of the expression.
    :raises DtsError: when the expression cannot be resolved.
    """
    return _ExpressionParser(_tokenize_expression(expander.expand(text))).parse()


# --------------------------------------------------------------------------
# device tree model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Phandle:
    """A reference to another node, by label or by path."""

    target: str


@dataclass
class Cells:
    """A ``<...>`` property value.

    :param values: integers and node references, in declaration order.
    :param bits: cell width, as set by a ``/bits/`` prefix.
    """

    values: list[int | Phandle]
    bits: int = 32


PropertyValue = Cells | str | bytes


@dataclass
class Property:
    """A device tree property.

    :param name: property name.
    :param parts: comma separated values, cells, strings or byte strings.
    :param raw: original text of the value.
    """

    name: str
    parts: list[PropertyValue] = field(default_factory=list)
    raw: str = ""

    def strings(self) -> list[str]:
        """Return the string parts of the property."""
        return [part for part in self.parts if isinstance(part, str)]

    def cells(self) -> list[int | Phandle]:
        """Return every cell of the property, parts concatenated."""
        out: list[int | Phandle] = []
        for part in self.parts:
            if isinstance(part, Cells):
                out.extend(part.values)
        return out

    def ints(self) -> list[int]:
        """Return the integer cells of the property, references excluded."""
        return [cell for cell in self.cells() if isinstance(cell, int)]

    def first_int(self, default: int | None = None) -> int | None:
        """Return the first integer cell, or ``default``."""
        values = self.ints()
        return values[0] if values else default

    def phandle(self) -> Phandle | None:
        """Return the first node reference of the property."""
        for cell in self.cells():
            if isinstance(cell, Phandle):
                return cell
        return None


@dataclass
class Node:
    """A device tree node.

    :param name: node name, unit address included.
    :param parent: parent node, None for the root.
    :param labels: labels attached to the node.
    :param props: properties, indexed by name.
    :param children: children, indexed by name.
    """

    name: str
    parent: "Node | None" = None
    labels: list[str] = field(default_factory=list)
    props: dict[str, Property] = field(default_factory=dict)
    children: dict[str, "Node"] = field(default_factory=dict)

    @property
    def path(self) -> str:
        """Return the absolute path of the node."""
        if self.parent is None:
            return "/"
        prefix = self.parent.path
        return f"{prefix}{self.name}" if prefix == "/" else f"{prefix}/{self.name}"

    @property
    def basename(self) -> str:
        """Return the node name without its unit address."""
        return self.name.split("@", 1)[0]

    @property
    def unit_address(self) -> str | None:
        """Return the unit address of the node, if any."""
        return self.name.split("@", 1)[1] if "@" in self.name else None

    def child(self, name: str) -> "Node | None":
        """Return a direct child by name, with or without unit address."""
        if name in self.children:
            return self.children[name]
        return next(
            (child for child in self.children.values() if child.basename == name),
            None,
        )

    def compatible(self) -> list[str]:
        """Return the ``compatible`` strings of the node."""
        prop = self.props.get("compatible")
        return prop.strings() if prop else []

    def status(self) -> str:
        """Return the node status, ``okay`` when unspecified."""
        prop = self.props.get("status")
        values = prop.strings() if prop else []
        return values[0] if values else "okay"

    def walk(self) -> Iterator["Node"]:
        """Iterate over the node and all its descendants."""
        yield self
        for child in self.children.values():
            yield from child.walk()

    def ancestors(self) -> Iterator["Node"]:
        """Iterate over the parents of the node, closest first."""
        node = self.parent
        while node is not None:
            yield node
            node = node.parent


@dataclass
class DeviceTree:
    """A parsed device tree.

    :param root: root node of the tree.
    :param labels: every label of the sources, resolved to its node.
    """

    root: Node
    labels: dict[str, Node] = field(default_factory=dict)

    def find(self, path: str) -> Node | None:
        """Return the node at ``path``, or None."""
        node = self.root
        for part in path.strip("/").split("/"):
            if not part:
                continue
            child = node.child(part)
            if child is None:
                return None
            node = child
        return node

    def resolve(self, reference: Phandle | None) -> Node | None:
        """Return the node targeted by ``reference``."""
        if reference is None:
            return None
        if reference.target.startswith("/"):
            return self.find(reference.target)
        return self.labels.get(reference.target)


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------

_LABEL_RE = re.compile(r"([A-Za-z_]\w*)\s*:\s*")
_NAME_RE = re.compile(r"[^\s{};=]+")
_BITS_RE = re.compile(r"/bits/\s*(\d+)\s*")


class _Parser:
    """Recursive descent parser of a preprocessed device tree source."""

    def __init__(self, text: str, expander: MacroExpander) -> None:
        self.text = text
        self.pos = 0
        self.expander = expander
        self.root = Node(name="")
        self.labels: dict[str, Node] = {}

    # -- low level helpers -------------------------------------------------

    def _skip_ws(self) -> None:
        """Move past the white space at the current position."""
        while self.pos < len(self.text) and self.text[self.pos].isspace():
            self.pos += 1

    def _startswith(self, token: str) -> bool:
        """Tell whether the source continues with ``token``."""
        return self.text.startswith(token, self.pos)

    def _expect(self, token: str) -> None:
        """Consume ``token``, raising when it is not there."""
        self._skip_ws()
        if not self._startswith(token):
            raise DtsError(f"Expected {token!r} at offset {self.pos}")
        self.pos += len(token)

    # -- structure ---------------------------------------------------------

    def parse(self) -> DeviceTree:
        """Parse the whole source and return the resulting tree."""
        while True:
            self._skip_ws()
            if self.pos >= len(self.text):
                break
            if self._startswith("/dts-v1/") or self._startswith("/plugin/"):
                self._skip_statement()
                continue
            if self._startswith("/memreserve/") or self._startswith("/omit-if-no-ref/"):
                self._skip_statement()
                continue
            if self._startswith("/") and self._is_root_node():
                self.pos += 1
                self._expect("{")
                self._parse_body(self.root)
                continue
            labels = self._parse_labels()
            if self._startswith("&"):
                target = self._parse_reference_target()
                node = self.labels.get(target) if not target.startswith("/") else None
                if node is None and target.startswith("/"):
                    node = DeviceTree(self.root, self.labels).find(target)
                if node is None:
                    self._skip_block()
                    continue
                self._register_labels(node, labels)
                self._expect("{")
                self._parse_body(node)
                continue
            raise DtsError(
                f"Unexpected content at offset {self.pos}: {self.text[self.pos : self.pos + 40]!r}"
            )
        return DeviceTree(root=self.root, labels=self.labels)

    def _is_root_node(self) -> bool:
        """Tell whether the source continues with the ``/ { ... }`` node."""
        return re.compile(r"/\s*\{").match(self.text, self.pos) is not None

    def _skip_statement(self) -> None:
        """Move past the end of the current statement."""
        end = self.text.find(";", self.pos)
        self.pos = len(self.text) if end < 0 else end + 1

    def _skip_semicolon(self) -> None:
        """Consume the semicolon closing a node, when present."""
        self._skip_ws()
        if self._startswith(";"):
            self.pos += 1

    def _skip_block(self) -> None:
        """Move past a whole ``{ ... };`` block."""
        depth = 0
        while self.pos < len(self.text):
            char = self.text[self.pos]
            self.pos += 1
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    self._skip_semicolon()
                    return

    def _parse_labels(self) -> list[str]:
        """Read the ``label:`` prefixes of the next node or property."""
        labels: list[str] = []
        while True:
            self._skip_ws()
            match = _LABEL_RE.match(self.text, self.pos)
            if not match:
                return labels
            labels.append(match.group(1))
            self.pos = match.end()

    def _parse_reference_target(self) -> str:
        """Read a ``&label`` or ``&{/path}`` reference and return its target."""
        self.pos += 1  # '&'
        if self._startswith("{"):
            end = self.text.find("}", self.pos)
            target = self.text[self.pos + 1 : end]
            self.pos = end + 1
            return target
        match = re.compile(r"[A-Za-z_]\w*").match(self.text, self.pos)
        if match is None:
            raise DtsError(f"Invalid reference at offset {self.pos}")
        self.pos = match.end()
        return match.group(0)

    def _register_labels(self, node: Node, labels: Sequence[str]) -> None:
        """Attach labels to a node and record them in the label table."""
        for label in labels:
            if label not in node.labels:
                node.labels.append(label)
            self.labels[label] = node

    def _parse_body(self, node: Node) -> None:
        """Parse the body of a node, merging it into ``node``."""
        while True:
            self._skip_ws()
            if self.pos >= len(self.text):
                raise DtsError("Unterminated node body")
            if self._startswith("}"):
                self.pos += 1
                self._skip_semicolon()
                return
            if self._startswith("/delete-node/"):
                self.pos += len("/delete-node/")
                self._skip_ws()
                name = self._read_name()
                victim = node.child(name)
                if victim is not None:
                    node.children.pop(victim.name, None)
                self._skip_statement()
                continue
            if self._startswith("/delete-property/"):
                self.pos += len("/delete-property/")
                self._skip_ws()
                node.props.pop(self._read_name(), None)
                self._skip_statement()
                continue

            labels = self._parse_labels()
            if self._startswith("&"):  # reference inside a body, ignored
                self._skip_block()
                continue
            name = self._read_name()
            self._skip_ws()
            if self._startswith("{"):
                self.pos += 1
                child = node.child(name) if "@" not in name else node.children.get(name)
                if child is None or child.name != name:
                    child = node.children.get(name)
                if child is None:
                    child = Node(name=name, parent=node)
                    node.children[name] = child
                self._register_labels(child, labels)
                self._parse_body(child)
                continue
            if self._startswith("="):
                self.pos += 1
                node.props[name] = self._parse_value(name)
                continue
            self._expect(";")
            node.props[name] = Property(name=name, parts=[], raw="")

    def _read_name(self) -> str:
        """Read a node or property name."""
        match = _NAME_RE.match(self.text, self.pos)
        if match is None:
            raise DtsError(f"Invalid name at offset {self.pos}")
        self.pos = match.end()
        return match.group(0)

    # -- values ------------------------------------------------------------

    def _read_raw_value(self) -> str:
        """Read the raw text of a property value, up to its semicolon."""
        start = self.pos
        depth = 0
        in_string = False
        while self.pos < len(self.text):
            char = self.text[self.pos]
            if in_string:
                if char == "\\":
                    self.pos += 2
                    continue
                if char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char in "<[(":
                depth += 1
            elif char in ">])":
                depth -= 1
            elif char == ";" and depth == 0:
                raw = self.text[start : self.pos]
                self.pos += 1
                return raw
            self.pos += 1
        raise DtsError("Unterminated property value")

    def _parse_value(self, name: str) -> Property:
        """Read and parse the value of the property called ``name``."""
        raw = self._read_raw_value()
        parts: list[PropertyValue] = []
        for chunk in _split_parts(raw):
            parsed = self._parse_part(chunk)
            if parsed is not None:
                parts.append(parsed)
        return Property(name=name, parts=parts, raw=raw.strip())

    def _parse_part(self, chunk: str) -> PropertyValue | None:
        """Parse one comma separated part of a property value."""
        chunk = chunk.strip()
        if not chunk:
            return None
        if chunk.startswith('"'):
            return chunk[1:-1].encode().decode("unicode_escape")
        if chunk.startswith("["):
            digits = re.sub(r"[^0-9a-fA-F]", "", chunk[1:-1])
            return bytes.fromhex(digits if len(digits) % 2 == 0 else digits[:-1])
        if chunk.startswith("&"):
            return Cells(values=[Phandle(chunk[1:].strip().strip("{}"))])
        bits = 32
        match = _BITS_RE.match(chunk)
        if match:
            bits = int(match.group(1))
            chunk = chunk[match.end() :].strip()
        if chunk.startswith("<"):
            return Cells(values=self._parse_cells(chunk[1:-1]), bits=bits)
        return None

    def _parse_cells(self, body: str) -> list[int | Phandle]:
        """Parse the content of a ``<...>`` value into cells and references.

        Parsing stops on the first cell that cannot be evaluated, which keeps
        the properties built on unsupported macros usable up to that point.
        """
        values: list[int | Phandle] = []
        pos = 0
        while pos < len(body):
            char = body[pos]
            if char.isspace():
                pos += 1
                continue
            if char == "&":
                if body.startswith("&{", pos):
                    end = body.find("}", pos)
                    values.append(Phandle(body[pos + 2 : end]))
                    pos = end + 1
                    continue
                match = re.compile(r"&([A-Za-z_]\w*)").match(body, pos)
                if match is None:
                    raise DtsError("Invalid reference in cell list")
                values.append(Phandle(match.group(1)))
                pos = match.end()
                continue
            if char == "(":
                end = _matching_paren(body[pos:])
                if end < 0:
                    raise DtsError("Unbalanced parenthesis in cell list")
                token = body[pos : pos + end + 1]
                pos += end + 1
            else:
                match = re.compile(r"[^\s&]+").match(body, pos)
                assert match is not None
                token = match.group(0)
                pos = match.end()
            try:
                values.append(evaluate(token, self.expander))
            except DtsError:
                return values
        return values


def parse_dts(text: str, macros: dict[str, Macro] | None = None) -> DeviceTree:
    """Parse an already preprocessed device tree source.

    :param text: source with every include expanded.
    :param macros: macros used to evaluate the cell expressions.
    :return: the parsed tree.
    :raises DtsError: on a syntax error.
    """
    return _Parser(text, MacroExpander(macros or {})).parse()


def read_dts(path: Path | str, include_dirs: Sequence[Path | str] = ()) -> DeviceTree:
    """Preprocess and parse the device tree source at ``path``.

    :param path: DTS or DTSI file to read.
    :param include_dirs: directories searched for angle bracket includes,
        typically ``<kernel>/include``.
    :return: the parsed tree.
    :raises DtsError: when a file is missing or the source is invalid.
    """
    preprocessor = Preprocessor(include_dirs)
    text = preprocessor.process(path)
    return parse_dts(text, preprocessor.macros)


# --------------------------------------------------------------------------
# address and interrupt helpers
# --------------------------------------------------------------------------


def _cell_count(node: Node | None, name: str, default: int) -> int:
    """Return an integer property of a node, or ``default``."""
    if node is None:
        return default
    prop = node.props.get(name)
    value = prop.first_int() if prop else None
    return default if value is None else value


def address_cells(node: Node) -> int:
    """Return ``#address-cells`` applying to the children of ``node``."""
    return _cell_count(node, "#address-cells", 2)


def size_cells(node: Node) -> int:
    """Return ``#size-cells`` applying to the children of ``node``."""
    return _cell_count(node, "#size-cells", 1)


def _pack(cells: Sequence[int]) -> int:
    """Concatenate 32 bit cells into a single integer, most significant first."""
    value = 0
    for cell in cells:
        value = (value << 32) | (cell & 0xFFFFFFFF)
    return value


def reg_entries(node: Node) -> list[tuple[int, int]]:
    """Return the ``(address, size)`` entries of ``reg``, parent relative."""
    prop = node.props.get("reg")
    if prop is None or node.parent is None:
        return []
    naddr = address_cells(node.parent)
    nsize = size_cells(node.parent)
    cells = prop.ints()
    stride = naddr + nsize
    if stride == 0:
        return []
    entries: list[tuple[int, int]] = []
    for start in range(0, len(cells) - stride + 1, stride):
        address = _pack(cells[start : start + naddr])
        length = _pack(cells[start + naddr : start + stride]) if nsize else 0
        entries.append((address, length))
    return entries


def _ranges(bus: Node) -> list[tuple[int, int, int]] | None:
    """Return ``(child, parent, size)`` triplets of the ``ranges`` property."""
    prop = bus.props.get("ranges")
    if prop is None:
        return None
    cells = prop.ints()
    if not cells:
        return []  # identity mapping
    naddr = address_cells(bus)
    nsize = size_cells(bus)
    nparent = address_cells(bus.parent) if bus.parent else naddr
    stride = naddr + nparent + nsize
    if stride == 0:
        return []
    out: list[tuple[int, int, int]] = []
    for start in range(0, len(cells) - stride + 1, stride):
        child = _pack(cells[start : start + naddr])
        parent = _pack(cells[start + naddr : start + naddr + nparent])
        length = _pack(cells[start + naddr + nparent : start + stride])
        out.append((child, parent, length))
    return out


def translate(node: Node, address: int) -> int | None:
    """Translate a node relative address into a CPU visible address."""
    current = node.parent
    value = address
    while current is not None and current.parent is not None:
        mapping = _ranges(current)
        if mapping is None:
            return None  # not a memory mapped bus
        if mapping:
            for child, parent, length in mapping:
                if child <= value < child + length:
                    value = value - child + parent
                    break
            else:
                return None
        current = current.parent
    return value


def interrupt_parent(tree: DeviceTree, node: Node) -> Node | None:
    """Return the interrupt controller handling the interrupts of ``node``."""
    for candidate in (node, *node.ancestors()):
        prop = candidate.props.get("interrupt-parent")
        if prop is not None:
            parent = tree.resolve(prop.phandle())
            if parent is not None:
                return parent
    return None
