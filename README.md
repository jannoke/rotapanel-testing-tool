# Rotapanel Testing Tool

A Python tool for connecting to and testing **Rotapanel RP-2000** prismatic signs
over a TCP/RS-485 bridge.

---

## Table of Contents

1. [Overview](#overview)
2. [Project Structure](#project-structure)
3. [Requirements](#requirements)
4. [Installation](#installation)
5. [Configuration](#configuration)
6. [CLI Usage](#cli-usage)
7. [Protocol Reference](#protocol-reference)
8. [Error Codes](#error-codes)
9. [Python API](#python-api)
10. [Troubleshooting](#troubleshooting)

---

## Overview

The Rotapanel RP-2000 is a prismatic sign with three display faces (**A**, **B**, **C**).
Communication with the sign uses RS-485 at 2400 bps (8N1).  In typical installations
a network device converts the RS-485 bus to a TCP socket, so this tool connects via
plain TCP and speaks the native RP-2000 protocol.

Features:
- Turn panel to **side A, B, or C**
- Control (TL) **lighting on / off**
- Read current **status** (side, lighting, errors)
- **Scan** all 64 RS-485 node addresses in parallel
- **Error detection** and reporting
- **Automated test suite** with timing and pass/fail reporting
- YAML **configuration file** with CLI overrides

---

## Project Structure

```
rotapanel-testing-tool/
├── rotapanel/
│   ├── __init__.py        # Package exports
│   ├── protocol.py        # RS-485 / RP-2000 frame building & parsing
│   ├── connection.py      # TCP connection management
│   ├── device.py          # High-level device control (turn, light, status)
│   ├── scanner.py         # Parallel device scanner
│   └── tests.py           # Automated test sequences & reporting
├── cli.py                 # Command-line interface
├── config.yaml            # Example configuration file
├── requirements.txt       # Python dependencies
└── README.md
```

---

## Requirements

- Python 3.8 or later
- [PyYAML](https://pypi.org/project/PyYAML/) ≥ 6.0

---

## Installation

```bash
# Clone the repository
git clone https://github.com/jannoke/rotapanel-testing-tool.git
cd rotapanel-testing-tool

# Install dependencies
pip install -r requirements.txt
```

---

## Configuration

Copy and edit `config.yaml`:

```yaml
host: "192.168.1.100"   # IP of the RS485-to-TCP converter
port: 5000              # TCP port
timeout: 2.0            # Socket timeout (seconds)
retries: 3              # Retries on failure
```

All settings can be overridden per-command with CLI flags.

---

## CLI Usage

All sub-commands share these global flags:

| Flag | Description | Default |
|---|---|---|
| `--host IP` | Converter IP address | `192.168.1.100` |
| `--port N` | TCP port | `5000` |
| `--timeout N` | Socket timeout (s) | `2.0` |
| `--retries N` | Retry attempts | `3` |
| `--config FILE` | Path to YAML config | _(none)_ |
| `-v / --verbose` | Debug logging | off |

### scan — discover devices

```bash
python cli.py scan --host 192.168.1.100 --port 5000
python cli.py scan --host 192.168.1.100 --port 5000 --start 0 --end 15
```

Scans device IDs 0–63 (or the specified range) in parallel and lists which
devices are online together with their current side and lighting state.

Options: `--start N`, `--end N`, `--workers N`

### status — query device state

```bash
python cli.py status --host 192.168.1.100 --port 5000 --device-id 1
```

Prints the current side, lighting state, service mode, and any errors.

### control — rotate to a side

```bash
python cli.py control --host 192.168.1.100 --port 5000 --device-id 1 --side A
python cli.py control --host 192.168.1.100 --port 5000 --device-id 1 --side B
python cli.py control --host 192.168.1.100 --port 5000 --device-id 1 --side C
```

Sends a TURN (prepare) command followed by a GO (execute) command.

### light — control lighting

```bash
python cli.py light --host 192.168.1.100 --port 5000 --device-id 1 --state on
python cli.py light --host 192.168.1.100 --port 5000 --device-id 1 --state off
```

Sends a LIGHT (prepare) command followed by a GO (execute) command.

### check-errors — error report

```bash
python cli.py check-errors --host 192.168.1.100 --port 5000 --device-id 1
```

Returns exit code **0** if no errors are present, **1** if errors are found.

### test — automated test suite

```bash
python cli.py test --host 192.168.1.100 --port 5000 --device-id 1
python cli.py test --host 192.168.1.100 --port 5000 --device-id 1 \
    --step-delay 2.0 --light-delay 1.0
```

Runs the full test sequence:

1. Status request
2. Error check
3. Turn → A
4. Turn → B
5. Turn → C
6. Turn → A (home)
7. Light ON
8. Light OFF

Prints a formatted pass/fail report.  Returns exit code **0** if all steps
pass, **1** otherwise.

### Using a config file

```bash
python cli.py --config config.yaml scan
python cli.py --config config.yaml test --device-id 3
```

---

## Protocol Reference

### Physical Layer

| Parameter | Value |
|---|---|
| Medium | RS-485 2-wire twisted pair |
| Baud rate | 2400 bps |
| Data bits | 8 |
| Start bits | 1 |
| Stop bits | 1 |
| Parity | None |
| Handshaking | None |

### Data-Link Frame (outgoing)

```
┌────────┬──────────────┬──────────────────────┬─────┐
│ HEADER │ ADDR_IND     │ APPLICATION DATA     │ BCC │
│ 0x00   │ '#' (0x23)   │ command bytes …      │ 1 B │
└────────┴──────────────┴──────────────────────┴─────┘

BCC = 255 − (sum(APPLICATION DATA) & 0xFF)
```

### TURN Command

Prepares the panel for rotation (or requests status):

```
APPLICATION DATA = [device_id, 'T', cmd_spec]
```

| `cmd_spec` | Meaning |
|---|---|
| `0x00` | No rotation — status request only |
| `0x40` | Prepare turn to side **A** |
| `0x80` | Prepare turn to side **B** |
| `0xC0` | Prepare turn to side **C** |

### GO Command

Executes the previously prepared rotation:

```
APPLICATION DATA = [device_id, 'G']
```

No reply is sent by the device for a GO command.

### LIGHT Command

Prepares the lighting state:

```
APPLICATION DATA = [device_id, 'L', state]
```

| `state` | Meaning |
|---|---|
| `0x00` | Lighting **OFF** |
| `0x01` | Lighting **ON** |

Follow with a GO command to execute.

### REPLY Message (incoming)

```
┌────────┬──────────┬─────────┬────────┬─────┬──────┐
│ 0x00   │ 'R'      │ address │ status │ BCC │ 0xFE │
│ 1 byte │ 1 byte   │ 1 byte  │ 1 byte │ 1 B │ EOT  │
└────────┴──────────┴─────────┴────────┴─────┴──────┘

BCC = 255 − (sum(['R', address, status]) & 0xFF)
EOT = 0xFE  (must follow immediately after BCC)
```

### Status Byte

| Bit(s) | Mask | Meaning |
|---|---|---|
| 7–6 | `0xC0` | Panel position: `01`=A  `10`=B  `11`=C |
| 4 | `0x10` | Lighting: `0`=off  `1`=on |
| 3 | `0x08` | Service mode: `0`=normal  `1`=service |
| 2 | `0x04` | Temporary error: `0`=none  `1`=present |
| 1 | `0x02` | Error: `0`=none  `1`=present |
| 0 | `0x01` | Side A at startup: `0`=found  `1`=not found |

### Addressing

| Address | Meaning |
|---|---|
| `0x00`–`0x3F` | Individual node (0–63) |
| `0xFF` | Broadcast (no reply expected) |

---

## Error Codes

| Error | Status bit | Description |
|---|---|---|
| Mechanical error | bit 1 | Motor or mechanical fault |
| Temporary error | bit 2 | Recoverable / transient fault |
| Side A not found | bit 0 | Homing reference not detected at startup |

If a device is in **service mode** (bit 3) it is under maintenance; normal
commands may be ignored.

---

## Python API

```python
from rotapanel import RotapanelConnection, RotapanelDevice, RotapanelScanner, RotapanelTester

# ── direct device control ────────────────────────────────────────────────
with RotapanelConnection("192.168.1.100", 5000) as conn:
    dev = RotapanelDevice(device_id=1, connection=conn)

    status = dev.get_status()
    print(status.side, status.lighting_on)

    dev.turn_to_side("B")   # or dev.turn_to_side_b()
    dev.light_on()
    dev.light_off()

    report = dev.check_errors()
    if report["has_errors"]:
        print(report["error_list"])

# ── scanner ──────────────────────────────────────────────────────────────
scanner = RotapanelScanner("192.168.1.100", 5000)
results = scanner.scan()                          # scans 0–63
online  = RotapanelScanner.online_devices(results)
alive   = scanner.is_alive(5)                     # quick single-device check

# ── automated tests ──────────────────────────────────────────────────────
with RotapanelConnection("192.168.1.100", 5000) as conn:
    tester = RotapanelTester(device_id=1, connection=conn)
    report = tester.run_full_test()
    print(report.summary())
    print("Pass?", report.all_passed)
```

---

## Troubleshooting

| Symptom | Likely cause | Solution |
|---|---|---|
| `ConnectionError: Could not connect` | Wrong IP/port or converter offline | Verify the converter's IP and TCP port; check network path |
| `ConnectionError: Receive timed out` | Device not responding | Check RS-485 wiring and device power; increase `--timeout` |
| `ParseError: BCC mismatch` | Corrupted frame | Check cable quality; reduce baud rate on converter if adjustable |
| `ParseError: header not found` | Unexpected bytes from converter | Verify converter framing mode (transparent / raw) |
| Device shown as OFFLINE in scan | Wrong ID or device off | Confirm the dip-switch address on the RP-2000 unit |
| Side A not found error | Homing failure at power-on | Power-cycle the unit; inspect the homing sensor |
| Mechanical error | Motor fault | Power-cycle; check for obstructions; contact service |
