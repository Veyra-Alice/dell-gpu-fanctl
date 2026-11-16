"""Dell RACADM actuator with mandatory offset and sensor readback."""

from __future__ import annotations

import re
import subprocess

from .controller import Level, RACADM_VALUE, parse_current_offset


class Racadm:
    def __init__(self, binary: str, expected_fans: list[str], inlet_limit: float):
        if not expected_fans or len(set(expected_fans)) != len(expected_fans):
            raise ValueError("expected fan names must be unique and nonempty")
        self.binary = binary
        self.expected_fans = expected_fans
        self.inlet_limit = inlet_limit

    def command(self, *args: str) -> str:
        result = subprocess.run([self.binary, *args], capture_output=True, text=True,
                                check=True, timeout=12)
        if "ERROR:" in result.stdout.upper():
            raise RuntimeError("RACADM reported an error")
        return result.stdout

    def offset(self) -> Level:
        level = parse_current_offset(self.command("get", "system.thermalsettings.FanSpeedOffset"))
        if level is None:
            raise ValueError("unrecognised RACADM offset")
        return level

    def set_verified(self, level: Level) -> None:
        self.command("set", "system.thermalsettings.FanSpeedOffset", RACADM_VALUE[level])
        if self.offset() != level:
            raise RuntimeError("fan offset readback did not match request")

    def health(self) -> tuple[float, dict[str, int]]:
        return parse_sensors(self.command("getsensorinfo"), self.expected_fans, self.inlet_limit)


def parse_sensors(output: str, expected_fans: list[str], inlet_limit: float):
    inlet = None
    fans = {}
    for line in output.splitlines():
        line = line.strip()
        match = re.fullmatch(r"System Board Inlet Temp\s+(\S+)\s+(-?\d+(?:\.\d+)?)\s*C(?:\s+.*)?", line, re.I)
        if match:
            if inlet is not None or match.group(1).lower() != "ok":
                raise ValueError("inlet sensor is duplicated or unhealthy")
            inlet = float(match.group(2))
        for name in expected_fans:
            if line.lower().startswith(name.lower() + " "):
                match = re.fullmatch(re.escape(name) + r"\s+(\S+)\s+(\d+)\s*RPM(?:\s+.*)?", line, re.I)
                if not match or match.group(1).lower() != "ok" or int(match.group(2)) <= 0 or name in fans:
                    raise ValueError(f"fan sensor unhealthy or ambiguous: {name}")
                fans[name] = int(match.group(2))
    if inlet is None or not -10 <= inlet < inlet_limit or set(fans) != set(expected_fans):
        raise ValueError("inlet or expected fan sensors unavailable or unsafe")
    return inlet, fans
