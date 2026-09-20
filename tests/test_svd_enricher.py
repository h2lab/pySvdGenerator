# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Tests for targeted and additive SVD enrichment."""

from pathlib import Path
import xml.etree.ElementTree as ET

from pySvdGenerator.svd_enricher import enrich_svd


def _svd(path: Path, names: list[str]) -> None:
    root = ET.Element("device")
    peripherals = ET.SubElement(root, "peripherals")
    for name in names:
        base = 0x1000 if name == "GPT1" else 0x2000
        peripheral = ET.SubElement(peripherals, "peripheral")
        ET.SubElement(peripheral, "name").text = name
        ET.SubElement(peripheral, "baseAddress").text = f"0x{base:X}"
        block = ET.SubElement(peripheral, "addressBlock")
        ET.SubElement(block, "offset").text = "0x0"
        ET.SubElement(block, "size").text = "0x1000"
        ET.SubElement(block, "usage").text = "registers"
    ET.ElementTree(root).write(path, encoding="utf-8")


def _source(*names: str) -> dict[str, object]:
    return {
        "source": "Timers.pdf",
        "peripherals": {
            name: {
                "base_address": 0x1000 if name == "GPT1" else 0x2000,
                "registers": {
                    "CR": {"offset": 0, "width": 32, "fields": {}},
                },
            }
            for name in names
        },
    }


def test_enrich_updates_only_named_peripheral(tmp_path: Path) -> None:
    svd = tmp_path / "input.svd"
    _svd(svd, ["GPT1", "GPT2"])

    report = enrich_svd(svd, _source("GPT1", "GPT2"), peripherals=["gpt1"])

    root = ET.parse(report.output).getroot()
    registers = root.findall("./peripherals/peripheral/registers")
    assert len(registers) == 1
    assert report.matches[0].peripheral == "GPT1"


def test_allow_new_adds_named_missing_peripheral(tmp_path: Path) -> None:
    svd = tmp_path / "input.svd"
    _svd(svd, ["GPT2"])

    report = enrich_svd(
        svd,
        _source("GPT1"),
        peripherals=["GPT1"],
        allow_new=True,
    )

    root = ET.parse(report.output).getroot()
    added = root.find("./peripherals/peripheral[name='GPT1']")
    assert added is not None
    assert added.find("registers/register/name").text == "CR"
    assert report.unused_sources == []
