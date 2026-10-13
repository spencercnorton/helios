# Helios — Session Journal


## 2026-10-13 — claude — Muse Bypass shell + tracker grant (v0.103.2)

- **What changed:** #17 — Bypass spawns `muse serve --disable-sandbox`;
  interactive Muse gets `NORVI_TRACKER_ENV` (Infisical stays Claude-only).
- **Why:** Muse requires bwrap `--ro-bind-symlink` (absent in Ubuntu's
  0.11.1), falls back to its embedded arm64 bwrap under qemu-user, and
  AppArmor's `apparmor_restrict_unprivileged_userns` denies the namespace.
- **Still open:** Apollo MCP import into Muse settings; Ask-mode sandbox on
  Ubuntu; a native x86_64 Muse binary if one exists.

## 2026-10-13 — Fix Muse "unsafe provider" send rejection

- **Branch:** `muse/fix-unsafe-provider`
- **Symptom:** on Apollo (0.103.0), Spark 1.2 showed in the picker but every
  Muse send failed with "Helios rejected an unsafe provider identity";
  log: `session … came from unknown provider ''`.
- **Root cause:** `MainWindow._driver_provider` allowlist never learned
  `muse`, so the identity fence in `_on_session_started` rejected every
  Muse session. Headless driver probes bypassed the window fence, which
  is why they passed. Sibling parity gaps fixed in the same change:
  `work_coordinator.detach_native`, `session_handoff` labels, and the
  work-store concurrency lanes (`muse: 4`).
- **Tests:** `test_driver_provider_resolves_muse` +
  `test_session_started_confirms_muse_runtime_identity` — both fail on
  0.103.0 code and pass with the fix (stash-proven).
- **Release:** 0.103.1 patch.

## 2026-10-12 — Model picker navigation, duplicates, customization

- **Branch:** `muse/model-picker`
- **What changed:** shared picker hygiene in `model_catalog` (`dedupe_entries`,
  `visible_entries`, `collapse_latest_twins`) applied at the window catalog,
  chat toolbar, and Settings choices; chat picker search is always present
  (was gated above 25 total rows) with a checkmark on the current model;
  Settings default-model combo is searchable; new Settings → Providers →
  Shown models list hides any model from all pickers (selected model exempt,
  restore by re-ticking, stored as `hidden_models`).
- **Validation:** ruff clean; GTK-hidden suite 1,891 passed / 90 skipped;
  picker + Settings tests pass on the GTK lane, including hidden-until-selected
  rendering. Note: a selected-row `set_icon_name` would have erased the
  button label — the marker is a `✓` label prefix + CSS class instead.
- **Release:** 0.103.0 prepared after 0.102.0 (Muse provider) shipped.

## 2026-10-12 — Muse native provider (MSP)

- **Branch:** `muse/native-provider`
- **What changed:** new `muse` native provider driving `muse serve` (MSP v1):
  binary discovery (`HELIOS_MUSE_BINARY` > PATH > `~/.local/bin`), account and
  catalog probes via `account/read` + `model/list`, MSP driver with streaming,
  tool cards, subagent/workflow dock parity, approvals and user-input through
  the existing question dialog, durable execution receipts and transcript
  mirroring (`helios-muse`), Settings → Providers group with terminal sign-in
  and stdin-only API-key storage, and live-proven-only model selection.
- **Evidence:** live `muse serve --provider meta` probes proved the catalog
  (wonder/spark, 1,007,997 context), experimental `account/read` states, and a
  full driver turn (`pong`, success receipt with real token usage) on macOS.
- **Validation:** ruff clean; full GTK-hidden suite 1,891 passed / 90 skipped;
  native driver + signal-contract tests passed; new `test_muse_env.py` and
  `test_muse_driver.py` cover catalog parsing, fail-closed auth, tool
  normalization, agent lifecycle, and approval decisions.
- **Release:** 0.102.0 prepared for GitHub review and tag-driven publication.
- **Known gaps:** GTK visuals not exercised on Ubuntu from this host; Linux
  `muse` binary availability for apt users is detect-only (install guidance
  shown when missing); OpenRouter high-context and broader Settings work from
  the same review remain follow-up releases.

## 2026-10-12 — gekleos — Google native accounting and lifecycle repair

- **Branch:** `codex/google-native-integration`
- **What changed:** retain native Google input-step identity across acceptance
  and completion receipts; normalize token counters for the durable Work store;
  promote direct and queued messages only after native acceptance; preserve a
  second startup submission as a draft; restore the common activity clock and
  effective execution properties; suppress expected intentional-stop errors.
- **Evidence:** a real window reproduced the missing accepted-turn identifier
  and unsupported usage-field failures, plus a duplicate startup process. The
  repaired window completed Flash chat/continuity/resume and Pro file-edit,
  command-validation and queued-follow-up checks using the native subscription
  CLI. The changed file passed an independent Python check.
- **Validation:** 3,366 native GTK tests passed, including regressions using
  the real Work store and window accounting adapters. Ruff, compileall,
  whitespace and source secret scanning passed. Native Google regressions now
  run under Xvfb in the required CI check.
- **Release:** 0.101.5 prepared for GitHub review and tag-driven publication.
- **Status file updated:** separate private operating record tracks source,
  installation and running-app activation.

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
