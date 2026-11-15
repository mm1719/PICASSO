# Source sync record

## Latest development review — 2026-11-15 14:23 CST

Both repositories were fetched again before reviewing newly merged work.

| Reference | Revision |
| --- | --- |
| Public checkout reviewed | `a1b781d926f63ae523d18ddb0a35cd400ea08afb` |
| Latest reviewed development main | `7bb441b36a2d10d2f35f09f2b9f2b435a9ed4f92` |
| Retained implementation baseline | `7d2697cb52ef5fd45b6051eae33404b292623e60` |

The complete development delta contains **8 commits and 5 changed internal documentation files**: installation reports, a tester handoff and role decision records. Application, host, test, build, configuration and dependency sources are unchanged. There is no new implementation or regression test to import; the public implementation baseline therefore stays at the revision above. The fetched implementation and testing branches contain no additional commits outside the reviewed main.

The completed development milestones are a reported installed USB-ownership gate pass and preparation of an immutable test candidate. The next physical-lifecycle and large-batch campaign remains pending a native desktop-mount authorization prerequisite. This update publishes only a summary and the [validation boundary](VALIDATION.md#development-status-review--2026-11-15). Private reports, deployment identities, manifests and hardware evidence remain excluded; their results are not added to the public test totals or treated as production qualification.

## 2026-11-15 update

This repository is a maintained source showcase. Updates select reviewed, merged development work and keep the public adaptations needed for independent inspection. Original development history, private reports and deployment records are not imported.

| Reference | Revision |
| --- | --- |
| Previous public commit | `da833aee2a77fec8e1d6d364c9df2c72082d02fc` |
| Previous development source | `52bf1d28871a53fc5afdc24de5238f6c611a6961` |
| Development source selected for this update | `7d2697cb52ef5fd45b6051eae33404b292623e60` |

Development revisions are provenance identifiers; their Git objects and private repository access are not part of this public checkout.

## Included work

| Change | Public source and checks |
| --- | --- |
| Keep initial scanning throughput unknown until byte progress exists; preserve stage-specific units through polling and recovery | `apps/web/src/App.tsx`, `App.test.tsx`, `ScanningUnits.regression.test.tsx` |
| Scope interruption signals across launcher rollback so the restored GUI remains supervised | `host/usb_agent/plankton_launcher.py`, `tests/unit/test_launcher_lifecycle.py` |
| Close the switching progress helper when its own launcher lifetime pipe closes, including parent death before startup | `host/bin/plankton-switch-progress`, launcher adapter, `tests/unit/test_switch_progress_lifecycle.py` |
| Refresh the accepted frontend dependency set | Vite 8.2.4, Vitest family 4.1.12, locked transitive updates, and `NPM_DEPENDENCY_METADATA.json` |
| Isolate broker transport failures, persist cleanup before detach, and revalidate device identity during rollback | `host/usb_mount_broker.py` and broker transport, cleanup and replacement-device regressions |
| Recover unacknowledged claims through the USB service and API; let Refresh retry the exact pending cleanup | USB agent/ownership modules, `apps/api/plankton_api/app.py`, `apps/web/src/App.tsx`, in-process API and mocked UI regressions |

All synchronized implementation and package files match the selected source byte-for-byte. The new regressions preserve their upstream assertions. Public adaptations replace a private hostname in three synthetic launcher window-list rows with `workstation`, retain mocked account lookups in the existing broker test, and inline the temporary API settings fixture instead of importing the unexported integration suite. The test requirements now include the API and its in-process client without the inference stack.

## Public adaptations retained

- Generic deployment placeholders. The initial plain text wordmark was subsequently replaced with the owner-approved original SVG in the showcase presentation update below.
- Loopback frontend with no default API proxy.
- Synthetic score fixture and portable test selection, including mocked service-account lookups.
- Showcase-specific Makefile, Python test settings, development requirements and documentation.
- Existing class mapping, preprocessing, 0.4 score routing, output schemas and uncalibrated-score wording.

## Deferred and excluded work

The selected merged source includes the independently reviewed broker disconnect, device-identity and failed-claim cleanup repairs. Their source acceptance does not qualify installed hardware recovery or establish that an exFAT/kernel stall is fixed; see [security boundaries](SECURITY.md#recovery-validation-boundary). Unmerged future work remains outside this snapshot.

Private hardware/desktop QA automation, internal reports, decision logs, release manifests, datasets and checkpoints remain excluded. The public maintainer's [decision log](../decision_logs/public_maintainer_decision_log.md) records the source-sync publication decisions.

## Showcase presentation update — 2026-11-15

After the source sync and README restructuring, the owner approved restoring `apps/web/src/assets/logo_picasso.svg` and linking the September 2026 v1 beta demo hosted on Google Drive. The original SVG replaces the temporary wordmark without changing application logic. Four new screenshots render the public frontend with synthetic API responses and a simulation banner; they are not extracted from private research runs or from the historical video. The [capture record](screenshots/README.md) documents their source, reproduction and limits. No additional development implementation changes were imported in this presentation update.

## Next update

Fetch both repositories and compare new work against the latest reviewed development main above, retaining the implementation baseline separately. Select newly reviewed and merged implementation changes when available. Preserve public adaptations, bring the relevant portable regressions with each code change, regenerate dependency metadata when its lockfile changes, and rerun affected public checks. Review all tracked files and the final diff for private content before publishing. Record the new reviewed revision and actual results; source publication does not approve a deployment.
