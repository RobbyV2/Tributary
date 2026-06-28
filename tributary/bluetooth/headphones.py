import logging
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Protocol, TypeVar

from tributary.audio.pipewire import Graph, Node
from tributary.config import Config, Mac

logger = logging.getLogger("tributary.bluetooth.headphones")

_T = TypeVar("_T")


def _is_transport_error(e: BaseException) -> bool:
    return type(e).__name__ == "DBusError" or isinstance(e, (ConnectionError, OSError, TimeoutError))

BLUEZ_SERVICE = "org.bluez"
DEVICE_IFACE = "org.bluez.Device1"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
A2DP_SINK_UUID = "0000110b-0000-1000-8000-00805f9b34fb"
SINK_CLASS = "Audio/Sink"
ADDRESS_PROP = "api.bluez5.address"

BACKOFF_BASE = 1.0
BACKOFF_FACTOR = 2.0
BACKOFF_CAP = 30.0


class HeadphoneError(Exception):
    pass


def device_path(adapter_path: str, mac: Mac) -> str:
    return f"{adapter_path}/dev_{mac.upper().replace(':', '_')}"


def backoff_delay(attempt: int) -> float:
    return min(BACKOFF_CAP, BACKOFF_BASE * BACKOFF_FACTOR ** max(attempt, 0))


def find_sink_node(graph: Graph, mac: Mac) -> Node | None:
    target = mac.upper()
    device_ids = {
        n.device_id
        for n in graph.nodes
        if n.device_id is not None and str(n.props.get(ADDRESS_PROP, "")).upper() == target
    }
    return next(
        (n for n in graph.nodes if n.media_class == SINK_CLASS and n.device_id in device_ids),
        None,
    )


class DeviceProxy(Protocol):
    async def connect_profile(self, path: str, uuid: str) -> None: ...
    async def disconnect_profile(self, path: str, uuid: str) -> None: ...
    async def connected(self, path: str) -> bool: ...
    async def uuids(self, path: str) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class HeadphoneManager:
    mac: Mac
    adapter_path: str
    proxy: DeviceProxy

    @classmethod
    def from_config(cls, config: Config, adapter_path: str, proxy: DeviceProxy) -> "HeadphoneManager":
        return cls(config.headphone_mac, adapter_path, proxy)

    @property
    def path(self) -> str:
        return device_path(self.adapter_path, self.mac)

    async def _guard(self, coro: Awaitable[_T]) -> _T:
        try:
            return await coro
        except HeadphoneError:
            raise
        except Exception as e:
            if _is_transport_error(e):
                raise HeadphoneError(f"{self.mac}: {e}") from e
            raise

    async def connect(self) -> None:
        if A2DP_SINK_UUID not in await self._guard(self.proxy.uuids(self.path)):
            raise HeadphoneError(f"{self.mac}: device lacks A2DP Sink UUID {A2DP_SINK_UUID}")
        await self._guard(self.proxy.connect_profile(self.path, A2DP_SINK_UUID))

    async def disconnect(self) -> None:
        await self.proxy.disconnect_profile(self.path, A2DP_SINK_UUID)

    async def is_connected(self) -> bool:
        return await self._guard(self.proxy.connected(self.path))

    async def reconcile(self) -> bool:
        if await self.is_connected():
            return False
        await self.connect()
        return True

    def sink_node(self, graph: Graph) -> Node | None:
        return find_sink_node(graph, self.mac)


class DbusDeviceProxy:
    def __init__(self, bus: object) -> None:
        self._bus = bus
        self._objs: dict[str, object] = {}

    @classmethod
    async def connect(cls) -> "DbusDeviceProxy":
        from dbus_next.aio import MessageBus
        from dbus_next.constants import BusType

        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        return cls(bus)

    async def _obj(self, path: str) -> object:
        cached = self._objs.get(path)
        if cached is not None:
            return cached
        node = await self._bus.introspect(BLUEZ_SERVICE, path)
        obj = self._bus.get_proxy_object(BLUEZ_SERVICE, path, node)
        self._objs[path] = obj
        return obj

    async def _device(self, path: str) -> object:
        return (await self._obj(path)).get_interface(DEVICE_IFACE)

    async def _props(self, path: str) -> object:
        return (await self._obj(path)).get_interface(PROPS_IFACE)

    async def connect_profile(self, path: str, uuid: str) -> None:
        await (await self._device(path)).call_connect_profile(uuid)

    async def disconnect_profile(self, path: str, uuid: str) -> None:
        await (await self._device(path)).call_disconnect_profile(uuid)

    async def connected(self, path: str) -> bool:
        return bool((await (await self._props(path)).call_get(DEVICE_IFACE, "Connected")).value)

    async def uuids(self, path: str) -> tuple[str, ...]:
        return tuple((await (await self._props(path)).call_get(DEVICE_IFACE, "UUIDs")).value)
