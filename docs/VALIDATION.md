# Validation record

Updated **2026-11-23** for the public source sync described in [SOURCE_SYNC.md](SOURCE_SYNC.md). Current results below belong to this curated checkout. Earlier records are dated separately; none qualifies an installed release.

## Source-sync checks — 2026-11-23

| Check | Result | Scope |
| --- | --- | --- |
| Portable Python tests | **381 passed**, 27 modules | Synthetic data, fake host/USB/GTK/model adapters, in-process API and a temporary Unix socket |
| Frontend unit tests | **50 passed**, 5 files | Mocked API interactions, including explicit busy-USB retry without focus-triggered requests |
| Frontend lint, TypeScript and Vite build | **Passed** | Locked dependencies; generated assets excluded |
| Mocked Playwright workflows | **26 passed** | Isolated Vite server; all API responses mocked; includes release-before-retry and no overlapping zero-image cleanup |
| Synthetic screenshot walkthrough | **1 passed; four images reviewed** | Selection, preflight, progress and completion; updated disclosure, no research data |

`make test-portable` now includes `tests/integration/test_preflight_export_boundary.py`. Its two cases use the actual API, USB socket protocol, USB agent and worker with temporary files and fake host/scanner/model adapters. Cancel or session close after reviewed Preflight leaves no outputs; explicit job confirmation creates the next result directory, preserves the input and previous result, and matches exported bytes to local outputs. This checks normal application flow, **not operating-system enforcement against all possible early writes**. The model and settings fixtures are copied inline from the unexported control suite; upstream test assertions are unchanged.

The six new broker cases stub account/session lookup, command execution and release polling. They exercise the unique local-session policy without logging in, opening or closing any real window. They do not prove the initiating request's session or limit a per-user Files quit to one window/tab.

Python used the existing Python 3.11.15 environment with pytest 9.1.1, Pillow 12.3.0, FastAPI 0.141.1, Starlette 1.3.1 and httpx2 2.7.0. Plugin autoload was disabled, inherited application/Python test settings were cleared, and caches/test files were isolated outside the checkout. Tests ran outside restricted execution so local IPC was available. No dependencies were installed or upgraded.

Frontend checks used Node 20.20.2, npm 10.8.2, Vite 8.2.4 and Vitest 4.1.12 from the existing locked installation. Playwright 1.62.1 ran the 26 app cases and one opt-in capture with its cached Chromium build 1234, one worker and `PLAYWRIGHT_BASE_URL` unset. The screenshot fixture blocked unexpected API/external requests and disabled service workers. All four full-page PNGs retain the simulation banner and synthetic values; no application layout changes were made for capture.

The publication review passed across **143 files**: 19 changed/added files and 124 unchanged files. Five imported source/test files match the selected source exactly; the adapted integration test preserves all upstream functions/assertions and its inline fixtures. All **38 local links/anchors**, 59 Python syntax checks, 10 shell syntax checks, five JSON parses and patch-whitespace checks passed. The dependency graph is valid and its inventory matches all 174 non-root lockfile entries. Only the four declared PNGs are allowed; both SVGs have no external references. No configured private-content pattern hits were found. Public placeholders, proxy isolation and scientific contracts are preserved. Pattern scanning is a bounded check, not a security certification.

No real model inference, GPU, physical USB, deployed/live HTTP backend, browser-to-backend end-to-end run, installed desktop, reboot, image build or service mutation was performed. Fresh dependency installation, backend Ruff/Mypy, full coverage, mutation, fresh vulnerability and penetration audits were not run. GitHub desktop/mobile rendering and Drive playback were last checked on **2026-11-15** as recorded below; they were not rerun for this source refresh.

## Development status review — 2026-11-23

Merged development main `6a518f72e645761a4adb4d182b9188dec5a4c877` includes completed source repairs and additional controlled checks. The final two commits only extend private read-only WebKit peer QA; the selected public implementation remains `7e83edda5ad14b7a439b36f6dac8dca8585975a9`. The following are **reviewed development records, not executions or extra test counts for this public checkout**:

