import re
from collections.abc import Mapping
from dataclasses import dataclass

from tributary.audio.pipewire import Graph, Node
from tributary.config import Mac

_DEVICE_RE = re.compile(r"'org\.bluez\.Device1':\s*\{")
_ADDR_RE = re.compile(r"'Address':\s*<'([0-9A-Fa-f:]{17})'>")
_CONNECTED_RE = re.compile(r"'Connected':\s*<(true|false)>")
_UUID_RE = re.compile(r"'([0-9a-fA-F-]{36})'")


@dataclass(frozen=True, slots=True)
class BluezDevice:
    mac: Mac
    connected: bool
    uuids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Snapshot:
    graph: Graph
    devices: Mapping[Mac, BluezDevice]


@dataclass(frozen=True, slots=True)
class SourceAppeared:
    node: Node
    serial: int


@dataclass(frozen=True, slots=True)
class SourceDisappeared:
    serial: int


@dataclass(frozen=True, slots=True)
class DeviceConnected:
    mac: Mac


@dataclass(frozen=True, slots=True)
class DeviceDisconnected:
    mac: Mac


Event = SourceAppeared | SourceDisappeared | DeviceConnected | DeviceDisconnected


def parse_managed_objects(text: str) -> dict[Mac, BluezDevice]:
    out: dict[Mac, BluezDevice] = {}
    for chunk in _DEVICE_RE.split(text)[1:]:
        addr = _ADDR_RE.search(chunk)
        conn = _CONNECTED_RE.search(chunk)
        if addr is None or conn is None:
            continue
        mac = Mac(addr.group(1).upper())
        uuids = tuple(m.group(1) for m in _UUID_RE.finditer(chunk[: chunk.find("'Modalias'")]))
        out[mac] = BluezDevice(mac, conn.group(1) == "true", uuids)
    return out


def _sources_by_serial(graph: Graph) -> dict[int, Node]:
    return {n.serial: n for n in graph.sources() if n.serial is not None}


def normalize(prev: Snapshot | None, curr: Snapshot) -> tuple[Event, ...]:
    prev_src = _sources_by_serial(prev.graph) if prev is not None else {}
    curr_src = _sources_by_serial(curr.graph)
    prev_dev = prev.devices if prev is not None else {}
    events: list[Event] = []
    for serial in sorted(curr_src.keys() - prev_src.keys()):
        events.append(SourceAppeared(curr_src[serial], serial))
    for serial in sorted(prev_src.keys() - curr_src.keys()):
        events.append(SourceDisappeared(serial))
    for mac in sorted(curr.devices.keys() | prev_dev.keys()):
        was = mac in prev_dev and prev_dev[mac].connected
        now = mac in curr.devices and curr.devices[mac].connected
        match (was, now):
            case (False, True):
                events.append(DeviceConnected(mac))
            case (True, False):
                events.append(DeviceDisconnected(mac))
            case _:
                pass
    return tuple(events)
