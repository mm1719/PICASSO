# Screenshot provenance and reproduction

Regenerated on **2026-11-23** from public commit `ee74a0d68be44530a1867ad505d2f636b1b6af7a` plus the reviewed frontend update from `7e83edda5ad14b7a439b36f6dac8dca8585975a9`. The screenshots now show the local/remote Files guidance and explicit retry text. The owner-approved original SVG and synthetic capture fixture are unchanged; no application layout was changed for capture.

| Image | Visible step |
| --- | --- |
| [01-select-source.png](01-select-source.png) | Select `DEMO USB`, browse `/demo-batch`, enter an optional metadata path |
| [02-review-preflight.png](02-review-preflight.png) | Review synthetic image count and storage checks before confirming |
| [03-follow-progress.png](03-follow-progress.png) | Follow a simulated job at 50%; the overall ETA is still unknown |
| [04-review-results.png](04-review-results.png) | Review simulated export totals, archive path and score caveat |

## What is real and what is simulated

The page is the repository's actual frontend, rendered by Playwright Chromium. The capture script adds only the top banner, `SIMULATED UI · Synthetic data · No model or USB used`, outside the application. Images are full-page PNG captures at a 1440 × 1080 viewport and device scale 1; page content may extend below the viewport. No compositing or image retouching is applied.

Every API response is synthetic and defined in [the capture test](../../apps/web/e2e/showcase.spec.ts). The fake device, directory entries, metadata path, 12-image batch, predictions, rejection counts, throughput, storage figures and archive path do not come from a research dataset or installed workstation. No image or CSV contents are loaded. The banner applies to every status message, including GPU availability and export completion. The workstation-status card is the application's initial-load snapshot; the job panel updates through mocked polling.

The test exercises source selection, preflight confirmation, job creation, progress polling, completion summary and preparation for another job. It asserts the selected paths, preflight identifier and visible states. Unmocked API and external HTTP requests are blocked and fail the capture; service workers are disabled. The isolated Vite configuration has no API proxy.

This demonstrates interface behavior, not real scanning, inference accuracy, throughput, storage sizing, USB ownership, file export, archive integrity or installed recovery. It does not qualify hardware or a deployment. See [the validation record](../VALIDATION.md).

## Regenerate

Install the locked frontend dependencies as described in the [root README](../../README.md#build-and-inspect-the-interface), then run:

```bash
cd apps/web
npx playwright install chromium
env -u PLAYWRIGHT_BASE_URL PICASSO_SCREENSHOTS=1 npm run test:e2e -- e2e/showcase.spec.ts --workers=1
```

The first command downloads the Playwright browser if it is not already cached. The capture command starts its own loopback Vite server on port 4173 and overwrites only these four PNGs. It refuses an occupied port or an inherited `PLAYWRIGHT_BASE_URL`; do not point it at an installed service. Without `PICASSO_SCREENSHOTS=1`, the capture test is skipped so an ordinary browser test run does not rewrite documentation assets. Review all four images before publishing; fonts and rendering can vary between environments.

The recorded run used Node 20.20.2, npm 10.8.2, Playwright 1.62.1 and its cached Chromium build 1234 on Linux. **One capture test passed**, with no unexpected API/external HTTP requests or page errors; all four images were visually reviewed. The same run passed 26 mocked workflow regressions; frontend unit tests, lint and build also passed. This is separate from the historical GitHub/Drive browser checks below.

## Logo and historical video

The [original SVG logo](../../apps/web/src/assets/logo_picasso.svg) is included with the owner's explicit publication approval. It contains vector paths and inline styles, with no scripts, external references, embedded raster images or bundled fonts. Approval to publish is not an additional reuse license.

The root README uses a [presentation variant](../assets/picasso-logo-readme.svg) with a pale background rectangle for contrast on GitHub's dark theme. The original paths and colors are unchanged; the application and these screenshots continue to use the transparent original.

The [PICASSO v1 beta demo](https://drive.google.com/file/d/1i3bz5awIcIcf7QZb3rSTd7moFuEhFYoC/preview) is a separate historical recording from September 2026, approximately 5 minutes 17 seconds long. The owner confirmed its contents are approved for public linking. It includes the original logo, desktop workflow and exported result files; it is not the source of these synthetic screenshots. Its existing Drive link-sharing permission was left unchanged, and the video is not copied into this repository. It is neither a benchmark nor evidence that the current checkout has passed hardware validation.

The preview endpoint replaces the standard Drive viewer link after a mobile playback check exposed a viewer error. Desktop and Android-emulated playback through the published README were subsequently verified on 2026-11-15; playback was not rerun for the 2026-11-23 source refresh. The [live browser record](../VALIDATION.md#live-browser-follow-up--2026-11-15) also documents the single-column screenshot layout and full-size image-link checks, with their limits.
