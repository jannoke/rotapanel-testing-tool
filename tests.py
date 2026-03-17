"""Unit tests for the Rotapanel testing tool.

Tests cover protocol frame building/parsing, device control logic,
scanner behaviour, and the test-runner — all without requiring a real
TCP connection (sockets are mocked).
"""

from __future__ import annotations

import pathlib
import unittest
from unittest.mock import MagicMock, patch

from rotapanel import protocol
from rotapanel.protocol import (
    BROADCAST_ADDR,
    CMD_TURN_A,
    CMD_TURN_B,
    CMD_TURN_C,
    CMD_STATUS_ONLY,
    LIGHT_OFF,
    LIGHT_ON,
    DeviceStatus,
    ParseError,
    build_go,
    build_light,
    build_status_request,
    build_turn,
    parse_reply,
    _bcc,
    _frame,
)
from rotapanel.connection import RotapanelConnection, ConnectionError
from rotapanel.device import RotapanelDevice
from rotapanel.scanner import RotapanelScanner, ScanResult, _probe_one
from rotapanel.tests import RotapanelTester


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _build_reply(address: int, status: int) -> bytes:
    """Construct a valid REPLY frame for testing."""
    reply_ind = ord("R")
    bcc = _bcc(bytes([reply_ind, address, status]))
    return bytes([0x00, reply_ind, address, status, bcc, 0xFE])


# ──────────────────────────────────────────────
# Protocol — BCC
# ──────────────────────────────────────────────

class TestBCC(unittest.TestCase):
    def test_bcc_basic(self):
        # From VB example: TURN to side A for device 0
        # app_data = [0x00, ord('T'), 0x40]
        app_data = bytes([0x00, ord("T"), 0x40])
        result = _bcc(app_data)
        expected = (255 - (0x00 + ord("T") + 0x40)) & 0xFF
        self.assertEqual(result, expected)

    def test_bcc_go(self):
        # GO for device 1
        app_data = bytes([0x01, ord("G")])
        result = _bcc(app_data)
        expected = (255 - (0x01 + ord("G"))) & 0xFF
        self.assertEqual(result, expected)

    def test_bcc_wraps(self):
        # Ensure wrap-around: sum > 255
        app_data = bytes([0xFF, 0xFF])
        result = _bcc(app_data)
        self.assertGreaterEqual(result, 0)
        self.assertLessEqual(result, 255)

    def test_bcc_light(self):
        app_data = bytes([0x05, ord("L"), LIGHT_ON])
        result = _bcc(app_data)
        expected = (255 - (0x05 + ord("L") + LIGHT_ON)) & 0xFF
        self.assertEqual(result, expected)


# ──────────────────────────────────────────────
# Protocol — frame builders
# ──────────────────────────────────────────────

