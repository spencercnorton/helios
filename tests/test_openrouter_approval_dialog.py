"""Real approval actions, complete command display and constrained windows."""

import os
import subprocess
import time
from pathlib import Path

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk

from helios.widgets.question_dialog import present_question


COMMAND = "python3 - <<'PY'\n" + "print('inspection result')\n" * 80 + "# final command line\nPY"
EXACT = "Allow this exact command for this session"


def _payload():
    return {
        "presentation": "tool-approval", "allowOther": False, "requireExplicitChoice": True,
        "caption": "Working directory: /home/example/project",
        "grantScope": (
            "Session grants reset when this conversation closes or its permissions change. "
            "Only this exact command in this working directory is covered; "
            "it runs without a filesystem sandbox and its inputs may change."
        ),
        "approvalButtonLabels": {EXACT: "Allow exact command"},
        "detail": {"kind": "code", "title": "Command", "text": COMMAND},
        "questions": [{
            "header": "Tool approval",
            "question": "Allow OpenRouter to use Bash?\n\nOutput leaves this machine.\nSent to: DeepSeek via Test Provider",
            "options": ["Allow once", EXACT, "Deny"],
        }],
    }


def _walk(widget):
    yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from _walk(child)
        child = child.get_next_sibling()


def _settle():
    deadline = time.monotonic() + 0.25
    context = GLib.MainContext.default()
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        time.sleep(0.005)


@pytest.fixture
def show_dialog():
    Adw.init()
    windows = []

    def show(payload=None, width=900, height=700):
        window = Adw.Window(default_width=width, default_height=height)
        window.set_content(Gtk.Box())
        window.present()
        answers, dismissed = [], []
        dialog = present_question(window, payload or _payload(), answers.append,
                                  lambda: dismissed.append(True))
        windows.append(window)
        _settle()
        return window, dialog, answers, dismissed

    yield show
    for window in windows:
        window.destroy()
    _settle()


def test_approval_keeps_the_complete_command_out_of_the_alert_paragraph(show_dialog):
    _window, dialog, _answers, _dismissed = show_dialog()
    assert not isinstance(dialog, Adw.AlertDialog)
    widgets = list(_walk(dialog.get_child()))
    assert not any(isinstance(widget, Gtk.CheckButton) for widget in widgets)
    views = [widget for widget in widgets if isinstance(widget, Gtk.TextView)]
    assert len(views) == 1
    buffer = views[0].get_buffer()
    assert buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True) == COMMAND
    assert views[0].get_monospace() and not views[0].get_editable()
    assert any(isinstance(widget, Gtk.Label) and "Session grants reset" in widget.get_text()
               for widget in widgets)


@pytest.mark.parametrize("answer,label", [("Allow once", "Allow once"), (EXACT, "Allow exact command")])
def test_one_explicit_button_click_returns_the_original_grant(show_dialog, answer, label):
    window, _dialog, answers, dismissed = show_dialog()
    button = next(widget for widget in _walk(window)
                  if isinstance(widget, Gtk.Button) and widget.get_label() == label)
    button.emit("clicked")
    _settle()
    assert answers == [answer]
    assert not dismissed


@pytest.mark.parametrize("response", ["default", "close", "force_close"])
def test_default_dismissal_and_late_responses_cannot_grant(show_dialog, response):
    window, dialog, answers, dismissed = show_dialog()
    assert dialog.get_default_widget().get_label() == "Deny"
    allow = next(widget for widget in _walk(window)
                 if isinstance(widget, Gtk.Button) and widget.get_label() == "Allow once")
    if response == "default":
        dialog.get_default_widget().emit("clicked")
    else:
        getattr(dialog, response)()
    _settle()
    allow.emit("clicked")
    assert not answers
    assert dismissed == [True]


def test_native_question_ids_keep_their_answer_map(show_dialog):
    payload = _payload()
    payload["questions"][0]["id"] = "approval-id"
    window, _dialog, answers, _dismissed = show_dialog(payload)
    button = next(widget for widget in _walk(window)
                  if isinstance(widget, Gtk.Button) and widget.get_label() == "Allow once")
    button.emit("clicked")
    assert answers == [{"approval-id": {"answers": ["Allow once"]}}]


@pytest.mark.parametrize("width,height", [(900, 700), (460, 600)])
def test_consent_buttons_fit_the_window_with_a_long_command(show_dialog, width, height):
    window, dialog, _answers, _dismissed = show_dialog(width=width, height=height)
    assert dialog.get_child().get_width() >= min(width - 120, 550)
    labels = {"Deny", "Allow once", "Allow exact command"}
    buttons = [widget for widget in _walk(window)
               if isinstance(widget, Gtk.Button) and widget.get_label() in labels]
    assert len(buttons) == 3
    for button in buttons:
        ok, bounds = button.compute_bounds(window)
        assert ok and button.get_mapped()
        assert bounds.get_x() >= 0 and bounds.get_y() >= 0
        assert bounds.get_x() + bounds.get_width() <= window.get_width()
        assert bounds.get_y() + bounds.get_height() <= window.get_height()
    # Optional local visual evidence, never needed by CI or the live desktop.
    output = os.environ.get("HELIOS_APPROVAL_SCREENSHOTS")
    if output:
        assert os.environ.get("DISPLAY"), "Screenshot requires the isolated Xvfb display"
        destination = Path(output) / f"approval-{width}x{height}.png"
        destination.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["import", "-window", "root", str(destination)], check=True)
    assert len([widget for widget in _walk(dialog.get_child())
                if isinstance(widget, Gtk.ScrolledWindow)]) == 1


def test_normal_questions_keep_their_select_then_submit_flow(show_dialog):
    payload = _payload()
    payload.pop("presentation")
    _window, dialog, _answers, _dismissed = show_dialog(payload)
    assert any(isinstance(widget, Gtk.CheckButton) for widget in _walk(dialog.get_extra_child()))
    assert dialog.has_response("submit")
