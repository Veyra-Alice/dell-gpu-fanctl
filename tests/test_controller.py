import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from dell_gpu_fanctl import controller as c


GPU_ROWS = "\n".join(
    [
        "GPU-first, 0, NVIDIA RTX 2080 Ti, 50, 3, 34.0, Not Active, Not Active",
        "GPU-second, 1, NVIDIA A2, 75, 15, 45.0, Active, Not Active",
    ]
)


class SelectionTests(unittest.TestCase):
    def test_defaults_preserve_original_model(self):
        args = c.arguments([])
        self.assertEqual(args.model_match, ["RTX 2080 Ti"])
        self.assertFalse(args.all_gpus)

    def test_all_gpus_monitors_mixed_models(self):
        args = c.arguments(["--all-gpus"])
        cards = c.parse_gpus(GPU_ROWS, args.model_match, all_gpus=args.all_gpus)
        self.assertEqual([card.uuid for card in cards], ["GPU-first", "GPU-second"])
        reading = c.summarise(cards, utilisation_floor=10, power_floor=50)
        self.assertEqual(reading.temperature, 75)
        self.assertTrue(reading.active)
        self.assertTrue(reading.thermal_slowdown)

    def test_explicit_models_are_all_required(self):
        cards = c.parse_gpus(GPU_ROWS, ["2080 Ti", "A2"])
        self.assertEqual(len(cards), 2)
        with self.assertRaisesRegex(ValueError, "no GPU matched"):
            c.parse_gpus(GPU_ROWS, ["2080 Ti", "missing"])

    def test_selecting_all_and_specific_models_is_rejected(self):
        with self.assertRaises(SystemExit):
            with mock.patch("sys.stderr"):
                c.arguments(["--all-gpus", "--model-match", "A2"])
        with self.assertRaises(ValueError):
            c.parse_gpus(GPU_ROWS, ["A2"], all_gpus=True)

    def test_empty_and_malformed_telemetry_fail(self):
        with self.assertRaisesRegex(ValueError, "no GPUs"):
            c.parse_gpus("", [], all_gpus=True)
        with self.assertRaises(ValueError):
            c.parse_gpus("GPU-first, 0, incomplete", [], all_gpus=True)
        with self.assertRaises(ValueError):
            c.parse_gpus("GPU-first, 0, A2, N/A, 2, 20, Not Active, Not Active", [], all_gpus=True)


class PolicyTests(unittest.TestCase):
    def test_thermal_throttling_selects_maximum(self):
        reading = c.Reading(50, False, True, "throttled")
        self.assertEqual(c.raw_level(reading, 80), c.Level.MAX)

    def test_high_temperature_and_hysteresis(self):
        hot = c.Reading(80, True, False, "hot")
        nearly_cool = c.Reading(79, False, False, "cooling")
        cool = c.Reading(60, False, False, "cool")
        self.assertEqual(c.raw_level(hot, 80), c.Level.MAX)
        self.assertEqual(c.apply_hysteresis(c.Level.MAX, nearly_cool, 80), c.Level.MAX)
        self.assertEqual(c.apply_hysteresis(c.Level.MAX, cool, 80), c.Level.LOW)

    def test_missing_gpu_telemetry_attempts_maximum_offset(self):
        args = c.arguments(["--all-gpus", "--racadm", "/fake/racadm"])
        controller = c.Controller(args)
        with mock.patch.object(controller, "read_offset", return_value=c.Level.LOW), \
             mock.patch.object(controller, "read_gpus", side_effect=RuntimeError("no telemetry")), \
             mock.patch.object(c, "run", return_value="") as run:
            controller.cycle()
        self.assertEqual(controller.level, c.Level.MAX)
        self.assertEqual(run.call_args.args[0], ["/fake/racadm", "set", "system.thermalsettings.FanSpeedOffset", "3"])

    def test_dry_run_does_not_set_racadm(self):
        controller = c.Controller(c.arguments(["--all-gpus", "--dry-run"]))
        with mock.patch.object(c, "run") as run:
            controller.set_offset(c.Level.MAX, "test")
        run.assert_not_called()
        self.assertEqual(controller.level, c.Level.MAX)

    def test_offset_read_failure_attempts_maximum(self):
        controller = c.Controller(c.arguments(["--all-gpus"]))
        with mock.patch.object(controller, "read_offset", side_effect=RuntimeError("BMC unavailable")), \
             mock.patch.object(controller, "set_offset") as set_offset:
            controller.cycle()
        set_offset.assert_called_once_with(c.Level.MAX, "Dell offset read failed")


class CommandTests(unittest.TestCase):
    def run_with_fake_hardware(self, dry_run):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writes = root / "writes"
            gpu = root / "nvidia-smi"
            racadm = root / "racadm"
            gpu.write_text("#!/usr/bin/env python3\nprint(" + repr(GPU_ROWS) + ")\n")
            racadm.write_text(
                "#!/usr/bin/env python3\nimport pathlib,sys\n"
                "if sys.argv[1] == 'get': print('FanSpeedOffset=0')\n"
                "else: pathlib.Path(" + repr(str(writes)) + ").write_text(' '.join(sys.argv[1:]))\n"
            )
            gpu.chmod(0o700)
            racadm.chmod(0o700)
            cmd = [
                sys.executable, "-m", "dell_gpu_fanctl.controller", "--all-gpus",
                "--once", "--racadm", str(racadm), "--nvidia-smi", str(gpu),
            ]
            if dry_run:
                cmd.append("--dry-run")
            env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
            result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            return writes.read_text() if writes.exists() else None

    def test_mixed_gpu_throttling_sets_maximum_through_fake_racadm(self):
        self.assertEqual(self.run_with_fake_hardware(False), "set system.thermalsettings.FanSpeedOffset 3")

    def test_dry_run_makes_no_racadm_write(self):
        self.assertIsNone(self.run_with_fake_hardware(True))


if __name__ == "__main__":
    unittest.main()
