import logging
from dataclasses import dataclass

from tributary.audio import pipewire as pw
from tributary.audio.pipewire import (
    DEFAULT_SINK_NAME,
    Graph,
    Link,
    Node,
    Port,
    PwError,
    Runner,
    _run,
    dump,
    link_ports,
)
from tributary.bluetooth.headphones import find_sink_node
from tributary.config import Config, Mac

logger = logging.getLogger("tributary.audio.mixer")


def _chan(port: Port) -> str:
    return port.channel if port.channel is not None else "MONO"


def _by_chan(ports: tuple[Port, ...]) -> dict[str, Port]:
    return {_chan(p): p for p in ports}


def fan_ports(outs: dict[str, Port], ins: dict[str, Port]) -> tuple[tuple[Port, Port], ...]:
    match (set(outs) == {"MONO"}, set(ins) == {"MONO"}):
        case (True, True):
            return ((outs["MONO"], ins["MONO"]),)
        case (True, False):
            return tuple((outs["MONO"], ip) for ip in ins.values())
        case (False, True):
            return tuple((op, ins["MONO"]) for op in outs.values())
        case _:
            return tuple((outs[c], ins[c]) for c in outs if c in ins)


def ensure_bus(name: str = DEFAULT_SINK_NAME, *, run: Runner = _run) -> Node:
    return pw.create_null_sink(name, run=run)


def teardown(name: str = DEFAULT_SINK_NAME, *, run: Runner = _run) -> None:
    node = pw.dump(run=run).sink_by_name(name)
    if node is not None:
        for lk in pw.dump(run=run).links_of(node.id):
            match (lk.output_port, lk.input_port):
                case (int() as op, int() as ip):
                    pw.unlink(op, ip, run=run)
                case _:
                    continue
    pw.destroy_null_sink(name, run=run)


def input_ports(graph: Graph, mix_node: Node) -> dict[str, Port]:
    return _by_chan(graph.input_ports(mix_node.id))


def monitor_ports(graph: Graph, mix_node: Node) -> dict[str, Port]:
    return _by_chan(graph.monitor_ports(mix_node.id))


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    bus: Node | None
    headphone_present: bool
    headphone_links: tuple[Link, ...]
    error: PwError | None = None


@dataclass(frozen=True, slots=True)
class Mixer:
    headphone_mac: str
    sink_name: str = DEFAULT_SINK_NAME
    run: Runner = _run

    @classmethod
    def from_config(cls, cfg: Config, *, run: Runner = _run, sink_name: str = DEFAULT_SINK_NAME) -> "Mixer":
        return cls(headphone_mac=str(cfg.headphone_mac), sink_name=sink_name, run=run)

    def bus(self, graph: Graph) -> Node | None:
        return graph.sink_by_name(self.sink_name)

    def ensure_bus(self, graph: Graph | None = None) -> Node:
        if graph is not None:
            existing = graph.sink_by_name(self.sink_name)
            if existing is not None:
                return existing
        return ensure_bus(self.sink_name, run=self.run)

    def ensure_headphone_link(self, graph: Graph | None = None) -> tuple[Link, ...]:
        g = graph if graph is not None else dump(run=self.run)
        bus = g.sink_by_name(self.sink_name)
        if bus is None:
            logger.debug("ensure_headphone_link: bus %r absent", self.sink_name)
            return ()
        hp = find_sink_node(g, Mac(self.headphone_mac))
        if hp is None:
            logger.debug("ensure_headphone_link: headphone %s absent", self.headphone_mac)
            return ()
        out: list[Link] = []
        for op, ip in fan_ports(monitor_ports(g, bus), input_ports(g, hp)):
            try:
                out.append(link_ports(bus.id, op.id, hp.id, ip.id, run=self.run))
            except PwError as e:
                logger.warning("ensure_headphone_link: link %s->%s failed: %s", op.id, ip.id, e)
        return tuple(out)

    def reconcile(self, graph: Graph | None = None) -> ReconcileResult:
        g = graph if graph is not None else dump(run=self.run)
        had_bus = g.sink_by_name(self.sink_name) is not None
        try:
            bus = self.ensure_bus(g)
        except PwError as e:
            logger.error("reconcile: ensure_bus failed: %s", e)
            return ReconcileResult(None, False, (), e)
        g2 = g if had_bus else dump(run=self.run)
        hp = find_sink_node(g2, Mac(self.headphone_mac))
        links = self.ensure_headphone_link(g2)
        return ReconcileResult(bus, hp is not None, links, None)

    def teardown(self) -> None:
        try:
            teardown(self.sink_name, run=self.run)
        except PwError as e:
            logger.error("teardown: failed: %s", e)
