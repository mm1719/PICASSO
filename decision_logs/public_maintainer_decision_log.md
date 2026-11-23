# Public maintainer decisions

## 2026-11-15 11:00 CST — Maintain the public showcase from reviewed development results

- Background: The development repository continues to evolve while PICASSO presents completed engineering work as a side project. The owner requested a fresh comparison and publication of completed, suitable changes.
- Decision: Use the merged development revision recorded in `docs/SOURCE_SYNC.md`; carry its scan-progress, launcher-lifecycle and frontend-tooling fixes with portable regressions. Retain public sanitization and document pending recovery work. Keep the source-only beta scope and existing licensing status.
- Impact: Public source and documentation advance together with fresh validation. No private history, internal evidence, deployment configuration, research data or model artifacts are published, and no installed runtime is changed.

## 2026-11-15 11:09 CST — Include newly accepted recovery repairs before publication

- Background: The final fetch found newly merged broker/API recovery repairs and independent source QA while this update was being prepared.
- Decision: Advance the selected source to `7d2697cb52ef5fd45b6051eae33404b292623e60`, include its recovery implementation and portable regressions, and rerun affected public checks. Keep temporary API settings local to the public test module.
- Impact: The showcase includes the completed source repairs. Hardware recovery, kernel-stall diagnosis and deployment approval remain separate; no private QA reports or hardware artifacts are exported.

## 2026-11-23 14:36 CST — Sync completed source changes and keep research limits explicit

- Background: The owner requested another fetch and selective public update from completed development work.
- Decision: Review merged `6a518f72e645761a4adb4d182b9188dec5a4c877`, retaining `7e83edda5ad14b7a439b36f6dac8dca8585975a9` as the implementation baseline after the final private-QA-only delta; include busy-USB guidance, the conservative session guard, portable export/retry regressions and refreshed synthetic screenshots. Inline only the required fake model/settings fixtures and extend the portable test selection. Preserve existing public adaptations and licensing.
- Impact: Fresh checks pass: 381 Python, 50 frontend unit and 27 mocked browser tests, plus frontend lint/build. Controlled WebKit and guest research are summarized separately; origin/target-limited Files closure, physical lifecycle and the historical large-batch stall remain unqualified. No private reports, research prototype, deployment changes or real data are published.
