# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Generate a CMSIS-SVD description from Linux kernel device tree sources.

The device tree describes the memory map (peripheral base addresses and
address blocks), the interrupt assignment and the CPU cores, which is what
this generator emits. Device trees carry no register level description, so
the produced peripherals hold address blocks and interrupts but no
``<registers>`` section.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from .bindings import BindingIndex
from .dts import (
    DeviceTree,
    DtsError,
    Node,
    interrupt_parent,
    read_dts,
    reg_entries,
    translate,
)

__all__ = [
    "Device",
    "Interrupt",
    "Peripheral",
    "build_device",
    "find_dts_file",
    "generate_svd",
]

SKIPPED_PATHS = ("/chosen", "/aliases", "/memory", "/reserved-memory", "/cpus", "/thermal-zones")

CPU_NAMES = {
    "arm,cortex-a5": "CA5",
    "arm,cortex-a7": "CA7",
    "arm,cortex-a8": "CA8",
    "arm,cortex-a9": "CA9",
    "arm,cortex-a15": "CA15",
    "arm,cortex-a17": "CA17",
    "arm,cortex-a35": "CA53",
    "arm,cortex-a53": "CA53",
    "arm,cortex-a55": "CA53",
    "arm,cortex-a57": "CA57",
    "arm,cortex-a72": "CA72",
    "arm,cortex-m0": "CM0",
    "arm,cortex-m3": "CM3",
    "arm,cortex-m4": "CM4",
    "arm,cortex-m7": "CM7",
}

_SPI_BASE = 32
_PPI_BASE = 16
_DEFAULT_BLOCK_SIZE = 0x1000


@dataclass
class Interrupt:
    """An interrupt line exposed by a peripheral.

    :param name: unique interrupt name.
    :param value: interrupt number seen by the CPU.
    :param description: name given by ``interrupt-names``, when any.
    """

    name: str
    value: int
    description: str | None = None


@dataclass
class Peripheral:
    """A memory mapped peripheral of the device.

    :param name: SVD peripheral name, taken from the device tree label.
    :param base_address: CPU visible base address.
    :param blocks: ``(offset, size)`` address blocks, relative to the base.
    :param interrupts: interrupt lines of the peripheral.
    :param description: binding title, or the compatible strings.
    :param group_name: SVD group, the name without its instance index.
    :param compatible: ``compatible`` strings of the device tree node.
    :param path: device tree path of the node.
    """

    name: str
    base_address: int
    blocks: list[tuple[int, int]] = field(default_factory=list)
    interrupts: list[Interrupt] = field(default_factory=list)
    description: str | None = None
    group_name: str | None = None
    compatible: list[str] = field(default_factory=list)
    path: str = ""


@dataclass
class Device:
    """The full device description extracted from a device tree.

    :param name: SVD device name.
    :param description: ``model`` property of the device tree root.
    :param vendor: vendor name.
    :param cpu_name: CMSIS CPU name, ``other`` when unknown.
    :param cpu_compatible: ``compatible`` of the first core.
    :param cpu_count: number of cores declared under ``/cpus``.
    :param peripherals: peripherals of the device, sorted by base address.
    """

    name: str
    description: str
    vendor: str | None = None
    cpu_name: str = "other"
    cpu_compatible: str | None = None
    cpu_count: int = 1
    peripherals: list[Peripheral] = field(default_factory=list)


# --------------------------------------------------------------------------
# source discovery
# --------------------------------------------------------------------------


def find_dts_file(kernel_path: Path | str, name: str) -> Path:
    """Locate a ``.dtsi``/``.dts`` file inside the kernel sources.

    :param kernel_path: root of the kernel sources.
    :param name: bare SoC name (``imx8mm``), file name (``imx8mm.dtsi``) or
        a path relative to the kernel tree.
    :return: the resolved path of the source file.
    :raises DtsError: when no matching file is found.
    """
    kernel = Path(kernel_path)
    direct = Path(name)
    if direct.is_file():
        return direct.resolve()
    if (kernel / name).is_file():
        return (kernel / name).resolve()

    stem = direct.name
    candidates = [stem] if stem.endswith((".dtsi", ".dts")) else [f"{stem}.dtsi", f"{stem}.dts"]
    roots = sorted((kernel / "arch").glob("*/boot/dts"))
    for candidate in candidates:
        for root in roots:
            matches = sorted(root.rglob(candidate))
            if matches:
                return matches[0].resolve()
    raise DtsError(f"No device tree source matching {name!r} under {kernel}")


def _include_dirs(kernel: Path, source: Path) -> list[Path]:
    """Return the include search path used to preprocess a DTSI."""
    dirs = [source.parent, kernel / "include", kernel / "scripts/dtc/include-prefixes"]
    for parent in source.parents:
        dirs.append(parent)
        if parent.name == "dts":
            break
    return [directory for directory in dirs if directory.is_dir()]


