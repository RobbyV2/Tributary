import json
import logging
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType

logger = logging.getLogger("tributary.audio.pipewire")

DEFAULT_SINK_NAME = "tributary_mix"
DEFAULT_TIMEOUT = 5.0
DEFAULT_ATTEMPTS = 2
_BLUEZ_INPUT_RE = re.compile(r"^bluez_input\.")


class PwError(Exception):
    pass


class PwParseError(PwError):
    pass


class PwBinaryNotFound(PwError):
    def __init__(self, binary: str, argv: Sequence[str]) -> None:
        self.binary = binary
        self.argv = tuple(argv)
        super().__init__(f"pipewire binary not found: {binary!r} (argv={self.argv})")


class PwCommandError(PwError):
    def __init__(self, argv: Sequence[str], returncode: int | None, stderr: str) -> None:
        self.argv = tuple(argv)
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"command failed (rc={returncode}): {self.argv}\n{stderr}")


class PwTimeoutError(PwCommandError):
    pass


class Direction(Enum):
    IN = "in"
    OUT = "out"


@dataclass(frozen=True, slots=True)
class Port:
    id: int
    node_id: int | None
    name: str | None
    direction: Direction
    channel: str | None
    monitor: bool
    path: str | None
    serial: int | None
    props: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class Node:
    id: int
    serial: int | None
    name: str | None
    description: str | None
    media_class: str | None
    device_id: int | None
    factory_name: str | None
    props: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class Link:
    id: int
    output_node: int | None
    output_port: int | None
    input_node: int | None
    input_port: int | None
    serial: int | None
    props: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class Graph:
    nodes: tuple[Node, ...]
    ports: tuple[Port, ...]
    links: tuple[Link, ...]
    default_sink_name: str | None = None
    node_by_id: Mapping[int, Node] = field(init=False, repr=False, compare=False)
    port_by_id: Mapping[int, Port] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_by_id", MappingProxyType({n.id: n for n in self.nodes}))
        object.__setattr__(self, "port_by_id", MappingProxyType({p.id: p for p in self.ports}))

    def node(self, node_id: int) -> Node | None:
        return self.node_by_id.get(node_id)

    def port(self, port_id: int) -> Port | None:
        return self.port_by_id.get(port_id)

    def ports_of(self, node_id: int) -> tuple[Port, ...]:
        return tuple(p for p in self.ports if p.node_id == node_id)

    def output_ports(self, node_id: int) -> tuple[Port, ...]:
        return tuple(p for p in self.ports_of(node_id) if p.direction is Direction.OUT)

    def input_ports(self, node_id: int) -> tuple[Port, ...]:
        return tuple(p for p in self.ports_of(node_id) if p.direction is Direction.IN)

    def monitor_ports(self, node_id: int) -> tuple[Port, ...]:
        return tuple(p for p in self.output_ports(node_id) if p.monitor is True)

    def find_nodes(self, media_class: str | None = None, name_regex: str | re.Pattern[str] | None = None) -> tuple[Node, ...]:
        pattern = re.compile(name_regex) if isinstance(name_regex, str) else name_regex

        def keep(n: Node) -> bool:
            if media_class is not None and n.media_class != media_class:
                return False
            if pattern is not None and (n.name is None or pattern.search(n.name) is None):
                return False
            return True

        return tuple(n for n in self.nodes if keep(n))

    def sources(self) -> tuple[Node, ...]:
        def rank(n: Node) -> int:
            a2dp = n.props.get("api.bluez5.profile") == "a2dp-source" or n.factory_name == "api.bluez5.a2dp.source"
            return 0 if a2dp else 1

        cands = [
            n for n in self.nodes
            if n.name is not None and _BLUEZ_INPUT_RE.search(n.name)
            and (n.media_class is None or "Stream/Input/Audio/Internal" not in n.media_class)
        ]
        by_dev: dict[int, list[Node]] = {}
        for n in cands:
            by_dev.setdefault(n.device_id if n.device_id is not None else n.id, []).append(n)
        return tuple(min(g, key=rank) for g in by_dev.values())

    def sink_by_name(self, name: str) -> Node | None:
        return next((n for n in self.nodes if n.name == name), None)

    def default_sink(self) -> Node | None:
        return self.sink_by_name(self.default_sink_name) if self.default_sink_name is not None else None

    def device_siblings(self, node_id: int) -> tuple[Node, ...]:
        node = self.node(node_id)
        if node is None or node.device_id is None:
            return ()
        return tuple(n for n in self.nodes if n.id != node_id and n.device_id == node.device_id)

    def link_between(self, out_port: int, in_port: int) -> Link | None:
        return next((lk for lk in self.links if lk.output_port == out_port and lk.input_port == in_port), None)

    def links_into(self, port_id: int) -> tuple[Link, ...]:
        return tuple(lk for lk in self.links if lk.input_port == port_id)

    def links_from(self, port_id: int) -> tuple[Link, ...]:
        return tuple(lk for lk in self.links if lk.output_port == port_id)

    def links_of(self, node_id: int) -> tuple[Link, ...]:
        return tuple(lk for lk in self.links if lk.output_node == node_id or lk.input_node == node_id)


