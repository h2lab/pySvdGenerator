<!--
SPDX-FileType: DOCUMENTATION
SPDX-FileCopyrightText: 2026 H2Lab Development Team
SPDX-License-Identifier: Apache-2.0
-->
# `pySvdGenerator.cli`

The `pysvdgen` command, chaining the four stages of the generator. The entry
point is `pySvdGenerator.cli:run`, declared in `pyproject.toml`.

## Usage

```console
pysvdgen -k /src/linux -d imx8mm -p IMX8MDQLQRM.pdf -o imx8mm.svd
```

| Option | Description |
| --- | --- |
| `-k`, `--kernel` | root of the Linux kernel sources (required) |
| `-d`, `--dtsi` | DTSI name, without extension (required) |
| `-p`, `--pdf` | reference manual PDF (required) |
| `-o`, `--output` | SVD file to write (required) |
| `-w`, `--workspace` | working directory, kept on exit |
| `-c`, `--chapter` | only process the chapters whose name contains this text, repeatable |
| `--model` | ollama model used for extraction, default `qwen2.5-coder:7b`, falling back to `llama3.2` |
| `--ollama-host` | base URL of the ollama server, default `http://localhost:11434` |
| `--vendor` | vendor name written in the SVD |
| `-v`, `--verbose` | show the module logs and the tracebacks |

## Workspace

* With `-w`, the directory is created if needed and kept on exit. It holds
  the intermediate SVD, the `chapters/` documents and the `registers/` JSON
  cache; a second run reuses them and skips the work already done.
* Without `-w`, a temporary directory is created and removed on exit,
  whatever the outcome.

## Stages and reporting

The run prints one rule per stage, `1/4` to `4/4`, with a progress bar for
the splitting and the extraction. It ends with a table of the enriched
peripherals, then one panel per class of anomaly: device tree peripherals
left without registers, manual peripherals absent from the device tree, and
registers dropped because they fall outside the device tree address blocks.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | the SVD file has been written |
| 1 | a stage failed, the cause is printed in a red panel |
| 2 | no chapter matches the `--chapter` selection |

## Programmatic use

```python
from pySvdGenerator.cli import build_parser, run

code = run(["-k", "/src/linux", "-d", "imx8mm", "-p", "rm.pdf", "-o", "imx8mm.svd"])
build_parser().print_help()
```
