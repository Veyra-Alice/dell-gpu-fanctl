"""Receive guest telemetry and supervise a host's Dell fan offset."""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import signal
import socket
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .controller import Level, Reading, apply_hysteresis
from .hardware import Racadm
from .protocol import Registry, number

MAX_BODY = 16384


def read_config(path: str) -> dict:
    cfg = json.loads(Path(path).read_text())
    if not isinstance(cfg.get("clients"), list):
        raise ValueError("clients must be a list")
    names, tokens, uuids = set(), set(), set()
    for client in cfg["clients"]:
        name = client["name"]
        if not isinstance(name, str) or not name or name in names:
            raise ValueError("duplicate or invalid client name")
        names.add(name)
        token = Path(client["token_file"]).read_text().strip()
        if len(token) < 32 or token in tokens or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError("tokens must be unique, ASCII, and at least 32 characters")
        client["_token"] = token
        tokens.add(token)
        cards = client["gpu_uuids"]
        if not isinstance(cards, list) or not cards or len(set(cards)) != len(cards) or uuids.intersection(cards):
            raise ValueError("client GPU UUIDs must be nonempty and globally unique")
        if any(not isinstance(card, str) or not card for card in cards):
            raise ValueError("invalid GPU UUID")
        uuids.update(cards)
        if client.get("vm_id") is not None and (isinstance(client["vm_id"], bool) or not isinstance(client["vm_id"], int)):
            raise ValueError("VM ID must be an integer")
        target = float(client.get("target_temp", 80))
        if not 50 <= target <= 85:
            raise ValueError("target temperature must be 50–85 C")
        client["_target"] = target
        Level[str(client.get("active_minimum_level", "MEDIUM")).upper()]
    if not 5 <= float(cfg.get("stale_seconds", 15)) <= 120:
        raise ValueError("invalid telemetry timeout")
    if not 5 <= float(cfg.get("cycle_seconds", 5)) <= 60:
        raise ValueError("invalid cycle interval")
    number(cfg.get("lower_delay_seconds", 120), "lower_delay_seconds", 60, 86400)
    number(cfg["inlet_limit_c"], "inlet_limit_c", 1, 80)
    Level[str(cfg.get("minimum_level", "MAX")).upper()]
    return cfg


def vm_states(cfg: dict) -> dict[int, str]:
    ids = {client["vm_id"] for client in cfg["clients"] if client.get("vm_id") is not None}
    if not ids:
        return {}
    result = subprocess.run([cfg.get("pvesh", "pvesh"), "get", "/cluster/resources", "--type", "vm",
                             "--output-format", "json"], capture_output=True, text=True, check=True, timeout=8)
    resources = json.loads(result.stdout)
    node = cfg.get("proxmox_node") or socket.gethostname()
    states = {}
    for vmid in ids:
        matches = [x for x in resources if x.get("type") == "qemu" and x.get("vmid") == vmid]
        if len(matches) != 1 or matches[0].get("node") != node:
            states[vmid] = "unknown"
        elif matches[0].get("status") in ("running", "stopped"):
            local = subprocess.run([cfg.get("qm", "qm"), "status", str(vmid)],
                                   capture_output=True, text=True, check=True, timeout=8).stdout.strip()
            expected = matches[0]["status"]
            states[vmid] = expected if local == "status: " + expected else "unknown"
        else:
            states[vmid] = "unknown"
    return states


def desired_level(cfg: dict, registry: Registry, states: dict[int, str], current: Level):
    floor = Level[str(cfg.get("minimum_level", "MAX")).upper()]
    desired = floor
    reasons = []
    for client in cfg["clients"]:
        vmid = client.get("vm_id")
        state = states.get(vmid, "unknown") if vmid is not None else "running"
        if state == "stopped":
            reasons.append(client["name"] + ": stopped")
            continue
        if state != "running":
            return Level.MAX, client["name"] + ": VM state unknown"
        gpus, fault = registry.snapshot(client["name"], float(cfg.get("stale_seconds", 15)))
        if fault:
            return Level.MAX, client["name"] + ": " + fault
        for gpu in gpus or []:
            if gpu.thermal_slowdown is None:
                return Level.MAX, client["name"] + ": thermal slowdown state unavailable"
            reading = Reading(gpu.temperature, gpu.utilisation >= 10 or gpu.power >= 50,
                              gpu.thermal_slowdown, gpu.uuid)
            desired = max(desired, apply_hysteresis(current, reading, client["_target"]))
            if reading.active:
                desired = max(desired, Level[str(client.get("active_minimum_level", "MEDIUM")).upper()])
            reasons.append(f"{client['name']}/{gpu.uuid}: {gpu.temperature:g} C, "
                           f"{gpu.utilisation:g}%, {gpu.power:g} W")
    return desired, "; ".join(reasons) or "no configured clients"


