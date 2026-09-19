"""Dialog for handing the current session off to the shared scratchpad.

Presents key / summary / TTL / include-tail, prefilled from the session
(backend/session_handoff.py), and performs the write on a daemon thread so a
slow or down scratchpad never hitches the UI. On failure the dialog
re-presents itself with the user's edits intact — a typed summary must never
be lost to a network blip.

Follows the question_dialog.py shape: a module-level `present_*` function,
Adw.AlertDialog, callbacks back to the caller.
"""

from __future__ import annotations

import threading
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from helios.backend import scratchpad, session_handoff
from helios.log import get_logger

_log = get_logger("handoff")

_RESPONSE_WRITE = "write"
_RESPONSE_CANCEL = "cancel"

_TTL_CHOICES: list[tuple[str, float]] = [
    ("24 hours", 24.0),
    ("48 hours", 48.0),
    ("72 hours", 72.0),
    ("1 week", 168.0),
]
_DEFAULT_TTL_INDEX = 1  # 48h — matches the MCP scratch_handoff default.


def present_handoff_dialog(
    parent: Gtk.Widget,
    session,
    on_written: Callable[[str], None],
    on_error: Callable[[str], None],
    *,
    prefill: dict | None = None,
) -> None:
    """Show the hand-off dialog for `session` (a local projects.Session).

    `on_written(key)` fires after a successful write; `on_error(message)`
    after a failed one (the dialog also re-opens itself with the same values
    so nothing typed is lost)."""
    if getattr(parent, "_destroyed", False):
        return
    title = session.ensure_title()
    if prefill:
        key0 = prefill.get("key", "")
        summary0 = prefill.get("summary", "")
        ttl_index0 = int(prefill.get("ttl_index", _DEFAULT_TTL_INDEX))
        tail0 = bool(prefill.get("include_tail", True))
        recipient0 = prefill.get(
            "recipient_provider",
            session_handoff.default_recipient_provider(
                session.session_id,
                session.path,
            ),
        )
    else:
        key0 = session_handoff.suggest_key(title, session.session_id)
        summary0 = session_handoff.default_summary(session, title)
        ttl_index0 = _DEFAULT_TTL_INDEX
        tail0 = True
        recipient0 = session_handoff.default_recipient_provider(
            session.session_id,
            session.path,
        )

    dialog = Adw.AlertDialog.new(
        "Hand Off Session",
        "Write this session to the shared scratchpad so any Helios session "
        "on the tailnet can pick it up.",
    )
    dialog.add_response(_RESPONSE_CANCEL, "Cancel")
    dialog.add_response(_RESPONSE_WRITE, "Write to Scratchpad")
    dialog.set_response_appearance(_RESPONSE_WRITE, Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response(_RESPONSE_WRITE)
    dialog.set_close_response(_RESPONSE_CANCEL)

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)

    group = Adw.PreferencesGroup()
    key_row = Adw.EntryRow(title="Key")
    key_row.set_text(key0)
    group.add(key_row)

    ttl_row = Adw.ComboRow(title="Expires after")
    ttl_row.set_model(Gtk.StringList.new([label for label, _ in _TTL_CHOICES]))
    ttl_row.set_selected(ttl_index0)
    group.add(ttl_row)

    recipient_choices = [
        (
            session_handoff.provider_label(
                session_handoff.default_recipient_provider(
                    session.session_id,
                    session.path,
                )
            ),
            session_handoff.default_recipient_provider(
                session.session_id,
                session.path,
            ),
        ),
        ("Claude", "anthropic"),
        ("GPT", "openai"),
        ("OpenRouter", "openrouter"),
    ]
    seen = set()
    recipient_choices = [
        choice for choice in recipient_choices
        if not (choice[1] in seen or seen.add(choice[1]))
    ]
    recipient_row = Adw.ComboRow(title="Contact")
    recipient_row.set_subtitle("Target agent for this handoff")
    recipient_row.set_model(Gtk.StringList.new([label for label, _ in recipient_choices]))
    selected_recipient = 0
    for i, (_label, provider) in enumerate(recipient_choices):
        if provider == recipient0:
            selected_recipient = i
            break
    recipient_row.set_selected(selected_recipient)
    group.add(recipient_row)

    tail_row = Adw.SwitchRow(title="Include transcript tail")
    tail_row.set_subtitle(
        f"Last {session_handoff.TAIL_TURNS} messages, text only, truncated"
    )
    tail_row.set_active(tail0)
    group.add(tail_row)
    box.append(group)

    summary_label = Gtk.Label(label="Summary", xalign=0)
    summary_label.add_css_class("caption")
    summary_label.add_css_class("dim-label")
    box.append(summary_label)

    summary_view = Gtk.TextView()
    summary_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
    summary_view.set_top_margin(8)
    summary_view.set_bottom_margin(8)
    summary_view.set_left_margin(8)
    summary_view.set_right_margin(8)
    summary_view.get_buffer().set_text(summary0)

    summary_scroller = Gtk.ScrolledWindow()
    summary_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    summary_scroller.set_min_content_height(110)
    summary_scroller.set_max_content_height(220)
    summary_scroller.set_child(summary_view)
    summary_scroller.add_css_class("card")
    box.append(summary_scroller)

    dialog.set_extra_child(box)

    # An empty key has nowhere to go — gate the write response on it live.
    def _sync_write_enabled(*_a) -> None:
        if getattr(parent, "_destroyed", False):
            return
        dialog.set_response_enabled(_RESPONSE_WRITE, bool(key_row.get_text().strip()))

    key_row.connect("changed", _sync_write_enabled)
    _sync_write_enabled()

    def _on_response(_dlg, response: str) -> None:
        if getattr(parent, "_destroyed", False) or response != _RESPONSE_WRITE:
            return
        key = key_row.get_text().strip()
        buf = summary_view.get_buffer()
        summary = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip()
        ttl_index = min(ttl_row.get_selected(), len(_TTL_CHOICES) - 1)
        ttl_hours = _TTL_CHOICES[ttl_index][1]
        include_tail = tail_row.get_active()
        recipient_index = min(recipient_row.get_selected(), len(recipient_choices) - 1)
        recipient_provider = recipient_choices[recipient_index][1]

        def worker() -> None:
            try:
                data = session_handoff.build_payload(
                    session,
                    title,
                    include_tail=include_tail,
                    recipient_provider=recipient_provider,
                )
                source_provider = data.get("from_provider", "")
                scratchpad.write_entry(
                    key,
                    data,
                    summary=summary,
                    tags=session_handoff.contact_tags(
                        source_provider,
                        recipient_provider,
                    ),
                    ttl_hours=ttl_hours,
                    created_by=f"helios@{session_handoff.host_name()}",
                )
                err = ""
            except scratchpad.ScratchpadError as e:
                err = str(e)
            except Exception as e:  # payload build must never crash the UI
                _log.exception("handoff payload build failed")
                err = f"unexpected error: {e}"
            if not getattr(parent, "_destroyed", False):
                GLib.idle_add(
                    _finish_handoff,
                    parent,
                    session,
                    on_written,
                    on_error,
                    key,
                    summary,
                    ttl_index,
                    include_tail,
                    recipient_provider,
                    err,
                )

        threading.Thread(target=worker, name="helios-handoff-write", daemon=True).start()

    dialog.connect("response", _on_response)
    dialog.present(parent)


def _finish_handoff(
    parent: Gtk.Widget,
    session,
    on_written: Callable[[str], None],
    on_error: Callable[[str], None],
    key: str,
    summary: str,
    ttl_index: int,
    include_tail: bool,
    recipient_provider: str,
    err: str,
) -> bool:
    """Deliver a completed durable write only while its parent is alive."""
    if getattr(parent, "_destroyed", False):
        return False
    if not err:
        on_written(key)
        return False
    on_error(err)
    if getattr(parent, "_destroyed", False):
        return False
    # Re-present with the user's edits — the write can be retried once the
    # scratchpad is back. The write itself always finishes in the worker.
    present_handoff_dialog(
        parent,
        session,
        on_written,
        on_error,
        prefill={
            "key": key,
            "summary": summary,
            "ttl_index": ttl_index,
            "include_tail": include_tail,
            "recipient_provider": recipient_provider,
        },
    )
    return False
