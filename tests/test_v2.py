import json
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from dell_gpu_fanctl.collector import Sender, read_nvidia
from dell_gpu_fanctl.controller import Level
from dell_gpu_fanctl.hardware import Racadm, parse_sensors
from dell_gpu_fanctl.protocol import Registry, parse_sample
from dell_gpu_fanctl.server import FanLoop, desired_level, handler_for, vm_states


def payload(sequence=0, session="session", uuids=("GPU-a",), temp=42.5, slow=False):
    return {"sequence": sequence, "session": session,
            "gpus": [{"uuid": uuid, "temperature": temp, "utilisation": 0,
                      "power": 10.0, "thermal_slowdown": slow} for uuid in uuids]}


class ProtocolTests(unittest.TestCase):
    def test_fractional_readings_and_unsupported_throttle(self):
        cards = parse_sample(payload(temp=44.53, slow=None), {"GPU-a"})
        self.assertEqual(cards[0].temperature, 44.53)
        self.assertIsNone(cards[0].thermal_slowdown)

    def test_bad_inventory_and_nonfinite_numbers_are_rejected(self):
        for row in (payload(uuids=()), payload(uuids=("GPU-a", "GPU-a")),
                    payload(uuids=("GPU-b",)), payload(temp=float("nan")), payload(temp=True)):
            with self.subTest(row=row), self.assertRaises(ValueError):
                parse_sample(row, {"GPU-a"})

    def test_session_sequence_freshness_and_faults(self):
        now = [100.0]
        r = Registry({"guest": {"GPU-a"}}, clock=lambda: now[0])
        session = r.new_session("guest")
        r.accept("guest", payload(session=session))
        self.assertEqual(r.snapshot("guest", 15)[1], "")
        with self.assertRaises(ValueError):
            r.accept("guest", payload(session=session))
        self.assertIsNone(r.snapshot("guest", 15)[0])
        r.accept("guest", payload(1, session))
        now[0] += 16
        self.assertEqual(r.snapshot("guest", 15)[1], "telemetry stale")
        replacement = r.new_session("guest")
        with self.assertRaises(ValueError):
            r.accept("guest", payload(2, session))
        r.accept("guest", payload(0, replacement))
        self.assertEqual(r.snapshot("guest", 15)[1], "")

    def test_nvidia_fractional_and_missing_devices(self):
        result = mock.Mock(stdout="GPU-a, 44.53, 0, 1.18, Not Active, Not Active\n")
        with mock.patch("subprocess.run", return_value=result):
            self.assertEqual(read_nvidia("nvidia-smi", {"GPU-a"})[0]["temperature"], 44.53)
            with self.assertRaises(ValueError):
                read_nvidia("nvidia-smi", {"GPU-a", "GPU-b"})
        result.stdout += "GPU-b, 45.1, 0, 2.0, Not Active, Not Active\n"
        with mock.patch("subprocess.run", return_value=result):
            with self.assertRaises(ValueError):
                read_nvidia("nvidia-smi", {"GPU-a"})


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.reg = Registry({"guest": {"GPU-a"}}, clock=lambda: 100)
        self.cfg = {"minimum_level": "LOW", "stale_seconds": 15,
                    "clients": [{"name": "guest", "vm_id": 100, "_target": 80}]}

    def test_missing_vm_or_telemetry_max_but_stopped_is_idle(self):
        self.assertEqual(desired_level(self.cfg, self.reg, {}, Level.LOW)[0], Level.MAX)
        self.assertEqual(desired_level(self.cfg, self.reg, {100: "running"}, Level.LOW)[0], Level.MAX)
        self.assertEqual(desired_level(self.cfg, self.reg, {100: "stopped"}, Level.LOW)[0], Level.LOW)
        self.assertEqual(desired_level({"clients": [], "minimum_level": "LOW"}, self.reg, {}, Level.MAX)[0], Level.LOW)

    def test_temperature_slowdown_and_unsupported_sensor(self):
        session = self.reg.new_session("guest")
        self.reg.accept("guest", payload(0, session, temp=81))
        self.assertEqual(desired_level(self.cfg, self.reg, {100: "running"}, Level.LOW)[0], Level.MAX)
        self.reg.accept("guest", payload(1, session, temp=40, slow=True))
        self.assertEqual(desired_level(self.cfg, self.reg, {100: "running"}, Level.LOW)[0], Level.MAX)
        self.reg.accept("guest", payload(2, session, temp=40, slow=None))
        self.assertEqual(desired_level(self.cfg, self.reg, {100: "running"}, Level.LOW)[0], Level.MAX)

    def test_remote_vm_is_unknown_not_stopped(self):
        response = mock.Mock(stdout=json.dumps([{"type": "qemu", "vmid": 100,
                                                  "status": "running", "node": "other"}]))
        with mock.patch("subprocess.run", return_value=response):
            self.assertEqual(vm_states({**self.cfg, "proxmox_node": "here"})[100], "unknown")

    def test_local_and_cluster_vm_status_must_agree(self):
        cluster = mock.Mock(stdout=json.dumps([{"type": "qemu", "vmid": 100,
                                                 "status": "stopped", "node": "here"}]))
        local = mock.Mock(stdout="status: running\n")
        with mock.patch("subprocess.run", side_effect=[cluster, local]):
            self.assertEqual(vm_states({**self.cfg, "proxmox_node": "here"})[100], "unknown")
        local.stdout = "status: stopped\n"
        with mock.patch("subprocess.run", side_effect=[cluster, local]):
            self.assertEqual(vm_states({**self.cfg, "proxmox_node": "here"})[100], "stopped")


