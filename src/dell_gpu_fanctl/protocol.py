"""Validation and in-memory state for authenticated GPU telemetry."""

from __future__ import annotations

import math
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional


MAX_GPUS = 64


@dataclass(frozen=True)
class GpuSample:
    uuid: str
    temperature: float
    utilisation: float
    power: float
    thermal_slowdown: Optional[bool]


def number(value: Any, name: str, lower: float, upper: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    value = float(value)
    if not math.isfinite(value) or not lower <= value <= upper:
        raise ValueError(f"{name} outside valid range")
    return value


def parse_sample(data: Any, expected: set[str]) -> list[GpuSample]:
    if not isinstance(data, dict) or set(data) != {"session", "sequence", "gpus"}:
        raise ValueError("invalid sample fields")
    if not isinstance(data["session"], str) or len(data["session"]) > 128:
        raise ValueError("invalid session")
    sequence = data["sequence"]
    if isinstance(sequence, bool) or not isinstance(sequence, int) or not 0 <= sequence < 2**63:
        raise ValueError("invalid sequence")
    rows = data["gpus"]
    if not isinstance(rows, list) or len(rows) > MAX_GPUS:
        raise ValueError("invalid GPU list")
    result = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "uuid", "temperature", "utilisation", "power", "thermal_slowdown"
        }:
            raise ValueError("invalid GPU fields")
        uuid = row["uuid"]
        if not isinstance(uuid, str) or not uuid or len(uuid) > 128:
            raise ValueError("invalid GPU UUID")
        slow = row["thermal_slowdown"]
        if slow is not None and not isinstance(slow, bool):
            raise ValueError("invalid thermal slowdown state")
        result.append(GpuSample(uuid, number(row["temperature"], "temperature", 0, 150),
                                number(row["utilisation"], "utilisation", 0, 100),
                                number(row["power"], "power", 0, 2000), slow))
    uuids = [gpu.uuid for gpu in result]
    if len(set(uuids)) != len(uuids) or set(uuids) != expected:
        raise ValueError("GPU inventory differs from configured inventory")
    return result


@dataclass
class ClientState:
    expected: set[str]
    session: str = ""
    sequence: int = -1
    received: Optional[float] = None
    gpus: Optional[list[GpuSample]] = None
    fault: str = "awaiting telemetry"


class Registry:
    """Only a server-issued current session and increasing sequence are accepted."""

    def __init__(self, clients: dict[str, set[str]], clock: Callable[[], float] = time.monotonic):
        self.clients = {name: ClientState(set(uuids)) for name, uuids in clients.items()}
        self.lock = threading.Lock()
        self.clock = clock

    def new_session(self, name: str) -> str:
        with self.lock:
            state = self.clients[name]
            state.session = secrets.token_urlsafe(32)
            state.sequence = -1
            state.received = None
            state.gpus = None
            state.fault = "awaiting telemetry"
            return state.session

    def accept(self, name: str, data: Any) -> None:
        with self.lock:
            state = self.clients[name]
            try:
                gpus = parse_sample(data, state.expected)
                if not state.session or not secrets.compare_digest(data["session"], state.session):
                    raise ValueError("invalid session")
                if data["sequence"] <= state.sequence:
                    raise ValueError("replayed or out-of-order sample")
            except (ValueError, KeyError, TypeError) as exc:
                state.fault = str(exc)
                state.received = None
                state.gpus = None
                raise ValueError(str(exc)) from exc
            state.sequence = data["sequence"]
            state.received = self.clock()
            state.gpus = gpus
            state.fault = ""

    def snapshot(self, name: str, stale_seconds: float) -> tuple[Optional[list[GpuSample]], str]:
        with self.lock:
            state = self.clients[name]
            if state.fault:
                return None, state.fault
            if state.received is None or self.clock() - state.received > stale_seconds:
                return None, "telemetry stale"
            return list(state.gpus or []), ""

    def invalidate(self, name: str, reason: str) -> None:
        with self.lock:
            state = self.clients[name]
            state.fault = reason
            state.received = None
            state.gpus = None
