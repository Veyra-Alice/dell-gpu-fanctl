#!/usr/bin/env python3
"""Keep selected passive GPUs cool using Dell's DAPC fan offset."""

from __future__ import annotations

import argparse
import csv
import logging
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Iterable, Optional, Sequence


class Level(IntEnum):
    OFF = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    MAX = 4


RACADM_VALUE = {
    Level.OFF: "255",
    Level.LOW: "0",
    Level.MEDIUM: "2",
    Level.HIGH: "1",
    Level.MAX: "3",
}

RACADM_NAMES = {
    "off": Level.OFF,
    "none": Level.OFF,
    "low": Level.LOW,
    "low fan speed": Level.LOW,
    "medium": Level.MEDIUM,
    "medium fan speed": Level.MEDIUM,
    "high": Level.HIGH,
    "high fan speed": Level.HIGH,
    "max": Level.MAX,
    "maximum": Level.MAX,
    "max fan speed": Level.MAX,
}

QUERY_FIELDS = (
    "uuid,index,name,temperature.gpu,utilization.gpu,power.draw,"
    "clocks_event_reasons.hw_thermal_slowdown,"
    "clocks_event_reasons.sw_thermal_slowdown"
)


@dataclass(frozen=True)
class Gpu:
    uuid: str
    index: int
    name: str
    temperature: int
    utilisation: int
    power: float
    thermal_slowdown: bool


@dataclass(frozen=True)
class Reading:
    temperature: int
    active: bool
    thermal_slowdown: bool
    details: str


def run(
    command: Sequence[str],
    timeout: float = 15.0,
    attempts: int = 1,
    retry_seconds: float = 1.0,
) -> str:
    last_error: Optional[BaseException] = None
    for attempt in range(attempts):
        try:
            completed = subprocess.run(
                command,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
            )
            return completed.stdout
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(retry_seconds)
    if isinstance(last_error, subprocess.CalledProcessError):
        detail = (last_error.stderr or last_error.stdout or "").strip()
        raise RuntimeError(
            f"{command[0]} exited {last_error.returncode}"
            + (f": {detail}" if detail else "")
        ) from last_error
    raise RuntimeError(f"{command[0]} timed out") from last_error


def parse_int(value: str) -> int:
    return int(value.strip().split()[0])


def parse_float(value: str) -> float:
    return float(value.strip().split()[0])


def is_active_flag(value: str) -> bool:
    return value.strip().lower() == "active"


def parse_gpus(
    output: str, model_matches: Sequence[str], *, all_gpus: bool = False
) -> list[Gpu]:
    expected = [value.strip() for value in model_matches if value.strip()]
    if all_gpus and expected:
        raise ValueError("all-GPU monitoring cannot be combined with model matches")
    if not all_gpus and not expected:
        raise ValueError("at least one GPU model match is required")
    found = {value: False for value in expected}
    matches: list[Gpu] = []
    for row in csv.reader(output.splitlines(), skipinitialspace=True):
        if not row:
            continue
        if len(row) != 8:
            raise ValueError(f"unexpected nvidia-smi field count: {len(row)}")
        uuid, index, name, temperature, utilisation, power, hw_slow, sw_slow = row
        matched = [value for value in expected if value.lower() in name.lower()]
        if not all_gpus and not matched:
            continue
        for value in matched:
            found[value] = True
        matches.append(
            Gpu(
                uuid=uuid.strip(),
                index=parse_int(index),
                name=name.strip(),
                temperature=parse_int(temperature),
                utilisation=parse_int(utilisation),
                power=parse_float(power),
                thermal_slowdown=is_active_flag(hw_slow) or is_active_flag(sw_slow),
            )
        )
    missing = [value for value, present in found.items() if not present]
    if missing:
        raise ValueError(f"no GPU matched expected model(s): {', '.join(missing)}")
    if not matches:
        raise ValueError("no GPUs returned by nvidia-smi")
    return matches


def summarise(gpus: Iterable[Gpu], utilisation_floor: int, power_floor: float) -> Reading:
    cards = list(gpus)
    return Reading(
        temperature=max(card.temperature for card in cards),
        active=any(
            card.utilisation >= utilisation_floor or card.power >= power_floor
            for card in cards
        ),
        thermal_slowdown=any(card.thermal_slowdown for card in cards),
        details="; ".join(
            f"gpu{card.index}={card.temperature}C/{card.utilisation}%/{card.power:.0f}W"
            for card in cards
        ),
    )


def raw_level(reading: Reading, target: int) -> Level:
    if reading.thermal_slowdown or reading.temperature >= target:
        return Level.MAX
    if reading.temperature >= target - 4:
        return Level.HIGH
    if reading.active or reading.temperature >= 70:
        return Level.MEDIUM
    if reading.temperature >= 55:
        return Level.LOW
    return Level.OFF