class TestFrameBuilders(unittest.TestCase):
    def _assert_frame_structure(self, frame: bytes, app_data: bytes):
        self.assertEqual(frame[0], 0x00)         # HEADER
        self.assertEqual(frame[1], ord("#"))     # ADDR_IND
        self.assertEqual(frame[2:2 + len(app_data)], app_data)
        self.assertEqual(frame[-1], _bcc(app_data))

    def test_build_turn_side_a(self):
        frame = build_turn(0x01, CMD_TURN_A)
        expected_app = bytes([0x01, ord("T"), CMD_TURN_A])
        self._assert_frame_structure(frame, expected_app)

    def test_build_turn_side_b(self):
        frame = build_turn(0x02, CMD_TURN_B)
        expected_app = bytes([0x02, ord("T"), CMD_TURN_B])
        self._assert_frame_structure(frame, expected_app)

    def test_build_turn_side_c(self):
        frame = build_turn(0x03, CMD_TURN_C)
        expected_app = bytes([0x03, ord("T"), CMD_TURN_C])
        self._assert_frame_structure(frame, expected_app)

    def test_build_turn_status_only(self):
        frame = build_turn(0x0A, CMD_STATUS_ONLY)
        expected_app = bytes([0x0A, ord("T"), CMD_STATUS_ONLY])
        self._assert_frame_structure(frame, expected_app)

    def test_build_go(self):
        frame = build_go(0x05)
        expected_app = bytes([0x05, ord("G")])
        self._assert_frame_structure(frame, expected_app)

    def test_build_light_on(self):
        frame = build_light(0x07, LIGHT_ON)
        expected_app = bytes([0x07, ord("L"), LIGHT_ON])
        self._assert_frame_structure(frame, expected_app)

    def test_build_light_off(self):
        frame = build_light(0x07, LIGHT_OFF)
        expected_app = bytes([0x07, ord("L"), LIGHT_OFF])
        self._assert_frame_structure(frame, expected_app)

    def test_build_status_request(self):
        frame = build_status_request(0x0F)
        expected_app = bytes([0x0F, ord("T"), CMD_STATUS_ONLY])
        self._assert_frame_structure(frame, expected_app)

    def test_broadcast_address(self):
        frame = build_go(BROADCAST_ADDR)
        self.assertEqual(frame[2], BROADCAST_ADDR)

    def test_invalid_address_raises(self):
        with self.assertRaises(ValueError):
            build_turn(0x40, CMD_TURN_A)  # 0x40 = 64, just out of range

    def test_invalid_light_state_raises(self):
        with self.assertRaises(ValueError):
            build_light(0x01, 0x05)


# ──────────────────────────────────────────────
# Protocol — reply parsing
# ──────────────────────────────────────────────

class TestParseReply(unittest.TestCase):
    def test_parse_side_a(self):
        # Bit 7/6 = 01 → side A, no errors, no light
        raw = _build_reply(0x01, 0x40)
        status = parse_reply(raw)
        self.assertEqual(status.address, 0x01)
        self.assertEqual(status.side, "A")
        self.assertFalse(status.lighting_on)
        self.assertFalse(status.error)
        self.assertFalse(status.temporary_error)
        self.assertFalse(status.side_a_not_found)
        self.assertFalse(status.has_any_error)

    def test_parse_side_b(self):
        raw = _build_reply(0x02, 0x80)
        status = parse_reply(raw)
        self.assertEqual(status.side, "B")

    def test_parse_side_c(self):
        raw = _build_reply(0x03, 0xC0)
        status = parse_reply(raw)
        self.assertEqual(status.side, "C")

    def test_parse_lighting_on(self):
        raw = _build_reply(0x01, 0x40 | 0x10)  # side A + light
        status = parse_reply(raw)
        self.assertTrue(status.lighting_on)

    def test_parse_error_bits(self):
        raw = _build_reply(0x01, 0x40 | 0x04 | 0x02)  # side A + temp err + err
        status = parse_reply(raw)
        self.assertTrue(status.temporary_error)
        self.assertTrue(status.error)
        self.assertTrue(status.has_any_error)
        self.assertIn("Mechanical error", status.error_summary())
        self.assertIn("Temporary error", status.error_summary())

    def test_parse_side_a_not_found(self):
        raw = _build_reply(0x01, 0x40 | 0x01)
        status = parse_reply(raw)
        self.assertTrue(status.side_a_not_found)
        self.assertIn("Side A not found at startup", status.error_summary())

    def test_parse_service_mode(self):
        raw = _build_reply(0x01, 0x40 | 0x08)
        status = parse_reply(raw)
        self.assertTrue(status.service_mode)

    def test_parse_bcc_mismatch_raises(self):
        raw = _build_reply(0x01, 0x40)
        corrupted = bytearray(raw)
        corrupted[-2] ^= 0xFF  # flip BCC
        with self.assertRaises(ParseError):
            parse_reply(bytes(corrupted))

    def test_parse_too_short_raises(self):
        with self.assertRaises(ParseError):
            parse_reply(b"\x00\x52\x01")  # only 3 bytes

    def test_parse_missing_header_raises(self):
        with self.assertRaises(ParseError):
            parse_reply(b"\xFF\xFF\xFF\xFF\xFF\xFF")


