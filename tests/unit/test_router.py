import json
import re
from collections.abc import Sequence
from typing import Any

from tributary.audio.pipewire import Link, PwCommandError, parse_dump
from tributary.audio.router import (
    Delta,
    LinkSpec,
    actual_source_links,
    desired_links,
    diff,
    mac_of,
    reconcile,
    select_bluez_sources,
    select_host_source,
)
from tributary.config import Config, Mac, SourceAllowlist


class FakeRunner:
    def __init__(self, responses: Sequence[str | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> str:
        self.calls.append(list(argv))
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


class Boom(FakeRunner):
    def __call__(self, argv: Sequence[str]) -> str:
        raise AssertionError("run must not be called")


def mk_node(oid: int, name: str, media_class: str, dev: int | None = None, **extra: Any) -> dict[str, Any]:
    props: dict[str, Any] = {"node.name": name, "media.class": media_class, "object.serial": oid, **extra}
    if dev is not None:
        props["device.id"] = dev
    return {"id": oid, "type": "PipeWire:Interface:Node", "info": {"props": props}}


def mk_port(oid: int, node: int, name: str, channel: str | None, direction: str) -> dict[str, Any]:
    props: dict[str, Any] = {"node.id": node, "port.name": name, "object.serial": oid}
    if channel is not None:
        props["audio.channel"] = channel
    return {"id": oid, "type": "PipeWire:Interface:Port", "info": {"direction": direction, "props": props}}


def mk_link(oid: int, on: int, op: int, inn: int, ip: int) -> dict[str, Any]:
    return {"id": oid, "type": "PipeWire:Interface:Link", "info": {"output-node-id": on, "output-port-id": op, "input-node-id": inn, "input-port-id": ip, "props": {"object.serial": oid}}}


def mk_dump(*objs: dict[str, Any]) -> str:
    return json.dumps(list(objs))


def bus(oid: int = 10, name: str = "tributary_mix") -> list[dict[str, Any]]:
    return [
        mk_node(oid, name, "Audio/Sink"),
        mk_port(11, oid, "playback_FL", "FL", "input"),
        mk_port(12, oid, "playback_FR", "FR", "input"),
    ]


def stereo_source(oid: int, mac: str, fl: int, fr: int, dev: int = 81) -> list[dict[str, Any]]:
    return [
        mk_node(oid, f"bluez_input.{mac}", "Audio/Source", dev),
        mk_port(fl, oid, "capture_FL", "FL", "output"),
        mk_port(fr, oid, "capture_FR", "FR", "output"),
    ]


def mono_source(oid: int, mac: str, mono: int, dev: int = 82) -> list[dict[str, Any]]:
    return [
        mk_node(oid, f"bluez_input.{mac}", "Audio/Source", dev),
        mk_port(mono, oid, "capture_MONO", "MONO", "output"),
    ]


def test_mac_of_from_name(pw_dump_playing: list[dict[str, Any]]) -> None:
    n = parse_dump(pw_dump_playing).node(95)
    assert n is not None
    assert mac_of(n) == Mac("B4:23:A2:01:6D:27")


def test_mac_of_from_prop() -> None:
    g = parse_dump(mk_dump(mk_node(7, "bluez_input.weird", "Audio/Source", **{"api.bluez5.address": "aa:bb:cc:dd:ee:ff"})))
    n = g.node(7)
    assert n is not None
    assert mac_of(n) == Mac("AA:BB:CC:DD:EE:FF")


def test_mac_of_none() -> None:
    g = parse_dump(mk_dump(mk_node(7, "alsa_output.x", "Audio/Sink")))
    n = g.node(7)
    assert n is not None
    assert mac_of(n) is None


ABT_MAC = Mac("E4:5F:01:E6:31:85")


def test_select_a2dp_node(pw_dump_a2dp: list[dict[str, Any]]) -> None:
    g = parse_dump(pw_dump_a2dp)
    sel = select_bluez_sources(g, SourceAllowlist(macs=(ABT_MAC,)))
    assert tuple(n.id for n in sel) == (111,)


def test_mac_of_a2dp_underscore_name(pw_dump_a2dp: list[dict[str, Any]]) -> None:
    n = parse_dump(pw_dump_a2dp).node(111)
    assert n is not None and n.name == "bluez_input.E4_5F_01_E6_31_85.2"
    assert mac_of(n) == ABT_MAC


def test_desired_a2dp_stereo(pw_dump_a2dp: list[dict[str, Any]]) -> None:
    g = parse_dump(pw_dump_a2dp)
    mix, src = g.node(87), g.node(111)
    assert mix is not None and src is not None
    specs = desired_links(g, mix, (src,))
    assert set(specs) == {LinkSpec(111, 85, 87, 101), LinkSpec(111, 103, 87, 70)}


def test_select_filters_by_allowlist() -> None:
    objs = bus() + stereo_source(20, "AA:BB:CC:DD:EE:01", 21, 22) + stereo_source(30, "AA:BB:CC:DD:EE:02", 31, 32)
    g = parse_dump(mk_dump(*objs))
    allow = SourceAllowlist(macs=(Mac("AA:BB:CC:DD:EE:01"),))
    sel = select_bluez_sources(g, allow)
    assert tuple(n.id for n in sel) == (20,)


def test_select_by_pattern() -> None:
    objs = bus() + stereo_source(20, "AA:BB:CC:DD:EE:01", 21, 22) + mono_source(30, "AA:BB:CC:DD:EE:02", 31)
    g = parse_dump(mk_dump(*objs))
    allow = SourceAllowlist(patterns=(re.compile(r"bluez_input\."),))
    sel = select_bluez_sources(g, allow)
    assert tuple(n.id for n in sel) == (20, 30)


def test_desired_mono_fans_into_both() -> None:
    objs = bus() + mono_source(20, "AA:BB:CC:DD:EE:02", 25)
    g = parse_dump(mk_dump(*objs))
    mix = g.sink_by_name("tributary_mix")
    assert mix is not None
    specs = desired_links(g, mix, (g.node(20),))  # type: ignore[arg-type]
    assert set(specs) == {LinkSpec(20, 25, 10, 11), LinkSpec(20, 25, 10, 12)}


def test_desired_stereo_channel_match() -> None:
    objs = bus() + stereo_source(20, "AA:BB:CC:DD:EE:01", 21, 22)
    g = parse_dump(mk_dump(*objs))
    mix = g.sink_by_name("tributary_mix")
    assert mix is not None
    specs = desired_links(g, mix, (g.node(20),))  # type: ignore[arg-type]
    assert set(specs) == {LinkSpec(20, 21, 10, 11), LinkSpec(20, 22, 10, 12)}


def test_desired_from_fixture_mono(pw_dump_playing: list[dict[str, Any]]) -> None:
    g = parse_dump(pw_dump_playing)
    mix = g.node(50)
    src = g.node(95)
    assert mix is not None and src is not None
    specs = desired_links(g, mix, (src,))
    assert set(specs) == {LinkSpec(95, 93, 50, 54), LinkSpec(95, 93, 50, 55)}


def test_desired_skips_self_loop() -> None:
    objs = bus() + [mk_port(13, 10, "monitor_FL", "FL", "output"), mk_port(14, 10, "monitor_FR", "FR", "output")]
    g = parse_dump(mk_dump(*objs))
    mix = g.sink_by_name("tributary_mix")
    assert mix is not None
    assert desired_links(g, mix, (mix,)) == ()


def test_actual_source_links_all_into_mix() -> None:
    objs = bus() + stereo_source(20, "AA:BB:CC:DD:EE:01", 21, 22) + [
        mk_link(80, 20, 21, 10, 11),
        mk_link(81, 99, 0, 10, 12),
        mk_link(82, 20, 21, 70, 71),
    ]
    g = parse_dump(mk_dump(*objs))
    mix = g.sink_by_name("tributary_mix")
    assert mix is not None
    links = actual_source_links(g, mix, (g.node(20),))  # type: ignore[arg-type]
    assert tuple(sorted(lk.id for lk in links)) == (80, 81)


def test_reconcile_reaps_orphan_mix_link() -> None:
    objs = bus() + stereo_source(20, "AA:BB:CC:DD:EE:01", 21, 22) + mono_source(30, "AA:BB:CC:DD:EE:02", 35) + [
        mk_link(80, 20, 21, 10, 11),
        mk_link(81, 20, 22, 10, 12),
        mk_link(82, 30, 35, 10, 11),
    ]
    g = parse_dump(mk_dump(*objs))
    mix = g.sink_by_name("tributary_mix")
    assert mix is not None
    present = mk_dump(mk_link(82, 30, 35, 10, 11))
    fake = FakeRunner([present, "", mk_dump()])
    delta = reconcile(g, mix, (g.node(20),), run=fake)  # type: ignore[arg-type]
    assert delta.to_add == ()
    assert delta.to_remove == (g.link_between(35, 11),)


def test_reconcile_orphan_reap_idempotent() -> None:
    objs = bus() + stereo_source(20, "AA:BB:CC:DD:EE:01", 21, 22) + [mk_link(80, 20, 21, 10, 11), mk_link(81, 20, 22, 10, 12)]
    g = parse_dump(mk_dump(*objs))
    mix = g.sink_by_name("tributary_mix")
    assert mix is not None
    delta = reconcile(g, mix, (g.node(20),), run=Boom([]))  # type: ignore[arg-type]
    assert delta == Delta((), ())


def test_diff_add_and_remove() -> None:
    desired = (LinkSpec(20, 21, 10, 11), LinkSpec(20, 22, 10, 12))
    stale = Link(99, 30, 35, 10, 11, None, {})
    keep = Link(80, 20, 21, 10, 11, None, {})
    d = diff(desired, (keep, stale))
    assert d.to_add == (LinkSpec(20, 22, 10, 12),)
    assert d.to_remove == (stale,)


def test_reconcile_idempotent_no_ops() -> None:
    objs = bus() + stereo_source(20, "AA:BB:CC:DD:EE:01", 21, 22) + [mk_link(80, 20, 21, 10, 11), mk_link(81, 20, 22, 10, 12)]
    g = parse_dump(mk_dump(*objs))
    mix = g.sink_by_name("tributary_mix")
    assert mix is not None
    delta = reconcile(g, mix, (g.node(20),), run=Boom([]))  # type: ignore[arg-type]
    assert delta == Delta((), ())


def test_reconcile_adds_links() -> None:
    objs = bus() + stereo_source(20, "AA:BB:CC:DD:EE:01", 21, 22)
    g = parse_dump(mk_dump(*objs))
    mix = g.sink_by_name("tributary_mix")
    assert mix is not None
    fake = FakeRunner([
        mk_dump(), "", mk_dump(mk_link(80, 20, 21, 10, 11)),
        mk_dump(), "", mk_dump(mk_link(81, 20, 22, 10, 12)),
    ])
    delta = reconcile(g, mix, (g.node(20),), run=fake)  # type: ignore[arg-type]
    pairs = {(c[1], c[2]) for c in fake.calls if c and c[0] == "pw-link"}
    assert pairs == {("21", "11"), ("22", "12")}
    assert len(delta.to_add) == 2 and delta.to_remove == ()


def test_reconcile_removes_stale() -> None:
    objs = bus() + mono_source(20, "AA:BB:CC:DD:EE:02", 25) + [mk_link(80, 20, 25, 10, 11), mk_link(81, 20, 25, 10, 12), mk_link(82, 20, 26, 10, 11)]
    g = parse_dump(mk_dump(*objs))
    mix = g.sink_by_name("tributary_mix")
    assert mix is not None
    present = mk_dump(mk_link(82, 20, 26, 10, 11))
    fake = FakeRunner([present, "", mk_dump()])
    delta = reconcile(g, mix, (g.node(20),), run=fake)  # type: ignore[arg-type]
    assert (fake.calls[1][0], fake.calls[1][1]) == ("pw-link", "-d")
    assert delta.to_add == () and delta.to_remove == (g.link_between(26, 11),)


HP = Mac("B4:23:A2:01:6D:27")
DEFAULT_SINK = "alsa_output.pci-0000_06_00.6.analog-stereo"


def test_host_source_disabled(pw_dump_playing: list[dict[str, Any]]) -> None:
    g = parse_dump(pw_dump_playing)
    assert select_host_source(g, Config(headphone_mac=HP)) == ()


def test_host_source_default_sink(pw_dump_playing: list[dict[str, Any]]) -> None:
    g = parse_dump(pw_dump_playing)
    sel = select_host_source(g, Config(headphone_mac=HP, include_host_audio=True))
    assert tuple(n.id for n in sel) == (50,)


def test_host_source_named(pw_dump_playing: list[dict[str, Any]]) -> None:
    g = parse_dump(pw_dump_playing)
    cfg = Config(headphone_mac=HP, include_host_audio=True, host_source="bluez_output.B4_23_A2_01_6D_27.1")
    sel = select_host_source(g, cfg)
    assert tuple(n.id for n in sel) == (90,)


def test_host_source_missing_named(pw_dump_playing: list[dict[str, Any]]) -> None:
    g = parse_dump(pw_dump_playing)
    cfg = Config(headphone_mac=HP, include_host_audio=True, host_source="nonexistent")
    assert select_host_source(g, cfg) == ()


def test_host_monitor_fans_into_mix() -> None:
    objs = bus() + [
        mk_node(40, DEFAULT_SINK, "Audio/Sink"),
        mk_port(56, 40, "monitor_FL", "FL", "output"),
        mk_port(57, 40, "monitor_FR", "FR", "output"),
    ]
    g = parse_dump(mk_dump(*objs))
    mix = g.sink_by_name("tributary_mix")
    src = g.node(40)
    assert mix is not None and src is not None
    specs = desired_links(g, mix, (src,))
    assert set(specs) == {LinkSpec(40, 56, 10, 11), LinkSpec(40, 57, 10, 12)}


def test_host_compose_with_bluez(pw_dump_playing: list[dict[str, Any]]) -> None:
    g = parse_dump(pw_dump_playing)
    allow = SourceAllowlist(patterns=(re.compile(r"bluez_input\."),))
    cfg = Config(headphone_mac=HP, include_host_audio=True)
    sources = select_bluez_sources(g, allow) + select_host_source(g, cfg)
    assert isinstance(sources, tuple)
    assert 50 in {n.id for n in sources}


def test_reconcile_tolerates_failure() -> None:
    objs = bus() + stereo_source(20, "AA:BB:CC:DD:EE:01", 21, 22)
    g = parse_dump(mk_dump(*objs))
    mix = g.sink_by_name("tributary_mix")
    assert mix is not None
    fail = PwCommandError(["pw-link"], 1, "boom")
    fake = FakeRunner([
        mk_dump(), fail, mk_dump(), fail, mk_dump(),
        mk_dump(), "", mk_dump(mk_link(81, 20, 22, 10, 12)),
    ])
    delta = reconcile(g, mix, (g.node(20),), run=fake)  # type: ignore[arg-type]
    assert delta.to_add == (LinkSpec(20, 22, 10, 12),)