# --------------------------------------------------------------------------
# device tree to device model
# --------------------------------------------------------------------------


def _sanitize(name: str) -> str:
    """Turn a device tree name into a valid upper case SVD identifier."""
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", name).strip("_").upper()
    if not cleaned:
        return "PERIPHERAL"
    return f"P_{cleaned}" if cleaned[0].isdigit() else cleaned


def _peripheral_name(node: Node) -> str:
    """Return the SVD name of a node, its label when it has one."""
    if node.labels:
        return _sanitize(node.labels[0])
    return _sanitize(node.basename)


def _group_name(name: str) -> str:
    """Return the SVD group of a peripheral, its name without the index."""
    return re.sub(r"\d+$", "", name) or name


def _is_peripheral(node: Node) -> bool:
    """Tell whether a node describes a memory mapped peripheral."""
    if node.parent is None or not node.compatible():
        return False
    path = node.path
    if any(path == skip or path.startswith(f"{skip}/") for skip in SKIPPED_PATHS):
        return False
    device_type = node.props.get("device_type")
    if device_type and device_type.strings() and device_type.strings()[0] in ("cpu", "memory"):
        return False
    return "reg" in node.props


def _interrupts(tree: DeviceTree, node: Node, peripheral_name: str) -> list[Interrupt]:
    """Decode the ``interrupts`` property of a node.

    GIC numbers are translated to the CPU numbering, shared interrupts by
    ``+32`` and private ones by ``+16``.
    """
    controller = interrupt_parent(tree, node)
    prop = node.props.get("interrupts")
    if prop is None or controller is None:
        return []
    cells = prop.ints()
    stride_prop = controller.props.get("#interrupt-cells")
    stride = stride_prop.first_int(1) if stride_prop else 1
    stride = stride or 1
    is_gic = any("gic" in compatible for compatible in controller.compatible())

    names_prop = node.props.get("interrupt-names")
    names = names_prop.strings() if names_prop else []

    out: list[Interrupt] = []
    for index, start in enumerate(range(0, len(cells) - stride + 1, stride)):
        chunk = cells[start : start + stride]
        if is_gic and stride >= 3:
            kind, number = chunk[0], chunk[1]
            value = number + (_SPI_BASE if kind == 0 else _PPI_BASE)
        else:
            value = chunk[0]
        if value < 0:
            continue
        label = names[index] if index < len(names) else None
        name = _sanitize(f"{peripheral_name}_{label}") if label else _sanitize(peripheral_name)
        if label is None and index:
            name = f"{name}_{index}"
        out.append(Interrupt(name=name, value=value, description=label))
    return out


def _cpu_info(tree: DeviceTree) -> tuple[str, str | None, int]:
    """Return the CMSIS CPU name, its compatible and the number of cores."""
    cpus = tree.find("/cpus")
    if cpus is None:
        return "other", None, 1
    cores = [child for child in cpus.children.values() if child.basename == "cpu"]
    compatibles = [core.compatible()[0] for core in cores if core.compatible()]
    if not compatibles:
        return "other", None, max(len(cores), 1)
    primary = compatibles[0]
    return CPU_NAMES.get(primary, "other"), primary, len(cores)


def build_device(
    kernel_path: Path | str,
    soc: str,
    *,
    use_bindings: bool = True,
    vendor: str | None = None,
) -> Device:
    """Parse the device tree of ``soc`` and build the device model."""
    kernel = Path(kernel_path)
    source = find_dts_file(kernel, soc)
    tree = read_dts(source, _include_dirs(kernel, source))

    nodes = [node for node in tree.root.walk() if _is_peripheral(node)]

    peripherals: list[Peripheral] = []
    used: dict[str, int] = {}
    for node in nodes:
        entries = reg_entries(node)
        if not entries:
            continue
        base = translate(node, entries[0][0])
        if base is None:
            continue
        name = _peripheral_name(node)
        seen = used.get(name, 0)
        used[name] = seen + 1
        if seen:
            name = f"{name}_{seen}"

        blocks: list[tuple[int, int]] = []
        for address, length in entries:
            absolute = translate(node, address)
            if absolute is None:
                continue
            blocks.append((absolute - base, length or _DEFAULT_BLOCK_SIZE))

        peripherals.append(
            Peripheral(
                name=name,
                base_address=base,
                blocks=blocks or [(0, _DEFAULT_BLOCK_SIZE)],
                interrupts=_interrupts(tree, node, name),
                group_name=_group_name(name),
                compatible=node.compatible(),
                path=node.path,
            )
        )

    peripherals.sort(key=lambda peripheral: (peripheral.base_address, peripheral.name))
    _deduplicate_interrupts(peripherals)

    if use_bindings:
        index = BindingIndex.build(
            kernel, {compatible for item in peripherals for compatible in item.compatible}
        )
        for peripheral in peripherals:
            peripheral.description = index.describe(peripheral.compatible)
    for peripheral in peripherals:
        if not peripheral.description:
            peripheral.description = ", ".join(peripheral.compatible) or peripheral.path

    cpu_name, cpu_compatible, cpu_count = _cpu_info(tree)
    model_prop = tree.root.props.get("model")
    model = model_prop.strings()[0] if model_prop and model_prop.strings() else ""
    root_compatible = tree.root.compatible()

    return Device(
        name=_sanitize(Path(soc).stem),
        description=model or ", ".join(root_compatible) or f"Device tree of {source.name}",
        vendor=vendor or (root_compatible[0].split(",")[0] if root_compatible else None),
        cpu_name=cpu_name,
        cpu_compatible=cpu_compatible,
        cpu_count=cpu_count,
        peripherals=peripherals,
    )


