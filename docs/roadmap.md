# Next steps

- Verify local RACADM availability, device permissions, service dependencies and NVIDIA telemetry on Debian and Proxmox. Make executable paths and deployment configuration easy to set without editing code.
- Inventory GPU selection by stable UUID, and define what should happen when cards appear, disappear, lack a sensor or are replaced. Keep missing or stale telemetry visible and conservative.
- Design telemetry collection for GPUs passed through to guests: the Proxmox host may not have an NVIDIA driver bound to those devices. Authenticate the data, bound its age, and specify how a missing or disconnected guest affects fan policy.
- Separate telemetry, policy and Dell-specific control so other sensor sources and hardware backends can be added without changing the thermal policy. Other GPU vendors are future work.
- Test telemetry errors, BMC command failures, recovery, hysteresis and service restarts with simulated backends; then validate a reviewed configuration against real hardware before deployment.
- Document per-host fan behaviour and thermal limits, with observed results rather than assuming one curve works for every chassis or GPU.
