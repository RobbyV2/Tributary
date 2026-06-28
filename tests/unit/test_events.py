import dataclasses

import pytest

from tributary.audio import pipewire as pw
from tributary.config import Mac
from tributary.core.events import (
    BluezDevice,
    DeviceConnected,
    DeviceDisconnected,
    Snapshot,
    SourceAppeared,
    SourceDisappeared,
    normalize,
    parse_managed_objects,
)

BUDS = Mac("B4:23:A2:01:6D:27")


@pytest.fixture
def graphs(pw_dump_idle, pw_dump_playing) -> tuple[pw.Graph, pw.Graph]:
    return pw.parse_dump(pw_dump_idle), pw.parse_dump(pw_dump_playing)


@pytest.fixture
def managed(bluez_dir) -> str:
    return (bluez_dir / "getmanagedobjects.txt").read_text()


def snap(graph: pw.Graph, devices=()) -> Snapshot:
    return Snapshot(graph, {d.mac: d for d in devices})


def test_parse_managed_objects(managed: str) -> None:
    devs = parse_managed_objects(managed)
    assert BUDS in devs
    assert devs[BUDS].connected is False
    assert "0000110b-0000-1000-8000-00805f9b34fb" in devs[BUDS].uuids
    assert all(isinstance(m, str) for m in devs)


def test_source_appeared_idle_to_playing(graphs) -> None:
    idle, playing = graphs
    events = normalize(snap(idle), snap(playing))
    assert events == (SourceAppeared(playing.sources()[0], 416),)


def test_source_disappeared_playing_to_idle(graphs) -> None:
    idle, playing = graphs
    events = normalize(snap(playing), snap(idle))
    assert events == (SourceDisappeared(416),)


def test_device_connect_disconnect(managed: str) -> None:
    i = managed.index(BUDS)
    disc = parse_managed_objects(managed)
    conn = parse_managed_objects(managed[:i] + managed[i:].replace("'Connected': <false>", "'Connected': <true>", 1))
    g = pw.parse_dump([])
    assert normalize(Snapshot(g, disc), Snapshot(g, conn)) == (DeviceConnected(BUDS),)
    assert normalize(Snapshot(g, conn), Snapshot(g, disc)) == (DeviceDisconnected(BUDS),)


def test_serial_dedup_ignores_id_reuse(graphs) -> None:
    _, playing = graphs
    src = playing.sources()[0]
    moved = dataclasses.replace(src, id=src.id + 1000)
    nodes = tuple(moved if n is src else n for n in playing.nodes)
    reindexed = pw.Graph(nodes, playing.ports, playing.links)
    assert normalize(snap(playing), snap(reindexed)) == ()


def test_idempotent(graphs, managed: str) -> None:
    idle, playing = graphs
    devs = parse_managed_objects(managed)
    s = Snapshot(playing, devs)
    assert normalize(s, s) == ()
    assert normalize(snap(idle), snap(idle)) == ()


def test_cold_start_from_none(graphs) -> None:
    _, playing = graphs
    assert normalize(None, snap(playing)) == (SourceAppeared(playing.sources()[0], 416),)
