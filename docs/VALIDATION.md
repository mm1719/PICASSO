# Validation record

Updated 2026-11-15 for the public source sync described in [SOURCE_SYNC.md](SOURCE_SYNC.md). Checks below ran against this public checkout, including its sanitization and curated tests. They do not qualify an installed release.

## Source-sync checks

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

The browser access host was unavailable during this review. These are checks of the live site's rendered HTML, served assets, styles and permissions, not a browser screenshot review. Desktop/mobile layout, dark-theme rendering and in-browser video playback still need visual confirmation; HTTP 200 and sharing metadata alone do not prove playback.

## Not performed

No model loading, training or inference; no GPU use; no physical USB, malware, reboot, deployment or service mutation; no Docker image build; no API live integration suite; no browser-to-backend end-to-end run; no backend Ruff/Mypy run; no full coverage, mutation, CVE or penetration audit. The mocked screenshot test above is an interface demonstration, not an installed-system test.

The reviewed broker disconnect and failed-claim cleanup source repairs are included. Their portable regressions do not resolve the hardware and kernel qualification limits in [SECURITY.md](SECURITY.md#recovery-validation-boundary).

## Previous public baseline

The initial 2026-11-14 public commit `da833aee2a77fec8e1d6d364c9df2c72082d02fc` recorded 271 portable Python tests and 43 frontend unit tests passing. Those historical counts belong to that snapshot; the table above records this update's fresh results.

Private hardware reports and their gate counts are not published as validation for this checkout. Source-publication approval does not confer production readiness or assign a code-reuse license.
