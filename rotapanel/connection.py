"""TCP connection management for Rotapanel devices.

Handles connecting to the RS485-to-TCP converter, sending raw frames,
receiving replies, and automatic retry / timeout logic.
"""

from __future__ import annotations

import logging
import socket
import time
from typing import Optional

from rotapanel.protocol import REPLY_LENGTH

logger = logging.getLogger(__name__)

# Default network settings
DEFAULT_HOST: str = "192.168.1.100"
DEFAULT_PORT: int = 5000
DEFAULT_TIMEOUT: float = 2.0   # seconds per send/recv operation
DEFAULT_RETRIES: int = 3
INTER_RETRY_DELAY: float = 0.2  # seconds between retries


class ConnectionError(OSError):
    """Raised when a TCP connection cannot be established or is lost."""


class RotapanelConnection:
    """Manages a TCP connection to a Rotapanel RS485-to-TCP converter.

    Usage::

        conn = RotapanelConnection("192.168.1.100", 5000)
        conn.connect()
        reply = conn.send_and_receive(frame_bytes)
        conn.disconnect()

    Or use it as a context manager::

        with RotapanelConnection("192.168.1.100", 5000) as conn:
            reply = conn.send_and_receive(frame_bytes)
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.retries = retries
        self._sock: Optional[socket.socket] = None

    # ── lifecycle ────────────────────────────

    def connect(self) -> None:
        """Open a TCP connection to the converter.

        Raises:
            ConnectionError: If the connection cannot be established.
        """
        if self._sock is not None:
            return  # already connected

        last_exc: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(self.timeout)
                sock.connect((self.host, self.port))
                self._sock = sock
                logger.info(
                    "Connected to %s:%d (attempt %d)", self.host, self.port, attempt
                )
                return
            except OSError as exc:
                last_exc = exc
                logger.warning(
                    "Connect attempt %d/%d to %s:%d failed: %s",
                    attempt,
                    self.retries,
                    self.host,
                    self.port,
                    exc,
                )
                time.sleep(INTER_RETRY_DELAY)

        raise ConnectionError(
            f"Could not connect to {self.host}:{self.port} after {self.retries} attempts."
        ) from last_exc

    def disconnect(self) -> None:
        """Close the TCP connection."""
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            finally:
                self._sock = None
                logger.info("Disconnected from %s:%d", self.host, self.port)

    @property
    def is_connected(self) -> bool:
        """True if the socket is currently open."""
        return self._sock is not None

    # ── I/O ──────────────────────────────────

    def send(self, data: bytes) -> None:
        """Send raw bytes to the converter.

        Args:
            data: Frame bytes to transmit.

        Raises:
            ConnectionError: If not connected or the send fails.
        """
        if self._sock is None:
            raise ConnectionError("Not connected. Call connect() first.")
        try:
            self._sock.sendall(data)
            logger.debug("Sent %d bytes: %s", len(data), data.hex())
        except OSError as exc:
            self._sock = None
            raise ConnectionError(f"Send failed: {exc}") from exc

    def receive(self, length: int = REPLY_LENGTH) -> bytes:
        """Receive up to *length* bytes from the converter.

        Args:
            length: Number of bytes to read.

        Returns:
            Raw bytes received from the device.

        Raises:
            ConnectionError: If not connected, a timeout occurs, or the
                connection is closed by the remote end.
        """
        if self._sock is None:
            raise ConnectionError("Not connected. Call connect() first.")
        try:
            data = b''
            while len(data) < length:
                chunk = self._sock.recv(length - len(data))
                if not chunk:
                    self._sock = None
                    raise ConnectionError("Connection closed by remote host.")
                data += chunk
            logger.debug("Received %d bytes: %s", len(data), data.hex())
            return data
        except socket.timeout as exc:
            raise ConnectionError(f"Receive timed out after {self.timeout}s.") from exc
        except OSError as exc:
            self._sock = None
            raise ConnectionError(f"Receive failed: {exc}") from exc

    def send_and_receive(
        self,
        data: bytes,
        recv_length: int = REPLY_LENGTH,
    ) -> bytes:
        """Send *data* and wait for a reply.

        Retries the operation up to ``self.retries`` times on failure.

        Args:
            data       : Frame bytes to transmit.
            recv_length: Number of reply bytes to read.

        Returns:
            Raw reply bytes.

        Raises:
            ConnectionError: If all retry attempts fail.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            try:
                if not self.is_connected:
                    self.connect()
                self.send(data)
                reply = self.receive(recv_length)
                return reply
            except ConnectionError as exc:
                last_exc = exc
                logger.warning(
                    "send_and_receive attempt %d/%d failed: %s",
                    attempt,
                    self.retries,
                    exc,
                )
                self.disconnect()
                if attempt < self.retries:
                    time.sleep(INTER_RETRY_DELAY)

        raise ConnectionError(
            f"send_and_receive failed after {self.retries} attempts."
        ) from last_exc

    # ── context manager ───────────────────────

    def __enter__(self) -> "RotapanelConnection":
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self.disconnect()

    def __repr__(self) -> str:  # pragma: no cover
        state = "connected" if self.is_connected else "disconnected"
        return f"RotapanelConnection({self.host}:{self.port}, {state})"