def _direction(info: Mapping[str, object], props: Mapping[str, object]) -> Direction | None:
    d = info.get("direction")
    if d == "input":
        return Direction.IN
    if d == "output":
        return Direction.OUT
    d = props.get("port.direction")
    if d == "in":
        return Direction.IN
    if d == "out":
        return Direction.OUT
    return None


def parse_dump(data: str | bytes | list) -> Graph:
    if isinstance(data, list):
        items: object = data
    else:
        try:
            items = json.loads(data)
        except json.JSONDecodeError as e:
            raise PwParseError(f"invalid pw-dump JSON: {e}") from e
    if not isinstance(items, list):
        raise PwParseError(f"pw-dump root is {type(items).__name__}, expected list")
    nodes: list[Node] = []
    ports: list[Port] = []
    links: list[Link] = []
    default_sink_name: str | None = None
    for obj in items:
        t = obj.get("type")
        if t == "PipeWire:Interface:Metadata":
            for entry in obj.get("metadata") or ():
                if entry.get("key") == "default.audio.sink":
                    value = entry.get("value")
                    if isinstance(value, Mapping):
                        name = value.get("name")
                        if isinstance(name, str):
                            default_sink_name = name
            continue
        oid = obj.get("id")
        if not isinstance(oid, int):
            continue
        info = obj.get("info") or {}
        props = info.get("props") or {}
        match t:
            case "PipeWire:Interface:Node":
                nodes.append(Node(
                    id=oid,
                    serial=props.get("object.serial"),
                    name=props.get("node.name"),
                    description=props.get("node.description"),
                    media_class=props.get("media.class"),
                    device_id=props.get("device.id"),
                    factory_name=props.get("factory.name"),
                    props=MappingProxyType(dict(props)),
                ))
            case "PipeWire:Interface:Port":
                direction = _direction(info, props)
                if direction is None:
                    logger.debug("port %s missing direction; skipped", oid)
                    continue
                ports.append(Port(
                    id=oid,
                    node_id=props.get("node.id"),
                    name=props.get("port.name"),
                    direction=direction,
                    channel=props.get("audio.channel"),
                    monitor=bool(props.get("port.monitor", False)),
                    path=props.get("object.path"),
                    serial=props.get("object.serial"),
                    props=MappingProxyType(dict(props)),
                ))
            case "PipeWire:Interface:Link":
                output_node = info.get("output-node-id")
                if output_node is None:
                    output_node = props.get("link.output.node")
                output_port = info.get("output-port-id")
                if output_port is None:
                    output_port = props.get("link.output.port")
                input_node = info.get("input-node-id")
                if input_node is None:
                    input_node = props.get("link.input.node")
                input_port = info.get("input-port-id")
                if input_port is None:
                    input_port = props.get("link.input.port")
                links.append(Link(
                    id=oid,
                    output_node=output_node,
                    output_port=output_port,
                    input_node=input_node,
                    input_port=input_port,
                    serial=props.get("object.serial"),
                    props=MappingProxyType(dict(props)),
                ))
            case _:
                continue
    return Graph(tuple(nodes), tuple(ports), tuple(links), default_sink_name)


Runner = Callable[[Sequence[str]], str]


