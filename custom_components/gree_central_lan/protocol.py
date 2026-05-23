"""Protocol helpers and runtime client for Gree central HVAC devices."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
import json
import logging
import socket
import time
from typing import Any

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from homeassistant.components.network import async_get_ipv4_broadcast_addresses
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    CONF_MAIN_MAC,
    DEFAULT_DISCOVERY_TIMEOUT,
    DEFAULT_SYNC_INTERVAL_SECONDS,
    DEFAULT_MAIN_KEY,
    DEFAULT_TIMEOUT,
    STATUS_COLUMNS,
)
from .models import BridgeInfo, ClimateState, SubDeviceInfo

LOGGER = logging.getLogger(__name__)


class GreeProtocolError(Exception):
    """Raised when the controller cannot be reached or returns bad data."""


def _aes_encrypt(key: bytes, payload: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    encryptor = cipher.encryptor()
    return encryptor.update(payload) + encryptor.finalize()


def _aes_decrypt(key: bytes, payload: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    decryptor = cipher.decryptor()
    return decryptor.update(payload) + decryptor.finalize()


def _pad(text: str) -> bytes:
    raw = text.encode("utf-8")
    pad_length = 16 - (len(raw) % 16)
    return raw + bytes([pad_length] * pad_length)


def _unpad(payload: bytes) -> str:
    if payload:
        pad_length = payload[-1]
        if 0 < pad_length <= 16 and payload.endswith(bytes([pad_length] * pad_length)):
            payload = payload[:-pad_length]
    text = payload.decode("utf-8", errors="ignore")
    if "}" in text:
        text = text[: text.rindex("}") + 1]
    return text


def encrypt_payload(payload: dict[str, Any], key: str) -> str:
    """Encrypt an inner Gree payload."""
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return base64.b64encode(
        _aes_encrypt(key.encode("utf-8"), _pad(encoded))
    ).decode("utf-8")


def decrypt_payload(payload: str, key: str) -> dict[str, Any]:
    """Decrypt an inner Gree payload."""
    raw = base64.b64decode(payload)
    decrypted = _aes_decrypt(key.encode("utf-8"), raw)
    text = _unpad(decrypted)
    return json.loads(text)


def _send_request_sync(
    host: str,
    port: int,
    request: dict[str, Any],
    *,
    decrypt_key: str,
    timeout: float = DEFAULT_TIMEOUT,
    broadcast: bool = False,
) -> dict[str, Any]:
    """Send a UDP request and wait for a single response."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    if broadcast:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    try:
        sock.sendto(
            json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            (host, port),
        )
        data, _ = sock.recvfrom(65535)
    except (OSError, TimeoutError, socket.timeout) as err:
        raise GreeProtocolError(f"Timed out talking to {host}:{port}") from err
    finally:
        sock.close()

    try:
        root = json.loads(data)
    except json.JSONDecodeError as err:
        raise GreeProtocolError(f"Invalid JSON from {host}:{port}") from err
    if "pack" not in root:
        raise GreeProtocolError(f"Unexpected response from {host}: {root!r}")
    return decrypt_payload(root["pack"], decrypt_key)


def probe_bridge_sync(
    host: str,
    port: int = 7000,
    timeout: float = DEFAULT_TIMEOUT,
) -> BridgeInfo:
    """Connect to a controller, bind, and fetch its indoor units."""
    scan = _send_request_sync(
        host,
        port,
        {"t": "scan"},
        decrypt_key=DEFAULT_MAIN_KEY,
        timeout=timeout,
    )
    if scan.get("t") != "dev":
        raise GreeProtocolError(f"Expected dev packet from {host}, got {scan!r}")

    main_mac = str(scan["mac"])
    bind = _send_request_sync(
        host,
        port,
        {
            "cid": "app",
            "i": 1,
            "pack": encrypt_payload({"mac": main_mac, "t": "bind", "uid": "0"}, DEFAULT_MAIN_KEY),
            "t": "pack",
            "tcid": main_mac,
            "uid": 0,
        },
        decrypt_key=DEFAULT_MAIN_KEY,
        timeout=timeout,
    )
    if bind.get("t") != "bindOk":
        raise GreeProtocolError(f"Expected bindOk packet from {host}, got {bind!r}")

    session_key = str(bind["key"])
    sub_list = _send_request_sync(
        host,
        port,
        {
            "cid": "app",
            "i": 0,
            "pack": encrypt_payload({"t": "subDev", "mac": main_mac, "i": 0}, session_key),
            "t": "pack",
            "tcid": main_mac,
            "uid": 0,
        },
        decrypt_key=session_key,
        timeout=timeout,
    )
    if sub_list.get("t") != "subList":
        raise GreeProtocolError(f"Expected subList packet from {host}, got {sub_list!r}")

    subdevices = tuple(
        SubDeviceInfo(
            mac=str(item["mac"]),
            mid=str(item.get("mid", "")),
            name=str(item.get("name") or item["mac"]),
        )
        for item in sub_list.get("list", [])
    )

    return BridgeInfo(
        host=host,
        port=port,
        mac=main_mac,
        name=str(scan.get("name") or main_mac),
        brand=str(scan.get("brand") or "Gree"),
        model=str(scan.get("model") or "gree"),
        version=str(scan.get("ver") or ""),
        subdevices=subdevices,
    )


