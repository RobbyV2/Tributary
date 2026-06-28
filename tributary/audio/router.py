import logging
import re
from dataclasses import dataclass

from tributary.audio import mixer
from tributary.audio import pipewire as pw
from tributary.audio.mixer import _by_chan, fan_ports
from tributary.audio.pipewire import Graph, Link, Node, PwError, Runner, _run
from tributary.config import Config, Mac, SourceAllowlist

logger = logging.getLogger("tributary.audio.router")

_MAC_RE = re.compile(r"[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}")


@dataclass(frozen=True, slots=True)
class LinkSpec:
    out_node: int
    out_port: int
    in_node: int
    in_port: int


@dataclass(frozen=True, slots=True)
class Delta:
    to_add: tuple[LinkSpec, ...]
    to_remove: tuple[Link, ...]


def mac_of(node: Node) -> Mac | None:
    addr = node.props.get("api.bluez5.address")
    for text in (addr if isinstance(addr, str) else None, node.name):
        match text:
            case str():
                m = _MAC_RE.search(text)
                if m is not None:
                    return Mac(m.group(0).upper())
            case _:
                continue
    return None


def select_bluez_sources(graph: Graph, allowlist: SourceAllowlist) -> tuple[Node, ...]:
    def keep(n: Node) -> bool:
        mac = mac_of(n)
        return mac is not None and n.name is not None and allowlist.admits(mac, n.name)

    return tuple(n for n in graph.sources() if keep(n))


def select_host_source(graph: Graph, config: Config) -> tuple[Node, ...]:
    if not config.include_host_audio:
        return ()
    node = graph.sink_by_name(config.host_source) if config.host_source is not None else graph.default_sink()
    return (node,) if node is not None else ()


def desired_links(graph: Graph, mix_node: Node, sources: tuple[Node, ...]) -> tuple[LinkSpec, ...]:
    ins = mixer.input_ports(graph, mix_node)
    specs: list[LinkSpec] = []
    for src in sources:
        if src.id == mix_node.id:
            continue
        outs = _by_chan(graph.output_ports(src.id))
        specs.extend(LinkSpec(src.id, op.id, mix_node.id, ip.id) for op, ip in fan_ports(outs, ins))
    return tuple(specs)


def actual_source_links(graph: Graph, mix_node: Node, sources: tuple[Node, ...] = ()) -> tuple[Link, ...]:
    in_ids = {p.id for p in graph.input_ports(mix_node.id)}
    return tuple(lk for lk in graph.links if lk.input_port in in_ids)


def diff(desired: tuple[LinkSpec, ...], actual: tuple[Link, ...]) -> Delta:
    have = {(lk.output_port, lk.input_port) for lk in actual}
    want = {(s.out_port, s.in_port) for s in desired}
    to_add = tuple(s for s in desired if (s.out_port, s.in_port) not in have)
    to_remove = tuple(lk for lk in actual if (lk.output_port, lk.input_port) not in want)
    return Delta(to_add, to_remove)


def reconcile(graph: Graph, mix_node: Node, sources: tuple[Node, ...], *, run: Runner = _run) -> Delta:
    delta = diff(desired_links(graph, mix_node, sources), actual_source_links(graph, mix_node, sources))
    added: list[LinkSpec] = []
    removed: list[Link] = []
    for s in delta.to_add:
        try:
            pw.link_ports(s.out_node, s.out_port, s.in_node, s.in_port, run=run)
            added.append(s)
        except PwError as e:
            logger.warning("link %d->%d failed: %s", s.out_port, s.in_port, e)
    for lk in delta.to_remove:
        match (lk.output_port, lk.input_port):
            case (int() as op, int() as ip):
                try:
                    pw.unlink(op, ip, run=run)
                    removed.append(lk)
                except PwError as e:
                    logger.warning("unlink %d->%d failed: %s", op, ip, e)
            case _:
                continue
    return Delta(tuple(added), tuple(removed))
