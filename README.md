# Dell GPU fan controller

A Linux controller that uses NVIDIA GPU telemetry to adjust a Dell server's DAPC fan speed offset through local `racadm`. Originally developed for Alice's Noema host by Veyra, with Alice's supervision. This private project starts with the controller and service installed there.

The controller monitors temperature, utilisation, power and reported thermal throttling. It raises the offset quickly, applies hysteresis and a cooldown when reducing it, and attempts the maximum offset when telemetry cannot be read. It cannot guarantee a safe fan speed if RACADM, the management controller, or the hardware cannot accept a command; validate on the actual host and retain independent temperature monitoring.

## Current scope

- Linux with Python 3.9+, NVIDIA's `nvidia-smi`, and Dell `racadm` with access to `system.thermalsettings.FanSpeedOffset`.
- The GPU must be visible to `nvidia-smi` on the machine running the controller. A GPU passed through to a VM may require a separate guest telemetry path; that is not implemented yet.
- `--model-match` may be repeated and requires every specified model to be present. With no selection option, the original default matches `RTX 2080 Ti`.
- `--all-gpus` instead monitors every NVIDIA GPU reported locally, including mixed models. It fails if no GPUs are reported. It does not yet discover GPUs from other vendors or guests.
- Missing or malformed telemetry causes the controller to attempt the maximum offset. `--dry-run` still queries NVIDIA and RACADM but does not set an offset.

The checked-in [Noema unit](contrib/systemd/noema-reference.service) records the original installation, including its host-specific model selections and executable path. It is a reference, not a generic unit to enable unchanged elsewhere.

## Development

Run the hardware-free tests from the repository root:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m dell_gpu_fanctl.controller --help
```

The first commit preserves the exact running source and unit. The current project is not automatically deployed and has not yet been validated on Debian or Proxmox. See [the roadmap](docs/roadmap.md) for the next work.
