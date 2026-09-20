# Contributing to Helios

Thanks for your interest. Helios is a small project with one maintainer, so
the process is deliberately light — but a few things are fixed.

## How changes land

This GitHub repository is a **release mirror**: every commit on `main` is a
tagged release built from a private development tree, and `main` only ever
moves forward by a release. That has two consequences for contributors:

- Pull requests are reviewed **here**, but they are not merged here. An
  accepted change is applied to the development tree and ships in the next
  tagged release; the pull request is then closed with a reference to that
  release, and you keep the credit in the release notes.
- Please do not rebase your pull request onto anything but `main`.

## Before you start

- **Bugs** — open a [bug report](https://github.com/spencercnorton/helios/issues/new/choose).
  A report with reproduction steps, versions and a scrubbed log excerpt is
  usually fixed faster than a pull request that arrives without one.
- **Features** — open a feature request first. Helios has strong opinions
  about provider isolation, permissions and spend controls (see the README);
  an idea that cuts across them needs a conversation before code.
- **Security** — never in a public issue. Use
  [private vulnerability reporting](https://github.com/spencercnorton/helios/security/advisories/new);
  see [SECURITY.md](SECURITY.md).

## Working on the code

```bash
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-gtksource-5 libgtksourceview-5-0 python3-pytest
./scripts/helios            # run from the checkout
python3 -m pytest -q        # backend tests (GTK-free; this is what CI runs)
ruff check .                # lint; CI enforces it
```

- Anything imported by tests must stay GTK-free: CI runs them in a slim
  Python image with no `gi`. GTK code lives in `src/helios/widgets/` and
  `main_window.py`; the driver logic it calls lives in `src/helios/backend/`.
- PyGObject async callbacks must be pinned as instance attributes or they are
  garbage-collected before they fire. Read `docs/gtk4-gotchas.md` before
  touching a widget or a driver.
- Keep a change to one concern. A pull request that fixes a bug and
  reformats a file is two pull requests.
- Tests: a bug fix carries a regression test; a feature carries the smallest
  test that fails without it.

## Pull request checklist

The template asks for what changed, why, and how it was tested, plus a
confirmation that the diff carries no secrets, machine names or personal
paths. Fill it in — it is what the reviewer reads first.

## Licence

By contributing you agree that your contribution is licensed under the
[MIT License](LICENSE) that covers the project.
