<!--
SPDX-FileType: DOCUMENTATION
SPDX-FileCopyrightText: 2026 H2Lab Development Team
SPDX-License-Identifier: Apache-2.0
-->
# Module documentation

One page per module of `pySvdGenerator`, in pipeline order.

| Module | Page | Role |
| --- | --- | --- |
| `pySvdGenerator.dts` | [dts.md](dts.md) | Device tree source preprocessor and parser |
| `pySvdGenerator.bindings` | [bindings.md](bindings.md) | Peripheral descriptions from the kernel bindings |
| `pySvdGenerator.svd` | [svd.md](svd.md) | CMSIS-SVD generation from the device tree |
| `pySvdGenerator.chapter_splitter` | [chapter_splitter.md](chapter_splitter.md) | Reference manual splitting |
| `pySvdGenerator.register_extractor` | [register_extractor.md](register_extractor.md) | Register and bit field extraction |
| `pySvdGenerator.svd_enricher` | [svd_enricher.md](svd_enricher.md) | Merge of both descriptions |
| `pySvdGenerator.cli` | [cli.md](cli.md) | `pysvdgen` command line |

## Data flow

```text
kernel sources ─┬─► dts ──► svd ───────────────────────────┐
                └─► bindings ─┘                            │
                                                           ▼
reference manual ──► chapter_splitter ──► register_extractor ──► svd_enricher ──► SVD
```

The device tree gives the memory map, the interrupts and the cores. The
reference manual gives the registers and their bit fields. The enricher keeps
only what both descriptions agree on and reports the rest.

## Requirements

* a Java runtime, used by `tabula-py`;
* a reachable `ollama` server holding `qwen2.5-coder:7b`, or `llama3.2` as a
  fallback, queried by the extraction stage unless `--no-llm` is given.
