"""Rotapanel device scanner.

Scans the RS-485 bus (via a TCP converter) for active RP-2000 devices.
Devices have node addresses from 0 to 63 (0x00–0x3F).

Scanning uses parallel threads so that unresponsive addresses do not
block the entire scan.
"""

from __future__ import annotations

import logging
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import List, Optional

from rotapanel import protocol
from rotapanel.connection import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
    RotapanelConnection,
)

logger = logging.getLogger(__name__)

# Number of worker threads used during parallel scanning
DEFAULT_WORKERS: int = 8

# Short timeout used during scanning (we don't want to wait long per node)
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


def _probe_device(
    host: str,
    port: int,
    device_id: int,
    timeout: float,
) -> ScanResult:
    """Probe a single device address.

    Opens its own TCP connection so that each probe is independent.
    """
    conn = RotapanelConnection(host, port, timeout=timeout, retries=1)
    try:
        conn.connect()
        frame = protocol.build_status_request(device_id)
        conn.send(frame)
        raw = conn.receive(protocol.REPLY_LENGTH)
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
    except OSError as exc:
        return ScanResult(
            device_id=device_id,
            online=False,
            error_message=str(exc),
        )
    finally:
        conn.disconnect()


class RotapanelScanner:
    """Scans a range of RS-485 node addresses for live Rotapanel devices.

    Usage::

        scanner = RotapanelScanner("192.168.1.100", 5000)
        results = scanner.scan()
        online = scanner.online_devices(results)
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        timeout: float = SCAN_TIMEOUT,
        workers: int = DEFAULT_WORKERS,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.workers = workers

    def scan(
        self,
        start: int = protocol.MIN_ADDR,
        end: int = protocol.MAX_ADDR,
    ) -> List[ScanResult]:
        """Scan device IDs from *start* to *end* (inclusive).

        Args:
            start: First address to scan (default 0).
            end  : Last address to scan  (default 63).

        Returns:
            List of :class:`ScanResult`, one per address, sorted by
            device_id.
        """
        addresses = list(range(start, end + 1))
        total = len(addresses)
        logger.info(
            "Scanning %d addresses (%d–%d) on %s:%d with %d workers …",
            total,
            start,
            end,
            self.host,
            self.port,
            self.workers,
        )

        results: List[ScanResult] = []
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = {
                executor.submit(
                    _probe_device, self.host, self.port, addr, self.timeout
                ): addr
                for addr in addresses
            }
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                if result.online:
                    logger.info("Found device: %s", result)

        results.sort(key=lambda r: r.device_id)
        online_count = sum(1 for r in results if r.online)
        logger.info("Scan complete: %d/%d devices online.", online_count, total)
        return results

    @staticmethod
    def online_devices(results: List[ScanResult]) -> List[ScanResult]:
        """Filter *results* to only the online devices."""
        return [r for r in results if r.online]

    def is_alive(self, device_id: int) -> bool:
        """Quick check: is a specific device alive?

        Args:
            device_id: RS-485 node address (0–63).

        Returns:
            ``True`` if the device responds, ``False`` otherwise.
        """
        result = _probe_device(self.host, self.port, device_id, self.timeout)
        return result.online
