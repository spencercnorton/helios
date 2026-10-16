# Helios — Session Journal

## 2026-10-16 — claude — Usage stats visible for every provider (v0.105.0)

- **Branch:** `claude/usage-stats`
- **Symptom:** "none of our models show context or usage stats". The ring and
  hover popover existed, but: Muse emitted cumulative `(prompt, output)` as
  `(used, window)` and dropped MSP `session/contextUsage` into an activity
  event; Google never emitted `usage-updated` (window 0 → permanent "·");
  OpenRouter 429 rows carried the upstream name as `provider` and were always
  filtered out, and a failed turn zeroed the ring; switching to a live session
  reset the meter to 0 because the transcript re-read defers to live drivers;
  nothing was readable without hovering.
- **What changed:** Muse uses `session/contextUsage` and maps `usage/changed` /
  `usage/read` (`SubscriptionUsage`) to limit rows (`rate_limits.muse_rate_limit_rows`;
  `humanize_window_mins` moved there from `codex_app_contract`). Google emits
  the fill from each DONE `step_update.usage` (measured: per model call, input
  excludes `cache_read_tokens`; agy publishes no window, so it stays 0 and the
  caption reads "N used"). OpenRouter emits per round and skips zero fills.
  The window caches `drv._helios_last_usage` / `_helios_session_tokens` and
  restores them on switch; limit rows are filed per provider in the popover and
  no longer gated on the visible driver (quotas are account-wide). New
  `usage_caption` label beside the ring; popover gains "This session" totals and
  `set_max_width_chars(46)` on wrapped notes (it outgrew the window and clipped
  the legend numbers). Transcript assistant names now come from one table that
  includes Muse.
- **Validation:** worktree Helios run on Xvfb `:99` under `dbus-run-session`
  with a private `HELIOS_STATE_DIR`: Gemini `13.8k used`, Muse
  `49.8k / 1.008M · 5-hour 6%`, Claude `37.2k / 1M` with legend values and
  "This session". GPT could not be exercised there (Codex credentials sit in the
  GNOME keyring, unreachable from the private bus). Full suite green except the
  two `test_model_catalog_refresh` failures that also fail on `main`.
- **GPT review (gpt-6.1-sol, two passes) — fixed:** a 429 row never cleared
  (now removed after the next reported round); Google fill missing from the
  popover/tooltip; Codex footer/totals counted only the last request (now the
  delta of `tokenUsage.total`, seeded `{}` for a new thread, unknown for the
  first turn after a resume); Gemini "in" ignored `cache_read_tokens` — the old
  test claiming agy input already includes cache was contradicted by the
  measurement and was corrected; background Claude `get_context_usage` was not
  cached; `_apply_context_fill` guessed 1M for an unknown Google window.
- **Not done:** Muse/Gemini transcripts store no usage, so reopening an idle
  session of theirs shows 0 until its next turn.


## 2026-10-14 — Fix startup provider adoption race and in-flight resume driver tracking

- **Branch:** `fix/session-resume-provider-and-startup-race`
- **Symptom:** Resuming a Muse or Google session right after startup could switch the Work lead to the persisted model from `ui-state.json`, clear the transcript pane to a fresh chat, and spawn a contextless session on another provider; additionally, selecting/refreshing the sidebar while a large `session/resume` was still initializing unbound the starting driver and allowed duplicate resume processes.
- **Root cause:**
  1. `_adopt_session_provider` ran during startup session selection before the background model catalog worker populated `_catalog_entries_by_id` / `_openai_entries`, so `_model_for_provider` returned `""` and `_apply_model_catalog` never re-adopted the staged session's provider on completion. `_on_session_selected` also passed the un-adopted `self._model` into `resolve_native_work`.
  2. `DriverManager.driver_for_session`, `DriverManager.driver_for_work_participant`, `route_session_selection`, `_on_session_selected`, and `_ensure_driver` only matched drivers with `is_accepting_input=True` and populated `session_id`, missing `MuseServeDriver` and `GeminiCliDriver` instances while `is_initializing=True` during `session/resume`.
- **What changed:**
  - Record `_pending_session_provider_adoption` when `_adopt_session_provider` runs before catalog readiness, re-adopt on `_apply_model_catalog` completion (unless the user explicitly changed the model), guard `_ensure_driver` against cross-provider hijacking while the startup catalog is still loading, and only pass `self._model` to `resolve_native_work` when its provider matches `session_provider`.
  - Recognize `is_initializing` drivers and their `_helios_expected_resume_id` across `DriverManager`, `route_session_selection`, `_on_session_selected`, `_ensure_driver`, and `_on_composer_send`.
- **Tests:** Added unit coverage in `tests/test_model_catalog_refresh.py`, `tests/test_driver_manager.py`, `tests/test_session_router.py`, and `tests/test_work_participant_switch_window.py`.


## 2026-10-13 — muse — Muse parity audit, complementary tests, 0.103.3

- **What changed:** a Muse session running under Helios audited its own
  parity against Claude/GPT/Gemini, found the canonical checkout was a
  detached v0.101.5 tree with no Muse provider while `main` (0.103.2)
  already ships the complete Muse integration, and verified the ported
  tree byte-identical to `main` for every production file. This change
  lands only what was genuinely missing upstream: complementary
  `test_muse_env.py` coverage (provider identity/registry, model routing
  incl. OpenRouter exclusion, permission-mode parity, fail-closed
  discovery) and the Muse row in the sidebar-chip test.
- **Validation:** full suite green in this worktree (see PR); live
  `muse_env.fetch_auth_status` probe returns `accountLogin` with the
  4-model `muse-serve` catalog.
- **Release:** 0.103.3 (patch — tests and release bookkeeping only).

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
