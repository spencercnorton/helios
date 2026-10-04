# Changelog

All notable changes to Helios are documented here. Each release is a
`vX.Y.Z` tag on `main`; its GitHub Release carries the `.deb`, the tagged
source (`source.tar.gz`) and `SHA256SUMS.txt`.

## 0.99.7 — 2026-10-04

### Changed

- Extra instructions for every session (Claude, GPT and OpenRouter) now come from optional `*.md` files in `$XDG_CONFIG_HOME/helios/system-prompt.d/`, read in name order at startup. Helios no longer ships a built-in work-tracker policy.
- The cross-machine session pool defaults to `$XDG_DATA_HOME/helios/session-pool`; `HELIOS_SESSION_POOL` still overrides it, and the pool stays inert until the directory exists.
- The Shared pane's endpoint and key are read from `HELIOS_SCRATCHPAD_URL` and `HELIOS_SCRATCHPAD_KEY` (renamed; set these if you use the pane). Both are stripped from agent environments.
- `scripts/build-deb.sh` is now `scripts/build.sh`: it builds the `.deb` only and runs `lintian` when installed. Releases are built and published by the repository's Release workflow.
- Development happens in this repository: pull requests are reviewed and squash-merged here.
- CI runs on `ubuntu-24.04`, and the README footer links the whole suite.