def discover_bridges_sync(
    broadcast_addresses: list[str],
    port: int = 7000,
    timeout: float = DEFAULT_DISCOVERY_TIMEOUT,
) -> list[BridgeInfo]:
    """Broadcast a scan request and then fully probe each controller found."""
    if not broadcast_addresses:
        return []

    scan_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    scan_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    scan_socket.settimeout(0.25)

    try:
        payload = b'{"t":"scan"}'
        for broadcast_address in broadcast_addresses:
            scan_socket.sendto(payload, (broadcast_address, port))

        seen_hosts: dict[str, str] = {}
        end_time = timeout + time.monotonic()
        while time.monotonic() < end_time:
            try:
                data, address = scan_socket.recvfrom(65535)
            except socket.timeout:
                continue

            root = json.loads(data)
            if "pack" not in root:
                continue

            try:
                packet = decrypt_payload(root["pack"], DEFAULT_MAIN_KEY)
            except Exception as err:  # pragma: no cover - defensive
                LOGGER.debug("Ignoring undecodable discovery packet from %s: %s", address[0], err)
                continue

            if packet.get("t") != "dev":
                continue
            seen_hosts[str(packet["mac"])] = address[0]
    finally:
        scan_socket.close()

    controllers: list[BridgeInfo] = []
    for main_mac, host in seen_hosts.items():
        try:
            controller = probe_bridge_sync(host, port, timeout=DEFAULT_TIMEOUT)
        except GreeProtocolError as err:
            LOGGER.debug("Skipping controller %s at %s: %s", main_mac, host, err)
            continue
        controllers.append(controller)

    controllers.sort(key=lambda bridge: bridge.host)
    return controllers


async def async_probe_bridge(host: str, port: int = 7000) -> BridgeInfo:
    """Async wrapper around the synchronous bridge probe."""
    return await asyncio.to_thread(probe_bridge_sync, host, port, DEFAULT_TIMEOUT)


async def async_discover_bridges(
    hass: HomeAssistant,
    port: int = 7000,
) -> list[BridgeInfo]:
    """Discover controllers by broadcasting to each local IPv4 broadcast address."""
    broadcast_addresses = [str(address) for address in await async_get_ipv4_broadcast_addresses(hass)]
    return await asyncio.to_thread(discover_bridges_sync, broadcast_addresses, port, DEFAULT_DISCOVERY_TIMEOUT)


@dataclass(slots=True)
class _PacketWaiter:
    """A single in-flight request waiting for a matching packet."""

    predicate: Callable[[dict[str, Any]], bool]
    future: asyncio.Future[dict[str, Any]]


class _GreeDatagramProtocol(asyncio.DatagramProtocol):
    """Asyncio transport protocol used by the runtime client."""

    def __init__(self, client: "GreeCentralClient") -> None:
        self._client = client

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._client.set_transport(transport)  # type: ignore[arg-type]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._client.handle_datagram(data, addr)

    def error_received(self, exc: Exception) -> None:
        LOGGER.debug("UDP transport error: %s", exc)

    def connection_lost(self, exc: Exception | None) -> None:
        self._client.handle_connection_lost(exc)