def handler_for(cfg: dict, registry: Registry):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # Never put requests, authorisation headers or tokens in logs.

        def respond(self, status: int, body: dict):
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)
            self.close_connection = True

        def authenticated(self):
            header = self.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                return None
            token = header[7:]
            if not token.isascii():
                return None
            for client in cfg["clients"]:
                if secrets.compare_digest(token, client["_token"]):
                    return client["name"]
            return None

        def do_GET(self):
            name = self.authenticated()
            if name is None:
                return self.respond(401, {"error": "unauthorised"})
            if self.path != "/v1/session":
                return self.respond(404, {"error": "not found"})
            self.respond(200, {"session": registry.new_session(name)})

        def do_POST(self):
            name = self.authenticated()
            if name is None:
                return self.respond(401, {"error": "unauthorised"})
            if self.path != "/v1/sample":
                return self.respond(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length", "-1"))
                if length < 0 or length > MAX_BODY:
                    raise ValueError("invalid content length")
                data = json.loads(self.rfile.read(length))
                registry.accept(name, data)
            except (ValueError, UnicodeError):
                registry.invalidate(name, "invalid sample")
                return self.respond(400, {"error": "invalid sample"})
            self.respond(200, {"ok": True})
    return Handler


def notify_systemd(message: str):
    address = os.environ.get("NOTIFY_SOCKET")
    if address:
        if address.startswith("@"):
            address = "\0" + address[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(message.encode("ascii"))


class FanLoop:
    def __init__(self, cfg, registry, hardware, control, clock=time.monotonic):
        self.cfg = cfg
        self.registry = registry
        self.hw = hardware
        self.control = control
        self.clock = clock
        self.last_confirmed = Level.MAX
        self.lower_since = None

    def cycle(self):
        try:
            actual = self.hw.offset()
            inlet, fans = self.hw.health()
            if self.control and actual != self.last_confirmed:
                raise RuntimeError("unexpected hardware offset; refusing reduction")
            states = vm_states(self.cfg)
            desired, reason = desired_level(self.cfg, self.registry, states, actual)
            if desired < actual:
                if self.lower_since is None:
                    self.lower_since = self.clock()
                if self.clock() - self.lower_since < float(self.cfg.get("lower_delay_seconds", 120)):
                    desired = actual
            else:
                self.lower_since = None
            if self.control and desired != actual:
                self.hw.set_verified(desired)
                self.last_confirmed = desired
                self.lower_since = None
            logging.info("mode=%s actual=%s requested=%s inlet=%s fans=%s reason=%s",
                         "control" if self.control else "shadow", actual.name, desired.name,
                         inlet, len(fans), reason)
        except Exception as exc:
            self.lower_since = None
            logging.error("controller fault: %s", exc)
            if self.control:
                try:
                    self.hw.set_verified(Level.MAX)
                    self.last_confirmed = Level.MAX
                except Exception as fallback:
                    logging.critical("maximum offset could not be verified: %s", fallback)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--enable-control", action="store_true", help="allow RACADM writes after testing")
    parser.add_argument("--force-max", action="store_true", help="attempt and verify maximum offset, then exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.force_max:
        # The emergency path must not depend on client tokens or TLS material.
        cfg = json.loads(Path(args.config).read_text())
        hw = Racadm(cfg.get("racadm", "racadm"), cfg.get("expected_fans") or ["unused"],
                    float(cfg.get("inlet_limit_c", 40)))
        hw.set_verified(Level.MAX)
        return 0
    cfg = read_config(args.config)
    hw = Racadm(cfg.get("racadm", "racadm"), cfg["expected_fans"], float(cfg["inlet_limit_c"]))
    registry = Registry({c["name"]: set(c["gpu_uuids"]) for c in cfg["clients"]})
    http = ThreadingHTTPServer((cfg["listen_address"], int(cfg["listen_port"])), handler_for(cfg, registry))
    http.daemon_threads = True
    http.socket.settimeout(5)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(cfg["tls_cert_file"], cfg["tls_key_file"])
    http.socket = tls.wrap_socket(http.socket, server_side=True)
    if args.enable_control:
        hw.set_verified(Level.MAX)
    worker = threading.Thread(target=http.serve_forever, daemon=True)
    worker.start()
    notify_systemd("READY=1")
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda _s, _f: stop.set())
    loop = FanLoop(cfg, registry, hw, args.enable_control)
    try:
        while not stop.is_set():
            loop.cycle()
            notify_systemd("WATCHDOG=1")
            stop.wait(float(cfg.get("cycle_seconds", 5)))
    finally:
        http.shutdown()
        http.server_close()
        if args.enable_control:
            try:
                hw.set_verified(Level.MAX)
            except Exception as exc:
                logging.critical("shutdown maximum offset could not be verified: %s", exc)
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
