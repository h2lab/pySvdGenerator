<!--
SPDX-FileType: DOCUMENTATION
SPDX-FileCopyrightText: 2026 H2Lab Development Team
SPDX-License-Identifier: Apache-2.0
-->
# pySvdGenerator

Python module for generating SVD files.

The generator combines the two descriptions of a SoC that are publicly
available: the Linux device tree (memory map, interrupts, cores) and the
reference manual (registers and bit fields).

## Pipeline

1. **Device tree analysis** — `pySvdGenerator.dts` preprocesses and parses the
   DTSI (includes, `dt-bindings` macros, phandles, address translation);
   `pySvdGenerator.bindings` reads the peripheral descriptions from
   `Documentation/devicetree/bindings`.
2. **SVD generation** — `generate_svd()` writes a CMSIS-SVD file holding the
   CPU, the peripherals, their address blocks and their interrupts.
3. **Manual splitting** — `split_chapters()` cuts the reference manual PDF into
   one document per chapter inside a workspace.
4. **Register extraction** — `extract_registers()` reads the register memory
   maps and the bit field tables with `tabula-py`, merges the tables continued
   over several pages, separates the peripheral instances (`GPT1`, `GPT2`, ...)
   and returns a dictionary. A local `ollama` agent consolidates the result
   and is required by this stage.
5. **SVD enrichment** — `enrich_svd()` injects the registers and fields into
   the SVD, keeping only the registers that fall inside the address blocks
   declared by the device tree `reg` property.

Each module is documented in [doc/](doc/README.md).

## Command line

```console
pysvdgen -k /path/to/linux -d imx8mm -p IMX8MDQLQRM.pdf -o imx8mm.svd
```

| Option | Description |
| --- | --- |
| `-k`, `--kernel` | Linux kernel sources |
| `-d`, `--dtsi` | DTSI name, without extension |
| `-p`, `--pdf` | reference manual PDF |
| `-o`, `--output` | SVD file to write |
| `-w`, `--workspace` | working directory, kept on exit; a temporary one is created and removed when omitted |
| `-c`, `--chapter` | only process the chapters whose name contains this text (repeatable) |
| `--model`, `--ollama-host` | select the ollama model and server |
| `--vendor`, `-v` | vendor name written in the SVD, verbose logs |

Peripherals missing from the device tree, peripherals missing from the manual
and registers falling outside the device tree address blocks are reported at
the end of the run.

## Programmatic use

```python
from pySvdGenerator import enrich_svd, extract_registers, generate_svd, split_chapters

svd = generate_svd("/path/to/linux", "imx8mm", "build/imx8mm.svd")
chapters = split_chapters("IMX8MDQLQRM.pdf", "build/chapters", select=["Timers"])
registers = [extract_registers(chapter) for chapter in chapters]
report = enrich_svd(svd, registers, "imx8mm.svd")
```

## Requirements

### Java

`tabula-py` reads the PDF tables through a Java program, so a JRE must be
installed and reachable:

```console
java -version          # 11 or later
sudo apt install default-jre
```

### ollama

The extraction stage needs a reachable `ollama` server. The agent classifies
the tables the header heuristics cannot recognise, attributes the orphan bit
field tables, splits the register names of the peripheral instances and
repairs the field names damaged by the PDF text extraction.
`OllamaUnavailableError` is raised when the server does not answer.

Install the server, start it and pull a model:

```console
curl -fsSL https://ollama.com/install.sh | sh
ollama serve &                   # skipped when the service is already running
ollama pull qwen2.5-coder:7b     # default model
ollama pull llama3.2             # fallback model
```

Check that the server answers and knows the model:

```console
curl -s http://localhost:11434/api/tags
ollama list
```

| Item | Value |
| --- | --- |
| Default model | `qwen2.5-coder:7b`, about 5 GB of RAM |
| Fallback model | `llama3.2`, used when the requested model is not pulled |
| Default endpoint | `http://localhost:11434`, overridden by `--ollama-host` or `OLLAMA_HOST` |

The model is selected at startup: when the model asked for through `--model`
is missing on the server, the first available fallback is used and a warning
is logged; when nothing usable is pulled, the run stops with an explicit
error.
