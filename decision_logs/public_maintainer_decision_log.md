# Public maintainer decisions

## 2026-11-15 11:00 CST — Maintain the public showcase from reviewed development results

- Background: The development repository continues to evolve while PICASSO presents completed engineering work as a side project. The owner requested a fresh comparison and publication of completed, suitable changes.
- Decision: Use the merged development revision recorded in `docs/SOURCE_SYNC.md`; carry its scan-progress, launcher-lifecycle and frontend-tooling fixes with portable regressions. Retain public sanitization and document pending recovery work. Keep the source-only beta scope and existing licensing status.
- Impact: Public source and documentation advance together with fresh validation. No private history, internal evidence, deployment configuration, research data or model artifacts are published, and no installed runtime is changed.

## 2026-11-15 11:09 CST — Include newly accepted recovery repairs before publication

- Background: The final fetch found newly merged broker/API recovery repairs and independent source QA while this update was being prepared.
- Decision: Advance the selected source to `7d2697cb52ef5fd45b6051eae33404b292623e60`, include its recovery implementation and portable regressions, and rerun affected public checks. Keep temporary API settings local to the public test module.
- Impact: The showcase includes the completed source repairs. Hardware recovery, kernel-stall diagnosis and deployment approval remain separate; no private QA reports or hardware artifacts are exported.
