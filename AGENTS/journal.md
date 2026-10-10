# Helios — Session Journal

## 2026-10-10 — gekleos — Google model effort grouping and permissions integration

- **Branch:** `antigravity/google-effort-and-permissions`
- **What changed:** Google model discovery groups reasoning variants into base models with selectable effort in the execution toolbar; full permission mode selection enabled for Google sessions with corresponding CLI flags.
- **Validation:** 3,354 tests passed on Apollo; Ruff, compileall, and diff whitespace checks clean.
- **Release:** 0.101.2 prepared for the pull request and tag-driven release.
- **Next step:** submit pull request on GitHub and verify CI.
- **Status file updated:** separate operating record tracks rollout.

## 2026-10-10 — gekleos — Google subscription transport repair

- **Branch:** `fix/google-subscription-bridge`
- **What changed:** live Antigravity model discovery and account-quota checks, account-only routing, corrected native stream/session protocol, durable Work admission and receipts, transcript mirrors, and provider-specific headless permissions.
- **Validation:** authenticated two-turn continuity and process-group shutdown passed in an isolated scratch folder; focused protocol and failure-path tests passed. Full GTK release validation is recorded in the pull request.
- **Release:** 0.101.1 prepared for the pull request and tag-driven release.
- **Next step:** use an isolated worktree for bounded Google tasks and review proposed changes before merging.
- **Status file updated:** separate operating record tracks rollout.

## 2026-10-06 — gekleos — native CLI data presentation

- **Branch:** `fix/cli-data-presentation`
- **What changed:** Claude quota percentages/current labels and native tool progress; bucket-scoped Codex quotas, recovery/window removal and actionable hook notices; current protocol manifest and a documented integration audit.
- **Validation:** 3,250 GTK tests passed; 1,841 passed / 88 skipped with GTK hidden; Ruff, compileall, whitespace and secret scanning passed.
- **Release:** 0.100.0 prepared for the GitHub pull request and tag-driven release; existing installations were not changed.
- **Next step:** review the source changes and remaining integration work in `docs/CLI-DATA-AUDIT-20261006.md`.
- **Status file updated:** yes, in the separate operating record.
