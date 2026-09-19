<!--
SPDX-FileType: DOCUMENTATION
SPDX-FileCopyrightText: 2026 H2Lab Development Team
SPDX-License-Identifier: Apache-2.0
-->
# `pySvdGenerator.chapter_splitter`

Splitting of a reference manual into one PDF per chapter, so that the
register extraction works on small documents.

## Public API

| Symbol | Description |
| --- | --- |
| `split_chapters(document, workspace=DEFAULT_WORKSPACE, *, overwrite=True, select=None)` | Write one PDF per chapter, return their paths |
| `list_chapters(document)` | Chapters of the document, without writing anything |
| `matches(chapter, select)` | Tell whether a chapter matches a selection |
| `sanitize_name(title)` | Portable file name stem for a chapter title |
| `Chapter` | Title and page range of a chapter |
| `DEFAULT_WORKSPACE` | `Path("workspace")` |

## Behaviour

* Chapters are the first level entries of the PDF outline; a chapter runs
  from its own first page to the page before the next chapter.
* File names come from the chapter title: formatting code points such as the
  zero width space used by some manuals are removed, the text is reduced to
  ASCII and to `[A-Za-z0-9._-]`. Colliding names get a numeric suffix.
* `select` restricts the work to the chapters whose title or file name
  contains one of the given substrings, which avoids splitting thousands of
  pages when a single chapter is needed.
* `overwrite=False` keeps the files already present, making the split
  restartable inside a persistent workspace.
* A document without outline raises `ValueError`, a missing file raises
  `FileNotFoundError`.

## Example

```python
from pySvdGenerator import list_chapters, split_chapters

for chapter in list_chapters("IMX8MDQLQRM.pdf"):
    print(chapter.title, chapter.first_page, chapter.last_page, chapter.filename)

paths = split_chapters("IMX8MDQLQRM.pdf", "workspace", select=["Timers"])
# [PosixPath('workspace/Chapter_12_Timers.pdf')]
```