def _run(
    argv: Sequence[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    attempts: int = DEFAULT_ATTEMPTS,
    backoff: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    last: PwError | None = None
    for attempt in range(attempts):
        try:
            proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, check=False)
        except FileNotFoundError as e:
            raise PwBinaryNotFound(argv[0], argv) from e
        except subprocess.TimeoutExpired as e:
            last = PwTimeoutError(argv, None, e.stderr or "")
        else:
            if proc.returncode == 0:
                return proc.stdout
            last = PwCommandError(argv, proc.returncode, proc.stderr)
        if attempt + 1 < attempts:
            sleep(backoff * (attempt + 1))
    raise last  # attempts>=1 guarantees last is set


def dump(*, run: Runner = _run) -> Graph:
    return parse_dump(run(["pw-dump"]))


_VOLUME_RE = re.compile(r"Volume:\s*([0-9]+(?:\.[0-9]+)?)(\s*\[MUTED\])?")


@dataclass(frozen=True, slots=True)
class Volume:
    level: float
    muted: bool


def get_volume(node_id: int, *, run: Runner = _run) -> Volume:
    out = run(["wpctl", "get-volume", str(node_id)])
    m = _VOLUME_RE.search(out)
    if m is None:
        raise PwParseError(f"cannot parse wpctl volume: {out!r}")
    return Volume(float(m.group(1)), m.group(2) is not None)


def set_volume(node_id: int, level: float, *, run: Runner = _run) -> None:
    run(["wpctl", "set-volume", str(node_id), f"{level:.2f}"])


def set_mute(node_id: int, mute: bool, *, run: Runner = _run) -> None:
    run(["wpctl", "set-mute", str(node_id), "1" if mute else "0"])


def create_null_sink(name: str = DEFAULT_SINK_NAME, *, run: Runner = _run) -> Node:
    existing = dump(run=run).sink_by_name(name)
    if existing is not None:
        return existing
    blob = f"{{ factory.name=support.null-audio-sink node.name={name} media.class=Audio/Sink object.linger=true audio.position=[ FL FR ] node.description={name} }}"
    try:
        run(["pw-cli", "create-node", "adapter", blob])
    except PwCommandError:
        pass
    n = dump(run=run).sink_by_name(name)
    if n is not None:
        return n
    try:
        run(["pactl", "load-module", "module-null-sink", f"sink_name={name}", "channel_map=front-left,front-right", f"sink_properties=node.description={name}"])
    except PwCommandError:
        pass
    n = dump(run=run).sink_by_name(name)
    if n is not None:
        return n
    raise PwError(f"could not create null sink {name!r}")


def destroy_null_sink(name: str = DEFAULT_SINK_NAME, *, run: Runner = _run) -> None:
    node = dump(run=run).sink_by_name(name)
    if node is None:
        return
    try:
        run(["pw-cli", "destroy", str(node.id)])
    except PwCommandError:
        pass
    if dump(run=run).sink_by_name(name) is None:
        return
    idx = _pactl_null_sink_module(name, run)
    if idx is not None:
        try:
            run(["pactl", "unload-module", idx])
        except PwCommandError:
            pass
    if dump(run=run).sink_by_name(name) is None:
        return
    raise PwError(f"could not destroy null sink {name!r}")


def _pactl_null_sink_module(name: str, run: Runner) -> str | None:
    out = run(["pactl", "list", "short", "modules"])
    for line in out.splitlines():
        if "module-null-sink" in line and f"sink_name={name}" in line:
            return line.split()[0]
    return None


def link_ports(out_node: int, out_port: int, in_node: int, in_port: int, *, run: Runner = _run) -> Link:
    existing = dump(run=run).link_between(out_port, in_port)
    if existing is not None:
        return existing
    try:
        run(["pw-link", str(out_port), str(in_port)])
    except PwCommandError:
        pass
    lk = dump(run=run).link_between(out_port, in_port)
    if lk is not None:
        return lk
    try:
        run(["pw-cli", "create-link", str(out_node), str(out_port), str(in_node), str(in_port)])
    except PwCommandError:
        pass
    lk = dump(run=run).link_between(out_port, in_port)
    if lk is not None:
        return lk
    raise PwError(f"could not link {out_port}->{in_port}")


def unlink(out_port: int, in_port: int, *, run: Runner = _run) -> None:
    if dump(run=run).link_between(out_port, in_port) is None:
        return
    try:
        run(["pw-link", "-d", str(out_port), str(in_port)])
    except PwCommandError:
        pass
    lk = dump(run=run).link_between(out_port, in_port)
    if lk is None:
        return
    try:
        run(["pw-cli", "destroy", str(lk.id)])
    except PwCommandError:
        pass
    if dump(run=run).link_between(out_port, in_port) is None:
        return
    raise PwError(f"could not unlink {out_port}->{in_port}")