# ──────────────────────────────────────────────
# DeviceStatus
# ──────────────────────────────────────────────

class TestDeviceStatus(unittest.TestCase):
    def test_no_errors(self):
        s = DeviceStatus(1, 0x40)
        self.assertFalse(s.has_any_error)
        self.assertEqual(s.error_summary(), [])

    def test_all_errors(self):
        s = DeviceStatus(1, 0x40 | 0x07)  # all error bits set
        self.assertTrue(s.has_any_error)
        self.assertEqual(len(s.error_summary()), 3)

    def test_unknown_side(self):
        s = DeviceStatus(1, 0x00)  # bits 7/6 both 0 → undefined
        self.assertEqual(s.side, "unknown")


# ──────────────────────────────────────────────
# Connection
# ──────────────────────────────────────────────

class TestRotapanelConnection(unittest.TestCase):
    @patch("rotapanel.connection.socket.socket")
    def test_connect_success(self, mock_socket_cls):
        mock_sock = mock_socket_cls.return_value  # the instance returned by socket.socket()

        conn = RotapanelConnection("127.0.0.1", 9999, timeout=1.0, retries=1)
        conn.connect()
        self.assertTrue(conn.is_connected)
        mock_sock.connect.assert_called_once_with(("127.0.0.1", 9999))

    @patch("rotapanel.connection.socket.socket")
    def test_connect_failure_raises(self, mock_socket_cls):
        mock_sock = mock_socket_cls.return_value
        mock_sock.connect.side_effect = OSError("refused")

        conn = RotapanelConnection("127.0.0.1", 9999, timeout=0.1, retries=1)
        with self.assertRaises(ConnectionError):
            conn.connect()

    @patch("rotapanel.connection.socket.socket")
    def test_disconnect(self, mock_socket_cls):
        mock_sock = mock_socket_cls.return_value

        conn = RotapanelConnection("127.0.0.1", 9999, timeout=1.0, retries=1)
        conn.connect()
        conn.disconnect()
        self.assertFalse(conn.is_connected)
        mock_sock.close.assert_called_once()

    @patch("rotapanel.connection.socket.socket")
    def test_send_and_receive(self, mock_socket_cls):
        reply = _build_reply(0x01, 0x40)
        mock_sock = mock_socket_cls.return_value
        mock_sock.recv.return_value = reply

        conn = RotapanelConnection("127.0.0.1", 9999, timeout=1.0, retries=1)
        conn.connect()
        result = conn.send_and_receive(build_status_request(1))
        self.assertEqual(result, reply)

    def test_send_without_connect_raises(self):
        conn = RotapanelConnection("127.0.0.1", 9999)
        with self.assertRaises(ConnectionError):
            conn.send(b"\x00")

    def test_receive_without_connect_raises(self):
        conn = RotapanelConnection("127.0.0.1", 9999)
        with self.assertRaises(ConnectionError):
            conn.receive()

    @patch("rotapanel.connection.socket.socket")
    def test_context_manager(self, mock_socket_cls):
        mock_socket_cls.return_value  # ensure instance is created

        with RotapanelConnection("127.0.0.1", 9999, timeout=1.0, retries=1) as conn:
            self.assertTrue(conn.is_connected)
        self.assertFalse(conn.is_connected)


# ──────────────────────────────────────────────
# Device
# ──────────────────────────────────────────────

