# Security boundaries

This is a source review candidate, not a security certification. No dependency-vulnerability audit, penetration test, or real-device security qualification was performed for this snapshot.

## Local API only

The API entry point binds loopback. It does not implement user authentication, authorization, tenant isolation, or Host/Origin validation. Browser session identifiers manage lifecycle state and are not authentication credentials. Do not expose the API through a network bind, tunnel, reverse proxy, or shared-host environment. Binding to localhost is not a substitute for a complete security design.

The frontend showcase has no default API proxy, so starting Vite will not silently connect to an installed inference service.

## Trusted model loading

The adapter checks the configured SHA-256 before `torch.load(..., weights_only=False)`. That loader can execute pickle content. Both checkpoint and expected digest must be obtained through a trusted release process. An attacker-controlled checkpoint accompanied by its own matching digest is still unsafe. Never load a USB/user-uploaded checkpoint. No model was loaded during this preparation.

## Privileged host boundary

The application containers are designed to be non-root and do not receive raw removable block devices. The separate mount broker intentionally requires privileged capabilities. Included systemd and Polkit templates can change system access if installed, and contain deployment assumptions such as service names, UID 10001, and the placeholder desktop account `operator`. Review those assumptions before any deployment. Do not execute host utilities as part of the showcase quickstart.

## USB and output safeguards

The code contains path checks, no-follow handling, scan/import content binding, exclusive ownership, bounded broker operations, and verified/atomic output publication. ClamAV cannot guarantee that arbitrary input is harmless. Real scanner signatures, filesystem behavior, permission boundaries, and cleanup must be qualified on the target deployment.

## Publication hygiene

The export excludes history, private deployment configuration, logs, research images, datasets, weights and runtime artifacts. Review the final source allowlist rather than relying on `.gitignore` alone. Do not publish generated caches, test output, `node_modules`, built assets, runtime archives, or real examples without separately checking their contents and permissions.