def _deduplicate_interrupts(peripherals: Sequence[Peripheral]) -> None:
    """Make the interrupt names unique and drop the duplicated numbers."""
    seen: set[str] = set()
    values: set[int] = set()
    for peripheral in peripherals:
        kept: list[Interrupt] = []
        for interrupt in peripheral.interrupts:
            if interrupt.value in values:
                continue
            name = interrupt.name
            suffix = 1
            while name in seen:
                name = f"{interrupt.name}_{suffix}"
                suffix += 1
            interrupt.name = name
            seen.add(name)
            values.add(interrupt.value)
            kept.append(interrupt)
        peripheral.interrupts = kept


# --------------------------------------------------------------------------
# SVD rendering
# --------------------------------------------------------------------------


def _sub(parent: ET.Element, tag: str, text: str) -> ET.Element:
    """Append a child element carrying ``text`` and return it."""
    element = ET.SubElement(parent, tag)
    element.text = text
    return element


def _hex(value: int) -> str:
    """Format an integer the way SVD files write addresses."""
    return f"0x{value:X}"


def device_to_xml(device: Device) -> ET.ElementTree:
    """Render a :class:`Device` as a CMSIS-SVD XML tree."""
    root = ET.Element(
        "device",
        {
            "schemaVersion": "1.3",
            "xmlns:xs": "http://www.w3.org/2001/XMLSchema-instance",
            "xs:noNamespaceSchemaLocation": "CMSIS-SVD.xsd",
        },
    )
    if device.vendor:
        _sub(root, "vendor", device.vendor)
    _sub(root, "name", device.name)
    _sub(root, "version", "1.0")
    _sub(root, "description", device.description)

    cpu = ET.SubElement(root, "cpu")
    _sub(cpu, "name", device.cpu_name)
    _sub(cpu, "revision", "r0p0")
    _sub(cpu, "endian", "little")
    _sub(cpu, "mpuPresent", "true")
    _sub(cpu, "fpuPresent", "true")
    _sub(cpu, "nvicPrioBits", "4")
    _sub(cpu, "vendorSystickConfig", "false")

    _sub(root, "addressUnitBits", "8")
    _sub(root, "width", "32")
    _sub(root, "size", "32")
    _sub(root, "access", "read-write")
    _sub(root, "resetValue", "0x0")
    _sub(root, "resetMask", "0xFFFFFFFF")

    peripherals = ET.SubElement(root, "peripherals")
    for item in device.peripherals:
        element = ET.SubElement(peripherals, "peripheral")
        _sub(element, "name", item.name)
        if item.description:
            _sub(element, "description", item.description)
        if item.group_name:
            _sub(element, "groupName", item.group_name)
        _sub(element, "baseAddress", _hex(item.base_address))
        for offset, size in item.blocks:
            block = ET.SubElement(element, "addressBlock")
            _sub(block, "offset", _hex(offset))
            _sub(block, "size", _hex(size))
            _sub(block, "usage", "registers")
        for interrupt in item.interrupts:
            node = ET.SubElement(element, "interrupt")
            _sub(node, "name", interrupt.name)
            if interrupt.description:
                _sub(node, "description", interrupt.description)
            _sub(node, "value", str(interrupt.value))

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    return tree


def generate_svd(
    kernel_path: Path | str,
    soc: str,
    output: Path | str,
    *,
    use_bindings: bool = True,
    vendor: str | None = None,
) -> Path:
    """Generate an SVD file from the device tree of ``soc``.

    :param kernel_path: path to the Linux kernel sources.
    :param soc: SoC name (``imx8mm``), file name or path of the DTS/DTSI.
    :param output: path of the SVD file to write, including its name.
    :param use_bindings: look the peripheral descriptions up in the kernel
        device tree bindings documentation.
    :param vendor: vendor name, deduced from the root ``compatible`` when
        omitted.
    :return: the path of the written SVD file.
    """
    device = build_device(kernel_path, soc, use_bindings=use_bindings, vendor=vendor)
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    device_to_xml(device).write(target, encoding="utf-8", xml_declaration=True)
    return target
