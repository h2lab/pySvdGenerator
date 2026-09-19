<!--
SPDX-FileType: DOCUMENTATION
SPDX-FileCopyrightText: 2026 H2Lab Development Team
SPDX-License-Identifier: Apache-2.0
-->
# `pySvdGenerator.dts`

Self contained reader of Linux device tree sources. The module implements the
subset of the C preprocessor used by the kernel DTS files and a DTS parser, so
neither `dtc` nor any external tool is needed.

## Public API

| Symbol | Description |
| --- | --- |
| `read_dts(path, include_dirs=())` | Preprocess and parse a DTS or DTSI file |
| `parse_dts(text, macros=None)` | Parse an already preprocessed source |
| `Preprocessor(include_dirs)` | Expand `#include`, collect `#define` |
| `MacroExpander(macros)` | Textual macro expansion |
| `evaluate(text, expander)` | Evaluate an integer cell expression |
| `DeviceTree`, `Node`, `Property`, `Cells`, `Phandle` | Parsed tree model |
| `DtsError` | Raised on a missing file or a syntax error |

Address and interrupt helpers:

| Helper | Description |
| --- | --- |
| `address_cells(node)` / `size_cells(node)` | `#address-cells` / `#size-cells` applying to the children |
| `reg_entries(node)` | `(address, size)` pairs of the `reg` property, parent relative |
| `translate(node, address)` | Translate a node address into a CPU address through the `ranges` of every bus |
| `interrupt_parent(tree, node)` | Interrupt controller handling the node |

## Preprocessing

* `#include "file"`, `#include <dt-bindings/...>` and the legacy
  `/include/ "file"` form are expanded; angle bracket includes are searched in
  the directories given to the constructor, typically `<kernel>/include`.
* A file is included once only, which matches the include guards of the
  `dt-bindings` headers and keeps the DTSI tree finite.
* Object like and function like `#define` are collected; `#undef` removes
  them. Conditional directives are ignored, guards being harmless once files
  are included once.
* Property names starting with `#`, such as `#address-cells`, are *not*
  treated as directives.

## Expression evaluation

Cell values go through macro expansion then through a precedence climbing
parser supporting `+ - * / % << >> & | ^ ~ !`, the comparison operators,
`&&`, `||` and parentheses. Integer suffixes (`u`, `UL`) are accepted.

Parsing of a `<...>` value stops at the first cell that cannot be evaluated,
so properties built on unsupported constructs, pin control macros for
instance, stay usable up to that point instead of aborting the whole parse.

## Parsing

Supported constructs: `/dts-v1/`, `/plugin/`, `/memreserve/`, the root node,
node and property labels, `&label { ... }` and `&{/path} { ... }` overlays,
`/delete-node/`, `/delete-property/`, string, byte string (`[...]`) and cell
(`<...>`) values, `/bits/` prefixes and phandle references. Nodes declared
several times are merged, later definitions winning.

## Example

```python
from pySvdGenerator.dts import read_dts, reg_entries, translate

tree = read_dts(
    "/src/linux/arch/arm64/boot/dts/freescale/imx8mm.dtsi",
    ["/src/linux/include"],
)
uart = tree.labels["uart1"]
address, size = reg_entries(uart)[0]
print(hex(translate(uart, address)), hex(size))
print(uart.compatible(), uart.status())
```