class TestRotapanelDevice(unittest.TestCase):
    def _make_device(self, status_byte: int = 0x40):
        """Return a RotapanelDevice backed by a mocked connection."""
        reply = _build_reply(0x01, status_byte)
        conn = MagicMock(spec=RotapanelConnection)
        conn.send_and_receive.return_value = reply
        conn.is_connected = True
        dev = RotapanelDevice(device_id=1, connection=conn, go_delay=0)
        return dev, conn

    def test_get_status(self):
        dev, conn = self._make_device(0x40)
        status = dev.get_status()
        self.assertEqual(status.side, "A")
        conn.send_and_receive.assert_called_once()

    def test_turn_to_side_b(self):
        dev, conn = self._make_device(0x80)
        dev.turn_to_side("B")
        # TURN → send_and_receive, GO → send
        conn.send_and_receive.assert_called_once()
        conn.send.assert_called_once()
        sent_go = conn.send.call_args[0][0]
        self.assertEqual(sent_go[2], 0x01)   # device address
        self.assertEqual(sent_go[3], ord("G"))

    def test_turn_to_side_invalid_raises(self):
        dev, conn = self._make_device()
        with self.assertRaises(ValueError):
            dev.turn_to_side("Z")

    def test_turn_a_convenience(self):
        dev, conn = self._make_device(0x40)
        dev.turn_to_side_a()
        conn.send_and_receive.assert_called_once()

    def test_turn_b_convenience(self):
        dev, conn = self._make_device(0x80)
        dev.turn_to_side_b()
        conn.send_and_receive.assert_called_once()

    def test_turn_c_convenience(self):
        dev, conn = self._make_device(0xC0)
        dev.turn_to_side_c()
        conn.send_and_receive.assert_called_once()

    def test_light_on(self):
        dev, conn = self._make_device(0x40 | 0x10)
        status = dev.light_on()
        conn.send_and_receive.assert_called_once()
        conn.send.assert_called_once()
        # Verify the LIGHT frame contains 'L' + 0x01
        sent_frame = conn.send_and_receive.call_args[0][0]
        self.assertEqual(sent_frame[3], ord("L"))
        self.assertEqual(sent_frame[4], LIGHT_ON)

    def test_light_off(self):
        dev, conn = self._make_device(0x40)
        dev.light_off()
        sent_frame = conn.send_and_receive.call_args[0][0]
        self.assertEqual(sent_frame[3], ord("L"))
        self.assertEqual(sent_frame[4], LIGHT_OFF)

    def test_set_light_false(self):
        dev, conn = self._make_device(0x40)
        dev.set_light(False)
        sent_frame = conn.send_and_receive.call_args[0][0]
        self.assertEqual(sent_frame[4], LIGHT_OFF)

    def test_check_errors_no_errors(self):
        dev, _ = self._make_device(0x40)
        report = dev.check_errors()
        self.assertFalse(report["has_errors"])
        self.assertEqual(report["error_list"], [])

    def test_check_errors_with_errors(self):
        dev, _ = self._make_device(0x40 | 0x02)  # mechanical error
        report = dev.check_errors()
        self.assertTrue(report["has_errors"])
        self.assertIn("Mechanical error", report["error_list"])

    def test_invalid_device_id_raises(self):
        conn = MagicMock(spec=RotapanelConnection)
        with self.assertRaises(ValueError):
            RotapanelDevice(device_id=64, connection=conn)


# ──────────────────────────────────────────────
# Scanner
# ──────────────────────────────────────────────

