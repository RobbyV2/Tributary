import asyncio
import importlib
import sys

import pytest

import tributary.bluetooth.agent as agent_mod
from tributary.bluetooth.agent import (
    AGENT_MANAGER_PATH,
    AGENT_PATH,
    BLUEZ_SERVICE,
    CAPABILITY,
    AgentBackend,
    AgentRejected,
    PairingAgent,
    register,
)

DEVICE = "/org/bluez/hci0/dev_B4_23_A2_01_6D_27"


class FakeBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.exported: list[tuple[str, PairingAgent]] = []

    def export(self, path: str, agent: PairingAgent) -> None:
        self.exported.append((path, agent))
        self.calls.append(("export", path))

    async def register_agent(self, path: str, capability: str) -> None:
        self.calls.append(("register_agent", path, capability))

    async def request_default_agent(self, path: str) -> None:
        self.calls.append(("request_default_agent", path))


@pytest.fixture
def pairing() -> PairingAgent:
    return PairingAgent()


def test_constants() -> None:
    assert CAPABILITY == "NoInputNoOutput"
    assert AGENT_MANAGER_PATH == "/org/bluez"
    assert BLUEZ_SERVICE == "org.bluez"
    assert AGENT_PATH.startswith("/org/")


def test_confirmation_accepts(pairing: PairingAgent) -> None:
    assert pairing.request_confirmation(DEVICE, 123456) is None


def test_authorization_accepts(pairing: PairingAgent) -> None:
    assert pairing.request_authorization(DEVICE) is None


def test_authorize_service_accepts(pairing: PairingAgent) -> None:
    assert pairing.authorize_service(DEVICE, "0000110b-0000-1000-8000-00805f9b34fb") is None


def test_cancel_noop(pairing: PairingAgent) -> None:
    assert pairing.cancel() is None


def test_release_noop(pairing: PairingAgent) -> None:
    assert pairing.release() is None


def test_display_handlers_noop(pairing: PairingAgent) -> None:
    assert pairing.display_pin_code(DEVICE, "0000") is None
    assert pairing.display_passkey(DEVICE, 123456, 0) is None


def test_request_pin_code_rejected(pairing: PairingAgent) -> None:
    with pytest.raises(AgentRejected):
        pairing.request_pin_code(DEVICE)


def test_request_passkey_rejected(pairing: PairingAgent) -> None:
    with pytest.raises(AgentRejected):
        pairing.request_passkey(DEVICE)


def test_register_drives_backend(pairing: PairingAgent) -> None:
    backend = FakeBackend()
    assert isinstance(backend, AgentBackend)
    asyncio.run(register(pairing, backend))
    assert backend.exported == [(AGENT_PATH, pairing)]
    assert backend.calls == [
        ("export", AGENT_PATH),
        ("register_agent", AGENT_PATH, CAPABILITY),
        ("request_default_agent", AGENT_PATH),
    ]


def test_register_custom_path_and_capability(pairing: PairingAgent) -> None:
    backend = FakeBackend()
    asyncio.run(register(pairing, backend, path="/x/y", capability="KeyboardDisplay"))
    assert backend.calls == [
        ("export", "/x/y"),
        ("register_agent", "/x/y", "KeyboardDisplay"),
        ("request_default_agent", "/x/y"),
    ]


def test_import_without_dbus(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(sys.modules):
        if name == "dbus_next" or name.startswith("dbus_next."):
            monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "dbus_next", None)
    monkeypatch.delitem(sys.modules, agent_mod.__name__)
    reloaded = importlib.import_module(agent_mod.__name__)
    a = reloaded.PairingAgent()
    assert a.request_authorization(DEVICE) is None
