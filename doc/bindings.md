<!--
SPDX-FileType: DOCUMENTATION
SPDX-FileCopyrightText: 2026 H2Lab Development Team
SPDX-License-Identifier: Apache-2.0
-->
# `pySvdGenerator.bindings`

Lookup of the device tree binding documentation shipped with the kernel, used
to give every SVD peripheral a readable description.

## Public API

| Symbol | Description |
| --- | --- |
| `BindingIndex.build(kernel_path, compatibles)` | Scan the bindings for the given `compatible` strings |
| `BindingIndex.describe(compatibles)` | Title of the first documented compatible |
| `BindingIndex.titles` | Mapping `compatible` to binding title |
| `BindingIndex.sources` | Mapping `compatible` to the binding file |

## Behaviour

* Files are read under `Documentation/devicetree/bindings`, both the YAML
  bindings and the legacy `.txt` ones.
* The scan is driven by the wanted `compatible` strings: a file is opened
  once, checked for any pending string, and the search stops as soon as every
  string has been resolved.
* The title of a YAML binding is its `title:` entry; for a `.txt` binding the
  first meaningful line is used.
* A missing bindings directory yields an empty index, the generation then
  falls back to the raw `compatible` strings.

## Example

```python
from pySvdGenerator.bindings import BindingIndex

index = BindingIndex.build("/src/linux", {"fsl,imx8mm-uart", "arm,gic-v3"})
index.describe(["fsl,imx8mm-uart"])       # 'NXP i.MX UART'
index.sources["fsl,imx8mm-uart"]          # PosixPath('.../fsl-imx-uart.yaml')
```
