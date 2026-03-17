"""Rotapanel device control.

High-level API for a single RP-2000 prismatic sign:
  - Turn to side A / B / C
  - Light ON / OFF
  - Status query
  - Error reporting
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from rotapanel.connection import RotapanelConnection
from rotapanel import protocol
from rotapanel.protocol import RS485_BUFFER_SIZE

logger = logging.getLogger(__name__)


class RotapanelDevice:
    """Controls a single Rotapanel RP-2000 device.

    Two-step rotation protocol
    --------------------------
    The RP-2000 requires two messages to complete a rotation:

    1. TURN command  – tells the panel *which* side to prepare for.
    2. GO command    – tells the panel to actually perform the rotation.

    The device replies to the TURN command with a REPLY message that
    contains the current status.  No reply is sent for the GO command.

    Usage::

        with RotapanelConnection("192.168.1.100", 5000) as conn:
            dev = RotapanelDevice(device_id=1, connection=conn)
            status = dev.get_status()
            dev.turn_to_side("B")
    """

    def __init__(
        self,
        device_id: int,
        connection: RotapanelConnection,
        go_delay: float = 0.1,
    ) -> None:
        """
        Args:
            device_id : RS-485 node address (0–63).
            connection: An open (or auto-connecting) :class:`RotapanelConnection`.
            go_delay  : Seconds to wait between TURN and GO commands.
        """
        if not (protocol.MIN_ADDR <= device_id <= protocol.MAX_ADDR):
            raise ValueError(
                f"device_id must be 0–{protocol.MAX_ADDR}, got {device_id}."
            )
        self.device_id = device_id
        self.connection = connection
        self.go_delay = go_delay

    # ── status ────────────────────────────────

    def get_status(self) -> protocol.DeviceStatus:
        """Request and return the current device status.

        Sends a TURN with CMD_STATUS_ONLY (no rotation prepared).

        Returns:
            :class:`~rotapanel.protocol.DeviceStatus` with current state.

        Raises:
            rotapanel.connection.ConnectionError: On TCP failure.
            rotapanel.protocol.ParseError       : If the reply is malformed.
        """
        frame = protocol.build_status_request(self.device_id)
        logger.debug("[%02X] Sending status request", self.device_id)
        raw = self.connection.send_and_receive(frame, recv_length=RS485_BUFFER_SIZE)
        status = protocol.parse_reply(raw)
        logger.info("[%02X] Status: %s", self.device_id, status)
        return status

    # ── rotation ──────────────────────────────

    def turn_to_side(self, side: str) -> protocol.DeviceStatus:
        """Rotate the panel to the requested side (A, B, or C).

        Sends TURN → waits *go_delay* → sends GO.

        Args:
            side: One of ``'A'``, ``'B'``, or ``'C'`` (case-insensitive).

        Returns:
            :class:`~rotapanel.protocol.DeviceStatus` captured after the
            TURN command (before the rotation physically completes).

        Raises:
            ValueError: If *side* is not A, B, or C.
            rotapanel.connection.ConnectionError: On TCP failure.
            rotapanel.protocol.ParseError       : If the reply is malformed.
        """
        side = side.upper()
        cmd_map = {
            "A": protocol.CMD_TURN_A,
            "B": protocol.CMD_TURN_B,
            "C": protocol.CMD_TURN_C,
        }
        if side not in cmd_map:
            raise ValueError(f"Invalid side '{side}'. Choose A, B, or C.")

        cmd_spec = cmd_map[side]

        # Step 1 – TURN (prepare)
        turn_frame = protocol.build_turn(self.device_id, cmd_spec)
        logger.debug("[%02X] Sending TURN to side %s", self.device_id, side)
        raw = self.connection.send_and_receive(turn_frame, recv_length=RS485_BUFFER_SIZE)
        status = protocol.parse_reply(raw)
        logger.info("[%02X] TURN reply: %s", self.device_id, status)

        # Step 2 – GO (execute)
        time.sleep(self.go_delay)
        go_frame = protocol.build_go(self.device_id)
        logger.debug("[%02X] Sending GO", self.device_id)
        self.connection.send(go_frame)

        return status

    def turn_to_side_a(self) -> protocol.DeviceStatus:
        """Rotate the panel to side A."""
        return self.turn_to_side("A")

    def turn_to_side_b(self) -> protocol.DeviceStatus:
        """Rotate the panel to side B."""
        return self.turn_to_side("B")

    def turn_to_side_c(self) -> protocol.DeviceStatus:
        """Rotate the panel to side C."""
        return self.turn_to_side("C")

    # ── lighting ──────────────────────────────

    def set_light(self, on: bool) -> protocol.DeviceStatus:
        """Turn the panel's (TL) lighting on or off.

        Uses the LIGHT command followed by a GO command.

        Args:
            on: ``True`` to switch lighting on, ``False`` to switch it off.

        Returns:
            :class:`~rotapanel.protocol.DeviceStatus` captured after the
            LIGHT command.

        Raises:
            rotapanel.connection.ConnectionError: On TCP failure.
            rotapanel.protocol.ParseError       : If the reply is malformed.
        """
        state = protocol.LIGHT_ON if on else protocol.LIGHT_OFF
        light_frame = protocol.build_light(self.device_id, state)
        logger.debug("[%02X] Sending LIGHT %s", self.device_id, "ON" if on else "OFF")
        raw = self.connection.send_and_receive(light_frame, recv_length=RS485_BUFFER_SIZE)
        status = protocol.parse_reply(raw)
        logger.info("[%02X] LIGHT reply: %s", self.device_id, status)

        # Execute with GO
        time.sleep(self.go_delay)
        go_frame = protocol.build_go(self.device_id)
        logger.debug("[%02X] Sending GO (for LIGHT)", self.device_id)
        self.connection.send(go_frame)

        return status

    def light_on(self) -> protocol.DeviceStatus:
        """Switch the panel lighting ON."""
        return self.set_light(True)

    def light_off(self) -> protocol.DeviceStatus:
        """Switch the panel lighting OFF."""
        return self.set_light(False)

    # ── error checking ────────────────────────

    def check_errors(self) -> dict:
        """Query the device status and return an error report.

        Returns:
            Dictionary with keys:
              - ``device_id``    : int
              - ``has_errors``   : bool
              - ``error_list``   : list[str]
              - ``status``       : :class:`~rotapanel.protocol.DeviceStatus`

        Raises:
            rotapanel.connection.ConnectionError: On TCP failure.
            rotapanel.protocol.ParseError       : If the reply is malformed.
        """
        status = self.get_status()
        errors = status.error_summary()
        report = {
            "device_id": self.device_id,
            "has_errors": status.has_any_error,
            "error_list": errors,
            "status": status,
        }
        if errors:
            logger.warning("[%02X] Errors detected: %s", self.device_id, errors)
        else:
            logger.info("[%02X] No errors detected", self.device_id)
        return report

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"RotapanelDevice(id={self.device_id}, "
            f"conn={self.connection.host}:{self.connection.port})"
        )
