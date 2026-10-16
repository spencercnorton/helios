"""The pane answers agent tool calls with real WebKit (display lane only)."""

from __future__ import annotations

import time

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402


def _pane(tmp_path, monkeypatch):
    if not Gtk.init_check():
        pytest.skip("GTK display is unavailable")
    import tempfile

    monkeypatch.setenv("HELIOS_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "scratch").mkdir(exist_ok=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "scratch"))
    from helios.widgets.browser_pane import BrowserPane

    pane = BrowserPane(on_reveal=lambda: None, on_close=lambda: None, cwd=lambda: str(tmp_path / "proj"))
    if not pane.available:
        pytest.skip("WebKitGTK 6 is not installed")
    return pane


def _call(pane, tool, **args):
    replies = []
    pane.handle_call(tool, args, replies.append)
    deadline = time.monotonic() + 15
    while not replies and time.monotonic() < deadline:
        GLib.MainContext.default().iteration(False)
        time.sleep(0.01)
    assert len(replies) == 1, f"{tool} never answered"
    return replies[0]


def test_open_read_and_refuse(tmp_path, monkeypatch):
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "report.html").write_text("<title>Report</title><h1>Totals →</h1><p>42 rows</p>")
    pane = _pane(tmp_path, monkeypatch)

    opened = _call(pane, "browser_open", url="report.html")
    assert opened["ok"] and opened["status"] == "loaded" and opened["title"] == "Report"
    assert opened["url"].startswith("helios-file://")
    read = _call(pane, "browser_read", tab=opened["tab"])
    assert "Totals →" in read["text"] and "42 rows" in read["text"]  # UTF-8 without <meta charset>
    assert _call(pane, "browser_open", target="report.html")["error"].startswith('browser_open needs "url"')
    assert not _call(pane, "browser_open", url="javascript:alert(1)")["ok"]
    assert _call(pane, "browser_tabs")["tabs"][0]["tab"] == opened["tab"]
    assert not _call(pane, "browser_navigate", tab="99", action="reload")["ok"]
    assert not _call(pane, "browser_nope")["ok"]


def test_outside_the_project_asks_and_what_the_person_opened_is_redacted(tmp_path, monkeypatch):
    (tmp_path / "proj").mkdir()
    private = tmp_path / "private.html"
    private.write_text("<title>Bank statement</title><p>balance 1,234</p>")
    pane = _pane(tmp_path, monkeypatch)
    asked = []

    def deny(scope, args, verb, proceed, reply):
        if not scope:
            return proceed()
        asked.append((scope, verb))
        reply({"ok": False, "error": f"The person declined access to {scope}."})

    monkeypatch.setattr(pane, "_consent", deny)
    refused = _call(pane, "browser_open", url=str(private))
    assert not refused["ok"] and asked == [(str(private), "open")]

    # The person opens it themselves; the agent may see that a tab exists,
    # not what is in it, until it asks.
    view = pane.open(private.as_uri())
    deadline = time.monotonic() + 15
    while view.is_loading() or not view.get_title():
        GLib.MainContext.default().iteration(False)
        assert time.monotonic() < deadline
    tab = _call(pane, "browser_tabs")["tabs"][0]
    assert tab["title"] == "(not shared)" and "private" not in tab["url"]
    assert not _call(pane, "browser_read", tab=tab["tab"])["ok"]
    assert asked[-1] == (str(private), "read")


def test_project_files_cannot_reach_outside_through_symlinks_or_dotdot(tmp_path, monkeypatch):
    (tmp_path / "proj").mkdir()
    secret = tmp_path / "secret.html"
    secret.write_text("<title>Secret</title><p>SECRET-OUTSIDE</p>")
    (tmp_path / "proj" / "link.html").symlink_to(secret)
    (tmp_path / "proj" / "index.html").write_text(
        '<title>Index</title><p>inside</p><iframe src="../secret.html"></iframe>'
        f'<iframe src="{secret.as_uri()}"></iframe>')
    pane = _pane(tmp_path, monkeypatch)
    asked = []
    def deny(scope, args, verb, proceed, reply):
        if not scope:
            return proceed()
        asked.append(scope)
        reply({"ok": False, "error": "declined"})

    monkeypatch.setattr(pane, "_consent", deny)

    # resolve() follows the link to the real file, which is outside: it asks.
    assert not _call(pane, "browser_open", url="link.html")["ok"]
    assert asked == [str(secret)]
    index = _call(pane, "browser_open", url="index.html")
    assert index["ok"] and index["title"] == "Index", index
    read = _call(pane, "browser_read", tab=index["tab"])
    assert "inside" in read["text"] and "SECRET" not in read["text"]


class _Request:
    def __init__(self, uri, view_uri):
        from urllib.parse import urlsplit

        self._uri, self.view_uri, self.result = uri, view_uri, None
        self._path = urlsplit(uri).path

    def get_uri(self):
        return self._uri

    def get_path(self):
        return self._path

    def get_web_view(self):
        return type("View", (), {"get_uri": lambda _self: self.view_uri})()

    def finish(self, stream, length, mime):
        self.result = ("ok", stream.read_bytes(length, None).get_data(), mime)

    def finish_error(self, error):
        self.result = ("error", error.message)


def test_a_project_root_is_served_only_to_its_own_tab_and_only_inside(tmp_path):
    from helios.backend.browser_target import localize
    from helios.widgets.browser_pane import _serve_project_file

    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "notes.log").write_text("plain text")
    (tmp_path / "secret").write_text("SECRET")
    (tmp_path / "a" / "out").symlink_to(tmp_path / "secret")
    page = localize((tmp_path / "a" / "notes.log").as_uri(), str(tmp_path / "a"))
    key = page.split("/")[2]

    ok = _Request(page, f"helios-file://{key}/index.html")
    _serve_project_file(ok)
    assert ok.result == ("ok", b"plain text", "text/plain; charset=utf-8")
    for uri, view_uri in [
        (page, "helios-file://someotherkey/frame.html"),       # another chat's tab
        (f"helios-file://{key}/out", f"helios-file://{key}/"),  # symlink leaving the root
        (f"helios-file://{key}/%00x", f"helios-file://{key}/"),  # NUL: ValueError, not OSError
        (f"helios-file://{key}/", f"helios-file://{key}/"),     # a directory
    ]:
        request = _Request(uri, view_uri)
        _serve_project_file(request)
        assert request.result[0] == "error", uri


def test_text_without_a_known_type_is_shown_as_text():
    from helios.widgets.browser_pane import _mime

    assert _mime("/p/build.log", b"line 1\n") == "text/plain; charset=utf-8"
    assert _mime("/p/main.rs", "fn main() {} // é".encode()) == "text/plain; charset=utf-8"
    assert _mime("/p/report.html", b"<h1>") == "text/html"
    assert _mime("/p/blob.bin", b"\x00\x01binary") == "application/octet-stream"
