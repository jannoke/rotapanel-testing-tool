"""RS485/TCP protocol implementation for Rotapanel RP-2000.

Protocol specification (from ANWB protocol v1.0 + LIGHT extension):

Physical layer
--------------
- Medium   : RS-485, 2-wire twisted pair
- Baud rate: 2400 bps
- Format   : 8 data bits, 1 start bit, 1 stop bit, no parity

Data-link frame (outgoing)
--------------------------
  HEADER  |  ADDR_IND  |  APPLICATION DATA  |  BCC
  0x00    |   '#'      |  command bytes     |  checksum

  BCC = 255 - (sum(application_data) & 0xFF)

Reply frame (incoming from device)
-----------------------------------
  FILL  |  REPLY_IND  |  ADDRESS  |  STATUS  |  BCC  |  EOT
  0x00  |   'R'       |  1 byte   |  1 byte  | 1 byte| 0xFE

Application commands
--------------------
  TURN  : [addr, 'T', cmd_spec]
  GO    : [addr, 'G']
  LIGHT : [addr, 'L', state]

TURN cmd_spec
  0x00 = status request only (no rotation prepared)
  0x40 = prepare turn to side A
  0x80 = prepare turn to side B
  0xC0 = prepare turn to side C

LIGHT state
  0x00 = lighting OFF
  0x01 = lighting ON

Status byte bits
  Bit 7/6 : panel position  01=side A  10=side B  11=side C
  Bit 4   : lighting        0=off      1=on
  Bit 3   : service mode    0=normal   1=service
  Bit 2   : temporary error 0=none     1=present
  Bit 1   : error           0=none     1=present
  Bit 0   : side A startup  0=found    1=not found
"""

from __future__ import annotations

# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────
HEADER: int = 0x00
ADDR_IND: bytes = b"#"
REPLY_IND: bytes = b"R"
EOT: int = 0xFE

BROADCAST_ADDR: int = 0xFF

# Address range for individual nodes
MIN_ADDR: int = 0x00
MAX_ADDR: int = 0x3F

# TURN command specifications
CMD_STATUS_ONLY: int = 0x00
CMD_TURN_A: int = 0x40
CMD_TURN_B: int = 0x80
CMD_TURN_C: int = 0xC0

# LIGHT command specifications
LIGHT_OFF: int = 0x00
LIGHT_ON: int = 0x01

# Reply frame length (fill + 'R' + addr + status + BCC + EOT)
REPLY_LENGTH: int = 6

# Panel status masks / shifts
_STATUS_SIDE_MASK: int = 0xC0  # bits 7 and 6
_STATUS_LIGHT_BIT: int = 0x10  # bit 4
_STATUS_SERVICE_BIT: int = 0x08  # bit 3
_STATUS_TEMP_ERR_BIT: int = 0x04  # bit 2
_STATUS_ERR_BIT: int = 0x02  # bit 1
_STATUS_SIDE_A_INIT_BIT: int = 0x01  # bit 0

SIDE_CODE_A: int = 0x40  # 01xxxxxx
SIDE_CODE_B: int = 0x80  # 10xxxxxx
SIDE_CODE_C: int = 0xC0  # 11xxxxxx

SIDE_NAMES: dict[int, str] = {
    SIDE_CODE_A: "A",
    SIDE_CODE_B: "B",
    SIDE_CODE_C: "C",
}


# ──────────────────────────────────────────────
# BCC helpers
# ──────────────────────────────────────────────

def _bcc(application_data: bytes) -> int:
    """Return the Block Check Character for *application_data*.

    BCC = 255 - (sum(application_data) & 0xFF)
    """
    return (255 - (sum(application_data) & 0xFF)) & 0xFF


def _frame(application_data: bytes) -> bytes:
    """Wrap *application_data* in a complete data-link frame."""
    bcc = _bcc(application_data)
    return bytes([HEADER]) + ADDR_IND + application_data + bytes([bcc])


# ──────────────────────────────────────────────
# Frame builders
# ──────────────────────────────────────────────

def build_turn(device_id: int, cmd_spec: int) -> bytes:
    """Build a TURN command frame.

    Args:
        device_id: Node address (0x00-0x3F or 0xFF for broadcast).
        cmd_spec : Command specification byte (CMD_STATUS_ONLY,
                   CMD_TURN_A, CMD_TURN_B, CMD_TURN_C).

    Returns:
        Bytes to send over TCP.
    """
    _validate_address(device_id)
    app_data = bytes([device_id, ord("T"), cmd_spec])
    return _frame(app_data)