| Reviewed progress | Evidence boundary |
| --- | --- |
| Busy guidance and conservative session guard merged | Source regressions pass. A unique local login still does not prove the initiating Preflight origin or selectively target the blocking Files view; that stronger broker requirement remains unresolved. Real local/remote desktop acceptance remains separate. |
| Seeded-cache WebKit recovery and progress display | Controlled WebKitGTK runs with shipped frontend/helper bundles and synthetic endpoints support fresh assets, session handback/reopen and stage-specific units. The cache-recovery work was closed in development; this is not a natural large-batch run or a throughput benchmark. |
| Controlled busy/retry and GUI QA improvements | Synthetic WebKit checks observed refusal followed by an explicit retry without job creation. New QA tools improve observation and target checks; installed close/reopen/switch/rollback and the remaining desktop matrix are still incomplete. Those tools are not exported. |
| Isolated guest writer research | A synthetic guest experiment exercised a per-job directory and an unprivileged marker writer after an accepted exporting state. It did not publish product results or deliver the architecture into this product baseline; full-lifecycle write isolation and old-handle/replug cases remain open. |

The earlier installed ownership-gate pass remains a historical result for its exact deployment. It does not qualify this newer checkout. Full physical lifecycle/format and large-batch acceptance, including the historical 100k-image exFAT/kernel stall, remain unqualified. The old November 15 mount-authorization prerequisite below is a dated handoff, not a complete description of today's remaining work. No candidate or Production approval follows from this review.

Only these summaries are published. Private evidence, hardware identities, deployment paths, image/manifests and private test totals remain excluded.

## Historical source-sync checks — 2026-11-15

| Check | Result | Scope |
| --- | --- | --- |
| Portable Python tests | **373 passed** | 25 test modules; synthetic data, temporary files, fake host/USB adapters, fake GTK and in-process API requests |
| Frontend lint | **Passed** | `npm --prefix apps/web run lint` |
| Frontend TypeScript + Vite build | **Passed** | `npm --prefix apps/web run build`; generated assets excluded from publication |
| Frontend unit tests | **49 passed**, 4 files | `npm --prefix apps/web run test:unit`; mocked UI interactions, no live backend |
| Frontend dependency graph and inventory | **Passed** | `npm ls --all`; inventory matches all 174 non-root lockfile entries |
| Syntax checks | **Passed** | 57 Python files, 10 shell scripts, 5 JSON files |
| Source disclosure and path checks | **No configured pattern hits** | All 133 public files; private identifiers, deployment checkpoint digest, known token formats, private-key headers and email patterns |
| Public adaptations and documentation | **Passed** | Preserved placeholders, proxy isolation, synthetic fixtures and core mapping; local Markdown file links resolve |
| Patch whitespace | **Passed** | `git diff --check` |

Python validation used Python 3.11.15, pytest 9.1.1, Pillow 12.3.0, FastAPI 0.141.1, Starlette 1.3.1 and httpx2 2.7.0 in an existing environment, through `make test-portable`. Plugin autoload was disabled, inherited application environment variables were cleared, and temporary test files stayed outside the checkout. A fresh Python dependency installation was not verified. An initial runner invocation omitted the parent directory for its explicit pytest temporary path and produced setup errors. A later in-process API run stalled under restricted execution and was stopped. Correcting the runner setup and rerunning the unchanged final sources/tests outside that sandbox produced the result above; host/USB/transport operations remained mocked.

Frontend validation used Node 20.20.2 and npm 10.8.2. Dependencies were installed into the isolated checkout from the existing registry cache with `npm ci --offline --ignore-scripts --no-audit --no-fund`: 133 platform-applicable packages, versus 174 all-platform lockfile entries. The locked build used Vite 8.2.4 and Vitest 4.1.12. No new dependency-vulnerability audit was performed and no previous audit result is represented as a current result.

## Snapshot adaptations

