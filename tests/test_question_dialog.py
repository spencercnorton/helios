"""Question normalization/formatting tests (no window required)."""

import pytest

pytest.importorskip("gi")

from helios.widgets.question_dialog import (
    _auto_resolution_text,
    _format_answers,
    _normalize_questions,
    _positive_int,
)


def test_normalizes_up_to_three_questions_and_sanitizes_options():
    questions = _normalize_questions(
        {
            "allowOther": False,
            "questions": [
                {
                    "id": f"q{index}",
                    "header": f"Q {index}",
                    "question": "Choose",
                    "options": [
                        {"label": " Yes ", "description": " Fine "},
                        {"label": "", "description": "ignored"},
                    ],
                }
                for index in range(4)
            ],
        }
    )

    assert [question.question_id for question in questions] == ["q0", "q1", "q2"]
    assert questions[0].options == (("Yes", "Fine"),)
    assert questions[0].allow_other is False


def test_per_question_other_override_and_codex_alias():
    questions = _normalize_questions(
        {
            "allowOther": False,
            "questions": [
                {
                    "question": "First",
                    "allowOther": True,
                    "options": [{"label": "A"}],
                },
                {
                    "question": "Second",
                    "isOther": True,
                    "options": [{"label": "B"}],
                },
                {
                    "question": "Third",
                    "isOther": False,
                    "options": [{"label": "C"}],
                },
            ],
        }
    )

    assert [question.allow_other for question in questions] == [True, True, False]


def test_choice_less_question_always_allows_free_text():
    question = _normalize_questions(
        {
            "allowOther": False,
            "questions": [{"id": "details", "question": "Explain", "options": None}],
        }
    )[0]

    assert question.options == ()
    assert question.allow_other is True


def test_security_prompt_requires_explicit_choice_without_preselection():
    question = _normalize_questions(
        {
            "requireExplicitChoice": True,
            "questions": [
                {
                    "question": "Allow command?",
                    "options": [{"label": "Approve"}, {"label": "Decline"}],
                }
            ],
        }
    )[0]

    assert question.preselect_first is False


def test_native_ids_return_codex_answer_mapping():
    questions = _normalize_questions(
        {
            "questions": [
                {"id": "language", "question": "Language?"},
                {"id": "features", "question": "Features?", "multiSelect": True},
            ]
        }
    )

    assert _format_answers(questions, [["Python"], ["Fast", "Typed"]]) == {
        "language": {"answers": ["Python"]},
        "features": {"answers": ["Fast", "Typed"]},
    }


def test_legacy_single_question_remains_a_string():
    questions = _normalize_questions(
        {"questions": [{"header": "Stack", "question": "Pick", "multiSelect": True}]}
    )

    assert _format_answers(questions, [["GTK", "Python"]]) == "GTK, Python"


def test_idless_multi_question_fallback_is_readable_text():
    questions = _normalize_questions(
        {
            "questions": [
                {"header": "Language", "question": "Pick"},
                {"header": "Style", "question": "Pick"},
            ]
        }
    )

    assert _format_answers(questions, [["Python"], ["Concise"]]) == (
        "Language: Python\nStyle: Concise"
    )


def test_string_options_become_labelled_choices():
    """openrouter_driver emits ["Allow once", "Deny"] as bare strings. Dropped,
    they left the approval dialog with no choices, flipped allowOther on, and
    made approval depend on the user retyping the literal."""
    question = _normalize_questions(
        {
            "allowOther": False,
            "requireExplicitChoice": True,
            "questions": [
                {
                    "header": "Tool approval",
                    "question": "Run Bash?",
                    "options": ["Allow once", "Deny", "  ", 17],
                }
            ],
        }
    )[0]

    assert question.options == (("Allow once", ""), ("Deny", ""))
    assert question.allow_other is False       # not forced on by an empty list
    assert question.preselect_first is False   # requireExplicitChoice honoured


def test_auto_resolution_copy_is_bounded_and_provider_owned():
    assert _positive_int(None) == 0
    assert _positive_int(True) == 0
    assert _positive_int("2500") == 2500
    assert _positive_int(-4) == 0
    assert _auto_resolution_text(2500).endswith("3s.")
    assert "confirm" in _auto_resolution_text(0)
