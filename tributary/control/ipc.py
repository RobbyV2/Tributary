import asyncio
import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from tributary.audio import pipewire as pw
from tributary.audio.pipewire import Graph, Node, Port, PwError, Runner, _run
from tributary.bluetooth.headphones import find_sink_node
from tributary.audio.router import mac_of, select_bluez_sources, select_host_source
from tributary.config import AdapterMap, Config, ConfigError, Mac, parse_mac


class IpcError(Exception):
    pass


def socket_path() -> str:
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return str(Path(base) / "tributary.sock")


@dataclass(slots=True)
class RuntimeState:
    headphone_mac: Mac | None = None
    latency_ms: float | None = None


@dataclass(frozen=True, slots=True)
class ListReq:
    pass


@dataclass(frozen=True, slots=True)
class VolumeReq:
    source: str
    level: float


@dataclass(frozen=True, slots=True)
class MuteReq:
    source: str
    mute: bool


@dataclass(frozen=True, slots=True)
class HeadphoneReq:
    mac: str


@dataclass(frozen=True, slots=True)
class StatusReq:
    pass


@dataclass(frozen=True, slots=True)
class GraphReq:
    pass


Request = ListReq | VolumeReq | MuteReq | HeadphoneReq | StatusReq | GraphReq


@dataclass(frozen=True, slots=True)
class SourceInfo:
    mac: str | None
    name: str | None
    node_id: int
    volume: float
    muted: bool


@dataclass(frozen=True, slots=True)
class ListResp:
    sources: tuple[SourceInfo, ...]


@dataclass(frozen=True, slots=True)
class OkResp:
    detail: str


@dataclass(frozen=True, slots=True)
class StatusResp:
    headphone_mac: str
    connected: bool
    in_codec: str | None
    out_codec: str | None
    adapter_mode: str
    sink_hci: str | None
    source_hci: str | None
    latency_ms: float | None
    host_audio: bool


@dataclass(frozen=True, slots=True)
class GraphResp:
    topology: str


@dataclass(frozen=True, slots=True)
class ErrorResp:
    message: str


Response = ListResp | OkResp | StatusResp | GraphResp | ErrorResp


def encode_request(req: Request) -> str:
    match req:
        case ListReq():
            body: dict[str, object] = {"cmd": "list"}
        case VolumeReq(source=s, level=lvl):
            body = {"cmd": "volume", "source": s, "level": lvl}
        case MuteReq(source=s, mute=m):
            body = {"cmd": "mute", "source": s, "mute": m}
        case HeadphoneReq(mac=mac):
            body = {"cmd": "headphone", "mac": mac}
        case StatusReq():
            body = {"cmd": "status"}
        case GraphReq():
            body = {"cmd": "graph"}
    return json.dumps(body)


def decode_request(text: str) -> Request:
    try:
        d = json.loads(text)
    except json.JSONDecodeError as e:
        raise IpcError(f"bad request json: {e}") from e
    match d.get("cmd"):
        case "list":
            return ListReq()
        case "volume":
            return VolumeReq(str(d["source"]), float(d["level"]))
        case "mute":
            return MuteReq(str(d["source"]), bool(d["mute"]))
        case "headphone":
            return HeadphoneReq(str(d["mac"]))
        case "status":
            return StatusReq()
        case "graph":
            return GraphReq()
        case other:
            raise IpcError(f"unknown cmd {other!r}")


_TAG: dict[type, str] = {
    ListResp: "list",
    OkResp: "ok",
    StatusResp: "status",
    GraphResp: "graph",
    ErrorResp: "error",
}


def encode_response(resp: Response) -> str:
    return json.dumps({"kind": _TAG[type(resp)], **asdict(resp)})


def decode_response(text: str) -> Response:
    d = json.loads(text)
    body = {k: v for k, v in d.items() if k != "kind"}
    match d.get("kind"):
        case "list":
            return ListResp(tuple(SourceInfo(**x) for x in body["sources"]))
        case "ok":
            return OkResp(**body)
        case "status":
            return StatusResp(**body)
        case "graph":
            return GraphResp(**body)
        case "error":
            return ErrorResp(**body)
        case other:
            raise IpcError(f"unknown kind {other!r}")


def _str_prop(node: Node, key: str) -> str | None:
    value = node.props.get(key)
    return value if isinstance(value, str) else None


def _find_source(graph: Graph, config: Config, ident: str) -> Node | None:
    target = ident.upper()
    for n in select_bluez_sources(graph, config.allowlist):
        mac = mac_of(n)
        if (mac is not None and mac == target) or n.name == ident:
            return n
    return next((n for n in select_host_source(graph, config) if n.name == ident), None)


