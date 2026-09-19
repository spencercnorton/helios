"""Prose checks for ``draft_metadata`` output.

The only mechanically checkable claim a docstring makes is which parameters it
says the function takes, so that is what these test hardest — in every
convention a model actually emits, at the indentation it actually emits them.
Everything else here is a heuristic, and the tests bound it in both directions:
a placeholder must be caught, and a legitimate mention of the word TODO must
not be.
"""

from __future__ import annotations

import pytest

from helios.backend.specialty_verifiers import verify_metadata


def codes(findings):
    return [f.code for f in findings]


class TestMetadata:
    def test_accepts_a_plain_commit_message(self):
        out = {"text": "fix: guard the empty-history path"}
        assert verify_metadata(out, kind="commit_message") == []

    @pytest.mark.parametrize("text", [
        "TODO",
        "  TBD  ",
        "feat: x\n\nTODO:\n",
        "Replace the <describe the change> here",
        "Body {{summary}} follows",
        "See REPLACE_ME for details",
    ])
    def test_rejects_placeholder_shaped_text(self, text):
        assert codes(verify_metadata({"text": text}, kind="commit_message")) == [
            "PLACEHOLDER_TEXT"
        ]

    @pytest.mark.parametrize("text", [
        "fix: drop the stale TODO comment in parser.py",
        "docs: explain when to use FIXME vs TODO markers",
        "refactor: replace <int> annotations with int",
        "x" * 200,
    ])
    def test_accepts_legitimate_mentions_of_marker_words(self, text):
        """Commit messages and docstrings talk about TODO/FIXME markers all the
        time; a bare substring match rejected correct output."""
        assert verify_metadata({"text": text}, kind="commit_message") == []

    def test_rejects_a_parameter_invented_in_the_docstring_text(self):
        """The artifact a human reads is the text. Grading a `parameters` array
        the worker writes about itself checks nothing: omit it, or send an
        empty one, and the check disappears."""
        out = {"text": "Load a file.\n\nArgs:\n    path: the path\n"
                       "    encoding: the codec\n"}
        found = verify_metadata(out, kind="docstring", known_parameters={"path"})
        assert codes(found) == ["UNKNOWN_PARAMETER"]
        assert "encoding" in found[0].detail

    def test_rejects_an_invented_sphinx_parameter(self):
        out = {"text": "Load a file.\n\n:param path: the path\n:param mode: how\n"}
        found = verify_metadata(out, kind="docstring", known_parameters={"path"})
        assert codes(found) == ["UNKNOWN_PARAMETER"]

    def test_accepts_a_docstring_documenting_real_parameters(self):
        out = {"text": "Load a file.\n\nArgs:\n    path: the path\n\nReturns:\n"
                       "    The file contents.\n"}
        assert verify_metadata(out, kind="docstring", known_parameters={"path"}) == []

    NUMPY_DOC = (
        "Load a file.\n\n"
        "Parameters\n"
        "----------\n"
        "path : str\n"
        "    The path.\n"
        "encoding : str\n"
        "    INVENTED.\n\n"
        "Returns\n"
        "-------\n"
        "str\n"
    )

    def test_rejects_an_invented_numpy_parameter_at_column_zero(self):
        """A model returns the docstring as a bare string, so NumPy
        declarations arrive unindented. An indent-anchored pattern matched
        nothing at all — the check looked like it ran and found nothing."""
        found = verify_metadata(
            {"text": self.NUMPY_DOC}, kind="docstring", known_parameters={"path"}
        )
        assert codes(found) == ["UNKNOWN_PARAMETER"]
        assert "encoding" in found[0].detail

    def test_accepts_a_clean_numpy_docstring(self):
        clean = self.NUMPY_DOC.replace("encoding : str\n    INVENTED.\n", "")
        assert verify_metadata(
            {"text": clean}, kind="docstring", known_parameters={"path"}
        ) == []

    def test_numpy_multiple_names_on_one_line(self):
        text = "D.\n\nParameters\n----------\nx, y : int\n"
        found = verify_metadata({"text": text}, kind="docstring",
                                known_parameters={"x"})
        assert codes(found) == ["UNKNOWN_PARAMETER"]

    def test_star_args_are_compared_without_their_asterisks(self):
        text = "D.\n\nArgs:\n    *nope: x\n"
        assert codes(verify_metadata({"text": text}, kind="docstring",
                                     known_parameters={"path"})) == ["UNKNOWN_PARAMETER"]
        text_ok = "D.\n\nArgs:\n    *args: x\n"
        assert verify_metadata({"text": text_ok}, kind="docstring",
                               known_parameters={"args"}) == []

    @pytest.mark.parametrize("text", [
        "Load a file.\n\nUsage: call load()\n",
        "Load a file.\n\nNote: this is slow\n",
        "Load a file.\n\nReturns:\n    The contents.\n",
    ])
    def test_prose_outside_a_parameter_section_is_not_a_declaration(self, text):
        """The declaration pattern is loose enough to match ordinary prose, so
        it is only applied inside an Args/Parameters section."""
        assert verify_metadata({"text": text}, kind="docstring",
                               known_parameters={"path"}) == []

    def test_rejects_an_unknown_kind_instead_of_skipping_the_check(self):
        """A kind outside the input enum used to fall through silently, so a
        typo disabled the docstring check without any signal."""
        out = {"text": "Args:\n    bogus: x\n"}
        assert codes(verify_metadata(out, kind="docstrings")) == ["UNKNOWN_KIND"]

    def test_bounds_the_length(self):
        out = {"text": "a" * 100}
        assert codes(verify_metadata(out, kind="commit_message", max_chars=50)) == [
            "TEXT_TOO_LONG"
        ]

    def test_requires_text(self):
        assert codes(verify_metadata({}, kind="commit_message")) == ["MISSING_FIELD"]
        assert codes(verify_metadata({"text": "  "}, kind="commit_message")) == [
            "MISSING_FIELD"
        ]
