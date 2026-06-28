import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger("tributary.bluetooth.agent")

BLUEZ_SERVICE = "org.bluez"
AGENT_MANAGER_PATH = "/org/bluez"
AGENT_INTERFACE = "org.bluez.Agent1"
AGENT_MANAGER_INTERFACE = "org.bluez.AgentManager1"
AGENT_PATH = "/org/tributary/agent"
CAPABILITY = "NoInputNoOutput"
REJECTED = "org.bluez.Error.Rejected"


class AgentRejected(Exception):
    pass


class PairingAgent:
    def release(self) -> None:
        logger.debug("agent released")

    def request_pin_code(self, device: str) -> str:
        raise AgentRejected(f"pin code unsupported for {CAPABILITY}")

    def display_pin_code(self, device: str, pincode: str) -> None:
        logger.debug("display pin %s for %s", pincode, device)

    def request_passkey(self, device: str) -> int:
        raise AgentRejected(f"passkey unsupported for {CAPABILITY}")

    def display_passkey(self, device: str, passkey: int, entered: int) -> None:
        logger.debug("display passkey %06d (%d entered) for %s", passkey, entered, device)

    def request_confirmation(self, device: str, passkey: int) -> None:
        logger.info("auto-confirm %s passkey %06d", device, passkey)

    def request_authorization(self, device: str) -> None:
        logger.info("auto-authorize %s", device)

    def authorize_service(self, device: str, uuid: str) -> None:
        logger.info("auto-authorize service %s on %s", uuid, device)

    def cancel(self) -> None:
        logger.debug("pairing cancelled")


@runtime_checkable
class AgentBackend(Protocol):
    def export(self, path: str, agent: PairingAgent) -> None: ...
    async def register_agent(self, path: str, capability: str) -> None: ...
    async def request_default_agent(self, path: str) -> None: ...


async def register(
    agent: PairingAgent,
    backend: AgentBackend,
    *,
    path: str = AGENT_PATH,
    capability: str = CAPABILITY,
) -> None:
    backend.export(path, agent)
    await backend.register_agent(path, capability)
    await backend.request_default_agent(path)
    logger.info("registered %s agent at %s", capability, path)


async def connect_system_backend() -> AgentBackend:
    from dbus_next import DBusError
    from dbus_next.aio import MessageBus
    from dbus_next.constants import BusType
    from dbus_next.service import ServiceInterface, method

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    introspection = await bus.introspect(BLUEZ_SERVICE, AGENT_MANAGER_PATH)
    proxy = bus.get_proxy_object(BLUEZ_SERVICE, AGENT_MANAGER_PATH, introspection)
    manager = proxy.get_interface(AGENT_MANAGER_INTERFACE)

    def make_interface(agent: PairingAgent) -> ServiceInterface:
        class Agent1(ServiceInterface):
            def __init__(self) -> None:
                super().__init__(AGENT_INTERFACE)

            @method()
            def Release(self) -> None:
                agent.release()

            @method()
            def RequestPinCode(self, device: "o") -> "s":  # noqa: F821,N802
                try:
                    return agent.request_pin_code(device)
                except AgentRejected as e:
                    raise DBusError(REJECTED, str(e))

            @method()
            def DisplayPinCode(self, device: "o", pincode: "s") -> None:  # noqa: F821,N802
                agent.display_pin_code(device, pincode)

            @method()
            def RequestPasskey(self, device: "o") -> "u":  # noqa: F821,N802
                try:
                    return agent.request_passkey(device)
                except AgentRejected as e:
                    raise DBusError(REJECTED, str(e))

            @method()
            def DisplayPasskey(self, device: "o", passkey: "u", entered: "q") -> None:  # noqa: F821,N802
                agent.display_passkey(device, passkey, entered)

            @method()
            def RequestConfirmation(self, device: "o", passkey: "u") -> None:  # noqa: F821,N802
                agent.request_confirmation(device, passkey)

            @method()
            def RequestAuthorization(self, device: "o") -> None:  # noqa: F821,N802
                agent.request_authorization(device)

            @method()
            def AuthorizeService(self, device: "o", uuid: "s") -> None:  # noqa: F821,N802
                agent.authorize_service(device, uuid)

            @method()
            def Cancel(self) -> None:
                agent.cancel()

        return Agent1()

    class _SystemBackend:
        def export(self, path: str, agent: PairingAgent) -> None:
            bus.export(path, make_interface(agent))

        async def register_agent(self, path: str, capability: str) -> None:
            await manager.call_register_agent(path, capability)

        async def request_default_agent(self, path: str) -> None:
            await manager.call_request_default_agent(path)

    return _SystemBackend()


async def run_system_agent(agent: PairingAgent | None = None) -> None:
    a = agent if agent is not None else PairingAgent()
    backend = await connect_system_backend()
    await register(a, backend)
