# Changelog

All notable changes to Helios are documented here. Each release is a
`vX.Y.Z` tag on `main`; its GitHub Release carries the `.deb`, the tagged
source (`source.tar.gz`) and `SHA256SUMS.txt`.

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
