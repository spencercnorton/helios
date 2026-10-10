# Helios — Session Journal

## 2026-10-10 — gekleos — buffer startup prompts in Google CLI driver

- **Branch:** `antigravity/buffer-google-startup-prompt`
- **What changed:** buffer user prompts submitted while the native Google CLI initializes, dispatching them automatically upon native init verification and properly releasing buffered attempts on early aborts.
- **Validation:** focused unit tests passed in `test_gemini_driver.py` and `test_execution_settings_window.py`; full test suite on Apollo; Ruff, compileall, whitespace and version agreement clean.
- **Release:** 0.101.4 prepared for the pull request and tag-driven release.
- **Next step:** submit pull request on GitHub and verify CI.
- **Status file updated:** separate operating record tracks rollout.

## 2026-10-10 — gekleos — recognize Google provider in session manager

- **Branch:** `antigravity/fix-google-session-identity`
- **What changed:** recognized `google` provider in `_driver_provider`, `_adopt_session_provider`, and `_on_session_started`, preventing native Google sessions from being rejected as unknown provider identities; updated assistant and transcript labels; avoided tool snapshot clobbering.
- **Validation:** focused unit tests passed in `test_execution_settings_window.py`; 3,356 tests passed on Apollo; Ruff, compileall, whitespace and version agreement clean.
- **Release:** 0.101.3 prepared for the pull request and tag-driven release.
- **Next step:** submit pull request on GitHub and verify CI.
- **Status file updated:** separate operating record tracks rollout.

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
