"""What the browser pane will load, and what an agent may use without asking."""

from __future__ import annotations

import pytest

from helios.backend.browser_target import (
    TargetError,
    consent_scope,
    delocalize,
    free_roots,
    is_web_scope,
    localize,
    resolve,
)


def test_agent_and_url_bar_text_resolve_to_loadable_urls(tmp_path):
    report = tmp_path / "out" / "report.html"
    report.parent.mkdir()
    report.write_text("<h1>ok</h1>")
    assert resolve("out/report.html", str(tmp_path)) == report.resolve().as_uri()
    assert resolve(str(report)) == report.resolve().as_uri()
    assert resolve("localhost:5173/app") == "http://localhost:5173/app"
    assert resolve("127.0.0.1:8000") == "http://127.0.0.1:8000"
    assert resolve("0.0.0.0:8000") == "http://0.0.0.0:8000"
    assert resolve("example.com:8080/x") == "https://example.com:8080/x"
    assert resolve("docs.python.org/3/") == "https://docs.python.org/3/"
    assert resolve("https://example.com") == "https://example.com"
    assert resolve("file:///tmp/x.html") == "file:///tmp/x.html"


@pytest.mark.parametrize("text", [
    "", "javascript:alert(1)", "data:text/html,<b>x</b>", "mailto:a@b.c",
    "file:relative.html", "missing.html", "no such thing",
])
def test_everything_else_is_refused(tmp_path, text):
    with pytest.raises(TargetError):
        resolve(text, str(tmp_path))


def test_project_files_and_loopback_are_free_everything_else_asks(tmp_path):
    project = tmp_path / "proj"
    (project / "out").mkdir(parents=True)
    (project / "out" / "r.html").write_text("ok")
    cwd = str(project)

    # Project files are served through helios-file, which the agent may use...
    local = localize(resolve("out/r.html", cwd), cwd)
    assert local.startswith("helios-file://") and local.endswith("/out/r.html")
    assert consent_scope(local, cwd) == ""
    # ...but another chat's project is not this agent's to read.
    assert consent_scope(local, str(tmp_path / "elsewhere")) == str(project)
    for url in ("http://localhost:3000/", "http://127.0.0.1:8000", "http://[::1]:9000/", "http://0.0.0.0:8000"):
        assert consent_scope(url, cwd) == "", url
    assert consent_scope("https://mail.example.com/inbox", cwd) == "mail.example.com"
    assert consent_scope("http://localhost.evil.com/", cwd) == "localhost.evil.com"
    assert consent_scope("http://10.0.0.5:8000", cwd) == "10.0.0.5"
    # A file:// page can frame any file, so it is never free — even in the project.
    assert consent_scope((project / "out" / "r.html").as_uri(), cwd) == str(project / "out" / "r.html")
    assert consent_scope("file:///etc/passwd", cwd) == "/etc/passwd"
    assert localize("file:///etc/passwd", cwd) == "file:///etc/passwd"
    # A popup's about:blank inherits its opener's origin, so it is never free.
    assert consent_scope("about:blank", cwd) == "about:blank"
    assert is_web_scope("mail.example.com") and not is_web_scope("/etc/passwd")
    assert not is_web_scope("about:blank")
    # The temp directory is shared by every program: not free either.
    assert free_roots(str(project)) == [str(project.resolve())]
    assert consent_scope(localize("file:///tmp/x.html", ""), cwd) == "/tmp/x.html"


def test_fragments_survive_and_project_urls_map_back_for_other_apps(tmp_path):
    project = tmp_path / "proj"  # tmp_path is HOME under conftest
    project.mkdir()
    (project / "r.html").write_text("ok")
    local = localize(resolve(f"file://{project}/r.html?x=1#kpi"), str(project))
    assert local.startswith("helios-file://") and local.endswith("/r.html?x=1#kpi")
    assert delocalize(local) == f"file://{project.resolve()}/r.html?x=1#kpi"
    with pytest.raises(TargetError):
        resolve("file://[::1/x")


def test_a_folder_containing_the_home_frees_nothing(tmp_path, monkeypatch):
    import os

    home = os.path.realpath(os.path.expanduser("~"))
    for cwd in ("/", os.path.dirname(home), home):
        assert free_roots(cwd) == free_roots(""), cwd


@pytest.mark.parametrize("text", [
    "http://evil.com\\@localhost/", "file:///a%00b", "http://x.com/a b", "helios-file://unknown/x",
])
def test_addresses_python_and_webkit_would_read_differently_are_refused(text):
    with pytest.raises(TargetError):
        resolve(text)