class TestRotapanelScanner(unittest.TestCase):

    def test_online_devices_filter(self):
        results = [
            ScanResult(device_id=0, online=False),
            ScanResult(device_id=1, online=True, side="A", lighting_on=False),
            ScanResult(device_id=2, online=False),
            ScanResult(device_id=3, online=True, side="B", lighting_on=True),
        ]
        online = RotapanelScanner.online_devices(results)
        self.assertEqual([r.device_id for r in online], [1, 3])

    def test_scan_result_str_online(self):
        r = ScanResult(device_id=5, online=True, side="C", lighting_on=True)
        text = str(r)
        self.assertIn("ONLINE", text)
        self.assertIn("side=C", text)

    def test_scan_result_str_offline(self):
        r = ScanResult(device_id=7, online=False, error_message="timeout")
        text = str(r)
        self.assertIn("OFFLINE", text)
        self.assertIn("timeout", text)

    @patch("rotapanel.scanner._probe_one")
    @patch("rotapanel.scanner.RotapanelConnection")
    def test_scan_probes_each_address_sequentially(self, mock_conn_cls, mock_probe):
        """scan() calls _probe_one once per address, in order, using one connection."""
        mock_conn = mock_conn_cls.return_value
        mock_conn.is_connected = True
        mock_probe.side_effect = lambda conn, addr: ScanResult(device_id=addr, online=False)

        scanner = RotapanelScanner("127.0.0.1", 9999)
        results = scanner.scan(start=0, end=3)

        # One connection opened and closed
        mock_conn.connect.assert_called_once()
        mock_conn.disconnect.assert_called_once()

        # _probe_one called once per address with the same connection object
        self.assertEqual(mock_probe.call_count, 4)
        probed_ids = [call.args[1] for call in mock_probe.call_args_list]
        self.assertEqual(probed_ids, [0, 1, 2, 3])
        for call in mock_probe.call_args_list:
            self.assertIs(call.args[0], mock_conn)

    @patch("rotapanel.scanner._probe_one")
    @patch("rotapanel.scanner.RotapanelConnection")
    def test_scan_results_in_address_order(self, mock_conn_cls, mock_probe):
        mock_conn = mock_conn_cls.return_value
        mock_conn.is_connected = True
        mock_probe.side_effect = lambda conn, addr: ScanResult(device_id=addr, online=False)

        scanner = RotapanelScanner("127.0.0.1", 9999)
        results = scanner.scan(start=0, end=4)
        ids = [r.device_id for r in results]
        self.assertEqual(ids, [0, 1, 2, 3, 4])

    @patch("rotapanel.scanner.RotapanelConnection")
    def test_scan_all_offline_if_cannot_connect(self, mock_conn_cls):
        mock_conn = mock_conn_cls.return_value
        mock_conn.connect.side_effect = ConnectionError("refused")

        scanner = RotapanelScanner("127.0.0.1", 9999)
        results = scanner.scan(start=0, end=2)

        self.assertEqual(len(results), 3)
        self.assertTrue(all(not r.online for r in results))

    @patch("rotapanel.scanner._probe_one")
    @patch("rotapanel.scanner.RotapanelConnection")
    def test_scan_reconnects_after_connection_drop(self, mock_conn_cls, mock_probe):
        """If is_connected becomes False mid-scan, reconnect and continue."""
        mock_conn = mock_conn_cls.return_value

        # After address 1 is probed, simulate a connection drop
        call_count = [0]
        def side_effect(conn, addr):
            call_count[0] += 1
            if call_count[0] == 2:
                mock_conn.is_connected = False  # simulate drop after addr 1
            else:
                mock_conn.is_connected = True
            return ScanResult(device_id=addr, online=False, error_message="timeout")

        mock_conn.is_connected = True
        mock_probe.side_effect = side_effect

        scanner = RotapanelScanner("127.0.0.1", 9999)
        results = scanner.scan(start=0, end=3)

        # Should have attempted reconnect
        self.assertGreater(mock_conn.connect.call_count, 1)
        self.assertEqual(len(results), 4)

    @patch("rotapanel.scanner._probe_one")
    @patch("rotapanel.scanner.RotapanelConnection")
    def test_is_alive_true(self, mock_conn_cls, mock_probe):
        mock_conn = mock_conn_cls.return_value
        mock_probe.return_value = ScanResult(device_id=1, online=True, side="A", lighting_on=False)

        scanner = RotapanelScanner("127.0.0.1", 9999)
        self.assertTrue(scanner.is_alive(1))
        mock_conn.connect.assert_called_once()
        mock_conn.disconnect.assert_called_once()

    @patch("rotapanel.scanner._probe_one")
    @patch("rotapanel.scanner.RotapanelConnection")
    def test_is_alive_false(self, mock_conn_cls, mock_probe):
        mock_conn = mock_conn_cls.return_value
        mock_probe.return_value = ScanResult(device_id=2, online=False)

        scanner = RotapanelScanner("127.0.0.1", 9999)
        self.assertFalse(scanner.is_alive(2))

    @patch("rotapanel.scanner.RotapanelConnection")
    def test_is_alive_false_on_connection_error(self, mock_conn_cls):
        mock_conn = mock_conn_cls.return_value
        mock_conn.connect.side_effect = ConnectionError("refused")

        scanner = RotapanelScanner("127.0.0.1", 9999)
        self.assertFalse(scanner.is_alive(3))


