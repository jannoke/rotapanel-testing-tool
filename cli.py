#!/usr/bin/env python3
"""Command-line interface for the Rotapanel testing tool.

Sub-commands
------------
scan          Scan for live Rotapanel devices on the RS-485 bus.
status        Query the current status of a single device.
control       Turn a device to side A, B, or C.
light         Switch the panel lighting ON or OFF.
check-errors  Check and report device errors.
test          Run the full automated test suite.

Examples
--------
# Scan all 64 addresses
python cli.py scan --host 192.168.1.100 --port 5000

# Scan a range of addresses
python cli.py scan --host 192.168.1.100 --port 5000 --start 0 --end 15

# Turn device 1 to side B
python cli.py control --host 192.168.1.100 --port 5000 --device-id 1 --side B

# Switch lighting on for device 1
python cli.py light --host 192.168.1.100 --port 5000 --device-id 1 --state on

# Check errors on device 1
python cli.py check-errors --host 192.168.1.100 --port 5000 --device-id 1

# Run full automated test on device 1
python cli.py test --host 192.168.1.100 --port 5000 --device-id 1
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

import yaml  # type: ignore[import]

from rotapanel.connection import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
    DEFAULT_RETRIES,
    RotapanelConnection,
)
from rotapanel.device import RotapanelDevice
from rotapanel.scanner import RotapanelScanner
from rotapanel.tests import RotapanelTester
from rotapanel import protocol


# ──────────────────────────────────────────────
# Logging setup
# ──────────────────────────────────────────────

def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )


# ──────────────────────────────────────────────
# Config-file helper
# ──────────────────────────────────────────────

def _load_config(path: Optional[str]) -> dict:
    """Load a YAML config file.  Returns {} if *path* is None or missing."""
    if path is None:
        return {}
    try:
        with open(path) as fh:
            data = yaml.safe_load(fh) or {}
        logging.getLogger(__name__).debug("Loaded config from %s", path)
        return data
    except FileNotFoundError:
        logging.getLogger(__name__).warning("Config file not found: %s", path)
        return {}
    except yaml.YAMLError as exc:
        logging.getLogger(__name__).warning("Config parse error: %s", exc)
        return {}


# ──────────────────────────────────────────────
# Argument parser
# ──────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rotapanel",
        description="Rotapanel RP-2000 testing and control tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Global options
    parser.add_argument(
        "--host",
        default=None,
        help=f"RS485-to-TCP converter IP address (default: {DEFAULT_HOST})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"TCP port (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help=f"Socket timeout in seconds (default: {DEFAULT_TIMEOUT})",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=None,
        help=f"Number of retries on failure (default: {DEFAULT_RETRIES})",
    )
    parser.add_argument(
        "--config",
        default=None,
        metavar="FILE",
        help="Path to YAML configuration file",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging",
    )

    subs = parser.add_subparsers(dest="command", metavar="COMMAND")
    subs.required = True

    # ── scan ─────────────────────────────────
    scan_p = subs.add_parser("scan", help="Scan for live devices")
    scan_p.add_argument(
        "--start",
        type=int,
        default=0,
        help="First address to scan (default: 0)",
    )
    scan_p.add_argument(
        "--end",
        type=int,
        default=protocol.MAX_ADDR,
        help=f"Last address to scan (default: {protocol.MAX_ADDR})",
    )

    # ── status ───────────────────────────────
    status_p = subs.add_parser("status", help="Query device status")
    status_p.add_argument(
        "--device-id",
        type=int,
        required=True,
        metavar="ID",
        help="RS-485 node address (0–63)",
    )

    # ── control ──────────────────────────────
    ctrl_p = subs.add_parser("control", help="Turn panel to a specific side")
    ctrl_p.add_argument(
        "--device-id",
        type=int,
        required=True,
        metavar="ID",
        help="RS-485 node address (0–63)",
    )
    ctrl_p.add_argument(
        "--side",
        choices=["A", "B", "C", "a", "b", "c"],
        required=True,
        help="Target side (A, B, or C)",
    )

    # ── light ────────────────────────────────
    light_p = subs.add_parser("light", help="Control panel lighting")
    light_p.add_argument(
        "--device-id",
        type=int,
        required=True,
        metavar="ID",
        help="RS-485 node address (0–63)",
    )
    light_p.add_argument(
        "--state",
        choices=["on", "off", "ON", "OFF"],
        required=True,
        help="Lighting state: on or off",
    )

    # ── check-errors ─────────────────────────
    err_p = subs.add_parser("check-errors", help="Check device for errors")
    err_p.add_argument(
        "--device-id",
        type=int,
        required=True,
        metavar="ID",
        help="RS-485 node address (0–63)",
    )

    # ── test ─────────────────────────────────
    test_p = subs.add_parser("test", help="Run automated test suite")
    test_p.add_argument(
        "--device-id",
        type=int,
        required=True,
        metavar="ID",
        help="RS-485 node address (0–63)",
    )
    test_p.add_argument(
        "--step-delay",
        type=float,
        default=1.0,
        help="Delay between rotation steps in seconds (default: 1.0)",
    )
    test_p.add_argument(
        "--light-delay",
        type=float,
        default=0.5,
        help="Delay between light on/off in seconds (default: 0.5)",
    )

    return parser


# ──────────────────────────────────────────────
# Command handlers
# ──────────────────────────────────────────────

def cmd_scan(args: argparse.Namespace, host: str, port: int, timeout: float) -> int:
    scanner = RotapanelScanner(
        host=host,
        port=port,
        timeout=timeout,
    )
    print(f"Scanning addresses {args.start}–{args.end} on {host}:{port} …")
    results = scanner.scan(start=args.start, end=args.end)
    online = scanner.online_devices(results)
    print()
    for r in results:
        print(r)
    print()
    print(f"Found {len(online)} device(s) online out of {len(results)} scanned.")
    return 0


def cmd_status(
    args: argparse.Namespace, host: str, port: int, timeout: float, retries: int
) -> int:
    with RotapanelConnection(host, port, timeout=timeout, retries=retries) as conn:
        dev = RotapanelDevice(args.device_id, conn)
        status = dev.get_status()
    print(f"Device {args.device_id} status:")
    print(f"  Current side : {status.side}")
    print(f"  Lighting     : {'ON' if status.lighting_on else 'OFF'}")
    print(f"  Service mode : {'YES' if status.service_mode else 'NO'}")
    errors = status.error_summary()
    if errors:
        print(f"  Errors       : {', '.join(errors)}")
    else:
        print("  Errors       : none")
    return 0


def cmd_control(
    args: argparse.Namespace, host: str, port: int, timeout: float, retries: int
) -> int:
    side = args.side.upper()
    with RotapanelConnection(host, port, timeout=timeout, retries=retries) as conn:
        dev = RotapanelDevice(args.device_id, conn)
        status = dev.turn_to_side(side)
    print(f"Device {args.device_id}: turn to side {side} — OK")
    print(f"  Reply side: {status.side}")
    return 0


def cmd_light(
    args: argparse.Namespace, host: str, port: int, timeout: float, retries: int
) -> int:
    on = args.state.lower() == "on"
    with RotapanelConnection(host, port, timeout=timeout, retries=retries) as conn:
        dev = RotapanelDevice(args.device_id, conn)
        status = dev.set_light(on)
    state_str = "ON" if on else "OFF"
    print(f"Device {args.device_id}: lighting {state_str} — OK")
    print(f"  Reply side: {status.side}")
    return 0


def cmd_check_errors(
    args: argparse.Namespace, host: str, port: int, timeout: float, retries: int
) -> int:
    with RotapanelConnection(host, port, timeout=timeout, retries=retries) as conn:
        dev = RotapanelDevice(args.device_id, conn)
        report = dev.check_errors()
    if report["has_errors"]:
        print(f"Device {args.device_id}: ERRORS DETECTED")
        for err in report["error_list"]:
            print(f"  • {err}")
        return 1
    else:
        print(f"Device {args.device_id}: no errors detected")
        return 0


def cmd_test(
    args: argparse.Namespace, host: str, port: int, timeout: float, retries: int
) -> int:
    with RotapanelConnection(host, port, timeout=timeout, retries=retries) as conn:
        tester = RotapanelTester(
            device_id=args.device_id,
            connection=conn,
            step_delay=args.step_delay,
            light_delay=args.light_delay,
        )
        report = tester.run_full_test()
    print(report.summary())
    return 0 if report.all_passed else 1


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────

def main(argv: Optional[list] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)

    # Merge config file < CLI args (CLI wins)
    cfg = _load_config(args.config)
    host = args.host or cfg.get("host", DEFAULT_HOST)
    port = args.port or cfg.get("port", DEFAULT_PORT)
    timeout = args.timeout if args.timeout is not None else cfg.get("timeout", DEFAULT_TIMEOUT)
    retries = args.retries if args.retries is not None else cfg.get("retries", DEFAULT_RETRIES)

    try:
        if args.command == "scan":
            return cmd_scan(args, host, port, timeout)
        elif args.command == "status":
            return cmd_status(args, host, port, timeout, retries)
        elif args.command == "control":
            return cmd_control(args, host, port, timeout, retries)
        elif args.command == "light":
            return cmd_light(args, host, port, timeout, retries)
        elif args.command == "check-errors":
            return cmd_check_errors(args, host, port, timeout, retries)
        elif args.command == "test":
            return cmd_test(args, host, port, timeout, retries)
    except KeyboardInterrupt:
        print("\nAborted by user.")
        return 130
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).error("Fatal error: %s", exc, exc_info=args.verbose)
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
