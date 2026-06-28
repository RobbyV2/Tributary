import copy
import json
from collections.abc import Sequence
from typing import Any

import pytest

import tributary.audio.pipewire as pw
from tributary.audio.pipewire import (
    Direction,
    PwBinaryNotFound,
    PwCommandError,
    PwError,
    PwParseError,
    _run,
    create_null_sink,
    destroy_null_sink,
    dump,
    link_ports,
    parse_dump,
    unlink,
)

SINK = "tributary_mix"


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


def mk_node(oid: int, name: str, media_class: str = "Audio/Sink") -> dict[str, Any]:
    return {"id": oid, "type": "PipeWire:Interface:Node", "info": {"props": {"node.name": name, "media.class": media_class, "object.serial": oid}}}


def mk_link(oid: int, on: int, op: int, inn: int, ip: int) -> dict[str, Any]:
    return {"id": oid, "type": "PipeWire:Interface:Link", "info": {"output-node-id": on, "output-port-id": op, "input-node-id": inn, "input-port-id": ip, "props": {"object.serial": oid}}}


def mk_dump(*objs: dict[str, Any]) -> str:
    return json.dumps(list(objs))


def test_parse_counts_idle(pw_dump_idle: list[dict[str, Any]]) -> None:
    g = parse_dump(pw_dump_idle)
    assert (len(g.nodes), len(g.ports), len(g.links)) == (7, 11, 0)


def test_parse_counts_playing(pw_dump_playing: list[dict[str, Any]]) -> None:
    g = parse_dump(pw_dump_playing)
    assert (len(g.nodes), len(g.ports), len(g.links)) == (11, 20, 2)


def test_parse_accepts_str_bytes_list(pw_dump_playing: list[dict[str, Any]]) -> None:
    text = json.dumps(pw_dump_playing)
    assert parse_dump(text) == parse_dump(text.encode()) == parse_dump(pw_dump_playing)


def test_parse_malformed_json_raises() -> None:
    with pytest.raises(PwParseError):
        parse_dump("{not json")


def test_parse_non_list_raises() -> None:
    with pytest.raises(PwParseError):
        parse_dump("{}")


def test_sources_playing(pw_dump_playing: list[dict[str, Any]]) -> None:
    g = parse_dump(pw_dump_playing)
    assert tuple(n.id for n in g.sources()) == (95,)
    ids = {n.id for n in g.sources()}
    assert 90 not in ids and 91 not in ids


def test_sources_idle_empty(pw_dump_idle: list[dict[str, Any]]) -> None:
    assert parse_dump(pw_dump_idle).sources() == ()


def test_sources_a2dp_prefers_stream(pw_dump_a2dp: list[dict[str, Any]]) -> None:
    g = parse_dump(pw_dump_a2dp)
    assert tuple(n.id for n in g.sources()) == (111,)
    ids = {n.id for n in g.sources()}
    assert 78 not in ids and 83 not in ids


def test_node95_fields(pw_dump_playing: list[dict[str, Any]]) -> None:
    n = parse_dump(pw_dump_playing).node(95)
    assert n is not None
    assert n.media_class == "Audio/Source"
    assert n.device_id == 81
    assert n.serial == 416
    assert n.factory_name is None


def test_output_ports_95(pw_dump_playing: list[dict[str, Any]]) -> None:
    g = parse_dump(pw_dump_playing)
    outs = g.output_ports(95)
    assert tuple(p.id for p in outs) == (93,)
    assert outs[0].channel == "MONO"
    assert outs[0].monitor is False
    assert outs[0].direction is Direction.OUT
    assert len(g.ports_of(95)) == 1


def test_ports_of_node50(pw_dump_playing: list[dict[str, Any]]) -> None:
    g = parse_dump(pw_dump_playing)
    assert tuple(p.id for p in g.input_ports(50)) == (54, 55)
    mons = g.monitor_ports(50)
    assert tuple(p.id for p in mons) == (56, 57)
    assert all(p.monitor is True for p in mons)


def test_device_siblings(pw_dump_playing: list[dict[str, Any]]) -> None:
    g = parse_dump(pw_dump_playing)
    assert tuple(n.id for n in g.device_siblings(95)) == (90,)
    assert g.device_siblings(91) == ()
    assert tuple(n.id for n in g.device_siblings(50)) == (51,)


def test_link_between(pw_dump_playing: list[dict[str, Any]]) -> None:
    g = parse_dump(pw_dump_playing)
    lk = g.link_between(77, 54)
    assert lk is not None and lk.id == 80
    assert lk.output_node == 83 and lk.input_node == 50
    other = g.link_between(85, 55)
    assert other is not None and other.id == 78


def test_link_queries(pw_dump_playing: list[dict[str, Any]]) -> None:
    g = parse_dump(pw_dump_playing)
    assert tuple(lk.id for lk in g.links_into(54)) == (80,)
    assert tuple(lk.id for lk in g.links_from(77)) == (80,)
    assert tuple(lk.id for lk in g.links_of(83)) == (80, 78)
    assert tuple(lk.id for lk in g.links_of(50)) == (80, 78)