SENSORS = """System Board Inlet Temp       Ok      37C      -7C 47C
System Board Fan1               Ok          17040RPM    360RPM
System Board Fan2               Ok          17160RPM    360RPM
"""


class HardwareTests(unittest.TestCase):
    def test_fan_and_inlet_validation(self):
        self.assertEqual(parse_sensors(SENSORS, ["System Board Fan1", "System Board Fan2"], 40)[0], 37)
        for output in (SENSORS.replace("37C", "40C"), SENSORS.replace("Fan1               Ok", "Fan1               Failed"),
                       SENSORS.replace("17040RPM", "0RPM"), SENSORS.replace("System Board Fan2", "Missing Fan2")):
            with self.subTest(output=output), self.assertRaises(ValueError):
                parse_sensors(output, ["System Board Fan1", "System Board Fan2"], 40)

    def test_write_is_verified_against_hardware(self):
        hw = Racadm("racadm", ["System Board Fan1"], 40)
        with mock.patch.object(hw, "command", side_effect=["", "FanSpeedOffset=Low Fan Speed"]):
            with self.assertRaisesRegex(RuntimeError, "readback"):
                hw.set_verified(Level.MAX)
        with mock.patch.object(hw, "command", side_effect=["", "FanSpeedOffset=Max Fan Speed"]):
            hw.set_verified(Level.MAX)

    def test_control_cooldown_and_stale_data_restores_max(self):
        now = [0.0]
        reg = Registry({"guest": {"GPU-a"}}, clock=lambda: now[0])
        session = reg.new_session("guest")
        cfg = {"minimum_level": "LOW", "stale_seconds": 15, "lower_delay_seconds": 60,
               "clients": [{"name": "guest", "_target": 80}]}
        hw = mock.Mock()
        state = [Level.MAX]
        hw.offset.side_effect = lambda: state[0]
        hw.health.return_value = (30, {"fan": 12000})
        hw.set_verified.side_effect = lambda level: state.__setitem__(0, level)
        loop = FanLoop(cfg, reg, hw, True, clock=lambda: now[0])
        for second in (0, 10, 20, 30, 40, 50, 60):
            now[0] = second
            reg.accept("guest", payload(second, session))
            loop.cycle()
            if second < 60:
                self.assertEqual(state[0], Level.MAX)
        self.assertEqual(state[0], Level.LOW)
        now[0] = 76
        loop.cycle()
        self.assertEqual(state[0], Level.MAX)

    def test_bmc_error_attempts_max_and_shadow_never_writes(self):
        hw = mock.Mock()
        hw.offset.side_effect = RuntimeError("BMC down")
        FanLoop({"clients": []}, Registry({}), hw, True).cycle()
        hw.set_verified.assert_called_once_with(Level.MAX)
        hw.reset_mock()
        FanLoop({"clients": []}, Registry({}), hw, False).cycle()
        hw.set_verified.assert_not_called()

    def test_emergency_max_does_not_require_client_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            racadm, config, writes = root / "racadm", root / "config.json", root / "writes"
            racadm.write_text("#!/usr/bin/env python3\nimport pathlib,sys\n"
                              "if sys.argv[1] == 'get': print('FanSpeedOffset=Max Fan Speed')\n"
                              "else: pathlib.Path(" + repr(str(writes)) + ").write_text(' '.join(sys.argv[1:]))\n")
            racadm.chmod(0o700)
            config.write_text(json.dumps({"racadm": str(racadm), "clients": [{"token_file": "/missing"}]}))
            result = subprocess.run([sys.executable, "-m", "dell_gpu_fanctl.server",
                                     "--config", str(config), "--force-max"], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(writes.read_text(), "set system.thermalsettings.FanSpeedOffset 3")


class HttpsTests(unittest.TestCase):
    def test_tls_auth_session_replay_and_bad_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert, key = Path(tmp) / "cert.pem", Path(tmp) / "key.pem"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                            "-keyout", str(key), "-out", str(cert), "-days", "1",
                            "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"],
                           capture_output=True, check=True, timeout=30)
            token = "x" * 40
            cfg = {"clients": [{"name": "guest", "_token": token}]}
            reg = Registry({"guest": {"GPU-a"}})
            http = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(cfg, reg))
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert, key)
            http.socket = ctx.wrap_socket(http.socket, server_side=True)
            thread = threading.Thread(target=http.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"https://localhost:{http.server_address[1]}"
                sender = Sender(url, token, str(cert))
                session = sender.request("/v1/session")["session"]
                sender.request("/v1/sample", payload(session=session))
                self.assertEqual(reg.snapshot("guest", 15)[1], "")
                with self.assertRaises(urllib.error.HTTPError):
                    sender.request("/v1/sample", payload(session=session))
                self.assertIsNone(reg.snapshot("guest", 15)[0])
                with self.assertRaises(urllib.error.HTTPError):
                    Sender(url, "wrong", str(cert)).request("/v1/session")
                with self.assertRaises(urllib.error.URLError):
                    Sender(url.replace("localhost", "127.0.0.1"), token, str(cert)).request("/v1/session")
            finally:
                http.shutdown()
                http.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