class GreeCentralClient:
    """Long-lived client that uses a single UDP socket for push-style updates."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_data: dict[str, Any],
        *,
        sync_interval_seconds: int = DEFAULT_SYNC_INTERVAL_SECONDS,
    ) -> None:
        self.hass = hass
        self.host = str(entry_data[CONF_HOST])
        self.port = int(entry_data[CONF_PORT])
        self.main_mac = str(entry_data[CONF_MAIN_MAC])

        self.bridge: BridgeInfo | None = None
        self._transport: asyncio.DatagramTransport | None = None
        self._request_lock = asyncio.Lock()
        self._waiters: list[_PacketWaiter] = []
        self._listeners: set[Callable[[str], None]] = set()
        self._session_key: str | None = None
        self._states: dict[str, ClimateState] = {}
        self._sync_interval_seconds = max(0, int(sync_interval_seconds))
        self._unsubscribe_reconcile: Callable[[], None] | None = None
        self._reconcile_lock = asyncio.Lock()
        self.available = False

    async def async_setup(self) -> BridgeInfo:
        """Resolve the controller, open the socket, bind, and fetch initial states."""
        self.bridge = await self._resolve_bridge()
        self.host = self.bridge.host
        self.port = self.bridge.port

        for subdevice in self.bridge.subdevices:
            self._states.setdefault(subdevice.mac, ClimateState())

        loop = asyncio.get_running_loop()
        await loop.create_datagram_endpoint(
            lambda: _GreeDatagramProtocol(self),
            local_addr=("0.0.0.0", 0),
        )

        await self._async_bind()

        for subdevice in self.bridge.subdevices:
            await self.async_refresh_state(subdevice.mac)

        if self._sync_interval_seconds > 0:
            self._unsubscribe_reconcile = async_track_time_interval(
                self.hass,
                self._async_reconcile_states,
                timedelta(seconds=self._sync_interval_seconds),
            )

        self.available = True
        return self.bridge

    async def async_shutdown(self) -> None:
        """Tear down the client transport."""
        self.available = False
        if self._unsubscribe_reconcile is not None:
            self._unsubscribe_reconcile()
            self._unsubscribe_reconcile = None
        if self._transport is not None:
            self._transport.close()
            self._transport = None

        for waiter in self._waiters:
            if not waiter.future.done():
                waiter.future.cancel()
        self._waiters.clear()

    @callback
    def async_add_listener(self, listener: Callable[[str], None]) -> Callable[[], None]:
        """Register an entity callback for state changes."""
        self._listeners.add(listener)

        @callback
        def _unsubscribe() -> None:
            self._listeners.discard(listener)

        return _unsubscribe

    def get_state(self, subdevice_mac: str) -> ClimateState | None:
        """Return the cached state for an indoor unit."""
        return self._states.get(subdevice_mac)

    async def async_refresh_state(self, subdevice_mac: str) -> dict[str, Any]:
        """Fetch the latest status for one indoor unit."""
        self._ensure_ready()
        payload = {
            "cid": "app",
            "i": 0,
            "pack": encrypt_payload(
                {"cols": list(STATUS_COLUMNS), "mac": subdevice_mac, "t": "status"},
                self._session_key or DEFAULT_MAIN_KEY,
            ),
            "t": "pack",
            "tcid": self.main_mac,
            "uid": 0,
        }
        return await self._async_exchange(
            payload,
            lambda packet: packet.get("t") == "dat" and packet.get("mac") == subdevice_mac,
        )

    async def async_send_command(self, subdevice_mac: str, updates: dict[str, int]) -> dict[str, Any]:
        """Push new settings to one indoor unit."""
        self._ensure_ready()
        payload = {
            "cid": "app",
            "i": 0,
            "pack": encrypt_payload(
                {
                    "opt": list(updates.keys()),
                    "p": [int(value) for value in updates.values()],
                    "t": "cmd",
                    "sub": subdevice_mac,
                },
                self._session_key or DEFAULT_MAIN_KEY,
            ),
            "t": "pack",
            "tcid": self.main_mac,
            "uid": 0,
        }
        response = await self._async_exchange(
            payload,
            lambda packet: packet.get("t") == "res" and packet.get("mac") == subdevice_mac,
        )

        state = self._states.get(subdevice_mac)
        if state is None or state.target_temperature is None:
            await self.async_refresh_state(subdevice_mac)
        return response

    def set_transport(self, transport: asyncio.DatagramTransport) -> None:
        """Store the datagram transport after the protocol connects."""
        self._transport = transport

    def handle_connection_lost(self, exc: Exception | None) -> None:
        """Mark the client unavailable if the socket goes away."""
        self.available = False
        if exc is not None:
            LOGGER.debug("Gree transport closed with error: %s", exc)

    def handle_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        """Decode packets, satisfy pending requests, and fan out push updates."""
        try:
            root = json.loads(data)
        except json.JSONDecodeError:
            LOGGER.debug("Ignoring undecodable UDP packet from %s", addr)
            return

        if "pack" not in root:
            LOGGER.debug("Ignoring UDP packet without encrypted payload from %s: %s", addr, root)
            return

        packet: dict[str, Any] | None = None
        keys_to_try = [self._session_key, DEFAULT_MAIN_KEY]
        for key in keys_to_try:
            if not key:
                continue
            try:
                packet = decrypt_payload(root["pack"], key)
            except Exception:
                continue
            else:
                break

        if packet is None:
            LOGGER.debug("Ignoring UDP packet that could not be decrypted from %s", addr)
            return

        packet_type = packet.get("t")
        if packet_type == "bindOk":
            self._session_key = str(packet["key"])

        for waiter in tuple(self._waiters):
            if waiter.future.done():
                self._waiters.remove(waiter)
                continue
            if waiter.predicate(packet):
                waiter.future.set_result(packet)
                self._waiters.remove(waiter)
                break

        if packet_type in {"dat", "res"}:
            self._apply_state_packet(packet)

    async def _async_bind(self) -> None:
        """Bind the current socket to the controller session and enumerate units."""
        await self._async_exchange(
            {
                "cid": "app",
                "i": 1,
                "pack": encrypt_payload(
                    {"mac": self.main_mac, "t": "bind", "uid": "0"},
                    DEFAULT_MAIN_KEY,
                ),
                "t": "pack",
                "tcid": self.main_mac,
                "uid": 0,
            },
            lambda packet: packet.get("t") == "bindOk" and packet.get("mac") == self.main_mac,
        )

        sub_list = await self._async_exchange(
            {
                "cid": "app",
                "i": 0,
                "pack": encrypt_payload(
                    {"t": "subDev", "mac": self.main_mac, "i": 0},
                    self._session_key or DEFAULT_MAIN_KEY,
                ),
                "t": "pack",
                "tcid": self.main_mac,
                "uid": 0,
            },
            lambda packet: packet.get("t") == "subList",
        )

        if self.bridge is None:
            return

        self.bridge = BridgeInfo(
            host=self.bridge.host,
            port=self.bridge.port,
            mac=self.bridge.mac,
            name=self.bridge.name,
            brand=self.bridge.brand,
            model=self.bridge.model,
            version=self.bridge.version,
            subdevices=tuple(
                SubDeviceInfo(
                    mac=str(item["mac"]),
                    mid=str(item.get("mid", "")),
                    name=str(item.get("name") or item["mac"]),
                )
                for item in sub_list.get("list", [])
            ),
        )
        for subdevice in self.bridge.subdevices:
            self._states.setdefault(subdevice.mac, ClimateState())

    async def _async_exchange(
        self,
        payload: dict[str, Any],
        predicate: Callable[[dict[str, Any]], bool],
        timeout: float = DEFAULT_TIMEOUT,
    ) -> dict[str, Any]:
        """Send a request and wait until a matching packet arrives on the shared socket."""
        self._ensure_ready()

        async with self._request_lock:
            loop = asyncio.get_running_loop()
            future: asyncio.Future[dict[str, Any]] = loop.create_future()
            waiter = _PacketWaiter(predicate=predicate, future=future)
            self._waiters.append(waiter)

            self._transport.sendto(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                (self.host, self.port),
            )

            try:
                return await asyncio.wait_for(future, timeout=timeout)
            except TimeoutError as err:
                raise GreeProtocolError(f"Timed out waiting for response from {self.host}") from err
            finally:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)

    async def _resolve_bridge(self) -> BridgeInfo:
        """Verify the configured host and fall back to LAN discovery if needed."""
        try:
            bridge = await async_probe_bridge(self.host, self.port)
        except GreeProtocolError:
            bridge = None
        else:
            if bridge.mac == self.main_mac:
                return bridge

        controllers = await async_discover_bridges(self.hass, self.port)
        for controller in controllers:
            if controller.mac == self.main_mac:
                return controller

        raise GreeProtocolError(
            f"Unable to find Gree controller {self.main_mac} on the local network"
        )

    def _apply_state_packet(self, packet: dict[str, Any]) -> None:
        """Merge a status or command response packet into the local cache."""
        subdevice_mac = str(packet.get("mac", ""))
        if not subdevice_mac:
            return

        state = self._states.setdefault(subdevice_mac, ClimateState())
        if packet.get("t") == "dat":
            columns = [str(column) for column in packet.get("cols", [])]
            values = [int(value) for value in packet.get("dat", [])]
            state.apply_columns(columns, values)
        else:
            columns = [str(column) for column in packet.get("opt", [])]
            values = [int(value) for value in packet.get("val") or packet.get("p", [])]
            state.apply_columns(columns, values)

        self.available = True
        for listener in tuple(self._listeners):
            listener(subdevice_mac)

    async def _async_reconcile_states(self, _now) -> None:
        """Optionally reconcile state for changes made outside this HA session."""
        if self.bridge is None or self._reconcile_lock.locked():
            return

        async with self._reconcile_lock:
            for subdevice in self.bridge.subdevices:
                try:
                    await self.async_refresh_state(subdevice.mac)
                except GreeProtocolError as err:
                    self.available = False
                    LOGGER.debug("Reconcile refresh failed for %s: %s", subdevice.mac, err)
                    break

    def _ensure_ready(self) -> None:
        """Guard operations that require an open UDP transport."""
        if self._transport is None:
            raise GreeProtocolError("UDP transport is not connected")
