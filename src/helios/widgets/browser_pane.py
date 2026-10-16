"""Tabbed WebKit browser pane that agents open reports and dev servers in.

The person browses it like any browser (tabs, back/forward/reload, an address
bar that also takes file paths and ``localhost:PORT``). Agents reach it through
``helios-browser-mcp`` → D-Bus → :meth:`BrowserPane.handle_call`. Reading or
screenshotting a non-local page asks the person first, per host, because the
pane keeps its own sign-ins and a remote page may show their data.
"""

from __future__ import annotations

import base64
import json
from urllib.parse import urlsplit
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from helios import paths  # noqa: E402
from helios.backend import ui_state  # noqa: E402
from helios.backend.browser_target import TargetError, consent_scope, is_web_scope, resolve  # noqa: E402
from helios.log import get_logger  # noqa: E402

_log = get_logger("browser")

ALLOWED_HOSTS_KEY = "browser_allowed_hosts"
_LOAD_TIMEOUT_MS = 20_000
# Below the MCP server's own timeout, so a wedged page still gets an answer.
_CALL_DEADLINE_MS = 30_000
# OpenRouter's in-process MCP client caps a message at 1 MiB of JSON.
_MAX_PNG_BYTES = 600_000
_DEFAULT_READ_CHARS = 20_000
# An isolated script world: the page's own JS cannot shadow what we call.
_READ_SCRIPT = (
    "JSON.stringify({title: document.title, url: location.href,"
    " text: document.body ? document.body.innerText : ''})"
)

Reply = Callable[[dict], None]


def _once(reply: Reply) -> Reply:
    sent: list[bool] = []

    def once(result: dict) -> None:
        if not sent:
            sent.append(True)
            reply(result)

    return once


def _guarded(reply: Reply, fn: Callable, *args) -> None:
    """Run a deferred step; any exception still answers the agent."""
    try:
        fn(*args)
    except Exception as exc:
        _log.exception("browser tool step failed")
        reply({"ok": False, "error": str(exc)})


def _deadline(reply: Reply) -> None:
    def expire() -> bool:
        reply({"ok": False, "error": "The page did not answer in time."})
        return False

    GLib.timeout_add(_CALL_DEADLINE_MS, expire)


def _always_allowed() -> set[str]:
    return set(ui_state.store().get(ALLOWED_HOSTS_KEY, []) or [])


def _bounded_png(png: bytes) -> bytes:
    """Halve a large screenshot until it fits every provider's message limit."""
    if len(png) <= _MAX_PNG_BYTES:
        return png
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf

    loader = GdkPixbuf.PixbufLoader.new_with_type("png")
    loader.write(png)
    loader.close()
    image = loader.get_pixbuf()
    while len(png) > _MAX_PNG_BYTES and image.get_width() > 200:
        image = image.scale_simple(image.get_width() // 2, image.get_height() // 2,
                                   GdkPixbuf.InterpType.BILINEAR)
        png = image.save_to_bufferv("png", [], [])[1]
    return png


def _load_webkit():
    try:
        gi.require_version("WebKit", "6.0")
        from gi.repository import WebKit
    except (ImportError, ValueError):
        return None
    return WebKit