def apply_hysteresis(level: Level, reading: Reading, target: int) -> Level:
    desired = raw_level(reading, target)
    if desired >= level:
        return desired
    if level == Level.MAX and reading.temperature >= target - 2:
        return Level.MAX
    if level == Level.HIGH and reading.temperature >= target - 6:
        return Level.HIGH
    if level == Level.MEDIUM and (reading.active or reading.temperature >= 65):
        return Level.MEDIUM
    if level == Level.LOW and reading.temperature >= 53:
        return Level.LOW
    return desired


def parse_current_offset(output: str) -> Optional[Level]:
    for line in output.splitlines():
        if line.strip().lower().startswith("fanspeedoffset="):
            value = line.split("=", 1)[1].strip().lower()
            if value in RACADM_NAMES:
                return RACADM_NAMES[value]
            try:
                numeric = int(value)
            except ValueError:
                return None
            for level, racadm_value in RACADM_VALUE.items():
                if numeric == int(racadm_value):
                    return level
    return None


class Controller:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.level: Optional[Level] = None
        self.cool_since: Optional[float] = None
        self.last_verify = 0.0

    def read_gpus(self) -> Reading:
        output = run(
            [
                self.args.nvidia_smi,
                f"--query-gpu={QUERY_FIELDS}",
                "--format=csv,noheader",
            ]
        )
        return summarise(
            parse_gpus(output, self.args.model_match, all_gpus=self.args.all_gpus),
            self.args.utilisation_floor,
            self.args.power_floor,
        )

    def read_offset(self) -> Optional[Level]:
        output = run(
            [self.args.racadm, "get", "system.thermalsettings.FanSpeedOffset"],
            attempts=3,
        )
        return parse_current_offset(output)

    def set_offset(self, level: Level, reason: str) -> None:
        if level == self.level:
            return
        logging.warning(
            "fan offset %s -> %s: %s",
            self.level.name if self.level is not None else "UNKNOWN",
            level.name,
            reason,
        )
        if not self.args.dry_run:
            run(
                [
                    self.args.racadm,
                    "set",
                    "system.thermalsettings.FanSpeedOffset",
                    RACADM_VALUE[level],
                ],
                timeout=30.0,
                attempts=3,
            )
        self.level = level

    def choose_level(self, reading: Reading, now: float) -> Level:
        current = self.level if self.level is not None else Level.MAX
        desired = apply_hysteresis(current, reading, self.args.target_temp)

        cool_idle = not reading.active and reading.temperature < 55
        if cool_idle:
            if current == Level.OFF:
                self.cool_since = now
                return Level.OFF
            if self.cool_since is None:
                self.cool_since = now
            if desired == Level.OFF and now - self.cool_since < self.args.cooldown_seconds:
                return Level.LOW
        else:
            self.cool_since = None
        return desired

    def cycle(self) -> None:
        now = time.monotonic()
        if self.level is None or now - self.last_verify >= self.args.verify_seconds:
            try:
                actual = self.read_offset()
            except Exception as exc:
                logging.error("could not read Dell fan offset: %s", exc)
                self.set_offset(Level.MAX, "Dell offset read failed")
                self.last_verify = now
                return
            if actual is not None and actual != self.level:
                logging.info("observed Dell fan offset %s", actual.name)
                self.level = actual
            self.last_verify = now

        try:
            reading = self.read_gpus()
        except Exception as exc:
            self.cool_since = None
            self.set_offset(Level.MAX, f"GPU telemetry failed: {exc}")
            return

        desired = self.choose_level(reading, now)
        self.set_offset(
            desired,
            f"max={reading.temperature}C active={reading.active} "
            f"slowdown={reading.thermal_slowdown}; {reading.details}",
        )


def arguments(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-temp", type=int, default=84)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--cooldown-seconds", type=float, default=300.0)
    parser.add_argument("--verify-seconds", type=float, default=60.0)
    parser.add_argument("--utilisation-floor", type=int, default=10)
    parser.add_argument("--power-floor", type=float, default=50.0)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--model-match",
        action="append",
        default=None,
        help="required GPU model substring; repeat to monitor multiple models",
    )
    selection.add_argument(
        "--all-gpus",
        action="store_true",
        help="monitor every NVIDIA GPU reported by nvidia-smi",
    )
    parser.add_argument("--nvidia-smi", default="/usr/bin/nvidia-smi")
    parser.add_argument("--racadm", default="/opt/dell/srvadmin/sbin/racadm")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if args.all_gpus:
        args.model_match = []
    elif args.model_match is None:
        args.model_match = ["RTX 2080 Ti"]
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = arguments(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    controller = Controller(args)
    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    while not stopping:
        try:
            controller.cycle()
        except Exception:
            logging.exception("controller cycle failed")
        if args.once:
            break
        deadline = time.monotonic() + args.poll_seconds
        while not stopping and time.monotonic() < deadline:
            time.sleep(min(0.25, deadline - time.monotonic()))
    logging.info("stopped; retaining the last Dell offset as the fail-safe state")
    return 0


if __name__ == "__main__":
    sys.exit(main())
