import asyncio
import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

import tributary.core.daemon as daemon_mod
from tributary.audio.pipewire import parse_dump
from tributary.config import Config, Mac, SourceAllowlist
from tributary.control.ipc import RuntimeState
from tributary.core.daemon import Daemon, tick
from tributary.core.events import parse_managed_objects

FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures"
MIX = "tributary_mix"
HP_MAC = Mac("B4:23:A2:01:6D:27")
SRC_NODE = 95


def _objs(name: str) -> list[dict[str, Any]]:
    return json.loads((FIXTURES / "pipewire" / name).read_text())


def _bluez() -> dict[Mac, Any]:
    return parse_managed_objects((FIXTURES / "bluez" / "getmanagedobjects.txt").read_text())


def _cfg(**kw: Any) -> Config:
    allow = kw.pop("allowlist", SourceAllowlist(macs=(HP_MAC,)))
    return Config(headphone_mac=HP_MAC, allowlist=allow, **kw)


def _port_node(objs: list[dict[str, Any]], pid: int) -> int | None:
    for o in objs:
        if o.get("type") == "PipeWire:Interface:Port" and o["id"] == pid:
            return o["info"]["props"].get("node.id")
    return None


def _sink_objs(nid: int) -> list[dict[str, Any]]:
    def node() -> dict[str, Any]:
        return {"id": nid, "type": "PipeWire:Interface:Node", "info": {"props": {"node.name": MIX, "media.class": "Audio/Sink", "object.serial": nid}}}

    def port(pid: int, name: str, chan: str, direction: str, monitor: bool) -> dict[str, Any]:
        props: dict[str, Any] = {"node.id": nid, "port.name": name, "audio.channel": chan, "object.serial": pid}
        if monitor:
            props["port.monitor"] = True
        return {"id": pid, "type": "PipeWire:Interface:Port", "info": {"direction": direction, "props": props}}

    return [
        node(),
        port(nid + 1, "playback_FL", "FL", "input", False),
        port(nid + 2, "playback_FR", "FR", "input", False),
        port(nid + 3, "monitor_FL", "FL", "output", True),
        port(nid + 4, "monitor_FR", "FR", "output", True),
    ]


class Sim:
    def __init__(self, objs: list[dict[str, Any]]) -> None:
        self.objs = list(objs)
        self.calls: list[list[str]] = []
        self._next = 9000

    def _id(self) -> int:
        self._next += 1
        return self._next

    def _add_link(self, op: int, ip: int) -> None:
        if any(o.get("type") == "PipeWire:Interface:Link" and o["info"]["output-port-id"] == op and o["info"]["input-port-id"] == ip for o in self.objs):
            return
        link = {"id": self._id(), "type": "PipeWire:Interface:Link", "info": {"output-node-id": _port_node(self.objs, op), "output-port-id": op, "input-node-id": _port_node(self.objs, ip), "input-port-id": ip, "props": {}}}
        self.objs.append(link)

    def _del_link(self, op: int, ip: int) -> None:
        self.objs = [o for o in self.objs if not (o.get("type") == "PipeWire:Interface:Link" and o["info"]["output-port-id"] == op and o["info"]["input-port-id"] == ip)]

    def __call__(self, argv: Sequence[str]) -> str:
        self.calls.append(list(argv))
        match list(argv):
            case ["pw-dump"]:
                return json.dumps(self.objs)
            case ["pw-link", "-d", op, ip]:
                self._del_link(int(op), int(ip))
                return ""
            case ["pw-link", op, ip]:
                self._add_link(int(op), int(ip))
                return ""
            case ["pw-cli", "create-node", "adapter", blob]:
                m = re.search(r"node\.name=(\S+)", blob)
                if m is not None and m.group(1) == MIX and not any(o.get("info", {}).get("props", {}).get("node.name") == MIX for o in self.objs):
                    self.objs.extend(_sink_objs(self._id()))
                return ""
            case ["pw-cli", "destroy", _]:
                return ""
            case _:
                return ""


def test_playing_plans_source_and_headphone() -> None:
    sim = Sim(_objs("pw-dump-playing.json"))
    res = tick(parse_dump(json.dumps(_objs("pw-dump-playing.json"))), _bluez(), _cfg(), MIX, run=sim)
    assert res.bus is not None
    assert res.bus_created is True
    assert {s.out_node for s in res.delta.to_add} == {SRC_NODE}
    assert len(res.delta.to_add) == 2
    assert res.headphone_present is True
    assert len(res.headphone_links) == 2


def test_idempotent_second_tick() -> None:
    sim = Sim(_objs("pw-dump-playing.json"))
    tick(parse_dump(json.dumps(_objs("pw-dump-playing.json"))), _bluez(), _cfg(), MIX, run=sim)
    g2 = parse_dump(sim(["pw-dump"]))
    res = tick(g2, _bluez(), _cfg(), MIX, run=sim)
    assert res.bus_created is False
    assert res.delta.to_add == ()


def test_self_heal_creates_bus() -> None:
    sim = Sim(_objs("pw-dump-idle.json"))
    res = tick(parse_dump(json.dumps(_objs("pw-dump-idle.json"))), _bluez(), _cfg(), MIX, run=sim)
    assert res.bus_created is True
    assert res.bus is not None
    assert any(c[:3] == ["pw-cli", "create-node", "adapter"] for c in sim.calls)