# ──────────────────────────────────────────────
# Tester / TestReport
# ──────────────────────────────────────────────

class TestRotapanelTester(unittest.TestCase):
    def _make_tester(self, status_byte: int = 0x40):
        reply = _build_reply(0x01, status_byte)
        conn = MagicMock(spec=RotapanelConnection)
        conn.send_and_receive.return_value = reply
        conn.is_connected = True
        conn.host = "127.0.0.1"
        conn.port = 9999
        tester = RotapanelTester(
            device_id=1,
            connection=conn,
            step_delay=0,
            light_delay=0,
        )
        return tester

    def test_run_full_test_all_pass(self):
        tester = self._make_tester(0x40)
        report = tester.run_full_test()
        self.assertTrue(report.all_passed)
        self.assertEqual(report.total_steps, 8)

    def test_run_full_test_error_step_fails(self):
        # Status has mechanical error → error_check step should fail
        tester = self._make_tester(0x40 | 0x02)
        report = tester.run_full_test()
        # The error_check step should fail
        error_step = next(s for s in report.steps if s.name == "Error check")
        self.assertFalse(error_step.passed)

    def test_test_status_pass(self):
        tester = self._make_tester(0x40)
        result = tester.test_status()
        self.assertTrue(result.passed)
        self.assertAlmostEqual(result.duration_s, 0, delta=1.0)

    def test_test_status_fail_on_connection_error(self):
        conn = MagicMock(spec=RotapanelConnection)
        conn.send_and_receive.side_effect = ConnectionError("no route")
        conn.host = "127.0.0.1"
        conn.port = 9999
        tester = RotapanelTester(device_id=1, connection=conn, step_delay=0, light_delay=0)
        result = tester.test_status()
        self.assertFalse(result.passed)
        self.assertIn("no route", result.error)

    def test_side_cycle_steps(self):
        tester = self._make_tester(0x40)
        steps = tester.test_side_cycle()
        self.assertEqual(len(steps), 4)
        names = [s.name for s in steps]
        self.assertIn("Turn to side A", names)
        self.assertIn("Turn to side B", names)
        self.assertIn("Turn to side C", names)

    def test_light_cycle_steps(self):
        tester = self._make_tester(0x40)
        steps = tester.test_light_cycle()
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0].name, "Light ON")
        self.assertEqual(steps[1].name, "Light OFF")

    def test_report_summary_contains_pass(self):
        tester = self._make_tester(0x40)
        report = tester.run_full_test()
        summary = report.summary()
        self.assertIn("PASS", summary)
        self.assertIn("Device ID 1", summary)

    def test_report_summary_contains_fail_on_error(self):
        tester = self._make_tester(0x40 | 0x02)
        report = tester.run_full_test()
        summary = report.summary()
        self.assertIn("FAIL", summary)


# ──────────────────────────────────────────────
# CLI (smoke test — no real network needed)
# ──────────────────────────────────────────────

_REPO_ROOT = pathlib.Path(__file__).parent


class TestCLI(unittest.TestCase):
    def test_help_exits_cleanly(self):
        import subprocess, sys
        result = subprocess.run(
            [sys.executable, "cli.py", "--help"],
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("scan", result.stdout)
        self.assertIn("control", result.stdout)
        self.assertIn("light", result.stdout)
        self.assertIn("test", result.stdout)

    def test_scan_help(self):
        import subprocess, sys
        result = subprocess.run(
            [sys.executable, "cli.py", "scan", "--help"],
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
        )
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("--workers", result.stdout)

    def test_light_help(self):
        import subprocess, sys
        result = subprocess.run(
            [sys.executable, "cli.py", "light", "--help"],
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("--state", result.stdout)


if __name__ == "__main__":
    unittest.main()
