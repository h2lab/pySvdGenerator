<!--
SPDX-FileType: DOCUMENTATION
SPDX-FileCopyrightText: 2026 H2Lab Development Team
SPDX-License-Identifier: Apache-2.0
-->
# `pySvdGenerator.svd`

Generation of a CMSIS-SVD description from the Linux device tree of a SoC.

## Public API

| Symbol | Description |
| --- | --- |
| `generate_svd(kernel_path, soc, output, *, use_bindings=True, vendor=None)` | Write the SVD file, return its path |
| `build_device(kernel_path, soc, *, use_bindings=True, vendor=None)` | Build the `Device` model without writing |
| `device_to_xml(device)` | Render a `Device` as an XML tree |
| `find_dts_file(kernel_path, name)` | Locate a DTSI inside the kernel sources |
| `Device`, `Peripheral`, `Interrupt` | Data model |

## What is extracted

| SVD element | Device tree source |
| --- | --- |
| `<name>`, `<description>` | DTSI file name, root `model` and `compatible` |
| `<vendor>` | vendor part of the root `compatible`, or the `vendor` argument |
| `<cpu><name>` | `compatible` of the first `/cpus/cpu` node, mapped to the CMSIS names (`CA53`, `CM4`, ...) |
| `<peripheral>` | every node holding both `compatible` and `reg` |
| `<baseAddress>` | first `reg` entry, translated through the `ranges` of every parent bus |
| `<addressBlock>` | one per `reg` entry, offsets relative to the base address |
| `<interrupt>` | `interrupts` property, GIC numbers translated (`+32` for shared, `+16` for private), named after `interrupt-names` |
| `<groupName>` | peripheral name without its instance index |

Nodes under `/cpus`, `/memory`, `/chosen`, `/aliases`, `/reserved-memory` and
`/thermal-zones` are skipped, as are the nodes whose address cannot be
translated to a CPU address.

Interrupt names are made unique across the device and duplicated interrupt
numbers are dropped, both being rejected by the SVD schema.

## Registers

A device tree carries no register level description, so the generated
peripherals hold address blocks and interrupts but no `<registers>` section.
That section is added afterwards by
[`svd_enricher`](svd_enricher.md), from the reference manual.

## Example

```python
from pySvdGenerator import build_device, generate_svd

path = generate_svd("/src/linux", "imx8mm", "build/imx8mm.svd", vendor="NXP")

device = build_device("/src/linux", "imx8mm")
print(device.cpu_name, device.cpu_count, len(device.peripherals))
```
