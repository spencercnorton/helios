"""Dialog for provider-native user questions and approvals.

Claude's ``AskUserQuestion`` and Codex App Server's
``item/tool/requestUserInput`` share the same basic shape: one to three
questions, optional choices, and an optional free-text answer.  The dialog
keeps Claude's historical string callback for a single question without an
``id``.  When every question has a unique ``id`` it instead returns the Codex
answer map::

    {"question_id": {"answers": ["selected answer"]}}

The App Server driver is responsible for wrapping that map in the protocol's
top-level ``{"answers": ...}`` response object.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Callable, TypeAlias

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk, Pango  # noqa: E402


# Allowed responses for the AlertDialog. We map them to our own
# answer-or-cancel API in the response handler.
_RESPONSE_SUBMIT = "submit"
_RESPONSE_CANCEL = "cancel"
_MAX_QUESTIONS = 3

QuestionAnswer: TypeAlias = str | dict[str, dict[str, list[str]]]


@dataclass(frozen=True)
class _QuestionSpec:
    question_id: str
    header: str
    question: str
    multi_select: bool
    options: tuple[tuple[str, str], ...]
    allow_other: bool
    is_secret: bool
    preselect_first: bool


@dataclass
class _QuestionControls:
    option_buttons: list[Gtk.CheckButton]
    other_button: Gtk.CheckButton | None = None
    other_entry: Gtk.Entry | None = None
    free_entry: Gtk.Entry | None = None
    error_label: Gtk.Label | None = None


def present_question(
    parent: Gtk.Widget,
    input_payload: dict,
    on_answer: Callable[[QuestionAnswer], None],
    on_dismiss: Callable[[], None],
) -> Adw.Dialog | None:
    """Show up to three questions in one serialized dialog.

    Choice-less questions are rendered as free-text inputs. ``allowOther``
    can be set on the payload as the default and overridden per question;
    Codex's equivalent ``isOther`` field is accepted as an alias. A question
    without choices always gets a text field, even when ``allowOther`` is
    false, because text is then the only possible answer.
    """
    questions = _normalize_questions(input_payload)
    if not questions:
        on_dismiss()
        return None

    if (
        input_payload.get("presentation") == "tool-approval"
        and len(questions) == 1
        and not questions[0].multi_select
        and not questions[0].allow_other
        and 2 <= len(questions[0].options) <= 4
    ):
        return _present_approval(parent, input_payload, questions[0], on_answer, on_dismiss)

    single = len(questions) == 1
    title = questions[0].header if single else "Questions"
    body = questions[0].question if single else "Please answer each question."
    dlg = Adw.AlertDialog.new(title, body or None)
    dlg.add_response(_RESPONSE_CANCEL, "Cancel")
    dlg.add_response(_RESPONSE_SUBMIT, "Submit")
    dlg.set_response_appearance(_RESPONSE_SUBMIT, Adw.ResponseAppearance.SUGGESTED)
    require_explicit = bool(input_payload.get("requireExplicitChoice"))
    dlg.set_default_response(
        _RESPONSE_CANCEL if require_explicit else _RESPONSE_SUBMIT
    )
    dlg.set_close_response(_RESPONSE_CANCEL)

    body_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
    body_box.set_margin_top(6)
    caption = str(input_payload.get("caption") or "").strip()
    if caption:
        why = Gtk.Label(label=caption, xalign=0)
        why.add_css_class("caption")
        why.add_css_class("dim-label")
        why.set_wrap(True)
        body_box.append(why)
    detail = input_payload.get("detail")
    if isinstance(detail, dict) and str(detail.get("text") or "").strip():
        body_box.append(_build_detail(detail))
        # AlertDialog owns its sizing. Calling the inherited Dialog width
        # setter here crashes libadwaita 1.5; approvals use a regular Dialog.
    blocking = input_payload.get("isBlocking")
    if isinstance(blocking, bool):
        state = Gtk.Label(
            label=(
                "Codex is waiting for your answer."
                if blocking
                else "Your answer can guide the current turn."
            ),
            xalign=0,
        )
        state.add_css_class("caption")
        state.add_css_class("dim-label")
        state.set_wrap(True)
        body_box.append(state)

    auto_resolution_ms = _positive_int(input_payload.get("autoResolutionMs"))
    countdown_label: Gtk.Label | None = None
    countdown_source_id = 0
    resolution_deadline = 0.0
    if auto_resolution_ms:
        resolution_deadline = time.monotonic() + (auto_resolution_ms / 1000)
        countdown_label = Gtk.Label(xalign=0)
        countdown_label.add_css_class("caption")
        countdown_label.add_css_class("dim-label")
        countdown_label.set_wrap(True)
        body_box.append(countdown_label)
    controls: list[_QuestionControls] = []
    for index, spec in enumerate(questions):
        if index:
            body_box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        controls.append(
            _build_question(spec, body_box, show_prompt=not single)
        )

    # Keep large option sets and multi-question requests within the window.
    scroller = Gtk.ScrolledWindow()
    scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroller.set_propagate_natural_height(True)
    scroller.set_max_content_height(440 if not single else 320)
    scroller.set_child(body_box)
    dlg.set_extra_child(scroller)

    def answers_complete(*_args, reveal: bool = False) -> bool:
        complete = True
        for spec, state in zip(questions, controls):
            answered = bool(_read_question_answer(spec, state))
            if state.error_label is not None:
                state.error_label.set_visible(reveal and not answered)
            complete = complete and answered
        dlg.set_response_enabled(_RESPONSE_SUBMIT, complete)
        return complete

    for state in controls:
        for button in state.option_buttons:
            button.connect("toggled", answers_complete)
        if state.other_button is not None:
            state.other_button.connect("toggled", answers_complete)
        for entry in (state.other_entry, state.free_entry):
            if entry is not None:
                entry.connect("changed", answers_complete)
    answers_complete()

    def update_countdown() -> bool:
        nonlocal countdown_source_id
        if countdown_label is None:
            return False
        remaining_ms = max(
            0,
            int((resolution_deadline - time.monotonic()) * 1000),
        )
        countdown_label.set_label(_auto_resolution_text(remaining_ms))
        if remaining_ms <= 0:
            countdown_source_id = 0
            return False
        return True

    if countdown_label is not None:
        update_countdown()
        countdown_source_id = GLib.timeout_add(250, update_countdown)

    def cancel_countdown() -> None:
        nonlocal countdown_source_id
        if countdown_source_id:
            try:
                GLib.source_remove(countdown_source_id)
            except Exception:
                pass
            countdown_source_id = 0

    def on_response(_d, response: str) -> None:
        if response != _RESPONSE_SUBMIT:
            cancel_countdown()
            on_dismiss()
            return

        if not answers_complete(reveal=True):
            # A disabled response should not normally fire, but accessibility
            # or a programmatic response may still reach here. Keep the same
            # dialog open with inline errors; an incomplete answer is never a
            # dismissal and must not become a provider-side cancellation.
            return
        cancel_countdown()
        answers = [
            _read_question_answer(spec, state)
            for spec, state in zip(questions, controls)
        ]
        on_answer(_format_answers(questions, answers))

    dlg.connect("response", on_response)
    dlg.present(parent)
    return dlg


def _approval_label(text: str) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=0, selectable=True)
    label.set_wrap(True)
    label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    label.set_max_width_chars(80)
    return label


def _present_approval(
    parent: Gtk.Widget,
    payload: dict,
    question: _QuestionSpec,
    on_answer: Callable[[QuestionAnswer], None],
    on_dismiss: Callable[[], None],
) -> Adw.Dialog:
    """Keep action details scrollable and consent buttons directly available.

    Approval choices are actions, not a questionnaire. Each button returns
    the original provider answer; neither Enter nor dismissal grants consent.
    Session scope stays outside the scroller beside the consent buttons.
    """
    # AlertDialog sizes itself for short prose regardless of content-width.
    # A regular Dialog gives the command useful width and adapts to its parent.
    dlg = Adw.Dialog(title=question.header, content_width=760, content_height=560)
    toolbar = Adw.ToolbarView()
    toolbar.add_top_bar(Adw.HeaderBar())

    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    if question.question:
        content.append(_approval_label(question.question))
    caption = str(payload.get("caption") or "").strip()
    if caption:
        label = _approval_label(caption)
        label.add_css_class("caption")
        content.append(label)
    detail = payload.get("detail")
    if isinstance(detail, dict) and str(detail.get("text") or ""):
        content.append(_build_detail(detail, scrollable=False))

    scroller = Gtk.ScrolledWindow()
    scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroller.set_vexpand(True)
    scroller.set_min_content_height(80)
    scroller.set_child(content)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    box.set_margin_start(20)
    box.set_margin_end(20)
    box.set_margin_top(12)
    box.set_margin_bottom(20)
    box.append(scroller)
    scope = str(payload.get("grantScope") or "").strip()
    if scope:
        label = _approval_label(scope)
        label.add_css_class("caption")
        box.append(label)
    handled = False

    def finish(answer: str | None) -> None:
        nonlocal handled
        if handled:
            return
        handled = True
        dlg.close()
        if answer is not None:
            on_answer(_format_answers([question], [[answer]]))
        else:
            on_dismiss()

    def on_clicked(_button, answer: str) -> None:
        finish(None if answer == "Deny" else answer)

    def on_closed(_dialog) -> None:
        finish(None)

    # FlowBox wraps complete, direct action buttons when the parent narrows.
    actions = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                          homogeneous=True, column_spacing=8, row_spacing=8)
    actions.set_min_children_per_line(1)
    actions.set_max_children_per_line(4)
    short_labels = payload.get("approvalButtonLabels") or {}
    deny = None
    dlg._helios_click_cb = on_clicked
    for answer, _description in question.options:
        label = short_labels.get(answer, answer) if isinstance(short_labels, dict) else answer
        button = Gtk.Button(label=str(label), hexpand=True)
        button.add_css_class("pill")
        if answer == "Allow once":
            button.add_css_class("suggested-action")
        if answer == "Deny":
            deny = button
        button.connect("clicked", dlg._helios_click_cb, answer)
        actions.append(button)
    box.append(actions)
    toolbar.set_content(box)
    dlg.set_child(toolbar)
    dlg._helios_closed_cb = on_closed
    dlg.connect("closed", dlg._helios_closed_cb)
    dlg.set_default_widget(deny)
    dlg.set_focus(deny)
    dlg.present(parent)
    return dlg


def _normalize_questions(input_payload: dict) -> list[_QuestionSpec]:
    """Return a bounded, sanitized representation of provider questions."""
    if not isinstance(input_payload, dict):
        return []
    raw_questions = input_payload.get("questions") or []
    if not isinstance(raw_questions, list):
        return []

    default_other = input_payload.get("allowOther", True)
    default_preselect = not bool(input_payload.get("requireExplicitChoice"))
    defaults = input_payload.get("defaults")
    if isinstance(defaults, dict) and "allowOther" in defaults:
        default_other = defaults["allowOther"]

    normalized: list[_QuestionSpec] = []
    for raw in raw_questions:
        if not isinstance(raw, dict):
            continue

        options: list[tuple[str, str]] = []
        raw_options = raw.get("options") or []
        if isinstance(raw_options, list):
            for option in raw_options:
                if isinstance(option, str):
                    # A bare string is a labelled choice with no description.
                    # openrouter_driver emits ["Allow once", "Deny"] this way;
                    # dropping them left the tool-approval dialog with zero
                    # options, which flipped allow_other on below and forced the
                    # user to retype the literal that openrouter_driver:414
                    # compares against.
                    option = {"label": option}
                if not isinstance(option, dict):
                    continue
                label = str(option.get("label") or "").strip()
                description = str(option.get("description") or "").strip()
                if label:
                    options.append((label, description))

        if "allowOther" in raw:
            allow_other = bool(raw["allowOther"])
        elif "isOther" in raw:
            allow_other = bool(raw["isOther"])
        else:
            allow_other = bool(default_other)
        if not options:
            allow_other = True

        question_id = raw.get("id")
        normalized.append(
            _QuestionSpec(
                question_id=(
                    str(question_id).strip() if question_id is not None else ""
                ),
                header=str(raw.get("header") or "Question").strip() or "Question",
                question=str(raw.get("question") or "").strip(),
                multi_select=bool(raw.get("multiSelect")),
                options=tuple(options),
                allow_other=allow_other,
                is_secret=bool(raw.get("isSecret")),
                preselect_first=(
                    not bool(raw.get("requireExplicitChoice"))
                    if "requireExplicitChoice" in raw
                    else default_preselect
                ),
            )
        )
        if len(normalized) == _MAX_QUESTIONS:
            break
    return normalized


def _build_question(
    spec: _QuestionSpec,
    parent_box: Gtk.Box,
    *,
    show_prompt: bool,
) -> _QuestionControls:
    section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    required = Gtk.Label(label="Required", xalign=0)
    required.add_css_class("caption-heading")
    required.add_css_class("accent")
    section.append(required)
    if show_prompt:
        section.append(_option_row(spec.header, spec.question))

    if not spec.options:
        entry = Gtk.Entry()
        entry.set_hexpand(True)
        entry.set_placeholder_text("Type your answer…")
        entry.set_activates_default(True)
        if spec.is_secret:
            entry.set_visibility(False)
        section.append(entry)
        error = _required_error_label()
        section.append(error)
        parent_box.append(section)
        return _QuestionControls(
            option_buttons=[],
            free_entry=entry,
            error_label=error,
        )

    if spec.multi_select:
        buttons = _build_multi_select(list(spec.options), section)
    else:
        buttons = _build_single_select(
            list(spec.options),
            section,
            preselect_first=spec.preselect_first,
        )

    other_button: Gtk.CheckButton | None = None
    other_entry: Gtk.Entry | None = None
    if spec.allow_other:
        other_button, other_entry = _build_other_row(
            buttons,
            section,
            spec.multi_select,
            is_secret=spec.is_secret,
        )

    error = _required_error_label()
    section.append(error)
    parent_box.append(section)
    return _QuestionControls(
        option_buttons=buttons,
        other_button=other_button,
        other_entry=other_entry,
        error_label=error,
    )


def _build_detail(detail: dict, *, scrollable: bool = True) -> Gtk.Widget:
    """What is being approved, in full: a plan (markdown), a diff, or code.

    Approvals used to show a path and nothing else, so a Write or Edit was
    approved blind (2026-09-02 audit, gap 4). GtkSource and the markdown
    renderer are optional here: a plain monospace view is the floor.
    """
    kind = str(detail.get("kind") or "code")
    text = str(detail.get("text") or "")
    title = str(detail.get("title") or "").strip()
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    if title:
        heading = Gtk.Label(label=title, xalign=0)
        heading.add_css_class("caption-heading")
        heading.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        heading.set_tooltip_text(title)
        box.append(heading)
    body: Gtk.Widget | None = None
    if kind == "markdown":
        try:
            from helios.widgets.markdown import render_markdown

            body = render_markdown(text)
        except Exception:  # ponytail: renderer optional, plain text is the floor
            body = None
    elif kind == "diff":
        try:
            from helios.widgets.code_block import CodeBlock

            body = CodeBlock(text, "diff")
        except Exception:
            body = None
    if body is None:
        view = Gtk.TextView()
        view.set_editable(False)
        view.set_cursor_visible(False)
        view.set_monospace(True)
        view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        view.get_buffer().set_text(text)
        body = view
    if not scrollable:
        body.add_css_class("frame")
        box.append(body)
        return box
    scroller = Gtk.ScrolledWindow()
    scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    scroller.set_propagate_natural_height(True)
    scroller.set_max_content_height(320)
    scroller.set_min_content_height(80)
    scroller.add_css_class("frame")
    scroller.set_child(body)
    scroller.update_property(
        [Gtk.AccessibleProperty.LABEL],
        [f"{title or kind} being approved"],
    )
    box.append(scroller)
    return box


def _required_error_label() -> Gtk.Label:
    label = Gtk.Label(label="Choose or enter an answer.", xalign=0)
    label.add_css_class("caption")
    label.add_css_class("error")
    label.set_visible(False)
    return label


def _positive_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, result)


def _auto_resolution_text(remaining_ms: int) -> str:
    """Human copy for provider-owned automatic request resolution."""

    if remaining_ms <= 0:
        return "Waiting for Codex to confirm its automatic resolution…"
    seconds = max(1, (remaining_ms + 999) // 1000)
    return f"Codex may resolve this automatically in {seconds}s."


def _read_question_answer(
    spec: _QuestionSpec,
    controls: _QuestionControls,
) -> list[str]:
    if controls.free_entry is not None:
        text = controls.free_entry.get_text().strip()
        return [text] if text else []

    selected = [
        label
        for (label, _description), button in zip(
            spec.options,
            controls.option_buttons,
        )
        if button.get_active()
    ]
    if (
        controls.other_button is not None
        and controls.other_entry is not None
        and controls.other_button.get_active()
    ):
        other_text = controls.other_entry.get_text().strip()
        if other_text:
            selected.append(other_text)
    return selected


def _format_answers(
    questions: list[_QuestionSpec],
    answers: list[list[str]],
) -> QuestionAnswer:
    """Format provider-compatible output without depending on GTK state.

    Unique IDs identify the Codex protocol. The returned mapping is the value
    for its top-level ``answers`` field. ID-less inputs retain Claude's string
    result, including a readable multi-question fallback.
    """
    ids = [question.question_id for question in questions]
    if ids and all(ids) and len(set(ids)) == len(ids):
        return {
            question_id: {"answers": list(answer)}
            for question_id, answer in zip(ids, answers)
        }
    if len(answers) == 1:
        return ", ".join(answers[0])
    return "\n".join(
        f"{question.header}: {', '.join(answer)}"
        for question, answer in zip(questions, answers)
    )


def _build_single_select(
    options: list[tuple[str, str]],
    parent_box: Gtk.Box,
    *,
    preselect_first: bool = True,
) -> list[Gtk.CheckButton]:
    """Render radio buttons and preselect the first option."""
    buttons: list[Gtk.CheckButton] = []
    head: Gtk.CheckButton | None = None
    for index, (label, description) in enumerate(options):
        row = _option_row(label, description)
        button = Gtk.CheckButton()
        _set_choice_accessibility(button, label, description)
        button.set_valign(Gtk.Align.START)
        button.set_margin_top(4)
        if head is None:
            head = button
        else:
            button.set_group(head)
        if index == 0 and preselect_first:
            button.set_active(True)
        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row_box.append(button)
        row_box.append(row)
        gesture = Gtk.GestureClick()
        gesture.set_button(1)
        gesture.connect("released", lambda *_args, target=button: target.set_active(True))
        row.add_controller(gesture)
        parent_box.append(row_box)
        buttons.append(button)
    return buttons


def _build_multi_select(
    options: list[tuple[str, str]],
    parent_box: Gtk.Box,
) -> list[Gtk.CheckButton]:
    """Render independent checkboxes."""
    buttons: list[Gtk.CheckButton] = []
    for label, description in options:
        row = _option_row(label, description)
        button = Gtk.CheckButton()
        _set_choice_accessibility(button, label, description)
        button.set_valign(Gtk.Align.START)
        button.set_margin_top(4)
        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row_box.append(button)
        row_box.append(row)
        gesture = Gtk.GestureClick()
        gesture.set_button(1)
        gesture.connect(
            "released",
            lambda *_args, target=button: target.set_active(not target.get_active()),
        )
        row.add_controller(gesture)
        parent_box.append(row_box)
        buttons.append(button)
    return buttons


def _build_other_row(
    check_buttons: list[Gtk.CheckButton],
    parent_box: Gtk.Box,
    multi: bool,
    *,
    is_secret: bool = False,
) -> tuple[Gtk.CheckButton, Gtk.Entry]:
    """Append a selectable free-text ``Other`` row."""
    button = Gtk.CheckButton()
    _set_choice_accessibility(button, "Other", "Enter your own answer.")
    button.set_valign(Gtk.Align.START)
    button.set_margin_top(4)
    if not multi and check_buttons:
        button.set_group(check_buttons[0])

    entry = Gtk.Entry()
    entry.set_hexpand(True)
    entry.set_placeholder_text("Type your own answer…")
    entry.set_activates_default(True)
    entry.update_property(
        [Gtk.AccessibleProperty.LABEL, Gtk.AccessibleProperty.DESCRIPTION],
        ["Other answer", "Type an answer not listed in the choices."],
    )
    if is_secret:
        entry.set_visibility(False)

    def _select_other(*_args) -> None:
        button.set_active(True)

    entry.connect("changed", _select_other)

    label_box = _option_row("Other", "")
    row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    row_box.append(button)
    row_box.append(label_box)
    gesture = Gtk.GestureClick()
    gesture.set_button(1)
    if multi:
        gesture.connect(
            "released",
            lambda *_args: button.set_active(not button.get_active()),
        )
    else:
        gesture.connect("released", lambda *_args: button.set_active(True))
    label_box.add_controller(gesture)

    parent_box.append(row_box)
    parent_box.append(entry)
    return button, entry


def _option_row(label: str, description: str) -> Gtk.Widget:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    box.set_hexpand(True)
    title = Gtk.Label(label=label, xalign=0)
    title.add_css_class("body")
    title.set_wrap(True)
    box.append(title)
    if description:
        subtitle = Gtk.Label(label=description, xalign=0)
        subtitle.add_css_class("caption")
        subtitle.add_css_class("dim-label")
        subtitle.set_wrap(True)
        box.append(subtitle)
    return box


def _set_choice_accessibility(
    control: Gtk.Accessible, label: str, description: str
) -> None:
    """Name the actual control, not only its visually adjacent label widget."""
    control.update_property(
        [Gtk.AccessibleProperty.LABEL, Gtk.AccessibleProperty.DESCRIPTION],
        [label, description or label],
    )
