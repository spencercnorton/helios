"""What the browser pane will load, and which pages count as local."""

from __future__ import annotations

import pytest

from helios.backend.browser_target import TargetError, is_local, resolve


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


def test_only_files_and_loopback_are_local():
    for url in ("file:///tmp/r.html", "about:blank", "http://localhost:3000/",
                "http://127.0.0.1:8000", "http://[::1]:9000/", "http://0.0.0.0:8000"):
        assert is_local(url), url
    for url in ("https://example.com", "http://10.0.0.5:8000", "http://localhost.evil.com",
                "https://127.0.0.1.nip.io", "chrome://settings", ""):
        assert not is_local(url), url
