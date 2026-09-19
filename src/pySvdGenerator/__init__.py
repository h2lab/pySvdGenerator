# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""SVD generation tools."""

from .chapter_splitter import DEFAULT_WORKSPACE, Chapter, list_chapters, split_chapters
from .register_extractor import (
    ChapterRegisters,
    Field,
    PeripheralRegisters,
    Register,
    extract_peripherals,
    extract_registers,
)
from .svd import Device, Interrupt, Peripheral, build_device, generate_svd
from .svd_enricher import EnrichmentReport, PeripheralMatch, enrich_svd

__all__ = [
    "DEFAULT_WORKSPACE",
    "Chapter",
    "ChapterRegisters",
    "Device",
    "EnrichmentReport",
    "Field",
    "Interrupt",
    "Peripheral",
    "PeripheralMatch",
    "PeripheralRegisters",
    "Register",
    "build_device",
    "enrich_svd",
    "extract_peripherals",
    "extract_registers",
    "generate_svd",
    "list_chapters",
    "split_chapters",
]
