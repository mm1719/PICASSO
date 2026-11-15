# PICASSO

**Plankton Image Classification & Analysis System for Scientific Observation**

A local-first research application that turns a folder of plankton images into traceable classification results. PICASSO connects a React interface, a Python control API, resumable job processing, a GPU inference adapter, and a narrowly scoped USB service.

> **Source-only beta preview.** This is a curated engineering snapshot, not a stable release, a hosted service, or a complete deployment package. It includes no research images, datasets, trained weights, private deployment configuration, or original Git history. The source is published as a viewable showcase; no open-source license has been assigned.

## What the code demonstrates

- **Durable workflows:** SQLite-backed job state, batched prediction commits, and recovery paths for interrupted work.
- **Scientific output contracts:** stable image identifiers, a fixed 20-category mapping, deterministic ordering, rejected-image records, and CSV/JSON summaries.
- **GPU coordination:** a shared lock prevents the inference worker from competing with a cooperating training process.
- **USB trust boundaries:** scan-before-import, path and content checks, and exclusive ownership coordinated through a fixed-operation host broker.
- **Packaging and operations:** non-root application containers, read-only root filesystems, and reference deployment/rollback utilities.

These are implementation features, not a claim that every deployment or recovery scenario has passed qualification.

## Latest source update — 2026-11-15

This showcase now includes the next reviewed, merged development changes:

- Initial scanning speed stays at `Calculating…` until byte throughput is available; scanning is never presented as inference images/s.
- A signal that triggers launcher rollback no longer also closes the restored window. The switching progress window follows its own launcher's lifetime.
- Frontend tooling and its lockfile are updated, with the matching regression tests and dependency inventory.
- Broker requests survive disconnected clients, failed claims retain cleanup responsibility, and rollback rechecks device identity. A failed Preflight remains addressable so Refresh can retry cleanup before enabling another selection.

See the [source sync record](docs/SOURCE_SYNC.md) for the pinned source revision, included changes, and work still awaiting review. The [validation record](docs/VALIDATION.md) reports checks on this public checkout.

## Architecture

```text
Browser UI (React / TypeScript)
        | loopback HTTP
Control API (FastAPI) ---- SQLite job state
        |                       |
        |                Job worker ---- GPU lock ---- Inference adapter
        |                       |                         (PyTorch / timm)
        +---- Unix socket ------+
                   |
          Restricted USB service
                   | fixed operations
          Privileged mount broker
                   |
              Removable media
```

The ordinary workflow is browse → preflight/exclusive claim → scan and verified import → inference → archive and verified export → native desktop handoff. A security-blocked job follows a separate eject path. The current design uses **exclusive ownership**, not simultaneous writable desktop and service views. See [architecture](docs/ARCHITECTURE.md) and [security boundaries](docs/SECURITY.md).

## Explore without a model or USB

### 1. Run the CPU-only contract tests

Use a separate Python 3.11 environment:

```bash
python3.11 -m venv .venv-showcase
. .venv-showcase/bin/activate
python -m pip install -r requirements/showcase-dev.txt
make test-portable PYTHON=python
```

The exported tests use temporary files, synthetic scores, fake operating-system/USB adapters, and an in-process API test client. Launcher tests use fake host operations and a fake GTK module; helper-process tests use temporary state. They do not open real windows, start a live API server, load a model, use a GPU, require service accounts, or modify an attached USB. Run them on Linux with the included POSIX pipe/lock support. Dependencies need to be installed first; after that the test run itself is offline.

### 2. Build and inspect the interface

Use a current supported Node.js release compatible with the locked frontend dependencies:

```bash
npm --prefix apps/web ci --ignore-scripts
npm --prefix apps/web run lint
npm --prefix apps/web run test:unit
npm --prefix apps/web run build
npm --prefix apps/web run dev
```

Open the loopback address printed by Vite. The showcase defaults deliberately have **no API proxy**. Without an explicitly configured isolated backend, API-dependent controls show connection errors. This is interface inspection, not simulated scientific inference. Frontend unit tests exercise mocked interactions without a live backend. Do not point the preview at an existing research service.

The plain text wordmark is a replacement created for this snapshot; the original artwork is not included.

## Running real inference is a separate deployment task

The source contains the inference adapter, but **real inference cannot run from this checkout alone**. It requires a separately authorized and trusted compatible checkpoint, its independently trusted SHA-256, the fixed class mapping and preprocessing contract, CUDA-compatible dependencies, storage configuration, and reviewed host integration.

The runtime constructs `convnextv2_large.fcmae_ft_in22k_in1k` with `pretrained=False`; it does not download or include pretrained/trained weights. `PLANKTON_CHECKPOINT_PATH` and `PLANKTON_CHECKPOINT_SHA256` are deployment inputs, never values supplied by an uploaded file or job. The trusted checkpoint loader uses Python pickle semantics; a checksum is not proof that a file is safe.

`host/`, Dockerfiles, Compose files, and environment examples are **reference source**, not installation instructions to run on a personal workstation. They include privileged account, mount, service, and policy operations. Their placeholders must be reviewed for a dedicated deployment. See [deployment boundary](docs/DEPLOYMENT_BOUNDARY.md).

## Scope and limitations

- Single-machine, single-user, one active inference job; no public-network or multi-tenant security claim.
- Model scores are **uncalibrated softmax scores**, not probabilities of correctness. The existing routing threshold is 0.4.
- Twenty mapped categories are not twenty species. The mapping is preserved, including index 15, `370437 / Heterokontophyta`.
- No weights, model-quality benchmark, dataset redistribution, or full experiment reproduction is provided here.
- No claim of production readiness, complete hardware qualification, full reboot recovery validation, or project-wide 100% test coverage.
- The copied source may contain workstation assumptions. Passing the portable test subset does not qualify host deployment.
- The broker recovery fixes have source-level regression coverage; installed hardware recovery and exFAT/kernel-stall resolution remain unqualified. See the [recovery validation boundary](docs/SECURITY.md#recovery-validation-boundary).

## Repository guide

| Path | Purpose |
| --- | --- |
| `apps/api/` | Local control API |
| `apps/web/` | React interface and mocked UI tests |
| `apps/worker/` | Durable job execution and recovery |
| `packages/core/` | State, output, and classification contracts |
| `packages/control/` | Restricted USB-service client |
| `packages/inference/` | Trusted-checkpoint inference adapter |
| `packages/simulator/` | Development-only fake adapters, not production inference |
| `host/` | Deployment reference and privileged boundary implementation |
| `tests/` | Curated CPU-only portable tests with synthetic fixtures |
| `testing/usb_ownership_contract.py` | Pure evidence evaluator used by the tests |

## Validation and licensing

See the [validation record](docs/VALIDATION.md) for what was actually checked, and what was not.

**No open-source license has been assigned to this source showcase.** Public visibility and permission to reuse are different decisions. This repository does not grant an additional code-reuse license. A future code license requires a separate rights-holder decision. Dependencies retain their own terms; [third-party inventory](THIRD_PARTY.md) records the available metadata without granting rights to code, datasets, weights, or artwork.
