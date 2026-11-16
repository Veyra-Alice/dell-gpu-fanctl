# Dell GPU fan controller

An experimental Linux controller for Dell servers with NVIDIA GPUs, including
GPUs passed through to virtual machines. Guest collectors send telemetry over
HTTPS; the host controller combines their readings and adjusts the iDRAC fan
speed offset using local RACADM. Originally developed by Veyra with Alice's
supervision. Licensed under GPL-3.0-or-later.

**Status:** v2 is under development. The protocol and policy have hardware-free
tests, but the complete controller has not been validated on a live host or
under sustained GPU load. Run it in shadow mode first; do not depend on it as
the sole thermal protection. On startup the example configuration keeps the
minimum offset at `MAX`.

A missing or stale expected client, unexpected GPU inventory, unknown VM state,
thermal slowdown, unsupported slowdown sensor, failed fan or high inlet causes
a maximum-offset request. The server verifies fan-offset writes against the
BMC. It cannot guarantee cooling when the BMC or host cannot respond. A host
hang can leave the last offset in place. GPU core temperature alone does not
establish that memory and other board components are cool enough.

## Requirements

- Linux, Python 3.9 or newer, and OpenSSL to provision a certificate.
- Host: local Dell `racadm`, access to its in-band iDRAC interface, and a
  compatible Dell server. Proxmox is optional; when VM IDs are configured,
  the controller uses `pvesh` to distinguish a stopped VM from missing data.
- Guest: NVIDIA driver and `nvidia-smi`; the GPU UUIDs must be known.

Dell documents [local RACADM](https://www.dell.com/support/manuals/en-us/idrac7-8-lifecycle-controller-v2.60.60.60/idrac8_2.60.60.60_racadm/supported-racadm-interfaces?guid=guid-fe00618a-6741-4d99-9257-f07f63016d8f)
separately from remote RACADM. Distribution and hardware compatibility must be
verified; do not assume an Ubuntu package is supported on a Proxmox release.

## Configuration

Install the package on the host and each guest (`python3 -m pip install .`, or
install into a dedicated virtual environment). Copy `examples/server.json` on
the host and `examples/collector.json` on each guest. Adjust the executable
paths, host name, fan names, VM ID and GPU UUIDs. `nvidia-smi -L` lists the
UUIDs. Remove `vm_id` for a client that should always be considered running.
An empty `clients` list is valid; a configured client must list at least one
GPU. Never reuse a token across clients.

A client may set `active_minimum_level` to raise its required offset while
GPU utilisation is at least 10% or power is at least 50 W; the default is
`MEDIUM`. These are sampled readings, and some encoder workloads do not cross
either threshold. Choose an idle floor and temperature policy that also cover
those workloads and the time needed for telemetry and fans to respond.

Create one high-entropy token per guest, transfer it securely to that guest,
and restrict the files to the account running each service:

```sh
umask 077
openssl rand -hex 32 > /etc/dell-gpu-fanctl/gpu-guest.token
```

Use a trusted CA certificate, or create a self-signed host certificate whose
subject alternative name matches the address the guest will use. For example,
replace the example DNS name below with the real host name:

```sh
openssl req -x509 -newkey rsa:3072 -nodes -days 365 \
  -keyout /etc/dell-gpu-fanctl/server.key \
  -out /etc/dell-gpu-fanctl/server.crt \
  -subj '/CN=pve-host.example.org' \
  -addext 'subjectAltName=DNS:pve-host.example.org'
```

Copy the **public certificate** to each guest as its `server_ca_file`; never
copy the host private key. Protect host private keys and tokens with restrictive
permissions, and keep them out of Git. Bind the server to a suitable host
address and firewall its port to the intended guests. Certificate verification
is required; there is no unauthenticated or insecure TLS mode.

Start the server in **shadow mode**, which reports proposed decisions and reads
RACADM without writing fan offsets:

```sh
dell-gpu-fanctl-server --config /etc/dell-gpu-fanctl/server.json
dell-gpu-fanctl-collector --config /etc/dell-gpu-fanctl/collector.json
```

The examples under `contrib/systemd` are templates, not automatic
installation. The server unit opts into live writes with `--enable-control` and
attempts a verified maximum on start, stop and failure. Adapt the paths and test
all failure behaviour before enabling it. A successful readback does not prove
that airflow is adequate. Choose lower floors, inlet limits, GPU targets and
cooldown values only after measuring the actual hardware under sustained load.

The original v1 single-host code remains in the repository for history; it has
known error-handling shortcomings and is **not** the supported v2 control path.
See [the design](docs/passthrough-v2-design.md).

## Development

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The HTTPS integration test opens a loopback socket and creates a temporary
certificate. No tests change real fan settings.
