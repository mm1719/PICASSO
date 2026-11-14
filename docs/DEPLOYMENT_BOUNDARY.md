# Deployment boundary

The portable showcase is the CPU test suite and disconnected frontend inspection described in the main README. It is not a one-command appliance installer.

The following source is retained to show the engineering design, but is outside the showcase execution path:

- `host/bin/`: release switching, launchers, GPU-lock cooperation and administrative helpers.
- `host/systemd/`, `host/polkit/`, `host/tmpfiles.d/`: privileged service/account/storage policy.
- `host/usb_mount_broker.py`: mount ownership authority.
- Dockerfiles, Compose overlays and environment examples: dependency/build/deployment reference.

Do not install those files, run `sudo`, launch Docker, change services, mount media or point to real checkpoint paths while following the showcase. A separate deployment review must cover accounts, UID/GID allocation, desktop-session permissions, source paths, immutable build references, storage/retention, trusted checkpoints, ClamAV freshness, CUDA compatibility and hardware qualification. `operator` and other example values are placeholders, not a supported default installation.

The legacy image-build utility expects a real Git repository and its original release workflow. This source-only snapshot intentionally has no original history or private deployment manifests. No ready-to-run image or Docker build success is claimed.

The original checkpoint/data experiments and privileged hardware QA runners are intentionally not in the public preview. Their absence must not be interpreted as passing those tests.
