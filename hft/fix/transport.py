"""Transporte da sessão FIX: socket TCP (com TLS opcional) e loopback de teste."""

from __future__ import annotations

import socket
import ssl
from typing import Optional, Protocol


class Transport(Protocol):
    def connect(self) -> None: ...

    def send(self, payload: bytes) -> None: ...

    def receive(self, max_bytes: int = 65536) -> bytes:
        """Bytes disponíveis; b'' quando não há nada dentro do timeout."""

    def close(self) -> None: ...

    @property
    def connected(self) -> bool: ...


class SocketTransport:
    """Socket TCP com TCP_NODELAY — Nagle é inimigo de latência baixa."""

    def __init__(self, host: str, port: int, use_ssl: bool = False, timeout: float = 0.05):
        self.host = host
        self.port = port
        self.use_ssl = use_ssl
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None

    def connect(self) -> None:
        raw = socket.create_connection((self.host, self.port), timeout=10.0)
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if self.use_ssl:
            context = ssl.create_default_context()
            raw = context.wrap_socket(raw, server_hostname=self.host)
        raw.settimeout(self.timeout)  # leitura não bloqueante: o loop segue girando
        self._sock = raw

    def send(self, payload: bytes) -> None:
        if self._sock is None:
            raise ConnectionError("transporte não conectado")
        self._sock.sendall(payload)

    def receive(self, max_bytes: int = 65536) -> bytes:
        if self._sock is None:
            raise ConnectionError("transporte não conectado")
        try:
            data = self._sock.recv(max_bytes)
        except (socket.timeout, TimeoutError):
            return b""
        except ssl.SSLWantReadError:
            return b""
        if data == b"":
            raise ConnectionError("conexão encerrada pela contraparte")
        return data

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    @property
    def connected(self) -> bool:
        return self._sock is not None


class LoopbackTransport:
    """Transporte em memória: alimenta bytes de entrada e guarda os de saída.

    Usado nos testes para exercitar a sessão inteira sem rede.
    """

    def __init__(self):
        self.outbound: list[bytes] = []
        self._inbound = bytearray()
        self._connected = False
        self.closed = False

    def connect(self) -> None:
        self._connected = True

    def send(self, payload: bytes) -> None:
        if not self._connected:
            raise ConnectionError("transporte não conectado")
        self.outbound.append(payload)

    def feed(self, payload: bytes) -> None:
        """Simula bytes chegando da contraparte."""
        self._inbound.extend(payload)

    def receive(self, max_bytes: int = 65536) -> bytes:
        if not self._inbound:
            return b""
        chunk = bytes(self._inbound[:max_bytes])
        del self._inbound[: len(chunk)]
        return chunk

    def close(self) -> None:
        self._connected = False
        self.closed = True

    @property
    def connected(self) -> bool:
        return self._connected

    def sent_messages(self) -> list[str]:
        return [payload.decode("ascii") for payload in self.outbound]
