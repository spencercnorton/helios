## What changed

<!-- One paragraph. Link the issue if there is one: "Fixes #12". -->

## Why

## How it was tested

<!-- Paste the summary line of `python3 -m pytest -p no:cacheprovider` with `gi` hidden (e.g. `1816 passed, 91 skipped`), plus the Xvfb run if the change touches widgets, main_window.py or a driver. Say which distro and library versions: `dpkg -l libgtk-4-1 libadwaita-1-0 libgtksourceview-5-0 | awk '/^ii/{print $2, $3}'`, and the claude / codex CLI versions when a driver is involved. -->

## Checklist

- [ ] Backend tests pass the way CI runs them: `gi` hidden by the stub in [CONTRIBUTING.md](https://github.com/spencercnorton/helios/blob/main/CONTRIBUTING.md#backend-tests), with `git` on PATH (the checkpoint tests skip without it). With `python3-gi` installed a bare `pytest` runs the real GTK tests instead.
- [ ] If the change touches `src/helios/widgets/`, `main_window.py` or a driver in `src/helios/backend/process/`, the GTK tests pass under Xvfb with isolated state ([recipe](https://github.com/spencercnorton/helios/blob/main/CONTRIBUTING.md#gtk-tests)); public CI does not run them.
- [ ] `ruff check .` prints `All checks passed!` with the version CI pins (`pip install ruff==0.15.20`)
- [ ] A new test that imports `gi` starts with `gi = pytest.importorskip("gi")` and, if it builds widgets, skips when `Gtk.init_check()` is false — CI hides `gi`, so an unguarded import is a collection error
- [ ] No secrets, machine names, or personal paths in the diff
- [ ] Docs updated if behaviour changed: `README.md`, `docs/user-guide.md`, `docs/agent-setup.md` (§8.6 environment variables, §9 data and network inventory) and `debian/helios.1` for environment variables or files

<!--
How this lands: this repository is a release mirror. A maintainer reviews the
pull request here, applies accepted changes to the development tree, and the
change ships in the next tagged release — the pull request is then closed
with a reference to that release. See CONTRIBUTING.md.
-->