def test_dual_encoding(pw_dump_playing: list[dict[str, Any]]) -> None:
    expected = {(77, 54): 80, (85, 55): 78}
    orig = parse_dump(pw_dump_playing)
    assert {(lk.output_port, lk.input_port): lk.id for lk in orig.links} == expected

    dashed_removed = copy.deepcopy(pw_dump_playing)
    for o in dashed_removed:
        if o.get("type") == "PipeWire:Interface:Link":
            for k in ("output-node-id", "output-port-id", "input-node-id", "input-port-id"):
                del o["info"][k]
    g1 = parse_dump(dashed_removed)
    assert {(lk.output_port, lk.input_port): lk.id for lk in g1.links} == expected

    dotted_removed = copy.deepcopy(pw_dump_playing)
    for o in dotted_removed:
        if o.get("type") == "PipeWire:Interface:Link":
            for k in ("link.output.node", "link.output.port", "link.input.node", "link.input.port"):
                del o["info"]["props"][k]
    g2 = parse_dump(dotted_removed)
    assert {(lk.output_port, lk.input_port): lk.id for lk in g2.links} == expected


def test_find_nodes_and_sink_by_name(pw_dump_playing: list[dict[str, Any]]) -> None:
    g = parse_dump(pw_dump_playing)
    assert tuple(n.id for n in g.find_nodes(media_class="Audio/Sink")) == (50, 90)
    n50 = g.node(50)
    assert n50 is not None and n50.name is not None
    found = g.sink_by_name(n50.name)
    assert found is not None and found.id == 50


def test_dump_uses_pw_dump(pw_dump_playing: list[dict[str, Any]]) -> None:
    fake = FakeRunner([json.dumps(pw_dump_playing)])
    g = dump(run=fake)
    assert len(g.nodes) == 11
    assert fake.calls == [["pw-dump"]]


def test_create_idempotent_when_present() -> None:
    fake = FakeRunner([mk_dump(mk_node(100, SINK))])
    node = create_null_sink(run=fake)
    assert node.name == SINK
    assert fake.calls == [["pw-dump"]]


def test_link_idempotent_when_present() -> None:
    fake = FakeRunner([mk_dump(mk_link(200, 83, 77, 50, 54))])
    lk = link_ports(83, 77, 50, 54, run=fake)
    assert lk.id == 200
    assert fake.calls == [["pw-dump"]]


def test_create_verify_after_error() -> None:
    fake = FakeRunner([mk_dump(), PwCommandError(["pw-cli"], 1, "boom"), mk_dump(mk_node(100, SINK))])
    node = create_null_sink(run=fake)
    assert node.name == SINK
    assert fake.calls[1][:3] == ["pw-cli", "create-node", "adapter"]
    assert all(c[0] != "pactl" for c in fake.calls)


def test_create_fallback_to_pactl() -> None:
    fake = FakeRunner([mk_dump(), "", mk_dump(), "", mk_dump(mk_node(100, SINK))])
    node = create_null_sink(run=fake)
    assert node.name == SINK
    assert any(c[0] == "pactl" and "load-module" in c for c in fake.calls)


def test_create_ultimate_failure() -> None:
    fake = FakeRunner([mk_dump(), "", mk_dump(), "", mk_dump()])
    with pytest.raises(PwError):
        create_null_sink(run=fake)


def test_destroy_noop_when_absent() -> None:
    fake = FakeRunner([mk_dump()])
    destroy_null_sink(run=fake)
    assert fake.calls == [["pw-dump"]]


def test_destroy_success_primary() -> None:
    fake = FakeRunner([mk_dump(mk_node(100, SINK)), "", mk_dump()])
    destroy_null_sink(run=fake)
    assert ["pw-cli", "destroy", "100"] in fake.calls
    assert all(c[0] != "pactl" for c in fake.calls)


def test_unlink_noop_when_absent() -> None:
    fake = FakeRunner([mk_dump()])
    unlink(77, 54, run=fake)
    assert fake.calls == [["pw-dump"]]


def test_missing_binary_raises() -> None:
    with pytest.raises(PwBinaryNotFound) as ei:
        _run(["definitely-not-a-binary-xyz"])
    assert isinstance(ei.value, PwError)
    assert not isinstance(ei.value, OSError)


def test_run_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    class Proc:
        def __init__(self, rc: int, out: str = "") -> None:
            self.returncode = rc
            self.stdout = out
            self.stderr = ""

    seq = [Proc(1), Proc(0, "ok")]
    monkeypatch.setattr(pw.subprocess, "run", lambda *a, **k: seq.pop(0))
    slept: list[float] = []
    out = _run(["x"], attempts=2, backoff=0.0, sleep=slept.append)
    assert out == "ok"
    assert slept == [0.0]


def test_run_retries_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    class Proc:
        returncode = 1
        stdout = ""
        stderr = "fail"

    monkeypatch.setattr(pw.subprocess, "run", lambda *a, **k: Proc())
    slept: list[float] = []
    with pytest.raises(PwCommandError):
        _run(["x"], attempts=2, backoff=0.0, sleep=slept.append)
    assert slept == [0.0]
