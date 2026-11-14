# Architecture and scientific contracts

## Components

The loopback API accepts local requests and persists jobs in SQLite. A worker advances each job through explicit stages and commits predictions in batches. It waits for the shared GPU lock before creating the inference runtime. The lock coordinates cooperating processes; it is not a system-wide GPU scheduler.

USB operations are delegated to a restricted host service over a Unix socket. The privileged broker accepts a narrow set of operations and derives mount and device authority from validated state. Browse is non-owning. Preflight claims exclusive access, ordinary terminal handling restores the native desktop view, and security-blocked handling ejects the affected device.

Failure and recovery paths include partial transitions, interrupted output publication, disconnected devices, stale leases and persisted mount provenance. These are implementation mechanisms, not a guarantee of successful recovery for every host configuration.

## Data contract

- Input candidates are supported PNG/JPEG files or matching signatures; images are decoded and validated individually.
- Photo identifiers come from explicitly selected metadata or normalized source-relative paths. Metadata is optional.
- Classification targets use the fixed mapping in `packages/core/plankton_core/inference_contract.py`. Some are broad groups or non-taxonomic targets, not species.
- The adapter uses the existing resize/center-crop/normalization contract for the compatible 128×128 checkpoint. This snapshot does not certify a newly supplied model.
- Softmax values must be finite. Low-score cases below 0.4 receive the existing additional top-three output; these values are uncalibrated scores.

Current result publication contains `predictions_top1.csv`, `low_confidence_top3.csv`, `rejected_images.csv`, `counts_summary.csv`, and `job_summary.json`. The aggregate counts preserve discovered = predicted + rejected, with low-score predictions a subset of predicted images. Model weights and real scientific output rows are not bundled.

## Inspecting the design

Start with the core contracts, the worker state transitions and the pure ownership evidence evaluator. The portable tests use synthetic input and fake host observations so these contracts can be examined without mounting media or loading a neural network. Legacy identifiers and migration paths remain where they are part of compatibility behavior.