def _list(config: Config, graph: Graph, run: Runner) -> Response:
    out: list[SourceInfo] = []
    for n in select_bluez_sources(graph, config.allowlist):
        vol = pw.get_volume(n.id, run=run)
        out.append(SourceInfo(mac_of(n), n.name, n.id, vol.level, vol.muted))
    return ListResp(tuple(out))


def _apply_volume(config: Config, graph: Graph, source: str, level: float, run: Runner) -> Response:
    node = _find_source(graph, config, source)
    if node is None:
        return ErrorResp(f"no source {source!r}")
    try:
        pw.set_volume(node.id, level, run=run)
    except PwError as e:
        return ErrorResp(str(e))
    return OkResp(f"volume {source} = {level:.2f}")


def _apply_mute(config: Config, graph: Graph, source: str, mute: bool, run: Runner) -> Response:
    node = _find_source(graph, config, source)
    if node is None:
        return ErrorResp(f"no source {source!r}")
    try:
        pw.set_mute(node.id, mute, run=run)
    except PwError as e:
        return ErrorResp(str(e))
    return OkResp(f"mute {source} = {mute}")


def _status(config: Config, graph: Graph, state: RuntimeState) -> Response:
    mac = state.headphone_mac or config.headphone_mac
    hp = find_sink_node(graph, mac)
    out_codec = _str_prop(hp, "api.bluez5.codec") if hp is not None else None
    src = next(iter(select_bluez_sources(graph, config.allowlist)), None)
    in_codec = _str_prop(src, "api.bluez5.codec") if src is not None else None
    match config.adapters:
        case None:
            sink_hci: str | None = None
            source_hci: str | None = None
        case AdapterMap(sink_adapter=s, source_adapter=t):
            sink_hci, source_hci = s, t
    return StatusResp(
        str(mac), hp is not None, in_codec, out_codec, config.adapter_mode.value,
        sink_hci, source_hci, state.latency_ms, config.include_host_audio,
    )


def _node_name(graph: Graph, node_id: int | None) -> str:
    node = graph.node(node_id) if node_id is not None else None
    return node.name if node is not None and node.name is not None else f"#{node_id}"


def _port_name(graph: Graph, port_id: int | None) -> str:
    port = graph.port(port_id) if port_id is not None else None
    return port.name if isinstance(port, Port) and port.name is not None else f"#{port_id}"


def render_topology(graph: Graph) -> str:
    lines = [
        f"{_node_name(graph, lk.output_node)}:{_port_name(graph, lk.output_port)} -> "
        f"{_node_name(graph, lk.input_node)}:{_port_name(graph, lk.input_port)}"
        for lk in graph.links
    ]
    return "\n".join(lines)


def handler(request: Request, config: Config, graph: Graph, state: RuntimeState, *, run: Runner = _run) -> Response:
    match request:
        case ListReq():
            return _list(config, graph, run)
        case VolumeReq(source=s, level=lvl):
            return _apply_volume(config, graph, s, lvl, run)
        case MuteReq(source=s, mute=m):
            return _apply_mute(config, graph, s, m, run)
        case HeadphoneReq(mac=mac):
            try:
                state.headphone_mac = parse_mac(mac, field="headphone", source="ipc")
            except ConfigError as e:
                return ErrorResp(str(e))
            return OkResp(f"headphone -> {state.headphone_mac}")
        case StatusReq():
            return _status(config, graph, state)
        case GraphReq():
            return GraphResp(render_topology(graph))


async def serve_ipc(
    config: Config,
    dump_graph: Callable[[], Graph],
    state: RuntimeState,
    *,
    path: str | None = None,
    run: Runner = _run,
) -> asyncio.Server:
    sock = path or socket_path()
    Path(sock).unlink(missing_ok=True)

    async def on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = await reader.readline()
        if line:
            try:
                resp = handler(decode_request(line.decode()), config, dump_graph(), state, run=run)
            except (IpcError, KeyError, ValueError, PwError) as e:
                resp = ErrorResp(str(e))
            writer.write((encode_response(resp) + "\n").encode())
            await writer.drain()
        writer.close()
        await writer.wait_closed()

    return await asyncio.start_unix_server(on_client, path=sock)


async def request_async(req: Request, *, path: str | None = None) -> Response:
    reader, writer = await asyncio.open_unix_connection(path or socket_path())
    writer.write((encode_request(req) + "\n").encode())
    await writer.drain()
    line = await reader.readline()
    writer.close()
    await writer.wait_closed()
    return decode_response(line.decode())


def request(req: Request, *, path: str | None = None) -> Response:
    return asyncio.run(request_async(req, path=path))