def build_go(device_id: int) -> bytes:
    """Build a GO command frame.

    Args:
        device_id: Node address (0x00-0x3F or 0xFF for broadcast).

    Returns:
        Bytes to send over TCP.
    """
    _validate_address(device_id)
    app_data = bytes([device_id, ord("G")])
    return _frame(app_data)


def build_light(device_id: int, state: int) -> bytes:
    """Build a LIGHT command frame.

    Args:
        device_id: Node address (0x00-0x3F or 0xFF for broadcast).
        state    : LIGHT_ON (0x01) or LIGHT_OFF (0x00).

    Returns:
        Bytes to send over TCP.
    """
    _validate_address(device_id)
    if state not in (LIGHT_OFF, LIGHT_ON):
        raise ValueError(f"Invalid light state: 0x{state:02X}. Use LIGHT_ON or LIGHT_OFF.")
    app_data = bytes([device_id, ord("L"), state])
    return _frame(app_data)


def build_status_request(device_id: int) -> bytes:
    """Build a status-request TURN frame (CMD_STATUS_ONLY).

    Args:
        device_id: Node address.

    Returns:
        Bytes to send over TCP.
    """
    return build_turn(device_id, CMD_STATUS_ONLY)


# ──────────────────────────────────────────────
# Reply parsing
# ──────────────────────────────────────────────

class ParseError(Exception):
    """Raised when a reply frame cannot be parsed."""


class DeviceStatus:
    """Parsed status from a REPLY message."""

    def __init__(self, address: int, raw_status: int) -> None:
        self.address: int = address
        self.raw_status: int = raw_status

        side_code = raw_status & _STATUS_SIDE_MASK
        self.side: str = SIDE_NAMES.get(side_code, "unknown")
        self.lighting_on: bool = bool(raw_status & _STATUS_LIGHT_BIT)
        self.service_mode: bool = bool(raw_status & _STATUS_SERVICE_BIT)
        self.temporary_error: bool = bool(raw_status & _STATUS_TEMP_ERR_BIT)
        self.error: bool = bool(raw_status & _STATUS_ERR_BIT)
        self.side_a_not_found: bool = bool(raw_status & _STATUS_SIDE_A_INIT_BIT)

    # ── convenience ──────────────────────────

    @property
    def has_any_error(self) -> bool:
        return self.error or self.temporary_error or self.side_a_not_found

    def error_summary(self) -> list[str]:
        """Return a list of human-readable error descriptions."""
        errors: list[str] = []
        if self.error:
            errors.append("Mechanical error")
        if self.temporary_error:
            errors.append("Temporary error")
        if self.side_a_not_found:
            errors.append("Side A not found at startup")
        return errors

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"DeviceStatus(addr=0x{self.address:02X}, side={self.side}, "
            f"light={'on' if self.lighting_on else 'off'}, "
            f"service={self.service_mode}, "
            f"errors={self.error_summary() or 'none'})"
        )


def parse_reply(data: bytes) -> DeviceStatus:
    """Parse a raw reply frame received from the device.

    Expected frame layout:
        [0x00] ['R'] [addr] [status] [BCC] [0xFE]

    Args:
        data: Raw bytes received from the TCP socket.

    Returns:
        Parsed :class:`DeviceStatus` object.

    Raises:
        ParseError: If *data* is malformed or the BCC is invalid.
    """
    if len(data) < REPLY_LENGTH:
        raise ParseError(
            f"Reply too short: expected {REPLY_LENGTH} bytes, got {len(data)}."
        )

    # Scan for the 0x00 'R' signature (device may prepend extra bytes)
    start = -1
    for i in range(len(data) - REPLY_LENGTH + 1):
        if data[i] == 0x00 and data[i + 1] == ord("R"):
            start = i
            break

    if start == -1:
        raise ParseError(
            f"Reply frame header (0x00 'R') not found in: {data.hex()}"
        )

    fill = data[start]          # 0x00
    reply_ind = data[start + 1]  # 'R'
    address = data[start + 2]
    status = data[start + 3]
    received_bcc = data[start + 4]

    # Verify BCC — covers 'R' + address + status (the application data bytes)
    expected_bcc = _bcc(bytes([reply_ind, address, status]))
    if received_bcc != expected_bcc:
        raise ParseError(
            f"BCC mismatch: expected 0x{expected_bcc:02X}, got 0x{received_bcc:02X}."
        )

    return DeviceStatus(address, status)


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _validate_address(device_id: int) -> None:
    if not (MIN_ADDR <= device_id <= MAX_ADDR or device_id == BROADCAST_ADDR):
        raise ValueError(
            f"Invalid device address: 0x{device_id:02X}. "
            f"Must be 0x{MIN_ADDR:02X}–0x{MAX_ADDR:02X} or 0x{BROADCAST_ADDR:02X} (broadcast)."
        )
