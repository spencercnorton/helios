# Helios for autonomous agents — install, configure, verify

This document is for an agent with shell access on a Linux machine that has to install and configure Helios for a person, unattended, and prove that it worked. Every step is a command followed by its expected result, and every path, key, default and message is taken from the source of this release (0.99.2), so a mismatch is a finding, not a typo. It cannot sign the person in to Claude or Codex: those are interactive logins in the CLIs' own credential stores, and the agent hands them back to the person at the points marked below.

## Facts at a glance

| Fact | Value |
|---|---|
| Package name | `helios` (APT package, Debian source package and Python distribution) |
| Installed binary (APT) | `/usr/bin/helios` (console script `helios = helios.__main__:main`) |
| Launcher (source checkout) | `<checkout>/scripts/helios` — prepends `<checkout>/src` to `PYTHONPATH` and runs `python3 -m helios "$@"` with whatever `python3` is first on `PATH`; no virtual environment, no interpreter check |
| Application id | `dev.norvi.Helios` (also the D-Bus well-known name and `StartupWMClass`) |
| Desktop file (APT) | `/usr/share/applications/dev.norvi.Helios.desktop`, `Exec=helios` |
| Desktop file (source, after `scripts/install-desktop.sh`) | `~/.local/share/applications/dev.norvi.Helios.desktop`, `Exec=<checkout>/scripts/helios` |
| Icons | `icons/hicolor/{16,24,32,48,64,128,256,512}x<N>/apps/dev.norvi.Helios.png` under `/usr/share` (APT) or `~/.local/share` (source) |
| State directory | `~/.helios`, created on first use; `logs/` and `work/` are 0700 from creation and the directory itself is chmod 0700 on the first state-file write (at the latest when the window closes and `ui-state.json` is saved). `HELIOS_STATE_DIR` (read on every call; empty means unset) relocates only part of it — next two rows |
| Files that honour `HELIOS_STATE_DIR` | `logs/helios.log`, `work/work.db`, `conversation-perms.json`, `session-providers.json`, `checkpoints.json`, `checkpoint-trash/`, `session-goals.json`, `openrouter-routes.json`, `openrouter-sessions/`, `openrouter-context/`, `AGENTS.md` (optional input), `estate-mcp.json` (optional input), `project-perms.json` (legacy) |
| Files fixed to `~/.helios` whatever `HELIOS_STATE_DIR` says | `ui-state.json`, `openrouter.key`, `openrouter-models.json`, `model-catalog.json`, `title-cache.json`, `project-names.json`, `session-archive/`, `backups/` |
| Full relocation | Override `HOME`; every path above goes through `Path.home()` |
| Log file | `<state dir>/logs/helios.log`, rotating at 1,000,000 bytes with 3 backups (`helios.log.1`..`3`), directory 0700; the same lines also go to stderr |
| Settings file | `~/.helios/ui-state.json` (flat JSON object, 0600, written atomically via `ui-state.json.tmp`) |
| Per-conversation file | `<state dir>/conversation-perms.json` |
| Minimum GTK / libadwaita / GtkSourceView | 4.10 / 1.5 / 5.0 — checked at startup through gi namespaces `Gtk/4.0`, `Adw/1`, `GtkSource/5`; exit code 1 with a message when too old |
| Minimum Python | 3.11 — enforced only by `pyproject.toml` (`requires-python = ">=3.11"`) when pip-installed; the `.deb` depends on an unversioned `python3:any` and nothing checks it at startup, so an older interpreter fails on import |
| Command-line options | None. Helios registers no options of its own; `helios --version` prints `Unknown option --version` and exits 1, and so does every other flag. Only GLib's own `-h`/`--help`, `--help-all`, `--help-gapplication` and `--gapplication-service` are accepted. A positional argument prints a `GLib-GIO-CRITICAL` line ending `This application can not open files.` and exits 1 |
| Reading the version | `dpkg-query -W helios`, or `python3 -c "import helios; print(helios.__version__)"` (with `PYTHONPATH=~/helios/src` on a checkout) |
| Single instance | `Adw.Application` with `DEFAULT_FLAGS`: on a session bus a second `helios` activates and raises the running window instead of opening a second one |
| CLIs | Not packaged and not depended on. Claude sessions need the `claude` CLI (install: 5.1); GPT sessions need the `codex` CLI (optional); OpenRouter needs a key file (optional) |
| `.deb` Depends | `gir1.2-adw-1 gir1.2-gtk-4.0 gir1.2-gtksource-5 libgtksourceview-5-0 python3-gi python3-gi-cairo` plus the Python dependency; Recommends `git` (needed for Rewind) |

## 1. Choose the install path

| Condition | Path |
|---|---|
| Ubuntu 26.04 on `amd64` | Procedure A — APT repository `apt.globalentry.systems`, package `helios` (`Architecture: all`; only the `resolute` suite is published) |
| Anything else (Ubuntu 24.04, other distributions, other architectures) | Procedure B — run from a source checkout |

```bash
. /etc/os-release && echo "$ID $VERSION_ID $(dpkg --print-architecture 2>/dev/null || uname -m)"
```

Expected: `ubuntu 26.04 amd64` selects Procedure A; any other output (for example `ubuntu 24.04 amd64`, `ubuntu 26.04 arm64`, or `fedora <version> x86_64` on a system without `dpkg`) selects Procedure B.

## 2. Procedure A — APT (Ubuntu 26.04 amd64)

Step A1. Add the repository. `setup.sh` installs the signing key and the suite for the release; it is short, read it first if policy requires. As published, it needs root and `curl` (both checked up front) and `gpg` to verify the key (without `gpg` it stops at `error: signing key fingerprint mismatch`), takes the suite from `VERSION_CODENAME` in `/etc/os-release` and the architecture from `dpkg --print-architecture`, refuses anything but `resolute` on `amd64` before changing the machine, verifies the key fingerprint it was published with, writes `/etc/apt/keyrings/norvi-archive-keyring.asc` and `/etc/apt/sources.list.d/norvi.sources`, and runs `apt-get update` itself.

```bash
curl -fsSL https://apt.globalentry.systems/setup.sh | sudo sh
apt-cache policy helios
```

Expected: the `apt-get update` output, then the two lines `Norvi repository added. See what is available with:` and `  apt list '?origin(Norvi)'`, exit code 0; then a `Candidate:` line with a version and a version-table entry from `apt.globalentry.systems`. On an unsupported release the script prints `error: this repository has no packages for '<codename>'.` and on another architecture `error: this repository only has amd64 packages, and this machine is <arch>.`, exits 1 and changes nothing; use Procedure B.

Step A2. Install.

```bash
sudo apt install -y helios
dpkg -s helios | grep -E '^(Status|Version):'
```

Expected:

```
Status: install ok installed
Version: 0.99.2
```

`apt` pulls in `python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-gtksource-5 libgtksourceview-5-0` and, because Recommends are installed by default, `git`.

Step A3. Confirm the files the package placed.

```bash
test -x /usr/bin/helios && test -f /usr/share/applications/dev.norvi.Helios.desktop && echo FILES_OK
command -v git >/dev/null && echo GIT_OK
```

Expected: `FILES_OK` and `GIT_OK`. `git` is only a Recommends of the package (`--no-install-recommends` leaves it out); without it Rewind is silently unavailable, so install it if `GIT_OK` is missing.

Step A4. Run the runtime check in section 4.

## 3. Procedure B — source checkout

Step B1. Install the system packages. PyGObject and GTK must come from the distribution and must not be pip-installed; `pyproject.toml` declares no pip runtime dependencies on purpose. A `pip install -e .` of Helios itself is allowed but unnecessary: the launcher only needs `src` on `PYTHONPATH`. `git` is for the clone and for Rewind; `python3-pil` is required only by `scripts/install-desktop.sh`, which imports `PIL` to render the icon sizes.

```bash
sudo apt install -y python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-gtksource-5 libgtksourceview-5-0 git python3-pil
```

Expected: exit code 0. On a non-Debian distribution the package names differ and this document does not list them; what the installed set must provide is fixed by the source and is what section 4 checks: the `gi` Python module (PyGObject) for the `python3` that `scripts/helios` will run, the typelibs for the namespaces `Gtk` 4.0, `Adw` 1 and `GtkSource` 5 (`src/helios/deps.py`), PyGObject's cairo integration (the `.deb` names it `python3-gi-cairo`; the context meter is a `Gtk.DrawingArea` draw function), `git`, and `PIL` for `install-desktop.sh`. Install, then run section 4; each `ValueError: Namespace ... not available` names the piece still missing.

Step B2. Check the interpreter. Python older than 3.11 fails at import time (for example on `from enum import StrEnum`), not with a startup message.

```bash
python3 -c 'import sys; assert sys.version_info >= (3, 11), sys.version; print(sys.version.split()[0])'
```

Expected: a version of `3.11` or newer.

Step B3. Clone and read the version.

```bash
git clone --branch v0.99.2 https://github.com/spencercnorton/helios.git ~/helios
PYTHONPATH=~/helios/src python3 -c "import helios; print(helios.__version__)"
```

Expected: the clone prints `Note: switching to '<sha>'.` and a `You are in 'detached HEAD' state` paragraph (normal for a tag checkout), then `0.99.2`. Drop `--branch v0.99.2` to track the newest release instead; the version printed is then whatever `main` carries, and the `0.99.2` strings in this document, 7.3 and 12 are the release it was written for, not a mismatch.

