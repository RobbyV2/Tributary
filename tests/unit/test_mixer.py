import copy
import json
from collections.abc import Sequence
from typing import Any

import tributary.audio.mixer as mixer
import tributary.audio.pipewire as pw
import tributary.config as config
from tributary.audio.mixer import (
    Mixer,
    ensure_bus,
    input_ports,
    monitor_ports,
    teardown,
)
from tributary.audio.pipewire import PwError, parse_dump
from tributary.bluetooth.headphones import find_sink_node
from tributary.config import Mac

SINK = "tributary_mix"
HP_MAC = "B4:23:A2:01:6D:27"
HP_NAME = "bluez_output.B4_23_A2_01_6D_27.1"


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


def mk_node(oid: int, name: str, media_class: str = "Audio/Sink", dev: int | None = None) -> dict[str, Any]:
    props: dict[str, Any] = {"node.name": name, "media.class": media_class, "object.serial": oid}
    if dev is not None:
        props["device.id"] = dev
    return {"id": oid, "type": "PipeWire:Interface:Node", "info": {"props": props}}


def mk_port(oid: int, node: int, name: str, channel: str | None, direction: str, monitor: bool = False) -> dict[str, Any]:
    props: dict[str, Any] = {"node.id": node, "port.name": name, "object.serial": oid}
    if channel is not None:
        props["audio.channel"] = channel
    if monitor:
        props["port.monitor"] = True
    return {"id": oid, "type": "PipeWire:Interface:Port", "info": {"direction": direction, "props": props}}


def mk_link(oid: int, on: int, op: int, inn: int, ip: int) -> dict[str, Any]:
    return {"id": oid, "type": "PipeWire:Interface:Link", "info": {"output-node-id": on, "output-port-id": op, "input-node-id": inn, "input-port-id": ip, "props": {"object.serial": oid}}}


def mk_dump(*objs: dict[str, Any]) -> str:
    return json.dumps(list(objs))


def stereo_sink(oid: int, name: str, fl: int, fr: int, mfl: int, mfr: int) -> list[dict[str, Any]]:
    return [
        mk_node(oid, name),
        mk_port(fl, oid, "playback_FL", "FL", "input"),
        mk_port(fr, oid, "playback_FR", "FR", "input"),
        mk_port(mfl, oid, "monitor_FL", "FL", "output", monitor=True),
        mk_port(mfr, oid, "monitor_FR", "FR", "output", monitor=True),
    ]


def mono_sink(oid: int, name: str, mono: int, mmono: int) -> list[dict[str, Any]]:
    return [
        mk_node(oid, name),
        mk_port(mono, oid, "playback_MONO", "MONO", "input"),
        mk_port(mmono, oid, "monitor_MONO", "MONO", "output", monitor=True),
    ]


def _p(pid: int) -> Any:
    return parse_dump(mk_dump(mk_port(pid, 0, "x", None, "output"))).port(pid)


