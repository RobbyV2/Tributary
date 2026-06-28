import re
from pathlib import Path

from tributary.bluetooth.adapter import (
    AdapterManager,
    AdapterRoles,
    AdapterState,
    Role,
    hci_path,
    resolve_roles,
)
from tributary.config import AdapterMap, Config, Mac, SourceAllowlist

HP = Mac("AA:BB:CC:DD:EE:FF")


class FakeProxy:
    def __init__(self, state: dict[tuple[str, str], object] | None = None) -> None:
        self.state: dict[tuple[str, str], object] = dict(state or {})
        self.writes: list[tuple[str, str, object]] = []

    async def get(self, path: str, name: str) -> object:
        return self.state.get((path, name))

    async def set(self, path: str, name: str, value: object) -> None:
        self.writes.append((path, name, value))
        self.state[(path, name)] = value


def single_mgr(proxy: FakeProxy) -> AdapterManager:
    return AdapterManager.from_config(Config(headphone_mac=HP), ["hci0"], proxy)


def dual_mgr(proxy: FakeProxy) -> AdapterManager:
    cfg = Config(headphone_mac=HP, adapters=AdapterMap("hci0", "hci1"))
    return AdapterManager.from_config(cfg, ["hci0", "hci1"], proxy)


async def test_ensure_discoverable_writes_expected_props():
    proxy = FakeProxy()
    await single_mgr(proxy).ensure_discoverable()
    by_name = {name: value for _path, name, value in proxy.writes}
    assert by_name["Powered"] is True
    assert by_name["Pairable"] is True
    assert by_name["Discoverable"] is True
    assert by_name["PairableTimeout"] == 0
    assert by_name["DiscoverableTimeout"] == 0
    assert all(path == hci_path("hci0") for path, _n, _v in proxy.writes)


async def test_class_never_written():
    proxy = FakeProxy()
    mgr = single_mgr(proxy)
    await mgr.ensure_discoverable()
    await mgr.ensure_discoverable(Role.SOURCE)
    assert "Class" not in {name for _p, name, _v in proxy.writes}


def test_single_adapter_both_roles_one_controller():
    roles = single_mgr(FakeProxy()).roles
    assert roles.single is True
    assert roles.path_for(Role.SINK) == roles.path_for(Role.SOURCE) == hci_path("hci0")
    assert roles.paths() == (hci_path("hci0"),)


def test_dual_adapter_two_controllers():
    roles = dual_mgr(FakeProxy()).roles
    assert roles.single is False
    assert roles.path_for(Role.SINK) == hci_path("hci0")
    assert roles.path_for(Role.SOURCE) == hci_path("hci1")
    assert roles.paths() == (hci_path("hci0"), hci_path("hci1"))


def test_resolve_roles_maps_resolved_adapters():
    cfg = Config(headphone_mac=HP)
    roles = resolve_roles(cfg.resolve_adapters(["hci2", "hci0"]))
    assert roles == AdapterRoles(hci_path("hci0"), hci_path("hci0"), True)


def test_is_allowed_accepts_listed_mac():
    proxy = FakeProxy()
    mgr = AdapterManager(single_mgr(proxy).roles, SourceAllowlist(macs=(HP,)), proxy)
    assert mgr.is_allowed(HP) is True


def test_is_allowed_accepts_pattern_match():
    proxy = FakeProxy()
    allow = SourceAllowlist(patterns=(re.compile("Pixel"),))
    mgr = AdapterManager(single_mgr(proxy).roles, allow, proxy)
    assert mgr.is_allowed(Mac("11:22:33:44:55:66"), "Pixel Buds Pro 2") is True


def test_is_allowed_rejects_unlisted():
    proxy = FakeProxy()
    allow = SourceAllowlist(macs=(HP,), patterns=(re.compile("Pixel"),))
    mgr = AdapterManager(single_mgr(proxy).roles, allow, proxy)
    assert mgr.is_allowed(Mac("11:22:33:44:55:66"), "Random Speaker") is False


async def test_state_reads_props():
    p = hci_path("hci0")
    proxy = FakeProxy({(p, "Powered"): True, (p, "Pairable"): True, (p, "Discoverable"): False})
    assert await single_mgr(proxy).state() == AdapterState(True, True, False)


def _prop_access(text: str) -> dict[str, str]:
    return {m.group(1): m.group(2) for m in re.finditer(r'<property name="([^"]+)" type="[^"]+" access="([^"]+)"', text)}


def test_introspect_fixture_class_readonly_others_writable(bluez_dir: Path):
    access = _prop_access((bluez_dir / "adapter1-introspect-hci0.txt").read_text())
    assert access["Class"] == "read"
    for name in ("Discoverable", "Pairable", "Powered", "PairableTimeout", "DiscoverableTimeout"):
        assert access[name] == "readwrite"