Step B4. Optional: app-grid entry and icon.

```bash
~/helios/scripts/install-desktop.sh
grep '^Exec=' ~/.local/share/applications/dev.norvi.Helios.desktop
```

Expected output:

```
Installed Helios:
  Desktop entry: /home/<user>/.local/share/applications/dev.norvi.Helios.desktop
  Icons:         /home/<user>/.local/share/icons/hicolor/{16,24,32,48,64,128,256,512}x.../apps/dev.norvi.Helios.png
  Launcher:      /home/<user>/helios/scripts/helios

Helios should now appear in your app grid. Log out and back in if it doesn't.
Exec=/home/<user>/helios/scripts/helios
```

What the script writes:

| Written | Content |
|---|---|
| `~/.local/share/applications/dev.norvi.Helios.desktop` | `data/dev.norvi.Helios.desktop.in` with `@LAUNCHER@` replaced by the absolute `<checkout>/scripts/helios`, marked executable; `Terminal=false`, `StartupWMClass=dev.norvi.Helios`, `Categories=Development;IDE;`, no `%U`/`%F` (so no file-open) |
| `~/.local/share/icons/hicolor/<S>x<S>/apps/dev.norvi.Helios.png` for S in 16, 24, 32, 48, 64, 128, 256, 512 | Rendered from `data/icons/dev.norvi.Helios.png` with Pillow (LANCZOS), PNG only |
| Removed | any stale `~/.local/share/icons/hicolor/scalable/apps/dev.norvi.Helios.svg` |

It then runs `gtk-update-icon-cache -f -t` and `update-desktop-database` when they exist, ignoring failures. Without Pillow it fails with `ModuleNotFoundError: No module named 'PIL'`. Re-running the script overwrites the desktop file.

Step B5. Run the runtime check in section 4.

## 4. Runtime check (both procedures)

Read the installed library versions through gi, exactly as Helios does at startup. No display is needed.

```bash
python3 - <<'PY'
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); gi.require_version("GtkSource", "5")
from gi.repository import Gtk, Adw, GtkSource
print("GTK", Gtk.get_major_version(), Gtk.get_minor_version())
print("libadwaita", Adw.get_major_version(), Adw.get_minor_version())
print("GtkSourceView", GtkSource.get_major_version(), GtkSource.get_minor_version())
gi.require_foreign("cairo"); print("cairo ok")
PY
```

Expected: `GTK 4 <minor>` with minor at least 10, `libadwaita 1 <minor>` with minor at least 5, `GtkSourceView 5 <minor>`, then `cairo ok`. A `ValueError: Namespace ... not available` means the matching `gir1.2-*` package is missing. An `ImportError` on the last line means PyGObject's cairo integration is missing (`python3-gi-cairo` on Debian and Ubuntu, a hard dependency of the `.deb`); Helios's own checker below does not test it; the `.deb` depends on it because the toolbar's context meter (`src/helios/widgets/chat_toolbar.py`) is a `Gtk.DrawingArea` draw function, which is handed a cairo context.

Then run Helios's own checker. On a checkout prefix the command with `PYTHONPATH=~/helios/src`. Importing `helios.deps` configures logging, so `<state dir>/logs/` is created by this call.

```bash
python3 -c 'from helios.deps import check_runtime_versions as c; print(c())'
```

Expected: `[]`. A non-empty list is the exact bullet text Helios would refuse to start with, for example `['GTK 4.10+ is required, but 4.8 is installed.']`.

If a library is too old, `helios` logs `incompatible runtime: ...` at CRITICAL, prints this to stderr with one bullet per problem, attempts a GTK-only alert dialog titled `Helios can't start` (nothing on a headless machine), and exits with status 1:

```
Helios can't start because some system libraries are too old:

  • GTK 4.10+ is required, but 4.8 is installed.

Please upgrade these packages (on Ubuntu/Debian: gir1.2-gtk-4.0, gir1.2-adw-1, gir1.2-gtksource-5) and try again.
```

A missing namespace reads `libadwaita 1.5+ is required but is not available (<gi error>).`

## 5. Providers

### 5.1 Claude (required for Claude sessions)

How Helios finds `claude`. It never runs a bare `claude` through a shell. It resolves the binary in this order and takes the first that is a regular executable file (symlinks followed):

