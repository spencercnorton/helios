# Changelog

All notable changes to Helios are documented here. Each release is a
`vX.Y.Z` tag on `main`; its GitHub Release carries the `.deb`, the tagged
source (`source.tar.gz`) and `SHA256SUMS.txt`.

## 0.101.4 — 2026-10-10

### Fixed

- User prompts submitted while the Google CLI driver initializes are buffered as pending rather than rejected, automatically dispatching to the native CLI once native initialization completes.
- Aborted or interrupted Google session startups cleanly release buffered prompt attempts and execution lanes.

## 0.101.3 — 2026-10-10

### Fixed

- Google provider identity is recognized by the session manager and live driver registry so native session starts are accepted rather than rejected as unknown provider identities.
- Google assistant labels and transcript views display the proper provider name and avoid overwriting Claude's tool snapshot.

## 0.101.2 — 2026-10-10

### Fixed

- Google model selection groups reasoning variants (low, medium, high) into base models with the effort level selected through the execution toolbar instead of displaying separate model dropdown entries.
- Google sessions support all standard permission modes (Ask, Accept edits, Auto, Bypass, Plan, Never ask) with appropriate CLI flags (`--dangerously-skip-permissions`, `--mode plan`, `--mode accept-edits`, `--sandbox`).

## 0.101.1 — 2026-10-10

### Fixed

- Google models and labels now come from the live Antigravity catalog, without guessed models or context sizes. Account sign-in is verified through native quota results.
- Google sessions use Antigravity's persistent streaming protocol, native conversation IDs and terminal results, including guarded Work admission and uncertain-delivery handling.
- API/custom provider configurations and AI-credit fallback are refused on the Google account route; the separate Gemini CLI is no longer treated as an interchangeable transport.
- Google Settings shows the actual account/discovery status and refreshes model choices without blocking the main loop.

### Changed

- Google sessions offer Never ask with terminal sandboxing; workspace edits remain possible under the native CLI's policy. Added setup instructions and a separate-worktree trial workflow.

## 0.101.0 — 2026-10-06

### Added

- Native Google subscription support via Gemini CLI / Antigravity (`agy` / `gemini`).
- Unified session model dropdown grouped by provider with direct subscription routing.

### Changed

- Removed the segmented Claude / GPT / OpenRouter provider toggle from the main header bar in favor of per-session model selection.
- OpenRouter catalog strictly excludes models from native subscription providers (Anthropic, OpenAI, Google).

## 0.100.0 — 2026-10-06

### Changed

- Claude usage limits now show native percentages and readable weekly/extra-usage labels; active tool progress shows the CLI's elapsed time.
- Codex hook failures and blocking decisions appear as concise transcript notices, with private context excluded.
- The Codex protocol compatibility manifest now targets CLI 0.160.1.

### Fixed

- Codex quota buckets no longer inherit another bucket's windows or blocked status, and recovered limits and removed windows update correctly.
- Unavailable quota percentages stay unknown, and unchanged quota rows avoid redundant widget rebuilding.

## 0.99.8 — 2026-10-04

### Fixed

- The Release workflow's build artifact is now `helios-debian-candidate` and carries the package with its own `SHA256SUMS.txt`, so a downstream archive can check that the released `.deb` is byte-for-byte the one CI built for the tag.

## 0.99.7 — 2026-10-04

### Changed

- Extra instructions for every session (Claude, GPT and OpenRouter) now come from optional `*.md` files in `$XDG_CONFIG_HOME/helios/system-prompt.d/`, read in name order at startup. Helios no longer ships a built-in work-tracker policy.
- The cross-machine session pool defaults to `$XDG_DATA_HOME/helios/session-pool`; `HELIOS_SESSION_POOL` still overrides it, and the pool stays inert until the directory exists.
- The Shared pane's endpoint and key are read from `HELIOS_SCRATCHPAD_URL` and `HELIOS_SCRATCHPAD_KEY` (renamed; set these if you use the pane). Both are stripped from agent environments.
- `scripts/build-deb.sh` is now `scripts/build.sh`: it builds the `.deb` only and runs `lintian` when installed. Releases are built and published by the repository's Release workflow.
- Development happens in this repository: pull requests are reviewed and squash-merged here.
- CI runs on `ubuntu-24.04`, and the README footer links the whole suite.
- `system-prompt.d` is capped at 64 KiB in total; a file that would pass the cap is skipped with a warning.
- `scripts/install-helios-router-service` takes the client user from `HELIOS_ROUTER_CLIENT_USER` or `SUDO_USER` and no longer has a built-in default.

### Fixed

- The router service installer works from a source checkout: `deploy/helios-router.service.in` is now part of the tree.
