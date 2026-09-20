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
   and is queried by default; `--no-llm` (`use_llm=False`) restricts the stage
   to the deterministic parsing.
5. **SVD enrichment** — `enrich_svd()` injects the registers and fields into
   the SVD, keeping only the registers that fall inside the address blocks
   declared by the device tree `reg` property.

Each module is documented in [doc/](doc/README.md).

## Command line

The `pysvdgen` command always needs a reference manual PDF. It supports two
mutually exclusive modes:

* **Generate a new SVD:** provide both `--kernel` and `--dtsi`; `--output` is
   required.
* **Complete an existing SVD:** provide `--svd`; `--kernel` and `--dtsi` must
   not be used. The input file is updated in place unless `--output` is given.

The DTSI argument is the device-tree name without its `.dts` or `.dtsi`
extension. For example, this generates an SVD skeleton from the `imx8mm`
device tree and enriches it with all chapters found in the manual:

```console
pysvdgen \
   --kernel /path/to/linux \
   --dtsi imx8mm \
   --pdf IMX8MDQLQRM.pdf \
   --output build/imx8mm.svd
```

Use `--vendor` to set the vendor name in a newly generated SVD, and `--verbose`
to show module logs and detailed tracebacks when a stage fails:

```console
pysvdgen -k /path/to/linux -d imx8mm -p IMX8MDQLQRM.pdf \
   -o build/imx8mm.svd --vendor NXP --verbose
```

### Selecting chapters

By default, every chapter in the reference manual is processed. `--chapter`
filters by a case-insensitive substring of the chapter title or filename and
can be repeated. The following command processes only timer-related chapters:

```console
pysvdgen -k /path/to/linux -d imx8mm -p IMX8MDQLQRM.pdf \
   -o build/imx8mm.svd --chapter Timer
```

Several filters can be provided in one run:

```console
pysvdgen -k /path/to/linux -d imx8mm -p IMX8MDQLQRM.pdf \
   -o build/imx8mm.svd -c Timer -c Watchdog
```

If no chapter matches, the command exits with status `2` and leaves the output
unchanged.

### Completing an SVD incrementally

An existing SVD can be completed chapter by chapter. Peripherals that already
contain registers are preserved on later runs. Without `--output`, the input
file is updated in place; use a different output path to keep the original:

```console
# First pass: create the SVD with the timer chapters.
pysvdgen -k /path/to/linux -d imx8mm -p IMX8MDQLQRM.pdf \
   -o build/imx8mm.svd -c Timer

# Later passes: add more chapters to the same file.
pysvdgen -s build/imx8mm.svd -p IMX8MDQLQRM.pdf -c Connectivity
pysvdgen -s build/imx8mm.svd -p IMX8MDQLQRM.pdf -c "Mass Storage"

# Keep a copy while writing the completed result elsewhere.
pysvdgen -s build/imx8mm.svd -p IMX8MDQLQRM.pdf \
   -c Ethernet -o build/imx8mm-enriched.svd
```

### Workspace and repeatable runs

Use `--workspace` to keep the intermediate chapter PDFs and extracted JSON
files. A later run with the same workspace reuses cached extraction results;
this is useful when processing a large manual or when adding chapter filters
over several runs:

```console
pysvdgen -k /path/to/linux -d imx8mm -p IMX8MDQLQRM.pdf \
   -o build/imx8mm.svd -w build/pysvdgen-work -c Timer
pysvdgen -s build/imx8mm.svd -p IMX8MDQLQRM.pdf \
   -w build/pysvdgen-work -c Connectivity
```

When `--workspace` is omitted, a temporary workspace is created and removed
after the run, including when the run fails.

### Updating one peripheral

When completing an existing SVD, use `--peripheral` to restrict the
enrichment to a named peripheral. The option is case-insensitive and can be
repeated. This is useful when a chapter describes several instances but only
one of them should be updated:

```console
pysvdgen -s build/imx8mm.svd -p IMX8MDQLQRM.pdf \
   -c Timer --peripheral GPT1
```

If the named peripheral is present in the extracted chapter but missing from
the SVD, `--allow-new` adds it using the base address and registers from the
reference manual. `--allow-new` requires at least one `--peripheral` option:

```console
pysvdgen -s build/imx8mm.svd -p IMX8MDQLQRM.pdf \
   -c Timer --peripheral GPT1 --allow-new
```

Without `--allow-new`, a missing named peripheral is reported as an unused
manual source and is not added to the SVD. Existing peripherals not named with
`--peripheral` are left unchanged.

### Ollama and deterministic extraction

Register extraction queries the Ollama server at
`http://localhost:11434` by default. Select another model with `--model` (or
the more explicit `--llm-model` alias) and another server with
`--ollama-host`:

```console
pysvdgen -k /path/to/linux -d imx8mm -p IMX8MDQLQRM.pdf \
   -o build/imx8mm.svd --model llama3.2 \
   --ollama-host http://localhost:11434
```

Use `--no-llm` when no Ollama server is available. The command then uses only
the deterministic table parsing and the extracted result can be incomplete:

```console
pysvdgen -s build/imx8mm.svd -p IMX8MDQLQRM.pdf \
   -c Timer --no-llm
```

Run `pysvdgen --help` for the complete option list:

| Option | Description |
| --- | --- |
| `-k`, `--kernel` | Linux kernel source tree; required with `--dtsi` unless `--svd` is given |
| `-d`, `--dtsi` | device-tree name without its extension; required with `--kernel` unless `--svd` is given |
| `-s`, `--svd` | existing SVD file to complete; mutually exclusive with `--kernel` and `--dtsi` |
| `-p`, `--pdf` | reference manual PDF; always required |
| `-o`, `--output` | SVD file to write; required for a new SVD, otherwise defaults to updating `--svd` in place |
| `-w`, `--workspace` | working directory to keep and reuse |
| `-c`, `--chapter` | case-insensitive substring filter for chapter titles or filenames; repeatable |
| `--peripheral` | restrict enrichment to this peripheral name; repeatable and case-insensitive |
| `--allow-new` | add the selected peripheral when it is missing from the SVD; requires `--peripheral` |
| `--model`, `--llm-model` | Ollama model used for extraction |
| `--ollama-host` | Ollama server URL |
| `--no-llm` | disable Ollama and use deterministic extraction only |
| `--vendor` | vendor name written in a newly generated SVD |
| `-v`, `--verbose` | show module logs and detailed tracebacks |

The command returns `0` on success, `1` when a pipeline stage fails, `2` when
no chapter matches the filters, and `3` for inconsistent arguments.

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

The extraction stage queries a reachable `ollama` server by default. The
agent classifies the tables the header heuristics cannot recognise,
attributes the orphan bit field tables, splits the register names of the
peripheral instances and repairs the field names damaged by the PDF text
extraction. `OllamaUnavailableError` is raised when the server does not
answer. Use `--no-llm` to run without any server, at the price of a degraded
extraction.

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