1. `$HELIOS_CLAUDE_BINARY` (`~` expanded). A value that is not runnable is skipped silently and the search continues.
2. `claude` on `PATH` (`shutil.which`, resolved through symlinks).
3. `~/.local/bin/claude`.
4. The newest `~/.local/share/claude/versions/*` by version number (the native installer's layout).
5. The newest `~/.vscode*/extensions/anthropic.claude-code-*-linux-x64/resources/native-binary/claude`.
6. `~/.local/share/JetBrains/*/claude-code/*linux-x64/resources/native-binary/claude`.

Otherwise `ClaudeBinaryNotFound`: `Could not find a working `claude` binary. Set $HELIOS_CLAUDE_BINARY or fix your `claude` install.` Settings shows `Not found — set $HELIOS_CLAUDE_BINARY` in the Claude binary row. The window still opens without a binary; the message appears as a toast on the first send.

Installing the CLI. Helios neither packages nor installs `claude`; on a fresh machine nothing below finds it. The commands here are Anthropic's, not Helios's — confirm them against the current Claude Code install page if they fail. Native installer (Anthropic's recommended path): run it as the person, not with `sudo` (the installer refuses `sudo`). It installs under `$HOME` with the launcher at `~/.local/bin/claude`, a symlink into `~/.local/share/claude/versions/` — search steps 3 and 4 above, so a `.desktop` launch finds it with no PATH work — and it auto-updates:

```bash
command -v claude >/dev/null || curl -fsSL https://claude.ai/install.sh | bash
"$(command -v claude || echo ~/.local/bin/claude)" --version   # an existing npm install has no ~/.local/bin/claude
```

Expected: `<version> (Claude Code)`, 2.1.217 or later (the floor below). An npm install (`npm install -g @anthropic-ai/claude-code`) lands wherever npm's prefix is: a system prefix (`/usr/local/bin`) is on the desktop PATH and step 2 finds it; a Node version manager's prefix is not, and needs the symlink fix below.

```bash
command -v claude; ls -l ~/.local/bin/claude 2>/dev/null; ls ~/.local/share/claude/versions/ 2>/dev/null
```

Expected: at least one line names an executable. If only the first does and the path is outside `~/.local/bin` (for example under a Node version manager), read the next paragraph.

The desktop-launch PATH trap. A `.desktop` launch (`Exec=helios` or `Exec=<checkout>/scripts/helios`) runs without a login shell: `~/.bashrc` and `~/.profile` are not read, so a `claude` that is only on the terminal's `PATH` is invisible, and `HELIOS_*` variables exported in shell rc files are not seen. Steps 3 and 4 above exist for this reason. Check what a desktop launch will find by resolving with a minimal environment (on a source install `PYTHONPATH` must be given as an argument to `env`, after `-i`; a prefix before `env -i` is wiped):

```bash
env -i HOME="$HOME" PATH=/usr/local/bin:/usr/bin:/bin python3 -c "from helios.backend.claude_binary import find_claude_binary; print(find_claude_binary())"                                   # APT
env -i HOME="$HOME" PATH=/usr/local/bin:/usr/bin:/bin PYTHONPATH="$HOME/helios/src" python3 -c "from helios.backend.claude_binary import find_claude_binary; print(find_claude_binary())"   # source checkout
```

Expected: `<path> (via local-bin)` or another of `env`, `PATH`, `standalone`, `vscode-ext`, `jetbrains-plugin`. (`"$HOME/helios/src"` rather than `~/helios/src`: tilde expansion inside a `VAR=~/x` argument to `env` is a bash extension that POSIX `sh`/`dash` do not do.) A traceback ending in `ClaudeBinaryNotFound` means a desktop launch will not find it. Two fixes:

Simplest: make step 3 succeed. No environment variable is needed.

```bash
mkdir -p ~/.local/bin && { [ ~/.local/bin/claude -ef "$(command -v claude)" ] || ln -sfn "$(command -v claude)" ~/.local/bin/claude; } && ls -l ~/.local/bin/claude && ~/.local/bin/claude --version
```

Expected: a symlink to the real binary, then a version line. If `~/.local/bin/claude` already is that binary (the native installer's layout) nothing is rewritten; without the `-ef` guard, `ln -f` would replace it with a link to itself.

Or set `HELIOS_CLAUDE_BINARY` on the `Exec=` line. Do not export it in rc files. For the APT install, copy the system entry to the user directory (the user copy takes precedence by basename) and change `Exec`:

```bash
mkdir -p ~/.local/share/applications
sed 's|^Exec=helios$|Exec=env HELIOS_CLAUDE_BINARY=/absolute/path/to/claude helios|' \
  /usr/share/applications/dev.norvi.Helios.desktop > ~/.local/share/applications/dev.norvi.Helios.desktop
grep '^Exec=' ~/.local/share/applications/dev.norvi.Helios.desktop
```

Expected: `Exec=env HELIOS_CLAUDE_BINARY=/absolute/path/to/claude helios`. For a source install edit the file `install-desktop.sh` wrote, keeping the launcher path:

```bash
sed -i 's|^Exec=\(.*\)$|Exec=env HELIOS_CLAUDE_BINARY=/absolute/path/to/claude \1|' ~/.local/share/applications/dev.norvi.Helios.desktop
grep '^Exec=' ~/.local/share/applications/dev.norvi.Helios.desktop
```

Expected: `Exec=env HELIOS_CLAUDE_BINARY=/absolute/path/to/claude /home/<user>/helios/scripts/helios`. For a terminal launch, `HELIOS_CLAUDE_BINARY=/path/to/claude helios` works directly. Helios strips `HELIOS_CLAUDE_BINARY` from every child process, so it never reaches the CLI.

Version floor. When a dollar cap applies (see billing below) every interactive Claude session is started with `--max-budget-usd 10` and Helios requires Claude Code 2.1.217 or newer whose `claude --help` lists `--max-budget-usd`. Otherwise the spawn fails with `DriverSpawnError` and the toast `Claude's required process-family budget breaker is unavailable. Helios requires Claude Code 2.1.217+ with --max-budget-usd; update Claude Code or retry after checking the configured binary.` On subscription billing no cap is passed and the floor does not apply.

```bash
claude --version
claude --help 2>&1 | grep -c -- '--max-budget-usd'
```

Expected: `2.1.217` or later, and `1` (or more). Helios also probes `--effort` and `--forward-subagent-text`; each missing flag degrades one feature, logged at startup as `claude CLI <version>; degraded: ...` and toasted once per run as `Claude CLI <version> cannot do: ...`.

How billing mode is detected, per session start: `ANTHROPIC_API_KEY` set in Helios's own environment means per-token (cap on). Otherwise Helios runs `claude auth status --json` (10 s timeout) and reads `loggedIn`, `email`, `authMethod`, `apiProvider`, `orgName`, `subscriptionType`: subscription billing means `authMethod` is `claude.ai` or `claudeai` and `subscriptionType` is non-empty (cap off, no `--max-budget-usd` flag at all). Anything else, including an unreadable status, fails closed to per-token billing, so the cap and the version floor apply. A login done while Helios is open is picked up on the next chat.

Hand back to the person. Sign-in is interactive (browser or device code); Helios's own Settings button only opens a terminal emulator running `claude auth login --claudeai` and cannot run headless. Give the person one of the three flags Helios itself knows (`LOGIN_METHODS` in `src/helios/backend/claude_env.py`; `--claudeai` is its default):

```bash
claude auth login --claudeai   # claude.ai subscription (no dollar cap, no version floor)
claude auth login --console    # Anthropic Console, per-token billing (cap and floor apply)
claude auth login --sso        # force the SSO flow
```

Afterwards verify:

```bash
claude auth status --json
```

Expected: JSON containing `"loggedIn": true`.

### 5.2 Codex / GPT (optional)

Resolution: `$HELIOS_CODEX_BINARY`, then `codex` on `PATH`, then `~/.local/bin/codex`. Not found: `OpenAI Codex CLI not found. Install it (`npm install -g --prefix ~/.local @openai/codex`) or set $HELIOS_CODEX_BINARY.` A missing `codex` is silent at startup: the GPT header toggle is simply disabled with the tooltip `Add an OpenAI key in Settings → Providers to enable GPT`.

Sign-in truth is `codex login status` (15 s timeout, scrubbed environment): signed in means exit 0 and output containing `logged in` but not `not logged in`. Helios never reads `~/.codex/auth.json`.

```bash
codex login status; echo "exit=$?"
```

Expected when signed in: output containing `logged in` and `exit=0`. If not, either hand back `codex login` (ChatGPT account, interactive; there is no ChatGPT sign-in button in Settings, whose row reads `Not signed in` / `Paste an API key below to enable OpenAI models.`) or use the API-key path: Settings → Providers → "Use or replace an OpenAI API key" pipes the key to `codex login --with-api-key` on stdin (never argv, 30 s timeout) and keeps no copy. The same command from a shell has the same effect. It is refused with `Close active GPT sessions first` while a GPT session is live.

`OPENAI_API_KEY` is never read by Helios and never reaches any child process (its name matches the `_KEY` scrub marker), so exporting it does nothing. `CODEX_API_KEY` in Helios's environment is not forwarded to App Server sessions either; its only effect is to force per-token billing detection (token cap on).

GPT models become selectable only after `codex app-server` answers `model/list` (Helios sends `initialize` → `initialized` → `model/list {limit: 100, includeHidden: false}` → optional `collaborationMode/list`, 15 s deadline; catalog status `app-server`). A `chatgpt-fallback` catalog is informational and not selectable. The catalog is never cached on disk; it is re-queried at startup and every 6 hours. A send while not signed in fails with `Codex isn't signed in — add your OpenAI API key in Settings → Providers.` `HELIOS_CODEX_TRANSPORT=exec` makes every GPT session fail to start with `HELIOS_CODEX_TRANSPORT=exec is disabled: ...`; leave it unset.

### 5.3 OpenRouter (optional)

There is no environment variable for the OpenRouter key. The desktop key lives at `~/.helios/openrouter.key`: one line, at least 16 characters, no space, tab, CR, LF or NUL, mode 0600, written by Settings → Providers. Pre-seeding it while Helios is closed is supported and equivalent to saving it in Settings:

```bash
(umask 077; mkdir -p ~/.helios && chmod 700 ~/.helios && printf '%s\n' '<key>' > ~/.helios/openrouter.key)
stat -c '%a %n' ~/.helios/openrouter.key
```

Expected: `600 /home/<user>/.helios/openrouter.key`. Validate with the production rule (add `PYTHONPATH=~/helios/src` on a source install):

```bash
python3 -c 'from helios.backend.openrouter.key import load_key, validate_key; validate_key(load_key()); print("key-ok")'
```

Expected: `key-ok`. `KeyValidationError: not a usable OpenRouter API key` means the file is empty, shorter than 16 characters, or contains whitespace. Settings shows `Invalid key` / `The key was too short or malformed.` for the same input, and `Key saved` / `OpenRouter chat is enabled.` on success.

Without the file the provider status is `no-key`, the OpenRouter toggle is unselectable and no OpenRouter request is ever made. With a pre-seeded key and no cache the picker offers three built-in rows (`google/gemini-2.5-pro`, `moonshotai/kimi-k2`, `deepseek/deepseek-chat`, status `fallback`) until Settings is opened once: opening Settings with a key present, or saving one, fetches `https://openrouter.ai/api/v1/models` and caches it at `~/.helios/openrouter-models.json`. Model ids whose vendor prefix is `anthropic` or `openai` are never offered here; use the native providers.

The same fetch can be done from a shell, without the GUI, through the function Settings calls (`openrouter_entries(force=True)` in `src/helios/backend/model_catalog.py`; needs network; add `PYTHONPATH=~/helios/src` on a source install):

```bash
python3 -c 'from helios.backend import model_catalog as m; e, s = m.openrouter_entries(force=True); print(s, len(e))'
ls -l ~/.helios/openrouter-models.json
```

Expected: `fetched <N>` (the number of selectable models), a log line `OpenRouter catalog refreshed: <N> models`, and the cache file. `no-key 0` means the key file is missing or empty; `fallback 3` after a warning `OpenRouter catalog fetch failed (...); using cache` means the fetch failed and there was no earlier cache. The picker reads the cache at the next launch.

## 6. Configure without the GUI

### 6.1 `~/.helios/ui-state.json`

Two rules:

1. Edit only while Helios is not running. The file is read once when the process starts; every later change rewrites the whole file from memory, so a live edit is overwritten by the next change.
2. Partial files are fine. Unknown keys are ignored, a missing key returns the caller's default, and a missing, unreadable, non-JSON or non-object file loads as `{}`.

The file is written atomically (`ui-state.json.tmp` then rename) with `indent=2, sort_keys=True`, file 0600, directory 0700. Keys an agent may set:

| Key | Type | Default | Valid values | Meaning |
|---|---|---|---|---|
| `default_cwd` | string | unset (`""`) | absolute path of an existing directory | Working directory for new chats. A value that is not an existing directory is ignored with a log warning and the heuristic in 6.3 applies |
| `permission_mode` | string | `"default"` | `default`, `acceptEdits`, `auto`, `bypassPermissions`, `plan`, `dontAsk` | Global default permission mode. Unknown strings are coerced to `default` and re-persisted at startup |
| `permission_mode_confirmed` | boolean | `false` | `true`, `false` | Written `true` by Settings → "Save chat defaults". `bypassPermissions` is honoured at startup only when this is `true`; otherwise it is clamped to `default`. Leave it unset. For every mode except `bypassPermissions` it only stops a retired `project-perms.json` entry (6.2) from narrowing the default, so a fresh install does not need it; for `bypassPermissions` it is the person's confirmation (`resolve_startup_default` in `src/helios/backend/project_perms.py`), so never write it from a file. If the person wants Bypass as their global default, they choose it in Settings → "Save chat defaults" themselves |
| `model` | string | `"fable[1m]"` | a Claude alias or id, an OpenAI id, or an OpenRouter `vendor/model` id | Sticky model for the next new chat. An empty or absent value resolves to `fable[1m]` at startup, so `""` cannot be pre-set from the file; the picker's `Default (from Claude settings)` entry (empty id, `--model` omitted) is a runtime choice that reverts to `fable[1m]` on the next launch. Classification: an id containing `/` is OpenRouter; an id starting with `gpt-<digit>`, `o<digit>`, `codex` or `chatgpt` is OpenAI; anything else is Anthropic |
| `model_anthropic` | string | unset | as `model` | Last model used on the Claude side of the header toggle |
| `model_openai` | string | unset | as `model` | Last model used on the GPT side |
| `model_openrouter` | string | unset | as `model` | Last model used on the OpenRouter side |
| `effort_level` | string | `"high"` | `off`, `low`, `medium`, `high`, `xhigh`, `max` | Global reasoning effort. A legacy integer key `effort` (0/4000/8000/16000/32000) is migrated on read; the retired value `ultracode` reads as `max` |
| `effort_anthropic` | string | falls back to `effort_level` | as `effort_level` | Per-provider effort memory for Claude |
| `effort_openrouter` | string | `""` | `off`, `low`, `medium`, `high` | Per-provider effort memory for OpenRouter |
| `effort_openai` | — | — | — | Written by the app but read back as `""` on purpose: OpenAI effort is conversation-owned and never a global default. Do not set it |
| `color_scheme` | string | `"auto"` | `auto`, `light`, `dark` | Theme (Settings → Appearance → Theme). Anything else reads as `auto` |
| `glass_sidebar` | boolean | `false` | JSON `true`/`false`, or the strings `"true"`/`"false"` | Translucent sessions sidebar. Any other truthy value does not enable it |
| `title_backend` | string | `"ollama"` | `ollama`, `claude` | Session-title generator. Only the exact value `claude` opts into metered Claude haiku titles (`--max-budget-usd 0.05` per call); anything else means Ollama |
| `ollama_url` | string | `"http://localhost:11434"` | `http://` or `https://` URL with a host, no credentials, query or fragment | Ollama server for titles |
| `ollama_title_model` | string | `"qwen2.5-coder:14b"` | non-empty tag, at most 256 characters, no whitespace | Ollama model for titles |
| `notify_on_finish` | boolean | `true` | `true`, `false` | Desktop notification when a background session finishes or asks a question |
| `notify_on_mission_gate` | boolean | `true` | `true`, `false` | Notification when a mission in the optional Missions pane is gated |
| `show_pool_sessions` | boolean | `false` | `true`, `false` | Show sessions from an optional shared session pool (`HELIOS_SESSION_POOL`); inert without one |
| `openrouter_picker_models` | list of strings | `[]` | OpenRouter model ids | First tier of the OpenRouter model picker. Empty or absent shows the whole catalog |
| `window_width` | integer | `1400` | pixels | Window width, rewritten on close |
| `window_height` | integer | `900` | pixels | Window height, rewritten on close |
| `sessions_paned` | integer | `320` | pixels | Sessions sidebar splitter position |
| `panel_sessions` | boolean | `true` | `true`, `false` | Sessions column shown |
| `panel_context` | boolean | `false` | `true`, `false` | Right workspace pane shown |
| `session_filter` | string | `"all"` | `all`, `local`, `temp` | Sidebar filter |

Settings defers `permission_mode` and `model` to the "Save chat defaults" button, and `ollama_url` / `ollama_title_model` to Behavior → "Save and check" (editing the two rows only changes the status text to `Settings changed. Save and check to use this server and model.`); every other Settings control writes its key immediately.

Merge keys atomically with the same permissions Helios uses (fails loudly on a malformed existing file, which is what you want). The example creates the project directory first (why: 6.3) and splices `$HOME` from the shell so the JSON value is absolute; it sets nothing about permissions:

```bash
mkdir -p ~/project && git -C ~/project init -q
python3 -c 'import json,os,pathlib,sys; p=pathlib.Path.home()/".helios"/"ui-state.json"; p.parent.mkdir(parents=True,exist_ok=True); os.chmod(p.parent,0o700); d=json.loads(p.read_text()) if p.is_file() else {}; d=d if isinstance(d,dict) else {}; d.update(json.loads(sys.argv[1])); t=p.with_suffix(".json.tmp"); t.write_text(json.dumps(d,indent=2,sort_keys=True)); os.chmod(t,0o600); t.replace(p); print(json.dumps(d,sort_keys=True))' \
  '{"default_cwd": "'"$HOME"'/project"}'
stat -c '%a' ~/.helios/ui-state.json
```

Expected: `{"default_cwd": "/home/<user>/project"}` merged with whatever was there, printed on one line, then `600`. Add other keys from the table only when the person asked for them.

### 6.2 `<state dir>/conversation-perms.json`

Per-conversation overrides, nested by provider (`anthropic`, `openai`, `openrouter`) then by the provider's native session id. Helios writes this file itself when a chat's execution settings are changed; an agent normally has no reason to pre-write it. Same edit-while-closed rule as `ui-state.json`. The shape, for reference:

```json
{
  "anthropic": {
    "<claude session uuid>": {"permission_mode": "plan", "effort_key": "high", "workflow_mode": "default"}
  },
  "openai": {
    "<codex thread id>": {"permission_mode": "auto"}
  }
}
```

`permission_mode` is required; `effort_key` and `workflow_mode` are optional (`effort_key` is omitted on write when empty, `workflow_mode` when it equals `default`). Unknown fields are ignored. A stored `manual` mode reads as `default`; any other unknown mode makes the record invalid (no override).

`<state dir>/project-perms.json` is a retired read-only legacy file. Do not create it to configure permissions: a `bypassPermissions` entry in it is clamped to `default`; an entry with an unknown mode forces `plan` for that directory, and a malformed file (not JSON, not a JSON object, or containing an empty key) forces `plan` for every directory.

### 6.3 The first-chat trap

New-chat working directory at startup: `default_cwd` if it is an existing directory; otherwise the most recently modified local (non-pool) project Helios already knows about whose directory still exists and is not `$HOME`, preferring one that is not a throwaway (`/tmp`, under `/tmp/`, or any `_tmp_*` path component) and using a throwaway only when nothing else qualifies; otherwise `$HOME`. On a fresh install there are no projects, so the first chat opens in `$HOME`, and a chat whose working directory is `$HOME` is forced read-only (`plan`) for every provider: the composer toasts `This chat is in $HOME, so permissions are read only. It can inspect and answer, but pick a project folder to change files.` and any other permission choice is refused with `HOME chats use read-only permissions. Select a project folder before granting file changes.` Only `$HOME` itself is locked, not its subdirectories.

Set `default_cwd` to a project directory before the first launch. It must exist. A git repository is recommended: Rewind takes a checkpoint before each message only when the folder is inside a git worktree. A repository with no commits yet is enough (`snapshot()` in `src/helios/backend/checkpoints.py` treats an unborn `HEAD` as fine and skips `read-tree`). If the person has no project yet, the 6.1 example already made one (`~/project`, an empty repository) and merged it as `default_cwd`; substitute the person's own project directory there when they have one. Verify:

```bash
git -C ~/project rev-parse --show-toplevel
```

Expected: `/home/<user>/project` (the repository root; git resolves symlinks, so on a host where `/home` is a symlink it prints the resolved form). `default_cwd` must be an absolute path: `configured_default_cwd()` in `src/helios/backend/ui_state.py` tests `Path(configured).is_dir()` on the raw string, so `~/project` is ignored with the log line `default cwd ~/project is not a directory; ignoring` and the startup heuristic above applies (on a fresh profile with no prior projects that is `$HOME`, read-only). Helios creates `~/.claude/projects/-home-<user>-project/` for that directory at startup: the directory name is the path with every `/` replaced by `-` and nothing else changed (`encode_project_dirname` in `src/helios/backend/projects.py`); neither that check nor `ensure_local_project()` resolves symlinks, so the directory name mirrors the path exactly as written, and the directory is made with parents, so `~/.claude` need not exist before the first launch. A missing `~/.claude/projects` on a machine where the `claude` CLI has never run is also fine: discovery treats it as no sessions.

## 7. Launch and verify

### 7.1 From a desktop session

Run this from a shell inside the person's graphical session, where `DISPLAY` or `WAYLAND_DISPLAY` is set and the session D-Bus is reachable; a bare SSH shell has no display variable, so the window cannot open and the `gdbus --session` check below has no bus to ask. On a headless machine, or from SSH, use 7.2 instead.

```bash
env | grep -E '^(DISPLAY|WAYLAND_DISPLAY|DBUS_SESSION_BUS_ADDRESS)='
setsid -f helios >/dev/null 2>&1        # APT; on a checkout: setsid -f ~/helios/scripts/helios
sleep 8
gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus --method org.freedesktop.DBus.ListNames | grep -o 'dev.norvi.Helios'
```

Expected: a `DISPLAY=` or `WAYLAND_DISPLAY=` line (otherwise stop and use 7.2), then `dev.norvi.Helios`, and a window titled `Helios` (`set_title("Helios")` in `src/helios/main_window.py`). `busctl --user list --no-legend | grep dev.norvi.Helios` is the systemd equivalent. With no sessions the sidebar reads `No sessions yet — start a new chat.` and, because the disk is empty, Helios opens the fresh-chat view straight away: the composer is already visible, staged in the working directory chosen by the rule in 6.3 (`default_cwd` when set; `$HOME` on a fresh install, with its read-only toast). The welcome page titled `Helios` (description `All your Helios sessions - this machine and the shared pool.` followed by `Pick a session on the left, or press Ctrl+N for a new chat.`) appears only when sessions exist and none is selected. A second `helios` invocation activates the existing window and returns; it does not open a second one.

Quit cleanly from a shell with GLib's `gapplication` tool (package `libglib2.0-bin`, which also provides `gdbus`); it activates the app's `quit` action, the target of Ctrl+Q:

```bash
gapplication action dev.norvi.Helios quit
```

Expected: no output, and the D-Bus name disappears.

### 7.2 Headless smoke test under Xvfb

This proves the install starts a window on a machine with no desktop. Use an isolated `HOME` so nothing touches the person's `~/.helios` or `~/.claude`; `GDK_BACKEND=x11` because there is no Wayland compositor; `GSK_RENDERER=cairo` because with the default GL renderer under Xvfb the frame clock saturates and the window never finishes loading. Set `HELIOS_CMD` before overriding `HOME` so a checkout path resolves.

```bash
sudo apt install -y xvfb xauth dbus libglib2.0-bin xdotool   # Debian/Ubuntu names; elsewhere: Xvfb (xvfb-run), xauth, dbus-run-session, gdbus, gapplication, xdotool
HELIOS_CMD=/usr/bin/helios            # or /home/<user>/helios/scripts/helios
(
export HOME="$(mktemp -d)" GDK_BACKEND=x11 GSK_RENDERER=cairo
xvfb-run -a python3 -c 'import gi; gi.require_version("Gtk","4.0"); from gi.repository import Gtk; assert Gtk.init_check()' && echo DISPLAY_OK
xvfb-run -a dbus-run-session -- bash -c '
  "$1" >"$HOME/helios.out" 2>&1 &
  app=$!
  sleep 12
  gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus --method org.freedesktop.DBus.ListNames | grep -o "dev.norvi.Helios"
  xdotool search --name "^Helios$" | head -n 1
  gapplication action dev.norvi.Helios quit || kill "$app"
  for i in $(seq 10); do kill -0 "$app" 2>/dev/null || break; sleep 1; done
  kill "$app" 2>/dev/null
  wait "$app"; echo "exit=$?"
' _ "$HELIOS_CMD"
head -n 3 "$HOME/helios.out"; ls "$HOME/.helios/logs"
)
```

Expected: `DISPLAY_OK`, then `dev.norvi.Helios`, a numeric X window id, `exit=0`, then the first log lines and `helios.log`. `exit=143` means Helios was still running 10 s after the quit call (the name was not on the bus yet, so `gapplication` exited 1, or the action was accepted but the process did not exit) and was sent SIGTERM; the `dev.norvi.Helios` and window-id lines above still say whether it started. The script always terminates, so a shell tool never times out on it. The parentheses keep the temporary `HOME`, `GDK_BACKEND` and `GSK_RENDERER` out of the rest of your shell: section 12 line 5 searches `$HOME/.local/bin` and `$HOME/.local/share/claude/versions` with `~/.local/bin` stripped from `PATH`, and line 8 reads `$HOME/.helios/ui-state.json`, so both must run against the person's real `HOME`; sections 5.3 and 6.1 write there too. The isolated `HOME` hides `~/.local/bin` and the other home-relative search steps, so `claude` is found only through `PATH`; that is fine for a smoke test because the window opens without it.

Log lines on stderr have the form `HH:MM:SS LEVEL   helios.<module>: message`; the file adds the date (`YYYY-MM-DD HH:MM:SS ...`). Among the first INFO lines from `helios.window` in a fresh HOME:

```
10:00:00 INFO    helios.window: claude CLI <version>; degraded: nothing
10:00:00 INFO    helios.window: model catalog: <N> entries (openai: not-logged-in)
10:00:00 INFO    helios.window: model catalog: <N> entries (openrouter: no-key)
```

With no `claude` binary visible the first line reads `claude CLI unknown version; degraded: the reasoning-effort slider (no --effort), Claude's own spend cap (Helios stops the turn instead) (no --max-budget-usd), live subagent text in the Agent Dock (no --forward-subagent-text)`; expected in the smoke test, not on the real desktop. Two WARNING lines are benign anywhere: `helios.env-scrub: could not read the systemd user environment: ...` (a host without `systemctl`; an optional integration, inert unless configured) and, on an APT install, `Helios Router MCP launcher is missing: /usr/lib/python3/scripts/helios-router-mcp` at each Claude session start (an optional broker, inert unless configured). `HELIOS_DEBUG=1` switches to DEBUG including the Claude driver's spawn argv and every wire record.

On too-old libraries the process instead prints the section 4 message to stderr, logs it at CRITICAL and exits 1 without a window; `helios.log` still exists because logging is set up first.

### 7.3 Reading the version

There is no `--version`. Use one of:

```bash
dpkg-query -W helios                                                    # APT
python3 -c "import helios; print(helios.__version__)"                   # APT (module on the system path)
PYTHONPATH=~/helios/src python3 -c "import helios; print(helios.__version__)"    # source checkout
```

Expected: `helios	0.99.2` from `dpkg-query`, `0.99.2` from the other two. `man helios` is also installed by the package.

## 8. Reference

Sections 8–11 are reference and recovery; the procedure continues at section 12.

### 8.1 Permission modes

Key is what `ui-state.json` and `conversation-perms.json` store; the label is what the Settings combo and the Execution capsule show. All six modes are selectable for all three providers.

| Key | UI label | Claude `--permission-mode` | Codex `approvalPolicy` / sandbox / network | OpenRouter tool loop |
|---|---|---|---|---|
| `default` | Ask | `default` | `on-request` / `workspace-write` / off | In-workspace `Read`, `Grep`, `Glob` run; everything else asks |
| `acceptEdits` | Accept edits | `acceptEdits` | `on-request` / `workspace-write` / on | In-workspace reads and `Write`/`Edit` run; everything else asks |
| `auto` | Auto | `auto` | `on-request` / `workspace-write` / on | As `acceptEdits` |
| `bypassPermissions` | Bypass | `bypassPermissions` | `never` / `danger-full-access` / on | Every tool runs |
| `plan` | Plan | `plan` | `never` / `read-only` / off | In-workspace reads run; everything else is denied |
| `dontAsk` | Never ask | `dontAsk` | `never` / `workspace-write` / off | As `plan` |

Claude is always started with `--allow-dangerously-skip-permissions` (so Bypass can be selected later over the control protocol without enabling it), `--permission-prompt-tool stdio` (approvals come to Helios) and `--setting-sources user,project,local` (the person's own `~/.claude/settings.json` rules still apply). In `bypassPermissions` every Claude `can_use_tool` request is allowed; in `plan` and `dontAsk` every one is denied; in the other three a dialog is shown. `ExitPlanMode` (plan approval) is always a dialog. Codex sends the mapping every turn as `approvalPolicy`, `approvalsReviewer: "user"` and a `sandboxPolicy` object (`workspaceWrite` with `writableRoots: [realpath(cwd)]`, `readOnly`, or `dangerFullAccess`); with network off, a network-touching command fails inside the Codex sandbox and escalates to an approval. For OpenRouter a path argument outside the working directory strips a tool of its read-only or auto-edit standing.

HOME rule. `effective_execution_mode()` returns `plan` whenever the working directory resolves (symlinks followed) to `$HOME` itself, whatever mode or provider was asked for. The reason string is `HOME is locked to read-only permissions`.

Fail-safe. `SAFE_FALLBACK_MODE` is `default`. Unknown or corrupt persisted modes resolve to it; an unconfirmed `bypassPermissions` resolves to it and is re-persisted.

Precedence at spawn: an unsaved composer choice, then the `conversation-perms.json` record, then the global default (only if `permission_mode_confirmed` is `true`; otherwise the more restrictive of the global default and any legacy `project-perms.json` entry for that directory), then the HOME clamp.

### 8.2 Workflow modes

A separate axis from permissions: `default` (label Default, all three providers) and `plan` (label Plan, Codex only, offered only when the App Server's `collaborationMode/list` advertises it). Selecting the Plan workflow on Codex forces permission mode `plan` (read-only sandbox, `approvalPolicy: never`) for the thread and every turn. Stored as `workflow_mode` in `conversation-perms.json` (omitted when `default`).

### 8.3 Reasoning effort

Keys `off`, `low`, `medium`, `high`, `xhigh`, `max`; default `high`; toolbar labels Off, Low, Medium, High, X-High, Max.

| Provider | How the key reaches the model |
|---|---|
| Claude | `off` → `--max-thinking-tokens 0`. Any other key → `--effort <key>` when `claude --help` lists `--effort`; otherwise approximated as `--max-thinking-tokens` low=4000, medium=8000, high=16000, xhigh=32000, max=32000. The picker narrows to the CLI's `supportedEffortLevels` (`off` always kept). A live change sends `apply_flag_settings` then `set_max_thinking_tokens` over the control channel |
| Codex | Keys come from the App Server's `model/list` row (`supportedReasoningEfforts`); a fresh conversation sends `""` (model default). Never persisted globally |
| OpenRouter | Offered only when the model's catalog row lists `reasoning`; `off`, `low`, `medium`, `high`, default `medium`. Sent as the request's `reasoning` object: `off` → `{"enabled": false}`, others → `{"effort": "<key>"}`; `xhigh` maps to `high`; `max` is dropped |

Typed `/model <alias>` and `/effort <level>` are handled for Claude chats only.

### 8.4 Budgets

Three constants. There is no settings key, file or environment variable for any of them; the only lever is the billing mode.

| Provider | Breaker | Applies when | Disabled when |
|---|---|---|---|
| Claude | `--max-budget-usd 10` per Claude process (`CLAUDE_STANDARD_MAX_BUDGET_USD = 10.0`), covering the subagents that process spawns | Per-token billing: `ANTHROPIC_API_KEY` set, or `claude auth status --json` not showing a `claude.ai`/`claudeai` method with a subscription, or status unreadable | claude.ai subscription billing (no flag is passed) |
| Codex | 200,000 tokens aggregate over the thread and its children (`CODEX_STANDARD_TOKEN_BUDGET = 200_000`), sent as `tokenBudget` in `thread/goal/set` and counted host-side | API-key billing: `CODEX_API_KEY` set, or `codex login status` not reporting a ChatGPT login | ChatGPT subscription (`tokenBudget` omitted) |
| OpenRouter | $5.00 per Work (`WORK_COST_LIMIT_MICRO_USD = 5_000_000`) plus 25 tool rounds per turn, extendable to 100 while rounds stay productive | Always | Never |

A trip stops the work and keeps the draft.

### 8.5 "Allow for this session"

When the Claude CLI supplies `permission_suggestions` with a tool request, the approval dialog offers `Allow once`, `Allow for this session`, optionally `Allow and auto-accept edits`, and `Deny`. `Allow for this session` answers with `updatedPermissions: [{"type": "addRules", "behavior": "allow", "destination": "session", "rules": [...]}]`. `destination` is always `session`, never `localSettings`, so nothing is written to any `settings.json` and the grant ends when that chat's Claude process exits. Without a CLI suggestion the fallback rule is `Allow all <cmd> commands for this session` (Bash, `<first word>:*`, only for a simple command) or `Allow all <Tool> calls for this session`. OpenRouter session grants are an in-memory set on the driver, cleared whenever the permission mode changes; a Bash grant matches the exact command bytes in the exact working directory. Codex `Approve for session` sends `acceptForSession` to the App Server. None of the three is persisted by Helios.

### 8.6 Environment variables Helios reads

| Variable | Effect |
|---|---|
| `HELIOS_CLAUDE_BINARY` | Step 1 of the `claude` search |
| `HELIOS_CODEX_BINARY` | Step 1 of the `codex` search |
| `HELIOS_STATE_DIR` | Relocates the files listed in Facts at a glance (partial); read on every call |
| `HELIOS_DEBUG` | Any non-empty value → DEBUG logging with driver wire tracing; read at startup |
| `HELIOS_LOG_LEVEL` | Python level name, case-insensitive, default `INFO`; an unknown name silently falls back to `INFO`; ignored when `HELIOS_DEBUG` is set |
| `HELIOS_CODEX_TRANSPORT` | Only `exec` has an effect: every GPT session refuses to start. Leave unset |
| `HELIOS_ROUTER_SOCKET` | Unix socket of an optional Smart Routing broker (default `/run/helios-router/router.sock`); inert unless the socket answers |
| `HELIOS_SESSION_POOL` | Optional read-only cross-machine session pool; inert unless the path exists and `show_pool_sessions` is `true` |
| `HELIOS_TANDEM_BINARY`, `TANDEM_STATE_DIR` | Optional Missions-pane integration with an external engine; inert unless used |
| `CLAUDE_HOME` | Claude Code's home (default `~/.claude`); read once at import |
| `XDG_DATA_HOME` | Parent of the `helios/` snapshot files (default `~/.local/share`) |
| `TERMINAL` | First choice of terminal emulator for the Settings sign-in button |
| `ANTHROPIC_API_KEY`, `CODEX_API_KEY` | Force per-token billing detection (8.4) |
| `INFISICAL_CLIENT_ID`, `INFISICAL_CLIENT_SECRET`, `INFISICAL_API_URL`, `NORVI_TRACKER_API_TOKEN`, `NORVI_TRACKER_URL`, and the two shared-context pane variables named in `src/helios/backend/process/env_scrub.py` | Optional integrations, inert unless set. `NORVI_TRACKER_URL` is read once at import (`src/helios/backend/codex_context.py`); it is neither forwarded to children nor adopted from systemd. The first four are forwarded to some CLI children (9.4) and, when unset, adopted from `systemctl --user show-environment` at startup (logged as `adopted workload identity from the systemd user environment: <names>`) |

## 9. Data and network inventory

### 9.1 Under `~/.helios`

| Path | Read / written | Purpose |
|---|---|---|
| `logs/helios.log` (+ `.1`..`.3`) | W | Log, created before anything else, even when the dependency check fails |
| `ui-state.json` | R at startup, W on change and window close | Settings (section 6). Fixed to `~/.helios` |
| `conversation-perms.json` | R at startup, W on change | Per-conversation execution overrides |
| `session-providers.json` | R/W | `session_id → anthropic\|openai\|openrouter` |
| `checkpoints.json`, `checkpoint-trash/<tree12>-<unix>/` | R/W | Rewind metadata (max 50 per session); files displaced by a restore are moved here, never deleted |
| `session-goals.json` | R/W | Goals per session or Work |
| `work/work.db` (+ `-wal`, `-shm` while running), `work/<work_id>/artifacts/<sha256>` | R/W | Work graph (SQLite, WAL mode, `user_version` 11); directory 0700, db 0600. Created at first launch. If it cannot be opened Helios logs `Work graph unavailable; using legacy sessions` and continues |
| `openrouter.key` | R | OpenRouter key (5.3). Fixed to `~/.helios` |
| `openrouter-models.json`, `openrouter-routes.json`, `openrouter-sessions/<id>.json`, `openrouter-context/<sha256>.json` | R/W | Catalog cache (fixed to `~/.helios`), pinned routes (TTL 3600 s), authoritative message histories, context archive |
| `model-catalog.json` | R/W | Claude model scan cache keyed on the binary's path, mtime and size; refreshed at startup and every 6 hours. Fixed to `~/.helios` |
| `title-cache.json`, `project-names.json` | R/W | Generated titles; project display names. Fixed to `~/.helios` |
| `session-archive/<project-dirname>/<uuid>.jsonl` | W | Transcripts moved out of `~/.claude/projects` (9.2). Fixed to `~/.helios` |
| `backups/memory/...`, `backups/context/...` | W | Last 5 versions of each memory file, `CLAUDE.md` or `MEMORY.md` saved from the editor. Fixed to `~/.helios` |
| `AGENTS.md` | R (optional) | Global instruction file for OpenRouter sessions, loaded before the repository's `AGENTS.override.md`/`AGENTS.md`; absent is fine |
| `estate-mcp.json` | R (optional) | Extra MCP servers for Codex/OpenRouter sessions; absent means none. Optional, inert unless configured |
| `project-perms.json` | R (legacy) | Retired per-directory map; never written. Do not create |

Elsewhere: `$XDG_DATA_HOME/helios/tool_snapshot.json` and `codex_mcp_snapshot.json` (default `~/.local/share/helios/`; the last Claude session's tool list and the last Codex MCP inventory, shown in Settings → Tools), `~/.cache/helios/titlegen/` (working directory for `claude`-backed title generation, excluded from project discovery), and `~/.tandem` (read-only, optional Missions pane; absent means no missions).

### 9.2 Under `~/.claude` (`$CLAUDE_HOME`, default `~/.claude`)

| Path | Read / written | Purpose |
|---|---|---|
| `projects/<encoded-cwd>/*.jsonl` | R | Claude Code transcripts: the sessions in the sidebar |
| `projects/<encoded-cwd>/memory/`, `CLAUDE.md` | R/W | Memory and context files shown in the editor |
| `projects/<encoded-cwd>/` | W (mkdir) | Created for `default_cwd` at startup and for any directory added as a project |
| `projects/<encoded-cwd>/<thread_id>.jsonl` with version tag `helios-codex` or `helios-openrouter` | W | Mirrors of GPT and OpenRouter conversations so they appear beside Claude's. Codex's authoritative history stays in `~/.codex/sessions`, which Helios never writes |
| `projects/<enc>/` empty directories | removed | At every startup, only when completely empty |
| `projects-archive/<dirname>` | W (move) | Throwaway projects (working directory `/tmp`, under `/tmp/`, or any path component starting with `_tmp_`) with at most one transcript, idle more than 14 days, whose directory no longer exists. A plain `mv` back restores one |

Session archival moves transcripts. 15 s after the window opens and every 24 h, local sessions whose transcript is older than 4 days (`RETENTION_DAYS = 4`) are moved, at most 8 per run, to `~/.helios/session-archive/<project-dirname>/<uuid>.jsonl` together with their same-stem sidecar directory. A moved session is no longer found by `claude --resume`; move the file back into `~/.claude/projects/<project-dirname>/` to restore it. Sessions with a live driver are skipped, the automatic run calls no cloud model and writes no memory note, and a toast reads `Archived N old session(s).` The startup sweep (repeated every 120 s) also moves content-free sidecar `.jsonl` stubs older than 10 minutes to the same archive. Nothing is deleted.

Throwaway rule for display: sessions whose working directory is `/tmp`, under `/tmp/`, or has a `_tmp_*` component are hidden from the All and Local sidebar filters and shown only under Temporary.

### 9.3 Network

| Endpoint | When |
|---|---|
| `http://localhost:11434/api/generate` (POST; `ollama_url`) | Default title backend. On every sidebar reload, for the 30 most recent untitled local sessions, one request at a time, 30 s timeout. Ollama absent: logged at DEBUG (`ollama title failed`), first-message titles remain, nothing else changes |
| `http://localhost:11434/api/tags` (GET) | Only from Settings → Behavior "Save and check", 5 s timeout |
| `claude --print --model haiku --max-budget-usd 0.05 ...` | Only when `title_backend` is `claude`; skipped silently if the CLI lacks `--max-budget-usd` |
| `https://openrouter.ai/api/v1/models` | Only with a key: when Settings is opened or a key is saved (force refresh); periodic refreshes read the cache |
| `https://openrouter.ai/api/v1/key`, `https://openrouter.ai/api/v1/credits` | Only with a key and an OpenRouter model selected: at startup if the sticky `model` is an OpenRouter id, whenever the picker or provider toggle switches to an OpenRouter model from another provider, whenever the key is saved in Settings, and after a completed turn at most once per 60 s |
| `https://openrouter.ai/api/v1/chat/completions`, `https://openrouter.ai/api/v1/models/{model}/endpoints` | OpenRouter chats (bearer; redirects are refused so it never changes origin) and route pinning (no key, like the catalog fetch) |
| `http://127.0.0.1:9101` | Only when the optional shared-context pane is shown; optional, inert unless configured |
| `/run/helios-router/router.sock` (local Unix socket, `$HELIOS_ROUTER_SOCKET`) | At startup and every 300 s; an absent socket is silently "unavailable". Optional broker, inert unless configured |
| Anthropic and OpenAI APIs | Contacted by the `claude` and `codex` CLIs themselves, never by Helios |

Helios itself makes no Internet request at startup unless the sticky model is an OpenRouter model. Every Helios-side network failure is a logged warning, never fatal.

### 9.4 Child-environment scrub

Every CLI child (Claude session, title generation, archival, `claude auth status`, `claude mcp list`, Codex App Server, `codex login status`, OpenRouter Bash tool) gets a copy of Helios's environment with:

1. `HELIOS_INTERNAL_ENV` removed: `HELIOS_DEBUG`, `HELIOS_LOG_LEVEL`, `HELIOS_CLAUDE_BINARY`, `HELIOS_CODEX_BINARY`, `HELIOS_CODEX_TRANSPORT`, `HELIOS_ROUTER_SOCKET`, `HELIOS_TANDEM_BINARY` and the two shared-context pane variables (full list in `src/helios/backend/process/env_scrub.py`). `GPG_AGENT_INFO` is dropped. `HELIOS_STATE_DIR` and `HELIOS_SESSION_POOL` pass through.
2. Every variable whose upper-cased name contains `TOKEN`, `SECRET`, `PASSWORD`, `PASSWD`, `PASSPHRASE`, `_PASS`, `CREDENTIAL`, `APIKEY`, `_KEY`, `_PWD`, `_PAT`, `JWT`, `AUTH_CONFIG`, `ASKPASS` or `KEYRING` removed unless it is in that spawn's keep set. Benign names such as `TOKENIZERS_PARALLELISM` are dropped too; names without a marker (for example `DATABASE_URL`) survive.
3. `SSH_AUTH_SOCK` always kept (`POLICY_KEEP`), so agents can `ssh` and push as the person.

| Child | Credentials kept |
|---|---|
| Interactive Claude session | `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`, plus the optional integration variables `INFISICAL_CLIENT_ID`, `INFISICAL_CLIENT_SECRET`, `INFISICAL_API_URL`, `NORVI_TRACKER_API_TOKEN` (inert unless set) |
| Title generation, archival, `claude auth status`, `claude mcp list`, sign-in terminal | `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN` |
| `claude --help`, `claude --version` probes | none |
| Codex App Server (GPT sessions) | no provider credential (`OPENAI_API_KEY` and `CODEX_API_KEY` are both dropped); Codex uses its own credential store. `NORVI_TRACKER_API_TOKEN` if set. `RUST_LOG=error` is added if unset |
| `codex app-server` catalog probe (`model/list`), `codex login status`, `codex --version`, `codex login --with-api-key` | none (`OPENAI_API_KEY` and `CODEX_API_KEY` are both dropped) |
| OpenRouter Bash tool | no provider credential; `NORVI_TRACKER_API_TOKEN` if set |

The interactive Claude session additionally receives `CLAUDE_CODE_ENABLE_TODO_TOOLS=1`.

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Unknown option --version` (or any flag), exit 1 | Helios registers no command-line options; GLib rejects unknown ones | Read the version as in 7.3. Launch with no arguments |
| `GLib-GIO-CRITICAL **: ... This application can not open files.`, exit 1 | A positional argument was passed; the app has no file-open handler | Launch with no arguments; set the working directory with `default_cwd` |
| Window opens; sending toasts `Could not find a working `claude` binary. Set $HELIOS_CLAUDE_BINARY or fix your `claude` install.` | No runnable `claude` in the six search locations; a desktop launch has a short `PATH` | Symlink the binary into `~/.local/bin/claude`, or set `HELIOS_CLAUDE_BINARY` on the `.desktop` `Exec` line (5.1). Startup never fails on this; only a send does |
| Toast `Claude's required process-family budget breaker is unavailable. Helios requires Claude Code 2.1.217+ with --max-budget-usd; ...` | Per-token billing applies and the CLI is older than 2.1.217 or lacks `--max-budget-usd` | Update Claude Code, or sign the person in to a claude.ai subscription (no cap, no floor) |
| Toast `Claude CLI <version> cannot do: ...` once per run | `claude --help` lacks `--effort`, `--max-budget-usd` or `--forward-subagent-text`; or it has `--max-budget-usd` but is older than 2.1.217, which adds `budget enforcement across Claude's subagents (needs 2.1.217+)` | Update Claude Code; the log line `claude CLI <version>; degraded: ...` lists the same |
| Every chat opens in `$HOME` although `default_cwd` is set; log warning `default cwd <path> is not a directory; ignoring` (or `default cwd <path> is unreachable: ...`) | The configured path does not exist, is not a directory, or its parent cannot be read; the heuristic in 6.3 then applies | Create the directory or fix the path in `ui-state.json` while Helios is closed |
| GPT toggle disabled, tooltip `Add an OpenAI key in Settings → Providers to enable GPT` | `codex` missing, `codex login status` not logged in, or `codex app-server` `model/list` returned nothing | Install `codex` (`npm install -g --prefix ~/.local @openai/codex`), then `codex login` or an API key via Settings → Providers (5.2). `OPENAI_API_KEY` in the environment is ignored; `HELIOS_CODEX_TRANSPORT` must not be `exec` |
| Toast `Codex isn't signed in — add your OpenAI API key in Settings → Providers.` | `codex login status` reports not logged in at spawn | As above |
| Settings → Tools shows `No MCP servers configured` / `Add one with `claude mcp add …`.` | `claude mcp list` (30 s timeout) returned no lines | Configure servers with the Claude CLI; Helios only reads its output. The Codex MCP list is a snapshot written by a GPT session, empty until one has run |
| Sessions vanished from `claude --resume` | The archiver moved transcripts idle more than 4 days to `~/.helios/session-archive/<project-dirname>/`, or a throwaway project to `~/.claude/projects-archive/` | `mv` the `.jsonl` (and its same-stem directory) back into `~/.claude/projects/<project-dirname>/`. Nothing is deleted |
| `ImportError: cannot import name 'StrEnum' from 'enum'` or similar at launch from source | Python older than 3.11; the floor is not checked at startup | Use Python 3.11 or newer as the first `python3` on `PATH` (`scripts/helios` uses it) |
| `Helios can't start because some system libraries are too old:` on stderr, exit 1 | GTK < 4.10, libadwaita < 1.5 or GtkSourceView < 5 | Upgrade `gir1.2-gtk-4.0`, `gir1.2-adw-1`, `gir1.2-gtksource-5` and the libraries behind them (section 4); Ubuntu 24.04 and newer have them |
| `ValueError: Namespace Adw not available` (or Gtk, GtkSource) | The `gir1.2-*` typelib package is missing | Install the package named in the error |
| Titles are the first message, never generated | No Ollama at `ollama_url` (default `http://localhost:11434`), or `ollama_title_model` (default `qwen2.5-coder:14b`) is not pulled. The failure is logged at DEBUG only | Run Ollama and pull the model, change the two keys, or set `title_backend` to `claude` (metered). Not an error |
| Rewind dialog shows `No checkpoints yet` — `A checkpoint is taken before each message you send, in sessions whose folder is a git repository.` | The chat's working directory is not inside a git worktree, or `git` is not installed | Use a git repository as `default_cwd`; install `git` (only a Recommends of the package) |
| Toast `Sent without a checkpoint — the snapshot was taking too long. Rewind is unavailable for this message.` | The pre-turn `git` snapshot exceeded its 4000 ms deadline | Large or slow worktree; the message was still sent |
| Every chat is read-only | Working directory is `$HOME` | Set `default_cwd` (6.3) |
| Log warning `Helios Router MCP launcher is missing: /usr/lib/python3/scripts/helios-router-mcp` on every Claude spawn (APT install) | The optional Smart Routing launcher is resolved relative to the package and does not exist in a packaged install | Benign; the session continues without that MCP. Optional, inert unless configured |
| Log warning `could not read the systemd user environment: ...` | Host without `systemctl` | Benign; an optional integration, inert unless configured |
| Log warning `Work graph unavailable; using legacy sessions: ...` | `~/.helios/work/work.db` could not be opened | Check permissions and free space on `~/.helios`; Helios still runs |
| Where is the log; how to get more | — | `<state dir>/logs/helios.log` (Settings → Diagnostics shows the path). `HELIOS_DEBUG=1 helios` from a terminal adds the wire traffic to and from each CLI (spawn argv, every stdout record, stderr), which is what a bug report needs; `HELIOS_LOG_LEVEL=WARNING` quiets it. An unknown level name silently means `INFO` |

## 11. Uninstall / reset

APT:

```bash
sudo apt remove -y helios
dpkg -s helios 2>&1 | head -n 1
```

Expected: `dpkg-query: package 'helios' is not installed and no information is available`. The package ships no conffiles, so `remove` and `purge` leave the same state. The system packages it depended on stay unless you `sudo apt autoremove`. The 7.2 helpers are not dependencies; if they were installed only for the smoke test: `sudo apt remove --autoremove -y xvfb xauth xdotool` (leave `dbus` and `libglib2.0-bin`: the base system and 7.1's `gdbus`/`gapplication` use them).

Source: delete the checkout and, if `install-desktop.sh` was run:

```bash
rm -f ~/.local/share/applications/dev.norvi.Helios.desktop ~/.local/share/icons/hicolor/*/apps/dev.norvi.Helios.png
```

Reset state (what is lost, per path):

| Delete | Loses |
|---|---|
| `~/.helios/ui-state.json` | Settings, sticky model, default working directory |
| `~/.helios/openrouter.key` | The OpenRouter key |
| `~/.helios/work/` | Works, goals, execution history, artifacts |
| `~/.helios/conversation-perms.json`, `checkpoints.json`, `checkpoint-trash/` | Per-conversation modes; Rewind history and restored-over files |
| `~/.helios/session-archive/` | Archived Claude transcripts. Move them back to `~/.claude/projects/<project-dirname>/` first if the person wants them |
| `~/.helios/openrouter-sessions/`, `openrouter-context/` | OpenRouter conversation history |
| `~/.helios/logs/` | Logs |
| `~/.local/share/helios/`, `~/.cache/helios/` | Tool snapshots; title-generation scratch |

```bash
rm -rf ~/.helios ~/.local/share/helios ~/.cache/helios
```

`~/.claude` is left alone by both uninstall and reset. The only Helios-written files under it are the GPT/OpenRouter mirror transcripts (`helios-codex` / `helios-openrouter` version tag) in `~/.claude/projects/<encoded-cwd>/`, anything under `~/.claude/projects-archive/`, empty project directories created for a `default_cwd`, and memory notes saved through the editor; remove them by hand only if the person asks. `~/.codex` is never written by the Helios app; only the optional `scripts/setup-codex-estate --apply` helper appends to `~/.codex/config.toml` and `~/.codex/AGENTS.md` (or `AGENTS.override.md` when that exists), keeping a `.helios-backup-*` copy of any file it changes, and only when run explicitly.

## 12. Verification checklist

Run top to bottom on the installed machine, in a terminal, as the person. Each line prints one token; stop at the first missing one. 12.2 continues in the same shell as 12.1 (it uses `H` from line 1).

### 12.1 Any machine

```bash
H=${HELIOS_CMD:-/usr/bin/helios}     # checkout: H=/home/<user>/helios/scripts/helios and export PYTHONPATH=/home/<user>/helios/src
test -x "$H" && echo 1_BINARY_OK
dpkg-query -W helios 2>/dev/null || python3 -c "import helios; print('helios', helios.__version__)"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' && echo 2_PYTHON_OK
python3 -c 'from helios.deps import check_runtime_versions as c; assert c() == [], c(); print("3_LIBS_OK")'
command -v git >/dev/null && echo 4_GIT_OK
C=$(env -i HOME="$HOME" PATH=/usr/local/bin:/usr/bin:/bin PYTHONPATH="${PYTHONPATH:-}" python3 -c "import sys; from helios.backend.claude_binary import find_claude_binary; b = find_claude_binary(); print('5_CLAUDE_FOUND', b, file=sys.stderr); print(b.path)")
"$C" --help 2>&1 | grep -q -- '--max-budget-usd' && echo "6_CLAUDE_VERSION $("$C" --version)"
"$C" auth status --json | grep -q '"loggedIn": *true' && echo 7_CLAUDE_LOGGED_IN
python3 -c 'import json, pathlib, os; d = json.loads((pathlib.Path.home()/".helios"/"ui-state.json").read_text()); c = d.get("default_cwd", ""); assert c and os.path.isdir(c) and os.path.realpath(c) != os.path.realpath(os.path.expanduser("~")), c; print("8_DEFAULT_CWD_OK", c, d.get("permission_mode", "default"), d.get("permission_mode_confirmed", False))'
git -C "$(python3 -c 'import json, pathlib; print(json.loads((pathlib.Path.home()/".helios"/"ui-state.json").read_text())["default_cwd"])')" rev-parse --show-toplevel >/dev/null && echo 9_GIT_REPO_OK
```

Expected, in order: `1_BINARY_OK`, `helios	0.99.2` (or `helios 0.99.2`), `2_PYTHON_OK`, `3_LIBS_OK`, `4_GIT_OK`, `5_CLAUDE_FOUND <path> (via ...)` (printed on stderr; the path alone goes to stdout and into `C`, so lines 6 and 7 test the binary Helios will run, not whatever `claude` is on the agent's `PATH`), `6_CLAUDE_VERSION 2.1.217` or newer, `7_CLAUDE_LOGGED_IN`, `8_DEFAULT_CWD_OK <path> <mode> False` (`True` only if the person has already used Settings → "Save chat defaults"), `9_GIT_REPO_OK`. Line 7 is the one the agent cannot fix alone: hand `claude auth login --claudeai` (or `--console`) back to the person and re-run from line 7. Line 9 is advisory (Rewind only). Optional providers: `codex login status` printing `logged in` with exit 0 (5.2), and `stat -c '%a' ~/.helios/openrouter.key` printing `600` plus the `key-ok` check (5.3).

### 12.2 Desktop session only (7.1)

On a headless machine stop after 12.1 and use 7.2 instead; line 11 does not apply there because that recipe runs in an isolated `HOME`. Helios must not be running when the launch line starts (7.1 ends with its quit): a second invocation only raises the existing window and writes nothing to `$OUT`, so the line before it quits any instance that is still up.

```bash
[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && echo DISPLAY_OK || echo NO_DISPLAY_USE_7_2
gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus --method org.freedesktop.DBus.ListNames | grep -q dev.norvi.Helios && { gapplication action dev.norvi.Helios quit; sleep 2; }   # single-instance app: a running window would swallow the launch below
OUT=$(mktemp); setsid -f "$H" >/dev/null 2>"$OUT"; sleep 10     # this run's log lines only (Helios logs to stderr too); GTK's "cannot open display" lands here instead of vanishing
gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus --method org.freedesktop.DBus.ListNames | grep -q dev.norvi.Helios && echo 10_RUNNING_OK
test -d "${CLAUDE_HOME:-$HOME/.claude}/projects/$(python3 -c 'import json, pathlib; print(str(pathlib.Path(json.loads((pathlib.Path.home()/".helios"/"ui-state.json").read_text())["default_cwd"]).expanduser()).replace("/", "-"))')" && echo 11_PROJECT_DIR_OK
grep -E 'claude CLI .*; degraded: |model catalog: ' "$OUT" | tail -n 3
grep -c 'CRITICAL helios' "$OUT" | grep -qx 0 && echo 12_NO_CRITICAL
gapplication action dev.norvi.Helios quit; sleep 2
! gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus --method org.freedesktop.DBus.ListNames | grep -q dev.norvi.Helios && echo 13_QUIT_OK
```

Expected, in order: `DISPLAY_OK`, `10_RUNNING_OK`, `11_PROJECT_DIR_OK` (the startup in 6.3 created `~/.claude/projects/<encoded default_cwd>/`, which proves the configured directory was read and accepted), three log lines (`claude CLI <version>; degraded: nothing` and two `model catalog:` lines with `openai:` and `openrouter:` statuses), `12_NO_CRITICAL`, `13_QUIT_OK`. `grep 'CRITICAL helios'` matches Helios's own log lines (`LEVEL helios.<module>:`) and not GLib's `Gtk-CRITICAL **:` noise; because it reads this run's stderr rather than `helios.log`, a CRITICAL from an earlier run cannot fail a re-run.
