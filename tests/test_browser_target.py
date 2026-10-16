"""What the browser pane will load, and what an agent may use without asking."""

from __future__ import annotations

import pytest

from helios.backend.browser_target import TargetError, consent_scope, is_web_scope, resolve


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


def test_project_files_and_loopback_are_free_everything_else_asks(tmp_path, monkeypatch):
    import tempfile

    (tmp_path / "scratch").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "scratch"))
    project = tmp_path / "proj"
    (project / "out").mkdir(parents=True)
    cwd = str(project)
    for url in (f"file://{project}/out/r.html", "http://localhost:3000/", "http://127.0.0.1:8000",
                "http://[::1]:9000/", "http://0.0.0.0:8000"):
        assert consent_scope(url, cwd) == "", url
    assert consent_scope("https://mail.example.com/inbox", cwd) == "mail.example.com"
    assert consent_scope("http://localhost.evil.com/", cwd) == "localhost.evil.com"
    assert consent_scope("http://10.0.0.5:8000", cwd) == "10.0.0.5"
    # Files outside the project — credentials, other repos — are asked about.
    assert consent_scope("file:///etc/passwd", cwd) == "/etc/passwd"
    assert consent_scope(f"file://{project}/../secret.txt", cwd) == str(tmp_path / "secret.txt")
    # A popup's about:blank inherits its opener's origin, so it is never free.
    assert consent_scope("about:blank", cwd) == "about:blank"
    # A chat rooted at the home folder or / frees nothing outside the temp dir.
    home = str(__import__("pathlib").Path.home())
    assert consent_scope(f"file://{home}/.codex/auth.json", home) != ""
    assert consent_scope("file:///etc/hosts", "/") == "/etc/hosts"
    assert is_web_scope("mail.example.com") and not is_web_scope("/etc/passwd")
    assert not is_web_scope("about:blank")
