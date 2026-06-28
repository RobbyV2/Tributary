import enum
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from tributary.config import Config, Mac, ResolvedAdapters, SourceAllowlist

logger = logging.getLogger("tributary.bluetooth.adapter")

BLUEZ_SERVICE = "org.bluez"
BLUEZ_BASE = "/org/bluez"
ADAPTER_IFACE = "org.bluez.Adapter1"
PROPS_IFACE = "org.freedesktop.DBus.Properties"

_DISCOVERABLE_WRITES: tuple[tuple[str, object], ...] = (
    ("Powered", True),
    ("Pairable", True),
    ("Discoverable", True),
    ("PairableTimeout", 0),
    ("DiscoverableTimeout", 0),
)


class AdapterError(Exception):
    pass


class Role(enum.Enum):
    SINK = "sink"
    SOURCE = "source"


def hci_path(name: str) -> str:
    return f"{BLUEZ_BASE}/{name}"


@dataclass(frozen=True, slots=True)
class AdapterRoles:
    sink_path: str
    source_path: str
    single: bool

    def path_for(self, role: Role) -> str:
        match role:
            case Role.SINK:
                return self.sink_path
            case Role.SOURCE:
                return self.source_path

    def paths(self) -> tuple[str, ...]:
        return (self.sink_path,) if self.single else (self.sink_path, self.source_path)


def resolve_roles(resolved: ResolvedAdapters) -> AdapterRoles:
    return AdapterRoles(hci_path(resolved.sink_adapter), hci_path(resolved.source_adapter), resolved.single)


@dataclass(frozen=True, slots=True)
class AdapterState:
    powered: bool
    pairable: bool
    discoverable: bool


class AdapterProxy(Protocol):
    async def get(self, path: str, name: str) -> object: ...
    async def set(self, path: str, name: str, value: object) -> None: ...


@dataclass(frozen=True, slots=True)
class AdapterManager:
    roles: AdapterRoles
    allowlist: SourceAllowlist
    proxy: AdapterProxy

    @classmethod
    def from_config(cls, config: Config, available: Sequence[str], proxy: AdapterProxy) -> "AdapterManager":
        return cls(resolve_roles(config.resolve_adapters(available)), config.allowlist, proxy)

    async def ensure_discoverable(self, role: Role = Role.SINK) -> None:
        path = self.roles.path_for(role)
        for name, value in _DISCOVERABLE_WRITES:
            await self.proxy.set(path, name, value)

    async def state(self, role: Role = Role.SINK) -> AdapterState:
        path = self.roles.path_for(role)
        g = self.proxy.get
        return AdapterState(bool(await g(path, "Powered")), bool(await g(path, "Pairable")), bool(await g(path, "Discoverable")))

    def is_allowed(self, mac: Mac, name: str = "") -> bool:
        return self.allowlist.admits(mac, name)


class DbusAdapterProxy:
    def __init__(self, bus: object, variant: type) -> None:
        self._bus = bus
        self._Variant = variant
        self._ifaces: dict[str, object] = {}

    @classmethod
    async def connect(cls) -> "DbusAdapterProxy":
        from dbus_next import Variant
        from dbus_next.aio import MessageBus
        from dbus_next.constants import BusType

        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        return cls(bus, Variant)

    async def _iface(self, path: str) -> object:
        cached = self._ifaces.get(path)
        if cached is not None:
            return cached
        node = await self._bus.introspect(BLUEZ_SERVICE, path)
        obj = self._bus.get_proxy_object(BLUEZ_SERVICE, path, node)
        iface = obj.get_interface(PROPS_IFACE)
        self._ifaces[path] = iface
        return iface

    async def get(self, path: str, name: str) -> object:
        iface = await self._iface(path)
        return (await iface.call_get(ADAPTER_IFACE, name)).value

    async def set(self, path: str, name: str, value: object) -> None:
        if name == "Class":
            raise AdapterError("Adapter1.Class is read-only; configure via /etc/bluetooth/main.conf")
        iface = await self._iface(path)
        await iface.call_set(ADAPTER_IFACE, name, self._wrap(value))

    def _wrap(self, value: object) -> object:
        match value:
            case bool():
                return self._Variant("b", value)
            case int():
                return self._Variant("u", value)
            case _:
                raise AdapterError(f"unsupported property value {value!r}")