class BrowserPane(Gtk.Box):
    def __init__(
        self,
        *,
        on_reveal: Callable[[], None],
        on_close: Callable[[], None],
        cwd: Callable[[], str],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("helios-browser-pane")
        self._on_reveal = on_reveal
        #: The selected chat's folder; relative paths in the address bar resolve here.
        self._cwd = cwd
        self._views: dict[str, object] = {}
        self._next_id = 1
        self._waiters: dict[object, list[Callable[[str], None]]] = {}
        self._webkit = _load_webkit()
        self.available = self._webkit is not None
        if not self.available:
            page = Adw.StatusPage(
                icon_name="web-browser-symbolic",
                title="Browser unavailable",
                description="Install gir1.2-webkit-6.0 and restart Helios.",
            )
            page.set_vexpand(True)
            self.append(page)
            return

        state = paths.state_dir() / "browser"
        # Persistent, and separate from the person's own browser profile.
        self._session = self._webkit.NetworkSession.new(str(state / "data"), str(state / "cache"))

        bar = Gtk.Box(spacing=4)
        bar.add_css_class("toolbar")
        self._back = self._button(bar, "go-previous-symbolic", "Back", lambda: self._go("back"))
        self._forward = self._button(bar, "go-next-symbolic", "Forward", lambda: self._go("forward"))
        self._reload = self._button(bar, "view-refresh-symbolic", "Reload", lambda: self._go("reload"))
        self._address = Gtk.Entry(hexpand=True, placeholder_text="File path, localhost:PORT or URL")
        self._address.update_property([Gtk.AccessibleProperty.LABEL], ["Address"])
        self._address.connect("activate", self._on_address_activate)
        bar.append(self._address)
        self._button(bar, "tab-new-symbolic", "New tab", self._new_blank_tab)
        self._external = self._button(bar, "external-link-symbolic", "Open in your browser", self._open_external)
        self._button(bar, "window-close-symbolic", "Hide browser (Ctrl+Shift+B)", on_close)
        self.append(bar)

        self._tab_view = Adw.TabView()
        self._tab_view.connect("notify::selected-page", lambda *_: self._sync_toolbar())
        self._tab_view.connect("page-detached", self._on_page_detached)
        tab_bar = Adw.TabBar(view=self._tab_view, autohide=True)
        self.append(tab_bar)

        self._stack = Gtk.Stack(vexpand=True)
        self._stack.add_named(
            Adw.StatusPage(
                icon_name="web-browser-symbolic",
                title="Browser",
                description="Agents open reports, local files and dev servers here. "
                            "Type a path or URL above to open one yourself.",
            ),
            "empty",
        )
        self._stack.add_named(self._tab_view, "tabs")
        self.append(self._stack)
        self._sync_toolbar()

    # ── public ──────────────────────────────────────────────────────────

    def open(self, url: str, *, new_tab: bool = True, on_loaded: Callable | None = None):
        """Load ``url`` and select its tab; ``on_loaded(view, status)`` once it settles."""
        view = self._selected_view()
        if new_tab or view is None:
            view = self._add_tab(self._new_view())
        settled = (lambda status: on_loaded(view, status)) if on_loaded else None
        self._load(view, lambda: view.load_uri(url), settled)
        self._tab_view.set_selected_page(self._tab_view.get_page(view))
        return view

    def handle_call(self, tool: str, args: dict, reply: Reply) -> None:
        """One agent tool call. Always replies exactly once."""
        reply = _once(reply)
        if not self.available:
            reply({"ok": False, "error": "The Helios browser pane needs gir1.2-webkit-6.0."})
            return
        handler = getattr(self, f"_tool_{tool.removeprefix('browser_')}", None)
        if handler is None:
            reply({"ok": False, "error": f"Unknown browser tool: {tool}"})
            return
        _guarded(reply, handler, args, reply)

    # ── agent tools ─────────────────────────────────────────────────────

    def _tool_open(self, args: dict, reply: Reply) -> None:
        if not args.get("url"):
            # Claude defers MCP schemas, so a first call may guess the name.
            reply({"ok": False, "error": 'browser_open needs "url": a file path, localhost:PORT or URL.'})
            return
        cwd = self._call_cwd(args)
        try:
            url = resolve(str(args["url"]), cwd)
        except TargetError as exc:
            reply({"ok": False, "error": str(exc)})
            return

        def go() -> None:
            self._on_reveal()
            view = self.open(url, new_tab=args.get("new_tab", True) is not False,
                             on_loaded=lambda view, status: reply(self._describe(view, status, cwd)))
            view._helios_grant = consent_scope(url, cwd)

        self._consent(consent_scope(url, cwd), args, "open", go, reply)

    def _tool_navigate(self, args: dict, reply: Reply) -> None:
        view = self._view_for(args, reply)
        if view is None:
            return
        cwd = self._call_cwd(args)
        action, target = args.get("action"), args.get("url")
        done = lambda status: reply(self._describe(view, status, cwd))  # noqa: E731
        if target:
            try:
                url = resolve(str(target), cwd)
            except TargetError as exc:
                reply({"ok": False, "error": str(exc)})
                return

            def go() -> None:
                if self._alive(view, reply):
                    view._helios_grant = consent_scope(url, cwd)
                    self._load(view, lambda: view.load_uri(url), done)

            self._consent(consent_scope(url, cwd), args, "open", go, reply)
        # History moves revisit pages this tab already showed; what they land
        # on is redacted in the reply unless it is shareable.
        elif action == "back" and view.can_go_back():
            self._load(view, view.go_back, done)
        elif action == "forward" and view.can_go_forward():
            self._load(view, view.go_forward, done)
        elif action == "reload":
            self._load(view, view.reload, done)
        else:
            reply({"ok": False, "error": "Give a url, or an action this tab can take (back, forward, reload)."})

    def _tool_read(self, args: dict, reply: Reply) -> None:
        view = self._view_for(args, reply)
        if view is None:
            return
        cwd = self._call_cwd(args)
        limit = args.get("max_chars")
        limit = limit if isinstance(limit, int) and limit > 0 else _DEFAULT_READ_CHARS

        def finished(source, result) -> None:
            try:
                page = json.loads(source.evaluate_javascript_finish(result).to_string())
            except Exception as exc:
                reply({"ok": False, "error": f"Could not read the page: {exc}"})
                return
            if not self._shareable(view, str(page.get("url") or ""), cwd):
                reply({"ok": False, "error": "The page changed while it was being read; try again."})
                return
            text = str(page.get("text") or "")
            reply({"ok": True, "tab": view._helios_tab_id, "title": page.get("title") or "",
                   "url": page.get("url") or "", "text": text[:limit], "truncated": len(text) > limit})

        def run() -> None:
            if self._alive(view, reply):
                _deadline(reply)
                view.evaluate_javascript(_READ_SCRIPT, -1, "helios", None, None, finished)

        self._consent_for_view(view, args, "read", run, reply)

    def _tool_screenshot(self, args: dict, reply: Reply) -> None:
        view = self._view_for(args, reply)
        if view is None:
            return
        cwd = self._call_cwd(args)
        WebKit = self._webkit
        shown: list[str] = []

        def finished(source, result) -> None:
            try:
                png = _bounded_png(source.get_snapshot_finish(result).save_to_png_bytes().get_data())
            except Exception as exc:
                reply({"ok": False, "error": f"Could not capture the page: {exc}"})
                return
            if (view.get_uri() or "") != shown[0]:
                reply({"ok": False, "error": "The page changed during the screenshot; try again."})
                return
            reply({**self._describe(view, "loaded", cwd), "png_base64": base64.b64encode(png).decode()})

        def snap() -> bool:
            if self._alive(view, reply) and self._shareable(view, view.get_uri() or "", cwd, reply):
                shown.append(view.get_uri() or "")
                view.get_snapshot(WebKit.SnapshotRegion.VISIBLE, WebKit.SnapshotOptions.NONE, None, finished)
            return False

        def capture() -> None:
            if not self._alive(view, reply):
                return
            # Only a mapped view paints, so show the pane and this tab first.
            self._on_reveal()
            self._tab_view.set_selected_page(self._tab_view.get_page(view))
            _deadline(reply)
            GLib.timeout_add(150, lambda: _guarded(reply, snap) or False)

        self._consent_for_view(view, args, "see", capture, reply)

    def _tool_tabs(self, args: dict, reply: Reply) -> None:
        cwd = self._call_cwd(args)
        selected = self._selected_view()
        reply({"ok": True, "tabs": [
            {**self._describe(view, "loading" if view.is_loading() else "loaded", cwd),
             "selected": view is selected}
            for view in self._views.values()
        ]})

    # ── consent ─────────────────────────────────────────────────────────
    #
    # Same rule as the Claude desktop browser: the project's files and
    # loopback servers are the agent's own work; any other site (which may hold
    # the person's sign-ins) or file asks first. A yes covers that tab while it
    # stays on the same site; "Always" is per host and persisted.

    def _call_cwd(self, args: dict) -> str:
        return str(args.get("_cwd") or self._cwd() or "")

    def _shareable(self, view, url: str, cwd: str, reply: Reply | None = None) -> bool:
        scope = consent_scope(url, cwd)
        ok = not scope or scope == getattr(view, "_helios_grant", None) or scope in _always_allowed()
        if not ok and reply is not None:
            reply({"ok": False, "error": "The page changed; ask again to use it."})
        return ok

    def _consent_for_view(self, view, args: dict, verb: str, proceed: Callable[[], None], reply: Reply) -> None:
        cwd = self._call_cwd(args)
        if self._shareable(view, view.get_uri() or "", cwd):
            proceed()
            return
        scope = consent_scope(view.get_uri() or "", cwd)

        def granted() -> None:
            # Consent was for what the person was shown, not wherever the tab
            # went while they decided.
            if not self._alive(view, reply):
                return
            if consent_scope(view.get_uri() or "", cwd) != scope:
                reply({"ok": False, "error": "The page changed while waiting for consent; try again."})
                return
            view._helios_grant = scope
            proceed()

        self._consent(scope, args, verb, granted, reply)

    def _consent(self, scope: str, args: dict, verb: str, proceed: Callable[[], None], reply: Reply) -> None:
        if not scope or scope in _always_allowed():
            proceed()
            return
        agent = " ".join(str(args.get("_agent") or "The agent").split())[:40]
        if is_web_scope(scope):
            heading = f"Let {agent} {verb} {scope}?"
            body = ("It can load this site and see its text and screenshots, including "
                    "anything you are signed in to in the Helios browser.")
        elif scope.startswith("/"):
            heading = f"Let {agent} {verb} a file outside the project?"
            body = scope
        else:
            heading = f"Let {agent} {verb} this page?"
            body = scope[:300]
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("deny", "Deny")
        dialog.add_response("once", "Allow")
        if is_web_scope(scope):
            dialog.add_response("always", f"Always allow {scope}")
            dialog.set_response_appearance("always", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("deny")
        dialog.set_close_response("deny")

        def answered(_dialog, response: str) -> None:
            if response == "deny":
                reply({"ok": False, "error": f"The person declined access to {scope}."})
                return
            if response == "always":
                # Re-read: another dialog may have added a host meanwhile.
                ui_state.store().set(ALLOWED_HOSTS_KEY, sorted(_always_allowed() | {scope}))
            _guarded(reply, proceed)

        dialog.connect("response", answered)
        self._on_reveal()
        dialog.present(self)

    # ── tabs and loading ────────────────────────────────────────────────

    def _new_view(self, related=None):
        WebKit = self._webkit
        view = WebKit.WebView(related_view=related) if related else WebKit.WebView(network_session=self._session)
        view.set_hexpand(True)
        view.set_vexpand(True)
        settings = view.get_settings()
        settings.set_enable_developer_extras(True)
        # Agent-written reports often omit <meta charset>; WebKit would read
        # them as Latin-1 and turn every "→" into mojibake.
        settings.set_default_charset("utf-8")
        view.connect("load-changed", self._on_load_changed)
        view.connect("load-failed", self._on_load_failed)
        view.connect("create", self._on_create)
        for prop in ("title", "uri", "is-loading"):
            view.connect(f"notify::{prop}", self._on_view_changed)
        return view

    def _add_tab(self, view):
        view._helios_tab_id = str(self._next_id)
        self._next_id += 1
        self._views[view._helios_tab_id] = view
        page = self._tab_view.append(view)
        page.set_title("New tab")
        self._stack.set_visible_child_name("tabs")
        return view

    def _load(self, view, start: Callable[[], None], on_loaded: Callable[[str], None] | None) -> None:
        """Start a navigation and call ``on_loaded`` once it settles.

        Registered before ``start`` because WebKit reports is-loading
        asynchronously: checking it right after load_uri() reads False.
        """
        if on_loaded is not None:
            fired = []

            def settle(status: str) -> None:
                if not fired:
                    fired.append(status)
                    on_loaded(status)

            def give_up() -> bool:
                waiters = self._waiters.get(view, [])
                if settle in waiters:
                    waiters.remove(settle)
                settle("still loading")
                return False

            self._waiters.setdefault(view, []).append(settle)
            GLib.timeout_add(_LOAD_TIMEOUT_MS, give_up)
        start()

    def _settle(self, view, status: str) -> None:
        for waiter in self._waiters.pop(view, []):
            waiter(status)

    def _on_load_changed(self, view, event) -> None:
        if event == self._webkit.LoadEvent.FINISHED:
            # A first load in a fresh web process sets the title just after
            # FINISHED; give it a moment so the agent is told what loaded.
            GLib.timeout_add(0 if view.get_title() else 200,
                             lambda: self._settle(view, "loaded") and False)
        self._sync_toolbar()

    def _on_load_failed(self, view, _event, uri, error) -> bool:
        # A navigation superseded by the next one reports CANCELLED; the
        # waiters belong to the navigation that replaced it.
        if not error.matches(self._webkit.network_error_quark(), int(self._webkit.NetworkError.CANCELLED)):
            self._settle(view, f"failed: {error.message}")
        return False  # let WebKit show its error page

    def _on_create(self, view, _action):
        """Links that target a new window open as a tab beside their opener."""
        child = self._add_tab(self._new_view(related=view))
        self._tab_view.set_selected_page(self._tab_view.get_page(child))
        return child

    def _on_view_changed(self, view, _pspec) -> None:
        page = self._tab_view.get_page(view)
        if page is not None:
            page.set_title(view.get_title() or view.get_uri() or "New tab")
            page.set_loading(view.is_loading())
        if view is self._selected_view():
            self._sync_toolbar()

    def _on_page_detached(self, _tab_view, page, _position) -> None:
        view = page.get_child()
        self._views.pop(getattr(view, "_helios_tab_id", ""), None)
        self._settle(view, "closed")
        if not self._views:
            self._stack.set_visible_child_name("empty")
        self._sync_toolbar()

    # ── toolbar ─────────────────────────────────────────────────────────

    def _button(self, bar: Gtk.Box, icon: str, tooltip: str, callback) -> Gtk.Button:
        button = Gtk.Button.new_from_icon_name(icon)
        button.add_css_class("flat")
        button.set_tooltip_text(tooltip)
        button.connect("clicked", lambda *_: callback())
        bar.append(button)
        return button

    def _selected_view(self):
        page = self._tab_view.get_selected_page()
        return page.get_child() if page is not None else None

    def _sync_toolbar(self) -> None:
        view = self._selected_view()
        self._back.set_sensitive(bool(view and view.can_go_back()))
        self._forward.set_sensitive(bool(view and view.can_go_forward()))
        self._reload.set_sensitive(view is not None)
        self._external.set_sensitive(bool(view and (view.get_uri() or "").startswith(("http", "file"))))
        if not self._address.has_focus():
            self._address.set_text((view.get_uri() or "") if view else "")

    def _go(self, action: str) -> None:
        view = self._selected_view()
        if view is not None:
            {"back": view.go_back, "forward": view.go_forward, "reload": view.reload}[action]()

    def _on_address_activate(self, entry: Gtk.Entry) -> None:
        try:
            url = resolve(entry.get_text(), self._cwd())
        except TargetError as exc:
            entry.add_css_class("error")
            entry.set_tooltip_text(str(exc))
            return
        entry.remove_css_class("error")
        entry.set_tooltip_text(None)
        self.open(url, new_tab=self._selected_view() is None)
        self._selected_view().grab_focus()

    def _new_blank_tab(self) -> None:
        view = self._add_tab(self._new_view())
        self._tab_view.set_selected_page(self._tab_view.get_page(view))
        self._address.set_text("")
        self._address.grab_focus()

    def _open_external(self) -> None:
        view = self._selected_view()
        if view is not None and view.get_uri():
            Gtk.UriLauncher.new(view.get_uri()).launch(self.get_root(), None, None)

    # ── helpers ─────────────────────────────────────────────────────────

    def _view_for(self, args: dict, reply: Reply):
        tab = args.get("tab")
        view = self._views.get(str(tab)) if tab else self._selected_view()
        if view is None:
            reply({"ok": False, "error": "No such tab; call browser_open first." if tab is None
                   else f"No tab {tab}; call browser_tabs for the open ones."})
        return view

    def _alive(self, view, reply: Reply) -> bool:
        if view is not None and self._views.get(getattr(view, "_helios_tab_id", "")) is view:
            return True
        reply({"ok": False, "error": "The tab was closed."})
        return False

    def _describe(self, view, status: str, cwd: str) -> dict:
        if view is None or self._views.get(getattr(view, "_helios_tab_id", "")) is not view:
            return {"ok": False, "error": "The tab was closed."}
        url, title = view.get_uri() or "", view.get_title() or ""
        if not self._shareable(view, url, cwd):
            # A page the person reached themselves: say where, not what.
            parts = urlsplit(url)
            url = f"{parts.scheme}://{parts.hostname}/" if parts.hostname else "(not shared)"
            title = "(not shared)"
        return {"ok": True, "tab": view._helios_tab_id, "title": title, "url": url, "status": status}