def test_include_host_audio_adds_source() -> None:
    sim_on = Sim(_objs("pw-dump-playing.json"))
    on = tick(parse_dump(json.dumps(_objs("pw-dump-playing.json"))), _bluez(), _cfg(include_host_audio=True), MIX, run=sim_on)
    sim_off = Sim(_objs("pw-dump-playing.json"))
    off = tick(parse_dump(json.dumps(_objs("pw-dump-playing.json"))), _bluez(), _cfg(include_host_audio=False), MIX, run=sim_off)
    assert 50 in {s.out_node for s in on.delta.to_add}
    assert 50 not in {s.out_node for s in off.delta.to_add}
    assert len(on.delta.to_add) == len(off.delta.to_add) + 2


def test_allowlist_excludes_source() -> None:
    sim = Sim(_objs("pw-dump-playing.json"))
    res = tick(parse_dump(json.dumps(_objs("pw-dump-playing.json"))), _bluez(), _cfg(allowlist=SourceAllowlist()), MIX, run=sim)
    assert SRC_NODE not in {s.out_node for s in res.delta.to_add}
    assert res.delta.to_add == ()


def test_runtime_headphone_override_beats_config() -> None:
    sim = Sim(_objs("pw-dump-playing.json"))
    res = tick(parse_dump(json.dumps(_objs("pw-dump-playing.json"))), _bluez(), _cfg(), MIX, run=sim, headphone_mac=Mac("AA:BB:CC:DD:EE:FF"))
    assert res.headphone_present is False
    assert res.headphone_links == ()


_BUS_DUMP = json.dumps(_sink_objs(8000))


def _fake_run(argv: Sequence[str]) -> str:
    return _BUS_DUMP if list(argv)[:1] == ["pw-dump"] else ""


async def _no_bluez() -> dict[Mac, Any]:
    return {}


class _Headphones:
    async def reconcile(self) -> bool:
        return False


async def _serve_wiring(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, Any] = {}

    class FakeServer:
        def close(self) -> None:
            recorded["closed"] = True

        async def wait_closed(self) -> None:
            recorded["waited"] = True

    async def fake_serve_ipc(config: Config, dump_graph: Any, state: RuntimeState, *, run: Any = None) -> FakeServer:
        recorded["args"] = (config, dump_graph, state, run)
        return FakeServer()

    monkeypatch.setattr(daemon_mod, "serve_ipc", fake_serve_ipc)
    state = RuntimeState()
    d = Daemon(_cfg(), _Headphones(), lambda: parse_dump(_BUS_DUMP), _no_bluez, None, _fake_run, MIX, state)
    task = asyncio.create_task(d.serve())
    await asyncio.sleep(0.05)
    config, dump_graph, passed_state, run = recorded["args"]
    assert config is d.config
    assert dump_graph is d.fetch_graph
    assert passed_state is state
    assert run is _fake_run
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert recorded["closed"] is True


def test_serve_wires_ipc(monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_serve_wiring(monkeypatch))


class _Supervised:
    def __init__(self, seq: list[bool]) -> None:
        self.seq = list(seq)
        self.seen: list[bool] = []

    async def reconcile(self) -> bool:
        v = self.seq.pop(0) if self.seq else False
        self.seen.append(v)
        return v


async def _supervise(monkeypatch: pytest.MonkeyPatch) -> None:
    hp = _Supervised([False, True, False])
    sleeps = 0

    async def fake_sleep(_: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    d = Daemon(_cfg(), hp, lambda: parse_dump(_BUS_DUMP), _no_bluez, None, _fake_run, MIX)
    with pytest.raises(asyncio.CancelledError):
        await d._connect_loop()
    assert hp.seen == [False, True, False]


def test_connect_loop_supervises(monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_supervise(monkeypatch))


class _Failing:
    def __init__(self) -> None:
        self.attempts = 0

    async def reconcile(self) -> bool:
        self.attempts += 1
        raise daemon_mod.HeadphoneError("br-connection-page-timeout")


async def _supervise_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    hp = _Failing()
    sleeps = 0

    async def fake_sleep(_: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    d = Daemon(_cfg(), hp, lambda: parse_dump(_BUS_DUMP), _no_bluez, None, _fake_run, MIX)
    with pytest.raises(asyncio.CancelledError):
        await d._connect_loop()
    assert hp.attempts >= 2


def test_connect_loop_retries_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_supervise_failure(monkeypatch))


async def _teardown_always(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, Any] = {}

    class FakeServer:
        def close(self) -> None:
            recorded["closed"] = True

        async def wait_closed(self) -> None:
            pass

    async def fake_serve_ipc(config: Config, dump_graph: Any, state: RuntimeState, *, run: Any = None) -> FakeServer:
        return FakeServer()

    def spy_teardown(self: Any) -> None:
        recorded["torn"] = True

    async def boom_loop(self: Any) -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise RuntimeError("residual connect error")

    monkeypatch.setattr(daemon_mod, "serve_ipc", fake_serve_ipc)
    monkeypatch.setattr(daemon_mod.Mixer, "teardown", spy_teardown)
    monkeypatch.setattr(daemon_mod.Daemon, "_connect_loop", boom_loop)

    d = Daemon(_cfg(), _Headphones(), lambda: parse_dump(_BUS_DUMP), _no_bluez, None, _fake_run, MIX)
    task = asyncio.create_task(d.serve())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert recorded["torn"] is True
    assert recorded["closed"] is True


def test_serve_teardown_always_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_teardown_always(monkeypatch))
