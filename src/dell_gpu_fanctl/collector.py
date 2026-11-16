"""Send NVIDIA readings to an authenticated host controller over HTTPS."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from .protocol import parse_sample

FIELDS = ("uuid,temperature.gpu,utilization.gpu,power.draw,"
          "clocks_event_reasons.hw_thermal_slowdown,clocks_event_reasons.sw_thermal_slowdown")


def throttle(hw: str, sw: str):
    values = [hw.strip().lower(), sw.strip().lower()]
    if "active" in values:
        return True
    if all(value == "not active" for value in values):
        return False
    return None


def read_nvidia(binary: str, expected: set[str]) -> list[dict]:
    output = subprocess.run([binary, f"--query-gpu={FIELDS}", "--format=csv,noheader,nounits"],
                            capture_output=True, text=True, check=True, timeout=10).stdout
    gpus = []
    for row in csv.reader(output.splitlines(), skipinitialspace=True):
        if len(row) != 6:
            raise ValueError("unexpected nvidia-smi output")
        uuid, temperature, utilisation, power, hw, sw = (x.strip() for x in row)
        gpus.append({"uuid": uuid, "temperature": float(temperature),
                     "utilisation": float(utilisation), "power": float(power),
                     "thermal_slowdown": throttle(hw, sw)})
    parse_sample({"session": "local", "sequence": 0, "gpus": gpus}, expected)
    return gpus


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Sender:
    def __init__(self, url: str, token: str, ca: str):
        if not url.startswith("https://"):
            raise ValueError("controller URL must use HTTPS")
        self.url = url.rstrip("/")
        self.token = token
        ctx = ssl.create_default_context(cafile=ca)
        self.opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=ctx))

    def request(self, path: str, data=None):
        body = None if data is None else json.dumps(data, allow_nan=False).encode()
        request = urllib.request.Request(self.url + path, data=body,
                                         headers={"Authorization": "Bearer " + self.token,
                                                  "Content-Type": "application/json"})
        with self.opener.open(request, timeout=5) as response:
            raw = response.read(16385)
            if len(raw) > 16384:
                raise ValueError("oversized controller response")
            return json.loads(raw)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="JSON configuration file")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = json.loads(Path(args.config).read_text())
    expected = set(cfg["gpu_uuids"])
    token = Path(cfg["token_file"]).read_text().strip()
    if not token or not expected or len(expected) != len(cfg["gpu_uuids"]):
        raise ValueError("a token and unique expected GPU UUIDs are required")
    sender = Sender(cfg["controller_url"], token, cfg["server_ca_file"])
    interval = float(cfg.get("interval_seconds", 5))
    if not 1 <= interval <= 60:
        raise ValueError("invalid interval")
    session = None
    sequence = 0
    while True:
        try:
            if session is None:
                session = sender.request("/v1/session")["session"]
                sequence = 0
            try:
                gpus = read_nvidia(cfg.get("nvidia_smi", "nvidia-smi"), expected)
            except (OSError, ValueError, subprocess.SubprocessError):
                # An invalid inventory immediately invalidates the last sample.
                sender.request("/v1/sample", {"session": session, "sequence": sequence, "gpus": []})
                raise
            sender.request("/v1/sample", {"session": session, "sequence": sequence, "gpus": gpus})
            sequence += 1
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            logging.error("telemetry send failed: %s", type(exc).__name__)
            session = None
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