- Private desktop-account/host identifiers and the deployment checkpoint hash remain generic placeholders. No live deployment files were changed.
- The source-sync checks used a plain text replacement wordmark. The later presentation update below restores the owner-approved original SVG.
- The Vite preview retains no API proxy, preventing accidental connection to an already-running inference service.
- The test suite remains a deliberate portable subset. Launcher lifecycle tests were added with fake host operations; progress-helper subprocesses import a fake GTK module and open no real window. These tests require Linux/POSIX behavior and do not validate installed desktop integration.
- The original checkpoint-output fixture remains excluded. The synthetic score fixture does not validate model inference.
- The broker unit test continues to mock service-account lookups and ownership changes, avoiding dependence on workstation accounts.
- A private hostname in three synthetic launcher window-list rows was replaced with `workstation`; test assertions and behavior were preserved.
- The API cleanup test inlines its temporary `ApiSettings` fixture instead of importing an unexported integration module. The HTTP assertions are preserved and use an in-process test client with a fake USB transport; no live API server is started.

The source-sync publication scan checked text-only file membership and recorded file hashes. Pattern matching is not proof that every possible secret or vulnerability is absent. Syntax checks are not full backend lint or type checking.

## Screenshot and logo update — 2026-11-15

The later presentation update adds four synthetic screenshots, the original SVG logo with the owner's publication approval, and an owner-approved external link to the historical v1 beta video. The [capture record](screenshots/README.md) provides the exact starting commit, fixture and reproduction command.

| Check | Result | Scope |
| --- | --- | --- |
| Playwright screenshot walkthrough | **1 passed** | Source selection, preflight, job creation, progress, completion and reset; all API responses synthetic |
| Screenshot review | **4 images reviewed** | Simulation labels, complete page content, synthetic paths/counts, no research images or desktop content |
| Frontend lint and TypeScript + Vite build | **Passed** | Rechecked with the restored original SVG; generated output excluded |
| SVG inspection | **Passed** | Vector paths and inline styles; no scripts, external references, embedded images or fonts |
| Publication and link checks | **Passed** | 139 public files; only four explicitly allowed PNGs, no PNG metadata, no configured private-content pattern hits, local Markdown links resolve |

The capture blocked unmocked API/external HTTP requests and disabled service workers. It used an isolated loopback Vite server with no proxy and did not start the API or worker. The first attempt could not start Vite under restricted execution. A subsequent fixture expectation incorrectly assumed the workstation-status card polls; it was corrected to reflect the existing initial-load behavior. The successful run used the unchanged application with synthetic responses, and the images were then reviewed. These checks do not expand the earlier Python or unit-test counts, and the full existing browser suite was not rerun.

### Public-page review — 2026-11-15

Anonymous HTTP checks confirmed that GitHub renders the README with five image elements and the historical demo link. The original logo, four PNGs and their five enlargement pages returned HTTP 200; the served image bytes matched the reviewed local assets. The four steps retain their selection → preflight → progress → completion order, captions and alternative text. The Drive page returned HTTP 200 with the expected video title, and permission metadata confirmed anyone-with-link reader access.

GitHub's published dark-theme CSS uses `#0d1117` as the default background. The transparent logo's `#102b24` artwork has approximately 1.26:1 contrast against it. A README-only SVG variant adds a pale `#edf5f0` background rectangle while preserving every original path and color. The application SVG and screenshots are unchanged.

The browser access host was unavailable during this initial review, so it covered rendered HTML, served assets, styles and permissions only. The subsequent local Playwright review below completes the browser checks; HTTP 200 and sharing metadata alone do not prove playback.

### Live browser follow-up — 2026-11-15

The published README at commit `786e63d88f6ad8c9c3230bf7594b86c30cb389f5` was checked with Playwright 1.62.1 and Chromium 151.0.7922.34 on Linux, using fresh signed-out contexts. Desktop used a 1440 × 1000 viewport. Mobile used the Pixel 7 Android user agent and touch emulation with a 390 × 844 viewport. Both used device scale 1 for the inspection captures.

| Check | Result |
| --- | --- |
| Desktop and mobile, light and dark themes | **4 configurations passed**; Logo and all four screenshots loaded; four numbered steps remained in order; no page-level horizontal overflow |
| Screenshot readability | Replaced the two-column table with full-width steps and action captions; images now occupy 838 CSS pixels on desktop and 324 on mobile, versus roughly 127–142 on mobile before the fix |
| Full-size screenshot links | **8 click-through checks passed**; all four links opened the original 1440-pixel-wide PNG on both desktop and mobile |
| Desktop demo playback | **Passed**; followed the README link, started the player, decoded 1280 × 720 video; playback advanced from 1.21 to 28.88 seconds |
| Mobile demo playback | **Passed**; followed the README link and tapped Play; decoded 640 × 360 video; playback advanced from 1.03 to 28.69 seconds |

