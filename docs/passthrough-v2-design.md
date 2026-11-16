# Passthrough-aware fan controller: version 2 design

GPU telemetry comes from one or more authenticated guest collectors. A host
controller combines their demands and adjusts the local Dell iDRAC fan offset;
Dell's built-in thermal management remains active. Clients report readings,
not fan commands. The initial collector uses NVIDIA's `nvidia-smi`.

Each client has its own random secret and reports GPU UUID, core temperature,
utilisation, power and thermal slowdown state over HTTPS. The client verifies
the server certificate. The server binds each key to the expected GPU UUIDs,
limits request sizes, requires a server-issued session and monotonically
increasing sequence numbers, and measures freshness with its own monotonic
clock. A per-client key identifies the configured client; it does not prove
where a VM is running. A moved guest needs its destination and mapping updated.

With the Proxmox adapter, a VM definitively stopped on this host is idle. A VM
on another host, an unknown state, stale or malformed telemetry, missing or
extra GPUs, sensor errors, thermal slowdown or an unhealthy fan or inlet
requests maximum offset. Clients without a VM ID are assumed to be running.
No configured clients is a valid idle state. Set a minimum offset appropriate
to the chassis, including powered but idle passive GPUs; the shipped default
is maximum. Temperatures, active GPU demand and hysteresis determine requests
above that minimum. Reductions wait for a cooldown and fan writes are read back.
Each client may also set `active_minimum_level` (default `MEDIUM`) to raise
the fans as soon as reported GPU utilisation reaches 10% or power reaches 50 W.
This anticipatory floor does not detect every workload: for example, NVENC
can be active below both thresholds, so measured temperature and a validated
physical idle floor remain necessary.
Cluster and local VM status must agree; either can still change immediately
after a check. The measured idle floor must provide enough airflow while a VM
starts and the controller detects it. There is no VM-start interlock in v2.

Run in shadow mode first. Live control starts at maximum; service shutdown and
systemd failure hooks attempt maximum again. A failed BMC command or failed
readback is an alarm, not proof the fans changed. Local process supervision
cannot respond to a whole-host hang, and the BMC may retain a low setting.
Out-of-band peer monitoring is a possible future enhancement.

GPU core temperature alone cannot prove that memory or other board components
are adequately cooled, especially when the GPU exposes no memory temperature.
Validate the hardware backend, fan health, inlet envelope, idle floor and
sustained load on each chassis before lowering fan offsets. The old single-host
controller is retained as historical code and is not the v2 control path.
