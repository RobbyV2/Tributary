import asyncio
from collections.abc import Sequence

import pytest

from tributary.audio.pipewire import parse_dump
from tributary.config import AdapterMap, Config, Mac, SourceAllowlist
from tributary.control import ipc
from tributary.control.ipc import (
    ErrorResp,
    GraphReq,
    GraphResp,
    HeadphoneReq,
    ListReq,
    ListResp,
    MuteReq,
    OkResp,
    RuntimeState,
    StatusReq,
    StatusResp,
    VolumeReq,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
    handler,
)

HP_MAC = Mac("B4:23:A2:01:6D:27")


class FakeRunner:
    def __init__(self, responses: Sequence[str | Exception] = ()) -> None:
        self.responses = list(responses)
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> str:
        self.calls.append(list(argv))
        resp = self.responses.pop(0) if self.responses else ""
        if isinstance(resp, Exception):
            raise resp
        return resp


@pytest.fixture
def graph(pw_dump_playing):
    return parse_dump(pw_dump_playing)


@pytest.fixture
def config():
    return Config(headphone_mac=HP_MAC, allowlist=SourceAllowlist(macs=(HP_MAC,)))


@pytest.mark.parametrize("req", [
    ListReq(),
    VolumeReq("B4:23:A2:01:6D:27", 0.5),
    MuteReq("phone", True),
    HeadphoneReq("AA:BB:CC:DD:EE:FF"),
    StatusReq(),
    GraphReq(),
])
def test_request_roundtrip(req):
    assert decode_request(encode_request(req)) == req


def test_response_roundtrip():
    resps = [
        ListResp((ipc.SourceInfo("B4:23:A2:01:6D:27", "bluez_input.x", 95, 0.56, False),)),
        OkResp("done"),
        StatusResp("B4:23:A2:01:6D:27", True, None, "aac", "single", None, None, None, False),
        GraphResp("a -> b"),
        ErrorResp("nope"),
    ]
    for r in resps:
        assert decode_response(encode_response(r)) == r


def test_list_reports_bluez_source(graph, config):
    run = FakeRunner(["Volume: 0.56\n"])
    resp = handler(ListReq(), config, graph, RuntimeState(), run=run)
    assert isinstance(resp, ListResp)
    assert len(resp.sources) == 1
    src = resp.sources[0]
    assert src.mac == "B4:23:A2:01:6D:27"
    assert src.node_id == 95
    assert src.volume == 0.56
    assert src.muted is False
    assert run.calls == [["wpctl", "get-volume", "95"]]


def test_list_reports_muted(graph, config):
    run = FakeRunner(["Volume: 0.30 [MUTED]\n"])
    resp = handler(ListReq(), config, graph, RuntimeState(), run=run)
    assert resp.sources[0].muted is True


def test_volume_issues_wpctl(graph, config):
    run = FakeRunner([""])
    resp = handler(VolumeReq("B4:23:A2:01:6D:27", 0.42), config, graph, RuntimeState(), run=run)
    assert isinstance(resp, OkResp)
    assert run.calls == [["wpctl", "set-volume", "95", "0.42"]]


def test_volume_unknown_source(graph, config):
    run = FakeRunner()
    resp = handler(VolumeReq("ZZ", 0.5), config, graph, RuntimeState(), run=run)
    assert isinstance(resp, ErrorResp)
    assert run.calls == []


def test_mute_issues_wpctl(graph, config):
    run = FakeRunner([""])
    resp = handler(MuteReq("B4:23:A2:01:6D:27", True), config, graph, RuntimeState(), run=run)
    assert isinstance(resp, OkResp)
    assert run.calls == [["wpctl", "set-mute", "95", "1"]]


def test_status_single_mode_unmeasured_latency(graph, config):
    resp = handler(StatusReq(), config, graph, RuntimeState(), run=FakeRunner())
    assert isinstance(resp, StatusResp)
    assert resp.adapter_mode == "single"
    assert resp.sink_hci is None and resp.source_hci is None
    assert resp.latency_ms is None
    assert resp.out_codec == "aac"
    assert resp.connected is True
    assert resp.host_audio is False


def test_status_dual_mode_reports_hci(graph):
    cfg = Config(
        headphone_mac=HP_MAC,
        allowlist=SourceAllowlist(macs=(HP_MAC,)),
        adapters=AdapterMap("hci0", "hci1"),
    )
    resp = handler(StatusReq(), cfg, graph, RuntimeState(), run=FakeRunner())
    assert resp.adapter_mode == "dual"
    assert resp.sink_hci == "hci0"
    assert resp.source_hci == "hci1"


def test_headphone_updates_state(graph, config):
    state = RuntimeState()
    resp = handler(HeadphoneReq("aa:bb:cc:dd:ee:ff"), config, graph, state, run=FakeRunner())
    assert isinstance(resp, OkResp)
    assert state.headphone_mac == "AA:BB:CC:DD:EE:FF"


def test_headphone_rejects_bad_mac(graph, config):
    resp = handler(HeadphoneReq("not-a-mac"), config, graph, RuntimeState(), run=FakeRunner())
    assert isinstance(resp, ErrorResp)


def test_graph_renders_topology(graph, config):
    resp = handler(GraphReq(), config, graph, RuntimeState(), run=FakeRunner())
    assert isinstance(resp, GraphResp)
    assert "->" in resp.topology


def test_socket_roundtrip(graph, config, tmp_path):
    sock = str(tmp_path / "t.sock")

    async def go():
        server = await ipc.serve_ipc(config, lambda: graph, RuntimeState(), path=sock, run=FakeRunner())
        resp = await ipc.request_async(GraphReq(), path=sock)
        server.close()
        await server.wait_closed()
        return resp

    resp = asyncio.run(go())
    assert isinstance(resp, GraphResp)