def _bus_objs(pw_dump_playing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    objs = copy.deepcopy(pw_dump_playing)
    for o in objs:
        if o.get("type") == "PipeWire:Interface:Node" and o.get("id") == 50:
            o["info"]["props"]["node.name"] = SINK
    return objs


def _bus_graph(pw_dump_playing: list[dict[str, Any]]) -> pw.Graph:
    return parse_dump(_bus_objs(pw_dump_playing))


def _bus_dump(pw_dump_playing: list[dict[str, Any]]) -> str:
    return json.dumps(_bus_objs(pw_dump_playing))


def test_ensure_bus_idempotent_when_present() -> None:
    fake = FakeRunner([mk_dump(mk_node(100, SINK))])
    node = ensure_bus(run=fake)
    assert node.name == SINK
    assert fake.calls == [["pw-dump"]]


def test_input_ports_keyed_by_channel() -> None:
    g = parse_dump(mk_dump(*stereo_sink(50, SINK, 54, 55, 56, 57)))
    mix = g.sink_by_name(SINK)
    assert mix is not None
    ports = input_ports(g, mix)
    assert set(ports) == {"FL", "FR"}
    assert (ports["FL"].id, ports["FR"].id) == (54, 55)


def test_monitor_ports_keyed_by_channel() -> None:
    g = parse_dump(mk_dump(*stereo_sink(50, SINK, 54, 55, 56, 57)))
    mix = g.sink_by_name(SINK)
    assert mix is not None
    mons = monitor_ports(g, mix)
    assert set(mons) == {"FL", "FR"}
    assert (mons["FL"].id, mons["FR"].id) == (56, 57)


def test_mono_bus_ports_keyed_mono() -> None:
    g = parse_dump(mk_dump(*mono_sink(50, SINK, 60, 61)))
    mix = g.sink_by_name(SINK)
    assert mix is not None
    assert set(input_ports(g, mix)) == {"MONO"}
    assert set(monitor_ports(g, mix)) == {"MONO"}


def test_input_ports_excludes_monitor_outs(pw_dump_playing: list[dict[str, Any]]) -> None:
    g = parse_dump(pw_dump_playing)
    hp = g.node(90)
    assert hp is not None
    ports = input_ports(g, hp)
    assert set(ports) == {"FL", "FR"}
    assert (ports["FL"].id, ports["FR"].id) == (88, 84)


def test_find_headphone_sink_by_address(pw_dump_playing: list[dict[str, Any]]) -> None:
    g = parse_dump(pw_dump_playing)
    hp = find_sink_node(g, Mac(HP_MAC))
    assert hp is not None and hp.id == 90


def test_find_headphone_sink_ignores_name(pw_dump_playing: list[dict[str, Any]]) -> None:
    objs = copy.deepcopy(pw_dump_playing)
    for o in objs:
        if o.get("type") == "PipeWire:Interface:Node" and o.get("id") == 90:
            o["info"]["props"]["node.name"] = "bluez_output.deadbeef.1"
    g = parse_dump(objs)
    hp = find_sink_node(g, Mac(HP_MAC))
    assert hp is not None and hp.id == 90


def test_find_headphone_sink_absent(pw_dump_playing: list[dict[str, Any]]) -> None:
    g = parse_dump(pw_dump_playing)
    assert find_sink_node(g, Mac("AA:BB:CC:DD:EE:FF")) is None


def test_find_headphone_sink_excludes_none_dev(pw_dump_playing: list[dict[str, Any]]) -> None:
    objs = copy.deepcopy(pw_dump_playing)
    for o in objs:
        if o.get("type") == "PipeWire:Interface:Node" and o.get("id") == 90:
            del o["info"]["props"]["device.id"]
    g = parse_dump(objs)
    assert find_sink_node(g, Mac(HP_MAC)) is None


def test_fan_ports_stereo_channel_match() -> None:
    outs = {"FL": _p(56), "FR": _p(57)}
    ins = {"FL": _p(88), "FR": _p(84)}
    pairs = mixer.fan_ports(outs, ins)
    assert tuple((o.id, i.id) for o, i in pairs) == ((56, 88), (57, 84))


def test_fan_ports_mono_bus_fans_to_stereo_dest() -> None:
    outs = {"MONO": _p(61)}
    ins = {"FL": _p(88), "FR": _p(84)}
    pairs = mixer.fan_ports(outs, ins)
    assert {(o.id, i.id) for o, i in pairs} == {(61, 88), (61, 84)}


def test_fan_ports_collapse_stereo_into_mono_dest() -> None:
    outs = {"FL": _p(1), "FR": _p(2)}
    ins = {"MONO": _p(3)}
    pairs = mixer.fan_ports(outs, ins)
    assert {(o.id, i.id) for o, i in pairs} == {(1, 3), (2, 3)}


def test_ensure_bus_creates_when_absent() -> None:
    fake = FakeRunner([mk_dump(), "", mk_dump(mk_node(100, SINK))])
    m = Mixer(headphone_mac=HP_MAC, run=fake)
    node = m.ensure_bus()
    assert node.name == SINK
    create = [c for c in fake.calls if c[:3] == ["pw-cli", "create-node", "adapter"]]
    assert len(create) == 1
    blob = create[0][3]
    assert "node.name=tributary_mix" in blob
    assert "audio.rate" not in blob


def test_ensure_bus_noop_when_graph_has_bus(pw_dump_playing: list[dict[str, Any]]) -> None:
    g = _bus_graph(pw_dump_playing)
    fake = FakeRunner([])
    m = Mixer(headphone_mac=HP_MAC, run=fake)
    node = m.ensure_bus(g)
    assert node.id == 50 and node.name == SINK
    assert fake.calls == []
    accessor = m.bus(g)
    assert accessor is not None and accessor.id == 50


def test_ensure_bus_recreate_after_vanish(pw_dump_playing: list[dict[str, Any]]) -> None:
    g = parse_dump(pw_dump_playing)
    fake = FakeRunner([mk_dump(), "", mk_dump(mk_node(100, SINK))])
    m = Mixer(headphone_mac=HP_MAC, run=fake)
    node = m.ensure_bus(g)
    assert node.name == SINK
    assert any(c[:3] == ["pw-cli", "create-node", "adapter"] for c in fake.calls)


def test_ensure_headphone_link_channel_match(pw_dump_playing: list[dict[str, Any]]) -> None:
    g = _bus_graph(pw_dump_playing)
    fake = FakeRunner([mk_dump(), "", mk_dump(mk_link(200, 50, 56, 90, 88)), mk_dump(), "", mk_dump(mk_link(201, 50, 57, 90, 84))])
    m = Mixer(headphone_mac=HP_MAC, run=fake)
    links = m.ensure_headphone_link(g)
    pairs = {(c[1], c[2]) for c in fake.calls if c and c[0] == "pw-link"}
    assert pairs == {("56", "88"), ("57", "84")}
    assert {lk.id for lk in links} == {200, 201}


def test_ensure_headphone_link_idempotent(pw_dump_playing: list[dict[str, Any]]) -> None:
    g = _bus_graph(pw_dump_playing)
    present = mk_dump(mk_link(200, 50, 56, 90, 88), mk_link(201, 50, 57, 90, 84))
    fake = FakeRunner([present, present])
    m = Mixer(headphone_mac=HP_MAC, run=fake)
    links = m.ensure_headphone_link(g)
    assert {lk.id for lk in links} == {200, 201}
    assert all(c == ["pw-dump"] for c in fake.calls)


def test_ensure_headphone_link_noop_when_headphone_absent(pw_dump_playing: list[dict[str, Any]]) -> None:
    g = _bus_graph(pw_dump_playing)
    fake = FakeRunner([])
    m = Mixer(headphone_mac="AA:BB:CC:DD:EE:FF", run=fake)
    assert m.ensure_headphone_link(g) == ()
    assert fake.calls == []


def test_ensure_headphone_link_noop_when_bus_absent(pw_dump_playing: list[dict[str, Any]]) -> None:
    g = parse_dump(pw_dump_playing)
    fake = FakeRunner([])
    m = Mixer(headphone_mac=HP_MAC, run=fake)
    assert m.ensure_headphone_link(g) == ()
    assert fake.calls == []


def test_ensure_headphone_link_per_pair_tolerance(pw_dump_playing: list[dict[str, Any]]) -> None:
    g = _bus_graph(pw_dump_playing)
    fake = FakeRunner([PwError("boom"), mk_dump(), "", mk_dump(mk_link(201, 50, 57, 90, 84))])
    m = Mixer(headphone_mac=HP_MAC, run=fake)
    links = m.ensure_headphone_link(g)
    assert {lk.id for lk in links} == {201}


def test_reconcile_creates_then_links(pw_dump_playing: list[dict[str, Any]]) -> None:
    bus_dump = _bus_dump(pw_dump_playing)
    fake = FakeRunner([
        mk_dump(), "", bus_dump,
        bus_dump,
        mk_dump(), "", mk_dump(mk_link(200, 50, 56, 90, 88)),
        mk_dump(), "", mk_dump(mk_link(201, 50, 57, 90, 84)),
    ])
    m = Mixer(headphone_mac=HP_MAC, run=fake)
    res = m.reconcile(parse_dump(pw_dump_playing))
    assert res.bus is not None and res.bus.id == 50
    assert res.headphone_present is True
    assert len(res.headphone_links) == 2
    assert res.error is None


def test_reconcile_idempotent_converged(pw_dump_playing: list[dict[str, Any]]) -> None:
    g = _bus_graph(pw_dump_playing)
    present = mk_dump(mk_link(200, 50, 56, 90, 88), mk_link(201, 50, 57, 90, 84))
    fake = FakeRunner([present, present])
    m = Mixer(headphone_mac=HP_MAC, run=fake)
    res = m.reconcile(g)
    assert res.bus is not None and res.headphone_present is True
    assert len(res.headphone_links) == 2
    assert res.error is None
    assert all(c == ["pw-dump"] for c in fake.calls)


def test_reconcile_swallows_pwerror(pw_dump_playing: list[dict[str, Any]]) -> None:
    g = parse_dump(pw_dump_playing)
    fake = FakeRunner([mk_dump(), "", mk_dump(), "", mk_dump()])
    m = Mixer(headphone_mac=HP_MAC, run=fake)
    res = m.reconcile(g)
    assert res.error is not None
    assert res.bus is None
    assert res.headphone_present is False
    assert res.headphone_links == ()


def test_teardown_noop_when_absent() -> None:
    fake = FakeRunner([mk_dump(), mk_dump()])
    teardown(run=fake)
    assert all(c == ["pw-dump"] for c in fake.calls)


def test_teardown_unlinks_then_destroys() -> None:
    objs = stereo_sink(50, SINK, 54, 55, 56, 57) + [mk_node(95, "bluez_input.AA:BB:CC:DD:EE:FF", "Audio/Source"), mk_link(80, 95, 93, 50, 54), mk_link(81, 50, 56, 90, 88)]
    snap = mk_dump(*objs)
    nolinks = mk_dump(mk_node(50, SINK))
    fake = FakeRunner([
        snap,
        snap,
        snap, "", nolinks,
        snap, "", nolinks,
        nolinks, "", mk_dump(),
    ])
    teardown(run=fake)
    unlinks = {(c[2], c[3]) for c in fake.calls if c[:2] == ["pw-link", "-d"]}
    assert unlinks == {("93", "54"), ("56", "88")}
    assert ["pw-cli", "destroy", "50"] in fake.calls


def test_mixer_teardown_removes_links_and_sink() -> None:
    snap = mk_dump(
        *stereo_sink(50, SINK, 54, 55, 56, 57),
        *stereo_sink(90, HP_NAME, 88, 84, 82, 86),
        mk_link(200, 50, 56, 90, 88),
        mk_link(201, 50, 57, 90, 84),
    )
    nolinks = mk_dump(mk_node(50, SINK))
    fake = FakeRunner([snap, snap, snap, "", nolinks, snap, "", nolinks, nolinks, "", mk_dump()])
    m = Mixer(headphone_mac=HP_MAC, run=fake)
    m.teardown()
    unlinks = {(c[2], c[3]) for c in fake.calls if c[:2] == ["pw-link", "-d"]}
    assert unlinks == {("56", "88"), ("57", "84")}
    assert ["pw-cli", "destroy", "50"] in fake.calls


def test_mixer_teardown_noop_when_absent() -> None:
    fake = FakeRunner([mk_dump(), mk_dump()])
    m = Mixer(headphone_mac=HP_MAC, run=fake)
    m.teardown()
    assert all(c == ["pw-dump"] for c in fake.calls)


def test_mixer_teardown_swallows_pwerror() -> None:
    busonly = mk_dump(mk_node(50, SINK))
    fake = FakeRunner([busonly, busonly, busonly, "", busonly, "", busonly])
    m = Mixer(headphone_mac=HP_MAC, run=fake)
    m.teardown()
    assert ["pw-cli", "destroy", "50"] in fake.calls


def test_from_config_builds_mixer() -> None:
    cfg = config.loads('headphone_mac = "b4:23:a2:01:6d:27"\n')
    m = Mixer.from_config(cfg)
    assert m.headphone_mac == HP_MAC
    assert m.sink_name == SINK
    assert m.run is mixer._run


def test_mixer_methods_never_touch_real_run(monkeypatch: Any, pw_dump_playing: list[dict[str, Any]]) -> None:
    def boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("real subprocess invoked")

    monkeypatch.setattr(pw.subprocess, "run", boom)
    g = _bus_graph(pw_dump_playing)
    present = mk_dump(mk_link(200, 50, 56, 90, 88), mk_link(201, 50, 57, 90, 84))
    fake = FakeRunner([present, present])
    m = Mixer(headphone_mac=HP_MAC, run=fake)
    res = m.reconcile(g)
    assert res.bus is not None
    assert len(fake.calls) == 2
