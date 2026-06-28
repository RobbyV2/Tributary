from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tributary.audio.pipewire import parse_dump
from tributary.bluetooth.headphones import (
    A2DP_SINK_UUID,
    BACKOFF_CAP,
    HeadphoneError,
    HeadphoneManager,
    backoff_delay,
    device_path,
    find_sink_node,
)
from tributary.config import Mac

MAC = Mac("B4:23:A2:01:6D:27")
ADAPTER = "/org/bluez/hci0"
DUMP = (Path(__file__).parents[2] / "fixtures/pipewire/pw-dump-playing.json").read_text()


@dataclass
class FakeProxy:
    connected_val: bool = False
    uuid_list: tuple[str, ...] = (A2DP_SINK_UUID,)
    calls: list[tuple[str, str, str]] = field(default_factory=list)

    async def connect_profile(self, path: str, uuid: str) -> None:
        self.calls.append(("connect", path, uuid))

    async def disconnect_profile(self, path: str, uuid: str) -> None:
        self.calls.append(("disconnect", path, uuid))

    async def connected(self, path: str) -> bool:
        return self.connected_val

    async def uuids(self, path: str) -> tuple[str, ...]:
        return self.uuid_list


def mgr(proxy: FakeProxy) -> HeadphoneManager:
    return HeadphoneManager(mac=MAC, adapter_path=ADAPTER, proxy=proxy)


class DBusError(Exception):
    pass


@dataclass
class FailingProxy:
    where: str
    uuid_list: tuple[str, ...] = (A2DP_SINK_UUID,)
    connected_val: bool = False

    async def connect_profile(self, path: str, uuid: str) -> None:
        if self.where == "connect":
            raise DBusError("br-connection-page-timeout")

    async def disconnect_profile(self, path: str, uuid: str) -> None:
        pass

    async def connected(self, path: str) -> bool:
        if self.where == "connected":
            raise DBusError("org.bluez.Error.Failed")
        return self.connected_val

    async def uuids(self, path: str) -> tuple[str, ...]:
        if self.where == "uuids":
            raise DBusError("io")
        return self.uuid_list


async def test_connect_translates_dbus_error():
    with pytest.raises(HeadphoneError):
        await mgr(FailingProxy(where="connect")).connect()  # type: ignore[arg-type]


async def test_reconcile_translates_connect_dbus_error():
    with pytest.raises(HeadphoneError):
        await mgr(FailingProxy(where="connect")).reconcile()  # type: ignore[arg-type]


async def test_reconcile_translates_connected_dbus_error():
    with pytest.raises(HeadphoneError):
        await mgr(FailingProxy(where="connected")).reconcile()  # type: ignore[arg-type]


def test_device_path():
    assert device_path(ADAPTER, MAC) == "/org/bluez/hci0/dev_B4_23_A2_01_6D_27"


async def test_connect_uses_a2dp_sink_uuid():
    proxy = FakeProxy()
    await mgr(proxy).connect()
    assert proxy.calls == [("connect", "/org/bluez/hci0/dev_B4_23_A2_01_6D_27", A2DP_SINK_UUID)]


async def test_connect_missing_uuid_raises():
    proxy = FakeProxy(uuid_list=("00001124-0000-1000-8000-00805f9b34fb",))
    with pytest.raises(HeadphoneError):
        await mgr(proxy).connect()


async def test_reconcile_disconnected_attempts():
    proxy = FakeProxy(connected_val=False)
    assert await mgr(proxy).reconcile() is True
    assert proxy.calls == [("connect", "/org/bluez/hci0/dev_B4_23_A2_01_6D_27", A2DP_SINK_UUID)]


async def test_reconcile_connected_noop():
    proxy = FakeProxy(connected_val=True)
    assert await mgr(proxy).reconcile() is False
    assert proxy.calls == []


def test_backoff_increases_and_caps():
    delays = [backoff_delay(i) for i in range(10)]
    assert delays[0] < delays[1] < delays[2]
    assert all(b >= a for a, b in zip(delays, delays[1:]))
    assert delays[-1] == BACKOFF_CAP
    assert max(delays) == BACKOFF_CAP


def test_find_sink_node_from_fixture():
    node = find_sink_node(parse_dump(DUMP), MAC)
    assert node is not None
    assert node.id == 90
    assert node.media_class == "Audio/Sink"
    assert node.name == "bluez_output.B4_23_A2_01_6D_27.1"


def test_find_sink_node_absent():
    assert find_sink_node(parse_dump(DUMP), Mac("AA:BB:CC:DD:EE:FF")) is None


def test_manager_sink_node_delegates():
    node = mgr(FakeProxy()).sink_node(parse_dump(DUMP))
    assert node is not None and node.id == 90
