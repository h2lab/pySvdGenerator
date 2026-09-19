<!--
SPDX-FileType: DOCUMENTATION
SPDX-FileCopyrightText: 2026 H2Lab Development Team
SPDX-License-Identifier: Apache-2.0
-->
# `pySvdGenerator.register_extractor`

Extraction of the registers and bit fields described by a chapter of a
reference manual. Tables are read with `tabula-py` and the result is
consolidated by a local `ollama` agent.

## Public API

| Symbol | Description |
| --- | --- |
| `extract_registers(pdf, **kwargs)` | Extract a chapter and return the plain dictionary |
| `extract_peripherals(pdf, *, peripheral=None, pages=None, model=DEFAULT_MODEL, host=None, assistant=None, batch_size=BATCH_SIZE)` | Same work, returning the `ChapterRegisters` model |
| `ChapterRegisters`, `PeripheralRegisters`, `Register`, `Field` | Data model |
| `OllamaAssistant` | The consolidation agent |
| `OllamaUnavailableError` | Raised when the ollama server cannot be reached |
| `DEFAULT_MODEL`, `FALLBACK_MODELS`, `BATCH_SIZE` | Defaults: `qwen2.5-coder:7b`, `("llama3.2",)`, 25 pages |

## Stages

1. **Read** — the text of every page is read with `pypdf`, the tables with
   `tabula` in lattice mode, by batches of `batch_size` pages to keep the
   number of JVM calls low.
2. **Locate** — each table is attached to its page by looking its first long
   cell up in the page texts, tables being returned in page order.
3. **Classify** — a header holding an offset or address column together with
   a register column marks a memory map; a header starting with `Field` or
   `Bits` and holding a description marks a bit field table. Tables left
   unclassified are submitted to the agent.
4. **Merge** — a table continued on the next page, same header or no header
   and the same number of columns, is concatenated with the previous one.
5. **Parse** — memory maps give the registers (address, width, access, reset
   value), field tables give the fields (bit range, name, description).
   Address columns are read as hexadecimal, as manuals write them.
6. **Attach** — a field table is attributed to the registers whose *base*
   name matches the mnemonic printed in the section title, so the single
   `GPTx_CR` table fills `GPT1_CR` through `GPT6_CR`. Orphan tables are
   submitted to the agent.
7. **Split** — registers are grouped per peripheral instance, either from the
   `<INSTANCE>_<REGISTER>` form, or through the agent, or by address
   containment for the leftovers.
8. **Consolidate** — field names degraded by the PDF text extraction are
   rebuilt by the agent from their description.

## The ollama agent

The agent is a required part of the extraction. `OllamaAssistant` pings the
server at construction and raises `OllamaUnavailableError` when it does not
answer. Every request is made at temperature 0 with a JSON response format.

The model is resolved at construction against the models pulled on the
server: the one asked for when it is there, otherwise the first available
entry of `FALLBACK_MODELS` with a logged warning, `llama3.2` by default. A
name without a tag matches any tag, so `llama3.2` selects
`llama3.2:latest`. When nothing usable is pulled,
`OllamaUnavailableError` lists the models the server actually holds.

| Method | Question asked |
| --- | --- |
| `classify(table)` | Is this table a memory map, a field table, or neither? |
| `owner(table, text, known)` | Which register does this field table document? |
| `split_instances(names, hint)` | Split these register names into instance and mnemonic |
| `field_names(register, fields)` | What is the real mnemonic of these fields? |

A custom client can be injected, which is how the extraction is exercised
without a server:

```python
assistant = OllamaAssistant(client=my_client)
data = extract_registers(chapter, assistant=assistant)
```

## Output

```python
{
  "source": "workspace/Chapter_12_Timers.pdf",
  "peripherals": {
    "GPT1": {
      "base_address": 808255488,
      "registers": {
        "CR": {
          "offset": 0,
          "address": 808255488,
          "width": 32,
          "access": "read-write",
          "reset_value": 0,
          "description": "GPT Control Register (GPT1_CR)",
          "fields": {
            "EN": {
              "name": "EN",
              "bit_offset": 0,
              "bit_width": 1,
              "description": "GPT Enable...",
              "access": None,
              "reset_value": None
            }
          }
        }
      }
    }
  }
}
```

`offset` is relative to the base address of the instance, `address` is the
absolute address printed by the manual. The dictionary is consumed as is by
[`svd_enricher`](svd_enricher.md).

## Example

```python
from pySvdGenerator import extract_registers

data = extract_registers("workspace/Chapter_12_Timers.pdf", model="qwen2.5-coder:7b")
for name, peripheral in data["peripherals"].items():
    print(name, hex(peripheral["base_address"]), len(peripheral["registers"]))
```
