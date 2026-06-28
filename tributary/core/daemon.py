import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from tributary.audio import router
from tributary.audio.mixer import Mixer
from tributary.audio.pipewire import DEFAULT_SINK_NAME, Graph, Link, Node, Runner, _run, dump
from tributary.audio.router import Delta, mac_of, select_bluez_sources, select_host_source
from tributary.bluetooth.adapter import AdapterManager, Role
from tributary.bluetooth.headphones import BACKOFF_CAP, HeadphoneError, HeadphoneManager, backoff_delay, find_sink_node
from tributary.config import Config, Mac
from tributary.control.ipc import RuntimeState, serve_ipc
from tributary.core.events import BluezDevice, Snapshot, normalize

logger = logging.getLogger("tributary.core.daemon")


@dataclass(frozen=True, slots=True)
class TickResult:
    delta: Delta
    bus: Node | None
    bus_created: bool
    headphone_present: bool
    headphone_links: tuple[Link, ...]


def _admitted_sources(graph: Graph, config: Config, bluez_objects: Mapping[Mac, BluezDevice]) -> tuple[Node, ...]:
    known = set(bluez_objects)
    bluez = tuple(n for n in select_bluez_sources(graph, config.allowlist) if mac_of(n) in known)
    return bluez + select_host_source(graph, config)


def tick(
    graph: Graph,
    bluez_objects: Mapping[Mac, BluezDevice],
    config: Config,
    mix_node_name: str = DEFAULT_SINK_NAME,
    *,
    run: Runner = _run,
    headphone_mac: Mac | None = None,
) -> TickResult:
    hp_mac = headphone_mac or config.headphone_mac
    mx = Mixer(headphone_mac=str(hp_mac), sink_name=mix_node_name, run=run)
    had_bus = graph.sink_by_name(mix_node_name) is not None
    created = mx.ensure_bus(graph)
    g = graph if had_bus else dump(run=run)
    bus = g.sink_by_name(mix_node_name) or created
    sources = _admitted_sources(g, config, bluez_objects)
    delta = router.reconcile(g, bus, sources, run=run)
    headphone = find_sink_node(g, hp_mac)
    headphone_links = mx.ensure_headphone_link(g)
    return TickResult(delta, bus, not had_bus, headphone is not None, headphone_links)


@dataclass(slots=True)
class Daemon:
    config: Config
    headphones: HeadphoneManager
    fetch_graph: Callable[[], Graph]
    fetch_bluez: Callable[[], Awaitable[Mapping[Mac, BluezDevice]]]
    adapters: AdapterManager | None = None
    run: Runner = _run
    sink_name: str = DEFAULT_SINK_NAME
    state: RuntimeState = field(default_factory=RuntimeState)
    _prev: Snapshot | None = field(default=None, init=False)

    async def tick_once(self) -> TickResult:
        snap = Snapshot(self.fetch_graph(), await self.fetch_bluez())
        for event in normalize(self._prev, snap):
            logger.info("event %s", event)
        self._prev = snap
        return tick(snap.graph, snap.devices, self.config, self.sink_name, run=self.run, headphone_mac=self.state.headphone_mac)

    async def _connect_loop(self) -> None:
        import asyncio

        attempt = 0
        while True:
            try:
                connected = not await self.headphones.reconcile()
            except HeadphoneError as e:
                logger.warning("headphone connect: %s", e)
                connected = False
            if connected:
                attempt = 0
                await asyncio.sleep(self.config.reconcile_interval)
            else:
                await asyncio.sleep(backoff_delay(attempt))
                attempt = min(attempt + 1, int(BACKOFF_CAP))

    async def serve(self) -> None:
        import asyncio
        import contextlib

        match self.adapters:
            case None:
                pass
            case AdapterManager() as am:
                await am.ensure_discoverable(Role.SINK)
                if not am.roles.single:
                    await am.ensure_discoverable(Role.SOURCE)
        connect = asyncio.create_task(self._connect_loop())
        ipc = await serve_ipc(self.config, self.fetch_graph, self.state, run=self.run)
        try:
            while True:
                await self.tick_once()
                await asyncio.sleep(self.config.reconcile_interval)
        finally:
            connect.cancel()
            with contextlib.suppress(BaseException):
                ipc.close()
                await ipc.wait_closed()
            with contextlib.suppress(BaseException):
                await connect
            Mixer.from_config(self.config, run=self.run, sink_name=self.sink_name).teardown()


def _available_adapters() -> tuple[str, ...]:
    base = Path("/sys/class/bluetooth")
    if not base.exists():
        return ()
    return tuple(sorted(d.name for d in base.iterdir() if d.name.startswith("hci")))


async def _bluez_fetcher() -> Callable[[], Awaitable[dict[Mac, BluezDevice]]]:
    from dbus_next.aio import MessageBus
    from dbus_next.constants import BusType

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    node = await bus.introspect("org.bluez", "/")
    om = bus.get_proxy_object("org.bluez", "/", node).get_interface("org.freedesktop.DBus.ObjectManager")

    async def fetch() -> dict[Mac, BluezDevice]:
        out: dict[Mac, BluezDevice] = {}
        for _, ifaces in (await om.call_get_managed_objects()).items():
            dev = ifaces.get("org.bluez.Device1")
            if dev is None:
                continue
            addr, conn, uuids = dev.get("Address"), dev.get("Connected"), dev.get("UUIDs")
            if addr is None or conn is None:
                continue
            mac = Mac(str(addr.value).upper())
            out[mac] = BluezDevice(mac, bool(conn.value), tuple(uuids.value) if uuids is not None else ())
        return out

    return fetch


async def serve_system(config: Config, *, run: Runner = _run) -> None:
    from tributary.bluetooth.adapter import DbusAdapterProxy
    from tributary.bluetooth.agent import run_system_agent
    from tributary.bluetooth.headphones import DbusDeviceProxy

    adapters = AdapterManager.from_config(config, _available_adapters(), await DbusAdapterProxy.connect())
    headphones = HeadphoneManager.from_config(config, adapters.roles.source_path, await DbusDeviceProxy.connect())
    await run_system_agent()
    daemon = Daemon(config, headphones, lambda: dump(run=run), await _bluez_fetcher(), adapters, run)
    await daemon.serve()


def main() -> None:
    import asyncio
    import sys

    from tributary.config import ConfigError, load_config

    path = sys.argv[1] if len(sys.argv) > 1 else "/etc/tributary/config.toml"
    try:
        config = load_config(path)
    except ConfigError as e:
        logger.error("config: %s", e)
        raise SystemExit(1) from None
    try:
        asyncio.run(serve_system(config))
    except KeyboardInterrupt:
        raise SystemExit(0) from None


if __name__ == "__main__":
    main()
