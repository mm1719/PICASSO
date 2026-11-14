# Validation record

Prepared 2026-11-14 from a fixed source snapshot. The original project was read-only throughout preparation. This record covers the curated public-preview files, not a deployed release.

## Completed checks

| Check | Result | Scope |
| --- | --- | --- |
| Portable Python tests | **271 passed** | 18 test modules; synthetic data, temp files and fake host operations |
| Frontend lint | **Passed** | `npm run lint` using locked oxlint |
| Frontend TypeScript + Vite build | **Passed** | `npm run build`; built assets excluded from source deliverable |
| Frontend unit tests | **43 passed**, 2 files | Mocked UI interactions; no live backend |
| Source disclosure scan | No configured pattern hits in 122 exported text files | Private identifiers, known token formats, private-key headers and email checks; not a proof of absence of all secrets |

Python validation used Python 3.11.15, pytest 9.0.3 and Pillow 12.2.0 in an existing local environment. Plugin autoload was disabled and inherited application environment variables were cleared. A fresh environment installation was not verified.

Frontend validation used Node 20.20.2 and npm 10.8.2. Dependencies were installed from the existing registry cache with `npm ci --offline --ignore-scripts --no-audit --no-fund`; no dependency-vulnerability audit was performed. The locked frontend build used Vite 8.2.0 and Vitest 4.1.10.

## Snapshot adaptations

- Private desktop-account/host identifiers and the deployment checkpoint hash were replaced with generic placeholders. No live deployment files were changed.
- The original artwork was replaced with a plain text SVG wordmark.
- The Vite preview has no API proxy, preventing accidental connection to an already-running inference service.
- The test suite is a deliberate portable subset. Host installation, physical-device, GPU, load, full integration and original report-dependent tests were not exported.
- The original checkpoint-output fixture was not exported. Its contract check was replaced by an explicitly synthetic score fixture; this does not validate model inference.
- One broker unit test now mocks service-account lookups as well as ownership changes, avoiding dependence on accounts installed on the original workstation.

Static syntax checks also passed for 50 Python files, 10 shell scripts and 5 JSON files. Final archive membership and file hashes were checked against an explicit allowlist. This does not constitute full backend lint or type checking.

## Not performed

No model loading, training or inference; no GPU use; no physical USB, malware, reboot, deployment or service mutation; no Docker image build; no API live integration suite; no browser end-to-end run; no backend Ruff/Mypy run; no full coverage, mutation, CVE or penetration audit.

Historical beta reports are not copied into this export and their counts are not represented as results for this snapshot. Passing this selection is not full release qualification or a claim of production readiness. Source-publication approval was confirmed separately. No code-reuse license was assigned.
