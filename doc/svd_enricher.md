<!--
SPDX-FileType: DOCUMENTATION
SPDX-FileCopyrightText: 2026 H2Lab Development Team
SPDX-License-Identifier: Apache-2.0
-->
# `pySvdGenerator.svd_enricher`

Merge of the device tree description, which knows the memory map, and of the
reference manual description, which knows the registers.

## Public API

| Symbol | Description |
| --- | --- |
| `enrich_svd(svd, registers, output=None)` | Inject the registers into an SVD file, return the report |
| `load_dictionaries(data)` | Normalise the accepted inputs into chapter dictionaries |
| `EnrichmentReport`, `PeripheralMatch` | Result of a run |

`registers` accepts a dictionary produced by `extract_registers`, a sequence
of such dictionaries, the path of a JSON file, or a directory holding JSON
files. `output` defaults to updating the input file in place.

## Pairing

SVD peripherals and manual peripherals are paired in one global pass, from
the strongest criterion to the weakest, each source being used once:

1. identical base address;
2. identical instance name, `GPT1` with `GPT1`;
3. manual base address contained in an address block of 64 KiB or less.

The global ordering matters: a bus such as `AIPS1` covers the address of many
peripherals and would otherwise capture the first one it contains.

## Validation

A register is written only when its absolute address falls inside one of the
address blocks derived from the device tree `reg` property. The offset
written in the SVD is recomputed from the `baseAddress` of the device tree,
not from the base address seen in the manual.

Everything else is reported rather than silently dropped:

| Report entry | Meaning |
| --- | --- |
| `matches[].registers` / `.fields` | What has been injected |
| `matches[].rejected` | Registers outside the device tree address blocks |
| `unmatched_svd` | Device tree peripherals left without registers |
| `unused_sources` | Manual peripherals absent from the device tree |

A peripheral already holding a `<registers>` section and not covered by the
given dictionaries is left untouched and is not reported as unmatched, which
is what makes the chapter by chapter completion of a file possible.

## Generated XML

For every paired peripheral a `<registers>` section is appended, after the
address blocks and the interrupts, holding `<register>` elements with their
`name`, `description`, `addressOffset`, `size`, `access`, `resetValue` and
their `<fields>` with `bitOffset`, `bitWidth`, `description` and `access`.
An existing `<registers>` section is replaced.

## Example

```python
from pySvdGenerator import enrich_svd, extract_registers

data = extract_registers("workspace/Chapter_12_Timers.pdf")
report = enrich_svd("build/imx8mm.svd", data, "imx8mm.svd")

print(report.registers, report.fields)
for match in report.matches:
    print(match.peripheral, match.registers, len(match.rejected))
print(report.unmatched_svd, report.unused_sources)
```
