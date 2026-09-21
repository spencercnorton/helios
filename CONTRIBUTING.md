# Contributing to Helios

Thanks for your interest. Helios is a small project with one maintainer, so
the process is deliberately light — but a few things are fixed.

## How changes land

This GitHub repository is a **release mirror**: every commit on `main` after
the initial import is a tagged release built from a private development
tree, and `main` only ever
moves forward by a release. That has two consequences for contributors:

- Pull requests are reviewed **here**, but they are not merged here. An
  accepted change is applied to the development tree and ships in the next
  tagged release; the pull request is then closed with a comment that names
  that release and credits you.
- Please do not rebase your pull request onto anything but `main`.

## Before you start

- **Bugs** — open a [bug report](https://github.com/spencercnorton/helios/issues/new/choose).
  A report with reproduction steps, versions and a scrubbed log excerpt is
  usually fixed faster than a pull request that arrives without one. Run
  `HELIOS_DEBUG=1 ./scripts/helios` and take the excerpt from
  `~/.helios/logs/helios.log` (or stderr); strip paths, hostnames and any
  key material.
- **Features** — open a feature request first. Helios has strong opinions
  about provider isolation, permissions and spend controls (see
  [SECURITY.md](SECURITY.md#what-helios-does-with-credentials-and-data) and
  the *Permissions* and *Budgets* sections of
  [docs/user-guide.md](docs/user-guide.md)); an idea that cuts across them
  needs a conversation before code.
- **Security** — never in a public issue. Use
  [private vulnerability reporting](https://github.com/spencercnorton/helios/security/advisories/new);
  see [SECURITY.md](SECURITY.md).

## Working on the code

Requirements: Python 3.11 or newer (CI uses 3.13), GTK 4.10+, libadwaita
1.5+, GtkSourceView 5. `./scripts/helios` runs whatever `python3` is on
PATH and does not check the interpreter version; an older interpreter fails
on import rather than with a startup message.

```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-gtksource-5 libgtksourceview-5-0 python3-pytest git python3-pil
pip install ruff==0.15.20      # the exact version CI runs (pipx or a venv on Debian/Ubuntu)
./scripts/helios               # run from the checkout
./scripts/install-desktop.sh   # optional: app-grid entry and icon (needs python3-pil)
ruff check .                   # lint; CI enforces it
```

`ruff check .` prints `All checks passed!` when clean. A newer ruff reports
rules this project never adopted. `git` is a test dependency:
`tests/test_checkpoints.py` skips without it and `tests/test_git_changes.py`
errors, so install it before running the suite.

`./scripts/helios` uses your real `~/.helios` and `~/.claude/projects` and
tidies them at startup. To try a development build without touching them,
run it with a throwaway home: `HOME=$(mktemp -d) ./scripts/helios`.
`HELIOS_STATE_DIR` alone is not enough; several files are pinned to
`~/.helios`.

### Backend tests

This is what CI (`.github/workflows/ci.yml`) runs: ubuntu-latest, Python
3.13, and a `gi` stub on `PYTHONPATH` that raises `ModuleNotFoundError`, so
every GTK test skips. Reproduce it (CI also passes `-q`, which this block
drops so the summary line prints — see below):

```bash
mkdir -p "$HOME/nogtk"
printf 'raise ModuleNotFoundError("gtk hidden")\n' > "$HOME/nogtk/gi.py"
PYTHONPATH="$HOME/nogtk" python3 -m pytest -p no:cacheprovider
```

The last line is the summary, for example `1816 passed, 91 skipped in 18.9s`
(one fewer pass and one more skip on Python 3.11 or 3.12). Roughly 90 skips
are expected; adding `-rs` to the command lists the reasons, and every one
should read `gtk hidden` or `not part of this tree` (on Python 3.11 or 3.12
one more reads `pathlib.full_match is 3.13+`). Do not add `-q`:
`pyproject.toml` already sets it, and a second `-q` suppresses the summary
line. Paste that line in the pull request.

Without the stub, a machine with `python3-gi` installed runs the real GTK
tests instead. Only 19 of the 78 GTK test modules skip when there is no
display; most of the rest construct widgets regardless, so a bare run is
neither what CI runs nor safe headless.

The suite needs no sandbox and no install step: `tests/conftest.py` moves
`HOME`, `HELIOS_STATE_DIR` and `CLAUDE_HOME` to a temporary directory before
any `helios` import and puts `src/` on `sys.path`. Never pip-install
PyGObject.

### GTK tests

Public CI never executes a GTK test, so run them yourself before a widget or
driver pull request. `gi`/GTK state is process-global: run them under Xvfb
with isolated state, never against your live desktop session or your real
`~/.helios`.

```bash
sudo apt install xvfb xauth dbus
(
  export HOME="$(mktemp -d)"
  export HELIOS_STATE_DIR="$HOME/.helios" TANDEM_STATE_DIR="$HOME/.tandem" GDK_BACKEND=x11
  mkdir -p "$HELIOS_STATE_DIR" "$TANDEM_STATE_DIR"
  xvfb-run -a dbus-run-session -- python3 -c 'import gi; gi.require_version("Gtk", "4.0"); from gi.repository import Gtk; assert Gtk.init_check(), "GTK display initialization failed"'
  xvfb-run -a dbus-run-session -- python3 -m pytest -p no:cacheprovider
)
```

The preflight must pass before the suite; without it a broken display shows
up as a mix of display-unavailable skips and widget-construction failures
deep in the run instead of one clear error. `dbus-run-session` only quiets
libadwaita's portal lookups; drop it if it is absent. When the run is
genuine, adding `-rs` to the `xvfb-run` command above lists no `gtk hidden`
and no display-unavailable skips.

### Rules

- Modules under `src/helios/backend/` must not import `gi`, except the
  drivers in `src/helios/backend/process/` (`cli_driver.py`,
  `codex_driver.py`, `codex_app_driver.py`, `openrouter_driver.py`,
  `title_generator.py`). GTK code lives in `src/helios/widgets/`,
  `main_window.py`, `app.py` and `deps.py`.
- A test whose imports reach `gi` (widgets, `main_window.py`, the drivers)
  must call `gi = pytest.importorskip("gi")` at module scope before those
  imports, and skip when `Gtk.init_check()` is false if it builds widgets.
  CI hides `gi`, so an unguarded import is a collection error. Such tests
  skip in public CI; run them under Xvfb as above.
- PyGObject async callbacks must be pinned as instance attributes or they are
  garbage-collected before they fire. Read
  [docs/gtk4-gotchas.md](docs/gtk4-gotchas.md) before touching a widget or a
  driver.
- Keep a change to one concern. A pull request that fixes a bug and
  reformats a file is two pull requests.
- Tests: a bug fix carries a regression test; a feature carries the smallest
  test that fails without it.
- Do not bump the version. `src/helios/__init__.py` and `pyproject.toml`
  must stay equal (a test enforces it); the maintainer bumps both at
  release. There is no changelog to edit in this tree.

## Pull request checklist

The template asks for what changed, why, and how it was tested (the pytest
summary line), plus a confirmation that the diff carries no secrets, machine
names or personal paths. Fill it in — it is what the reviewer reads first.

## Licence

By contributing you agree that your contribution is licensed under the
[MIT License](LICENSE) that covers the project.
