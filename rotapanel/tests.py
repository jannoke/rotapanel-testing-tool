"""Automated test sequences for Rotapanel devices.

Runs structured test scenarios and produces a detailed report.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional

from rotapanel.connection import RotapanelConnection
from rotapanel.device import RotapanelDevice
from rotapanel import protocol

logger = logging.getLogger(__name__)

# Delay between side changes during a cycle test (seconds)
DEFAULT_STEP_DELAY: float = 1.0

# Delay between light on/off during a light test (seconds)
DEFAULT_LIGHT_DELAY: float = 0.5


@dataclass
class StepResult:
    """Result of a single test step."""

    name: str
    passed: bool
    duration_s: float
    detail: str = ""
    error: Optional[str] = None


@dataclass
class TestReport:
    """Aggregated results for a full test run."""

    device_id: int
    host: str
    port: int
    steps: List[StepResult] = field(default_factory=list)
    start_time: float = field(default_factory=time.time)
    end_time: Optional[float] = None

    # ── computed ─────────────────────────────

    @property
    def total_steps(self) -> int:
        return len(self.steps)

    @property
    def passed_steps(self) -> int:
        return sum(1 for s in self.steps if s.passed)

    @property
    def failed_steps(self) -> int:
        return self.total_steps - self.passed_steps

    @property
    def all_passed(self) -> bool:
        return self.failed_steps == 0

    @property
    def duration_s(self) -> float:
        end = self.end_time or time.time()
        return end - self.start_time

    # ── formatting ────────────────────────────

    def summary(self) -> str:
        status = "PASS" if self.all_passed else "FAIL"
        lines = [
            f"{'=' * 60}",
            f"Test Report — Device ID {self.device_id}  ({self.host}:{self.port})",
            f"Result  : {status}",
            f"Steps   : {self.passed_steps}/{self.total_steps} passed",
            f"Duration: {self.duration_s:.2f}s",
            f"{'=' * 60}",
        ]
        for step in self.steps:
            icon = "✓" if step.passed else "✗"
            line = f"  {icon} [{step.duration_s:5.2f}s] {step.name}"
            if step.detail:
                line += f"  — {step.detail}"
            if step.error:
                line += f"  ERROR: {step.error}"
            lines.append(line)
        lines.append(f"{'=' * 60}")
        return "\n".join(lines)


class RotapanelTester:
    """Runs automated test sequences against a Rotapanel device.

    Usage::

        with RotapanelConnection("192.168.1.100", 5000) as conn:
            tester = RotapanelTester(device_id=1, connection=conn)
            report = tester.run_full_test()
            print(report.summary())
    """

    def __init__(
        self,
        device_id: int,
        connection: RotapanelConnection,
        step_delay: float = DEFAULT_STEP_DELAY,
        light_delay: float = DEFAULT_LIGHT_DELAY,
    ) -> None:
        self.device = RotapanelDevice(device_id, connection)
        self.step_delay = step_delay
        self.light_delay = light_delay

    # ── individual tests ─────────────────────

    def test_status(self) -> StepResult:
        """Verify that the device responds to a status request."""
        start = time.time()
        try:
            status = self.device.get_status()
            return StepResult(
                name="Status request",
                passed=True,
                duration_s=time.time() - start,
                detail=f"side={status.side}, light={'on' if status.lighting_on else 'off'}",
            )
        except Exception as exc:
            return StepResult(
                name="Status request",
                passed=False,
                duration_s=time.time() - start,
                error=str(exc),
            )

    def test_error_check(self) -> StepResult:
        """Check that the device reports no active errors."""
        start = time.time()
        try:
            report = self.device.check_errors()
            passed = not report["has_errors"]
            detail = (
                "No errors"
                if passed
                else "Errors: " + ", ".join(report["error_list"])
            )
            return StepResult(
                name="Error check",
                passed=passed,
                duration_s=time.time() - start,
                detail=detail,
            )
        except Exception as exc:
            return StepResult(
                name="Error check",
                passed=False,
                duration_s=time.time() - start,
                error=str(exc),
            )

    def test_turn_to_side(self, side: str) -> StepResult:
        """Send a TURN + GO command and verify the device acknowledges it."""
        start = time.time()
        name = f"Turn to side {side.upper()}"
        try:
            status = self.device.turn_to_side(side)
            return StepResult(
                name=name,
                passed=True,
                duration_s=time.time() - start,
                detail=f"reply side={status.side}",
            )
        except Exception as exc:
            return StepResult(
                name=name,
                passed=False,
                duration_s=time.time() - start,
                error=str(exc),
            )

    def test_side_cycle(self) -> List[StepResult]:
        """Cycle through all three sides: A → B → C → A.

        Returns:
            A list of four :class:`StepResult` objects.
        """
        results = []
        for side in ("A", "B", "C", "A"):
            result = self.test_turn_to_side(side)
            results.append(result)
            time.sleep(self.step_delay)
        return results

    def test_light_on(self) -> StepResult:
        """Send LIGHT ON + GO and verify the device acknowledges it."""
        start = time.time()
        try:
            status = self.device.light_on()
            return StepResult(
                name="Light ON",
                passed=True,
                duration_s=time.time() - start,
                detail=f"reply light={'on' if status.lighting_on else 'off (pre-GO)'}",
            )
        except Exception as exc:
            return StepResult(
                name="Light ON",
                passed=False,
                duration_s=time.time() - start,
                error=str(exc),
            )

    def test_light_off(self) -> StepResult:
        """Send LIGHT OFF + GO and verify the device acknowledges it."""
        start = time.time()
        try:
            status = self.device.light_off()
            return StepResult(
                name="Light OFF",
                passed=True,
                duration_s=time.time() - start,
                detail=f"reply light={'on (pre-GO)' if status.lighting_on else 'off'}",
            )
        except Exception as exc:
            return StepResult(
                name="Light OFF",
                passed=False,
                duration_s=time.time() - start,
                error=str(exc),
            )

    def test_light_cycle(self) -> List[StepResult]:
        """Toggle the light ON then OFF.

        Returns:
            A list of two :class:`StepResult` objects.
        """
        on_result = self.test_light_on()
        time.sleep(self.light_delay)
        off_result = self.test_light_off()
        return [on_result, off_result]

    # ── full test suite ───────────────────────

    def run_full_test(self) -> TestReport:
        """Run the complete test suite.

        Sequence:
          1. Status request
          2. Error check
          3. Turn to side A
          4. Turn to side B
          5. Turn to side C
          6. Turn back to side A (home)
          7. Light ON
          8. Light OFF

        Returns:
            :class:`TestReport` with all results.
        """
        report = TestReport(
            device_id=self.device.device_id,
            host=self.device.connection.host,
            port=self.device.connection.port,
        )

        logger.info(
            "Starting full test for device %d on %s:%d",
            self.device.device_id,
            self.device.connection.host,
            self.device.connection.port,
        )

        # 1 – status
        report.steps.append(self.test_status())

        # 2 – errors
        report.steps.append(self.test_error_check())

        # 3-6 – side cycle A → B → C → A
        report.steps.extend(self.test_side_cycle())

        # 7-8 – light cycle
        report.steps.extend(self.test_light_cycle())

        report.end_time = time.time()
        logger.info(
            "Test complete: %d/%d steps passed in %.2fs",
            report.passed_steps,
            report.total_steps,
            report.duration_s,
        )
        return report