The standard Drive `/view` URL returned an embedded-player error under the Android user agent. The README and capture record now link to the same authorized video's `/preview` endpoint, which played in both contexts. The final playback checks observed `paused=false`, `readyState=4`, no media error, and a duration of about 317.25 seconds. Player controls were used after initialization; early automation clicks on loading overlays were not counted as successful checks. The video and its sharing permissions were not modified.

Browser screenshots were visually inspected alongside DOM assertions for image loading, dimensions, sequence and page width. These are point-in-time checks of GitHub and Drive, not a continuously monitored browser suite. Mobile emulation does not qualify physical phones, Safari or Firefox. The full video was not watched end to end, audio was not audited, and the application/backend test suites were not rerun for these documentation-only changes. The earlier synthetic screenshot and hardware-validation boundaries remain unchanged.

## Not performed

No model loading, training or inference; no GPU use; no physical USB, malware, reboot, deployment or service mutation; no Docker image build; no API live integration suite; no browser-to-backend end-to-end run; no backend Ruff/Mypy run; no full coverage, mutation, CVE or penetration audit. The mocked screenshot test above is an interface demonstration, not an installed-system test.

The reviewed broker disconnect and failed-claim cleanup source repairs are included. Their portable regressions do not resolve the hardware and kernel qualification limits in [SECURITY.md](SECURITY.md#recovery-validation-boundary).

## Development status review — 2026-11-15

A subsequent fetch reviewed development main `7bb441b36a2d10d2f35f09f2b9f2b435a9ed4f92` against the retained implementation baseline `7d2697cb52ef5fd45b6051eae33404b292623e60`. The full delta contains only five internal documentation files across eight commits. There are no application, host, test, build, configuration or dependency changes, and no changed path overlaps the public file set. The fetched implementation/testing branches contain no additional commits beyond that main.

The merged records report the following **separate development-deployment milestones**, not new executions against this curated public checkout:

| Development milestone | What it establishes and what remains open |
| --- | --- |
| Installed USB-ownership gate passed on the retained implementation baseline | Reported checks cover ownership isolation, recovery/refusal/retry paths and cleanup in that deployment. They do not establish full physical-lifecycle acceptance, model quality, capacity or resolution of the historical large-batch/kernel stall. |
| Immutable test candidate prepared and installed | A candidate is available for the next hardware campaign. Installation is not acceptance of that campaign or production promotion. |
| Device returned; native mount still requires authorization | The latest handoff leaves desktop-session mount authentication as a prerequisite before the physical-lifecycle/large-batch campaign can start. This is a host QA prerequisite, not evidence of a new application regression or a completed recovery fix. |

Only this summary is published. Private test counts, manifests, image identities, device identifiers, deployment paths and raw evidence are excluded. The public **373 Python tests, 49 frontend unit tests and one synthetic screenshot test** remain the previously recorded results for their stated scopes. No application tests, build, browser playback or hardware campaign was rerun for this documentation-only refresh, and no installed runtime was inspected or modified by the public-maintenance task.

Refresh verification covered all 140 public files: only five documentation files changed, and the other 135 files match the pre-review public commit byte-for-byte. All 36 local file/anchor references and patch-whitespace checks passed; publication-pattern scanning found no configured private-content hits. Pattern matching is not proof that every possible secret is absent. The reviewed main and implementation baseline remain distinct in [SOURCE_SYNC.md](SOURCE_SYNC.md), so documentation-only development progress is not represented as a new public implementation release.

## Previous public baseline

The initial 2026-11-14 public commit `da833aee2a77fec8e1d6d364c9df2c72082d02fc` recorded 271 portable Python tests and 43 frontend unit tests passing. Those historical counts belong to that snapshot; the dated tables above record later results.

Private hardware reports and their gate counts are not published as validation for this checkout. Source-publication approval does not confer production readiness or assign a code-reuse license.
