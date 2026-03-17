"""Rotapanel device scanner.

Scans the RS-485 bus (via a TCP converter) for active RP-2000 devices.
Devices have node addresses from 0 to 63 (0x00–0x3F).

RS-485 is a shared bus: only one device may be addressed at a time.
Scanning is therefore strictly sequential over a single TCP connection.
Sending multiple requests simultaneously would cause response collisions
on the bus.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from rotapanel import protocol
from rotapanel.connection import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    ConnectionError,
    RotapanelConnection,
)

logger = logging.getLogger(__name__)

# Per-device timeout during scanning.  Keep it short so the full scan
# completes in reasonable time, but long enough for a slow device to reply.
SCAN_TIMEOUT: float = 1.0


@dataclass
class ScanResult:
    """Result for a single scanned address."""

    device_id: int
    online: bool
    side: Optional[str] = None
    lighting_on: Optional[bool] = None
    has_errors: bool = False
    error_list: List[str] = field(default_factory=list)
    error_message: Optional[str] = None  # network / parse error

    def __str__(self) -> str:
        if not self.online:
            reason = f" ({self.error_message})" if self.error_message else ""
            return f"[{self.device_id:3d}] OFFLINE{reason}"
        errors = ", ".join(self.error_list) if self.error_list else "none"
        return (
            f"[{self.device_id:3d}] ONLINE  "
            f"side={self.side}  "
            f"light={'on' if self.lighting_on else 'off'}  "
            f"errors={errors}"
        )


def _probe_one(conn: RotapanelConnection, device_id: int) -> ScanResult:
    """Send one status request over *conn* and return a :class:`ScanResult`.

    The caller is responsible for keeping *conn* open across multiple calls.
    This function never closes *conn*.

    A timeout (device not present on the bus) raises
    :class:`~rotapanel.connection.ConnectionError` but does **not** invalidate
    the TCP connection — the next probe can reuse the same socket.
    """
    try:
        frame = protocol.build_status_request(device_id)
        conn.send(frame)
        raw = conn.receive(protocol.RS485_BUFFER_SIZE)
        status = protocol.parse_reply(raw)
        return ScanResult(
            device_id=device_id,
            online=True,
            side=status.side,
            lighting_on=status.lighting_on,
            has_errors=status.has_any_error,
            error_list=status.error_summary(),
        )
    except protocol.ParseError as exc:
        return ScanResult(
            device_id=device_id,
            online=False,
            error_message=f"Parse error: {exc}",
        )
    except ConnectionError as exc:
        return ScanResult(
            device_id=device_id,
            online=False,
            error_message=str(exc),
        )


class RotapanelScanner:
    """Scans a range of RS-485 node addresses for live Rotapanel devices.

    All probes share a **single TCP connection** and are sent one at a time.
    This is required because the RS-485 bus is shared: only one device may
    be addressed at any moment.

    Usage::

        scanner = RotapanelScanner("192.168.1.100", 5000)
        results = scanner.scan()
        online = RotapanelScanner.online_devices(results)
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        timeout: float = SCAN_TIMEOUT,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout

    def scan(
        self,
        start: int = protocol.MIN_ADDR,
        end: int = protocol.MAX_ADDR,
    ) -> List[ScanResult]:
        """Scan device IDs from *start* to *end* (inclusive), sequentially.

        Opens one TCP connection and sends one status request per address,
        waiting for the reply (or a timeout) before moving to the next address.

        Args:
            start: First address to scan (default 0).
            end  : Last address to scan  (default 63).

        Returns:
            List of :class:`ScanResult`, one per address, in address order.
        """
        addresses = list(range(start, end + 1))
        total = len(addresses)
        logger.info(
            "Scanning %d addresses (%d–%d) sequentially on %s:%d …",
            total, start, end, self.host, self.port,
        )

        results: List[ScanResult] = []
        conn = RotapanelConnection(self.host, self.port, timeout=self.timeout, retries=1)

        try:
            conn.connect()
        except ConnectionError as exc:
            # Cannot reach the converter at all — mark every address offline.
            logger.error("Could not connect to %s:%d: %s", self.host, self.port, exc)
            return [
                ScanResult(device_id=addr, online=False, error_message=str(exc))
                for addr in addresses
            ]

        try:
            for addr in addresses:
                result = _probe_one(conn, addr)
                results.append(result)
                if result.online:
                    logger.info("Found device: %s", result)
                else:
                    logger.debug("[%3d] no response (%s)", addr, result.error_message)

                # If the TCP connection was lost (not just a per-device timeout),
                # attempt to reconnect before continuing.
                if not conn.is_connected:
                    logger.warning("Connection lost; reconnecting …")
                    try:
                        conn.connect()
                    except ConnectionError as exc:
                        logger.error("Reconnect failed: %s — aborting scan", exc)
                        for remaining in addresses[len(results):]:
                            results.append(ScanResult(
                                device_id=remaining,
                                online=False,
                                error_message="Connection lost",
                            ))
                        break
        finally:
            conn.disconnect()

        online_count = sum(1 for r in results if r.online)
        logger.info("Scan complete: %d/%d devices online.", online_count, total)
        return results

    @staticmethod
    def online_devices(results: List[ScanResult]) -> List[ScanResult]:
        """Filter *results* to only the online devices."""
        return [r for r in results if r.online]

    def is_alive(self, device_id: int) -> bool:
        """Quick check: is a specific device alive?

        Opens a dedicated connection, sends one status request, then closes.

        Args:
            device_id: RS-485 node address (0–63).

        Returns:
            ``True`` if the device responds, ``False`` otherwise.
        """
        conn = RotapanelConnection(self.host, self.port, timeout=self.timeout, retries=1)
        try:
            conn.connect()
            result = _probe_one(conn, device_id)
            return result.online
        except ConnectionError:
            return False
        finally:
            conn.disconnect()
