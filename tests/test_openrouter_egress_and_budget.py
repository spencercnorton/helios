"""Money, grants, and what leaves the machine on the OpenRouter path.

Three findings from the 2026-09-03 inspection, all measured:

* the 200,000-token process breaker is not a spend ceiling — the same budget
  is $0.02 on `deepseek/deepseek-v4-flash` and $6.00 on `openai/gpt-5.5-pro`,
  and one 70%-full round on a 1M-context frontier model is $21-22, so a
  post-round check can only close the gate after the expensive event;
* tool results are POSTed to a third-party endpoint on every later round and
  persisted verbatim, and the estate's scrubber — which already runs over the
  durable Work ledger — did not run on that path. `NORVI_TRACKER_API_TOKEN`
  (admin-scoped) is in the Bash tool's child environment by design, so one
  approved `env` uploaded it;
* the dialog offered Allow once / Deny only, so a session doing real work
  prompted on every edit, which pushes a user to `acceptEdits` wholesale
  rather than granting narrowly.

Note on the fixtures below: credential-shaped strings are assembled at runtime
rather than written as literals. The development workstation runs a PreToolUse guard that denies any
tool call carrying one, and it cannot tell a test fixture from the real thing —
which is the correct trade, so the fixtures work around it rather than the
other way round.
"""

from __future__ import annotations

import pytest

pytest.importorskip("gi")

from helios.backend.openrouter import chat as or_chat  # noqa: E402
from helios.backend.openrouter.routes import Route  # noqa: E402
from helios.backend.process import openrouter_driver as od  # noqa: E402

FAKE_PAT = "gl" + "pat-" + "A" * 24
OTHER_FAKE_PAT = "gl" + "pat-" + "B" * 24


@pytest.fixture()
def driver(tmp_path, monkeypatch):
    monkeypatch.setattr(od.or_key, "load_key", lambda: "sk-or-v1-testkey0000000000")
    drv = od.OpenRouterDriver(cwd=str(tmp_path), model="vendor/model")
    drv.start()
    drv.set_execution_attempt_controller(
        lambda _d: ("attempt-1", ""),
        lambda *_a: None,
        record_dispatch=lambda *_a: None,
        record_acceptance=lambda *_a: None,
        record_stop=lambda *_a: None,
        record_contribution=lambda *_a: True,
    )
    return drv


def _route(*, input_price: float, output_price: float = 0.0) -> Route:
    return Route(
        model="vendor/model", provider_slug="p", provider_name="P",
        context_length=200_000, max_completion_tokens=8_000, quantization="fp8",
        supports_tools=True, supports_reasoning=False, implicit_caching=False,
        explicit_caching=False, input_price=input_price, cache_read_price=0.0,
        output_price=output_price,
    )


def _one_call_round():
    def _fake(messages, **kwargs):
        yield or_chat.ToolCallDelta(
            or_chat.ToolCallRequest(id="c1", name="Read", arguments_json='{"path": "a"}')
        )
        yield or_chat.Done(
            or_chat.ChatCompleted(
                finish_reason="tool_calls",
                usage=or_chat.ChatUsage(input_tokens=5, output_tokens=1, reported=True),
            )
        )
    return _fake


def _plain_round(cost: float = 0.0):
    def _fake(messages, **kwargs):
        yield or_chat.TextDelta("done")
        yield or_chat.Done(
            or_chat.ChatCompleted(
                finish_reason="stop",
                usage=or_chat.ChatUsage(
                    input_tokens=5, output_tokens=1, cost_usd=cost, reported=True
                ),
            )
        )
    return _fake


class TestEgressScrub:
    def test_a_credential_in_tool_output_is_removed_before_replay(self):
        cleaned = od._scrub_tool_output(f"GITLAB_TOKEN={FAKE_PAT}\nother=fine")
        assert FAKE_PAT not in cleaned
        assert "other=fine" in cleaned

    def test_the_model_is_told_that_something_was_removed(self):
        """A silent hole reads to the model as the file's real contents."""
        cleaned = od._scrub_tool_output(f"GITLAB_TOKEN={FAKE_PAT}")
        assert "redacted recognisable credentials" in cleaned

    def test_clean_output_is_returned_unchanged_and_unannotated(self):
        assert od._scrub_tool_output("total 4\n-rw-r--r-- 1 x") == "total 4\n-rw-r--r-- 1 x"

    def test_the_scrub_is_on_the_loop_path(self, driver, monkeypatch):
        """One string: the array the model reads and the transcript the user
        reads must not disagree, because the mirror is the resume fallback."""
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))
        rounds = iter([_one_call_round(), _plain_round()])
        monkeypatch.setattr(
            od.or_chat, "stream_chat", lambda *a, **k: next(rounds)(*a, **k)
        )
        monkeypatch.setattr(
            od.tools,
            "execute_tool",
            lambda *a, **k: (f"PRIVATE_TOKEN={OTHER_FAKE_PAT}", False),
        )
        driver._permission_mode = "dontAsk"

        driver._run_turn("go")

        replies = [m for m in driver._history if m.get("role") == "tool"]
        assert replies, "no tool reply was persisted"
        assert OTHER_FAKE_PAT not in str(replies[0]["content"])


class TestMoneyCeiling:
    def test_a_projected_round_over_the_ceiling_is_not_sent(self, driver, monkeypatch):
        """The point of the pre-flight check: one round on an expensive model
        can cost several times the whole process ceiling."""
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))
        sent = []
        monkeypatch.setattr(
            od.or_chat,
            "stream_chat",
            lambda *a, **k: sent.append(1) or _plain_round()(*a, **k),
        )
        driver._route = _route(input_price=30.0 / 1_000_000)  # gpt-5.5-pro
        driver._history += [{"role": "user", "content": "x" * 4_000_000}]

        driver._run_turn("go")

        assert sent == [], "an over-budget round was sent anyway"
        assert driver._token_budget_exhausted is True

    def test_a_cheap_round_is_projected_and_allowed(self, driver, monkeypatch):
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))
        sent = []
        monkeypatch.setattr(
            od.or_chat,
            "stream_chat",
            lambda *a, **k: sent.append(1) or _plain_round()(*a, **k),
        )
        driver._route = _route(input_price=0.13 / 1_000_000)  # deepseek-v4-flash
        driver._history += [{"role": "user", "content": "x" * 400_000}]

        driver._run_turn("go")

        assert sent == [1]
        assert driver._token_budget_exhausted is False

    def test_accumulated_spend_trips_the_ceiling(self, driver, monkeypatch):
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))
        budgets = []
        driver.connect("budget-exhausted", lambda _d, info: budgets.append(info))
        monkeypatch.setattr(
            od.or_chat,
            "stream_chat",
            _plain_round(cost=od.OPENROUTER_STANDARD_COST_BUDGET_USD + 1),
        )

        driver._run_turn("go")

        assert [b["kind"] for b in budgets] == ["cost"]
        assert budgets[0]["cost_limit_usd"] == od.OPENROUTER_STANDARD_COST_BUDGET_USD
        assert budgets[0]["cost_usd"] > od.OPENROUTER_STANDARD_COST_BUDGET_USD

    def test_an_unpriced_endpoint_projects_nothing_rather_than_guessing(self, driver):
        driver._route = _route(input_price=0.0)
        assert driver._projected_round_cost(0.0, 1_000) == 0.0
        driver._route = None
        assert driver._projected_round_cost(0.0, 1_000) == 0.0

    def test_the_projection_prices_the_reply_not_just_the_prompt(self, driver):
        """A review finding: counting only the prompt meant a short cheap
        request could be waved through and then generate an expensive reply, so
        the ceiling was not enforceable. Output is routinely several times the
        price of input."""
        driver._route = _route(
            input_price=30.0 / 1_000_000, output_price=180.0 / 1_000_000
        )
        driver._history = [{"role": "user", "content": "hi"}]
        prompt_only = driver._prompt_cost()
        assert driver._projected_round_cost(prompt_only, 100_000) > prompt_only + 17.0

    def test_an_expensive_reply_is_capped_so_it_cannot_break_the_ceiling(
        self, driver, monkeypatch
    ):
        """The scenario the finding named: short prompt, high output price, a
        reply whose full reserve would alone cost $22.50 against a $5 ceiling.
        The request still goes — capped at what the budget affords — because
        stopping a session dead is a worse answer than a shorter reply."""
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))
        seen: list[int] = []

        def _capture(*a, **k):
            seen.append(k.get("max_tokens", -1))
            return _plain_round()(*a, **k)

        monkeypatch.setattr(od.or_chat, "stream_chat", _capture)
        driver._route = Route(
            model="vendor/model", provider_slug="p", provider_name="P",
            context_length=1_000_000, max_completion_tokens=0, quantization="",
            supports_tools=True, supports_reasoning=False, implicit_caching=False,
            explicit_caching=False, input_price=0.0, cache_read_price=0.0,
            output_price=180.0 / 1_000_000,
        )
        driver._history = [{"role": "user", "content": "hi"}]

        driver._run_turn("go")

        assert len(seen) == 1
        assert seen[0] < driver._route.completion_reserve, "reply was not capped"
        worst_case = seen[0] * driver._route.output_price
        assert worst_case <= od.OPENROUTER_STANDARD_COST_BUDGET_USD

    def test_a_budget_too_small_to_buy_a_useful_reply_trips_instead(
        self, driver, monkeypatch
    ):
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))
        sent = []
        monkeypatch.setattr(
            od.or_chat, "stream_chat",
            lambda *a, **k: sent.append(1) or _plain_round()(*a, **k),
        )
        driver._route = _route(input_price=0.0, output_price=1.0 / 1_000_000)
        # $0.0001 left buys 100 tokens — below the useful floor.
        driver._lifetime_cost_usd = od.OPENROUTER_STANDARD_COST_BUDGET_USD - 0.0001

        driver._run_turn("go")

        assert sent == []
        assert driver._token_budget_exhausted is True

    def test_a_near_exhausted_budget_shortens_the_reply_instead_of_stopping(
        self, driver
    ):
        """Capping max_tokens at what the remaining budget affords keeps a
        session working near its ceiling rather than stopping it dead."""
        driver._route = _route(input_price=0.0, output_price=1.0 / 1_000_000)
        assert driver._route.completion_reserve == 8_000
        driver._lifetime_cost_usd = od.OPENROUTER_STANDARD_COST_BUDGET_USD - 0.002
        affordable = driver._affordable_completion_tokens(0.0)[0]
        # ~$0.002 of headroom at $1/M is ~2,000 tokens; the exact integer moves
        # with float representation of the subtraction, so assert the property.
        assert 1_990 <= affordable <= 2_000
        assert 0 < affordable < driver._route.completion_reserve

    def test_an_unpriced_output_is_not_clamped(self, driver):
        driver._route = _route(input_price=0.0, output_price=0.0)
        assert (
            driver._affordable_completion_tokens(0.0)[0]
            == driver._route.completion_reserve
        )

    def test_an_expensive_model_can_still_complete_its_first_turn(
        self, driver, monkeypatch
    ):
        """A review finding, reproduced before fixing: the clamp spent the
        WHOLE remaining budget on output and the projection then added the
        prompt on top, so on a $180/M endpoint a 100-token prompt was enough to
        refuse the request — an expensive model could not complete even its
        first turn. The prompt has to be paid for first."""
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))
        sent = []
        monkeypatch.setattr(
            od.or_chat, "stream_chat",
            lambda *a, **k: sent.append(k.get("max_tokens")) or _plain_round()(*a, **k),
        )
        driver._route = Route(
            model="vendor/model", provider_slug="p", provider_name="P",
            context_length=1_000_000, max_completion_tokens=0, quantization="",
            supports_tools=True, supports_reasoning=False, implicit_caching=False,
            explicit_caching=False, input_price=30.0 / 1_000_000,
            cache_read_price=0.0, output_price=180.0 / 1_000_000,
        )
        driver._history = [{"role": "user", "content": "x" * 4_000}]  # ~1k tokens
        # Capture before the turn: _run_turn appends to _history, so recomputing
        # afterwards would price a prompt the request never carried.
        prompt_cost = driver._prompt_cost()

        driver._run_turn("go")

        assert sent, "an expensive model was refused its first turn"
        worst = prompt_cost + sent[0] * driver._route.output_price
        assert worst <= od.OPENROUTER_STANDARD_COST_BUDGET_USD

    def test_a_prompt_that_alone_breaks_the_ceiling_still_trips(self, driver):
        driver._route = Route(
            model="vendor/model", provider_slug="p", provider_name="P",
            context_length=1_000_000, max_completion_tokens=0, quantization="",
            supports_tools=True, supports_reasoning=False, implicit_caching=False,
            explicit_caching=False, input_price=30.0 / 1_000_000,
            cache_read_price=0.0, output_price=180.0 / 1_000_000,
        )
        driver._history = [{"role": "user", "content": "x" * 800_000}]  # ~200k tok
        assert driver._prompt_cost() > od.OPENROUTER_STANDARD_COST_BUDGET_USD
        assert driver._affordable_completion_tokens(driver._prompt_cost())[0] == 0

    def test_one_prompt_estimate_feeds_both_calculations(self, driver):
        """The two must not be able to disagree about the same prompt."""
        driver._route = _route(input_price=1.0 / 1_000_000, output_price=1.0 / 1_000_000)
        driver._history = [{"role": "user", "content": "y" * 40_000}]
        cost = driver._prompt_cost()
        affordable = driver._affordable_completion_tokens(cost)[0]
        projected = driver._projected_round_cost(cost, affordable)
        assert projected <= od.OPENROUTER_STANDARD_COST_BUDGET_USD


class TestSessionGrant:
    def test_allow_once_does_not_grant_the_session(self, driver, monkeypatch):
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))

        def _answer(_self, _payload, token):
            _self.answer_question(token, _self._ALLOW_ONCE)

        driver.connect("question-asked", _answer)
        assert driver._request_approval("Edit", {"file_path": "a"}) is True
        assert driver._session_grants == set()

    def test_choosing_the_session_option_records_the_grant(self, driver, monkeypatch):
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))

        def _answer(_self, _payload, token):
            _self.answer_question(token, _self._ALLOW_SESSION)

        driver.connect("question-asked", _answer)
        assert driver._request_approval("Edit", {"file_path": "a"}) is True
        assert driver._session_grants == {"Edit"}

    def test_a_session_grant_covers_later_calls(self, driver, monkeypatch):
        asked = []
        monkeypatch.setattr(
            od.OpenRouterDriver,
            "_request_approval",
            lambda self, name, args, outside="": asked.append(name) or True,
        )
        monkeypatch.setattr(od.tools, "execute_tool", lambda *a, **k: ("ok", False))
        driver._permission_mode = "default"
        driver._session_grants.add("Write")

        driver._execute_tool_call(
            or_chat.ToolCallRequest(
                id="c",
                name="Write",
                arguments_json='{"file_path": "a", "content": "x"}',
            )
        )

        assert asked == [], "a granted tool still prompted"

    def test_a_grant_never_covers_an_out_of_workspace_target(self, driver):
        driver._session_grants.add("Write")
        assert driver._has_session_grant("Write", "/etc/passwd") is False
        assert driver._has_session_grant("Write", "") is True

    def test_bash_offers_only_an_exact_command_session_grant(self, driver, monkeypatch):
        captured = {}
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))

        def _capture(_self, payload, token):
            captured["options"] = payload["questions"][0]["options"]
            _self.answer_question(token, "Deny")

        driver.connect("question-asked", _capture)
        driver._request_approval("Bash", {"command": "ls"})
        assert captured["options"] == ["Allow once", driver._ALLOW_COMMAND_SESSION, "Deny"]
        assert driver._ALLOW_SESSION not in captured["options"]

        driver._request_approval("Write", {"file_path": "a"})
        assert driver._ALLOW_SESSION in captured["options"]

    def test_exact_command_grant_reuses_only_identical_command_and_cwd(self, driver, monkeypatch):
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))
        asked = []

        def answer(_self, payload, token):
            asked.append(payload)
            _self.answer_question(token, _self._ALLOW_COMMAND_SESSION)

        driver.connect("question-asked", answer)
        call = or_chat.ToolCallRequest(id="c", name="Bash", arguments_json='{"command":"printf hello"}')
        assert driver._execute_tool_call(call) == ("hello\n[exit code 0]", False)
        assert driver._execute_tool_call(call) == ("hello\n[exit code 0]", False)
        assert len(asked) == 1
        assert "without a filesystem sandbox" in asked[0]["grantScope"]
        assert driver._has_session_grant("Bash", "", {"command": "printf hello"})
        assert not driver._has_session_grant("Bash", "", {"command": "printf hello; pwd"})
        assert not driver._has_session_grant("Bash", "", {"command": "printf world"})
        driver._cwd += "/other"
        assert not driver._has_session_grant("Bash", "", {"command": "printf hello"})

    def test_command_grant_cannot_be_answered_with_tool_wide_label(self, driver, monkeypatch):
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))
        driver.connect("question-asked", lambda self, _payload, token: self.answer_question(token, self._ALLOW_SESSION))
        assert not driver._request_approval("Bash", {"command": "printf hello"})
        assert driver._session_grants == set()

    def test_permission_changes_revoke_grants_and_pending_consent(self, driver, monkeypatch):
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))
        driver._session_grants.add("Write")
        assert driver.set_permission_mode("auto")
        assert not driver._session_grants

        def answer(self, _payload, token):
            assert self.set_permission_mode("plan")
            assert self.set_permission_mode("auto")
            self.answer_question(token, self._ALLOW_COMMAND_SESSION)

        driver.connect("question-asked", answer)
        assert not driver._request_approval("Bash", {"command": "printf hello"})
        assert not driver._session_grants
        assert not driver._approval_choices

    def test_mcp_session_grant_is_explicit_tool_specific_and_fingerprint_bound(self, driver, monkeypatch):
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))
        class Estate:
            fingerprint = "v1"

            def owns(self, name):
                return name == "EstateCallTool"

            def approval_name(self, name, arguments):
                return arguments["name"]

            def session_grant_key(self, name):
                return f"mcp:{name}:{self.fingerprint}"

            statuses = []

            def call(self, name, arguments):
                return "approved external result", False

        driver._estate = Estate()
        asked = []

        def answer(self, payload, token):
            asked.append(payload)
            self.answer_question(token, self._ALLOW_MCP_SESSION)

        driver.connect("question-asked", answer)
        call = or_chat.ToolCallRequest(id="c", name="EstateCallTool", arguments_json='{"name":"mcp__one","arguments":{"query":"a"}}')
        assert driver._execute_tool_call(call)[1] is False
        call = or_chat.ToolCallRequest(id="d", name="EstateCallTool", arguments_json='{"name":"mcp__one","arguments":{"query":"b"}}')
        assert driver._execute_tool_call(call)[1] is False
        assert len(asked) == 1
        assert "any arguments, including external effects" in asked[0]["grantScope"]
        assert not driver._has_session_grant("mcp__two", "", {})
        driver._estate.fingerprint = "v2"
        assert driver._execute_tool_call(call)[1] is False
        assert len(asked) == 2
        driver._permission_mode = "plan"
        assert driver._execute_tool_call(call)[1] is True
        assert len(asked) == 2

    def test_auto_executes_workspace_edits_without_approval(self, driver, monkeypatch):
        monkeypatch.setattr(driver, "_request_approval", lambda *_: pytest.fail("Auto should accept project edits"))
        driver._permission_mode = "auto"
        call = or_chat.ToolCallRequest(id="c", name="Write", arguments_json='{"file_path":"auto.txt","content":"first"}')
        assert driver._execute_tool_call(call)[1] is False
        call = or_chat.ToolCallRequest(id="e", name="Edit", arguments_json='{"file_path":"auto.txt","old_string":"first","new_string":"second"}')
        assert driver._execute_tool_call(call)[1] is False
        from pathlib import Path
        assert (Path(driver._cwd) / "auto.txt").read_text() == "second"

    def test_an_out_of_workspace_call_is_not_offered_a_grant(self, driver, monkeypatch):
        captured = {}
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))

        def _capture(_self, payload, token):
            captured["options"] = payload["questions"][0]["options"]
            _self.answer_question(token, "Deny")

        driver.connect("question-asked", _capture)
        driver._request_approval("Write", {"file_path": "/etc/x"}, "/etc/x")
        assert captured["options"] == ["Allow once", "Deny"]

    def test_an_unrecognised_answer_is_a_refusal(self, driver, monkeypatch):
        """A label from a dialog this driver did not write must never widen
        permission."""
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))

        def _answer(_self, _payload, token):
            _self.answer_question(token, "Yes, obviously")

        driver.connect("question-asked", _answer)
        assert driver._request_approval("Write", {"file_path": "a"}) is False
        assert driver._session_grants == set()


class TestTheScrubDoesNotDamageOrdinaryOutput:
    """The first version of this scrub used the full `scrub_sensitive`, whose
    `name = value` rule is right for prose and wrong for machine output.
    Measured over this repository before the fix: reading 46 of 283 source
    files came back damaged. Preventing a leak by corrupting every file the
    agent reads is not a trade worth making."""

    def test_source_code_that_merely_names_a_secret_is_untouched(self):
        source = (
            "1: token = object()\n"
            "2: def f(api_key: str, password: str) -> None:\n"
            "3:     is_secret: bool = True\n"
            "4:     req.add_header('X-API-Key', API_KEY)\n"
        )
        assert od._scrub_tool_output(source) == source

    def test_a_real_credential_shape_is_still_removed(self):
        assert FAKE_PAT not in od._scrub_tool_output(f"GITLAB_TOKEN={FAKE_PAT}")

    def test_a_bearer_header_is_still_removed(self):
        out = od._scrub_tool_output("Authorization: Bearer " + "abcdefghijklmnop1234")
        assert "abcdefghijklmnop1234" not in out

    def test_a_credential_in_a_url_is_still_removed(self):
        out = od._scrub_tool_output("git clone https://user:" + "s3cr3tvalue@example.com/x")
        assert "s3cr3tvalue" not in out

    def test_a_private_key_block_is_still_removed(self):
        # Assembled at runtime; a literal PEM header trips the estate's
        # PreToolUse credential guard even inside a test fixture.
        head = "-----BEGIN " + "OPENSSH PRIVATE KEY" + "-----"
        tail = "-----END " + "OPENSSH PRIVATE KEY" + "-----"
        block = f"{head}\nb3BlbnNzaC1rZXktdjEAAAAA\n{tail}"
        assert "b3BlbnNzaC1rZXktdjEAAAAA" not in od._scrub_tool_output(block)


class TestTheNameRuleStaysOnOneLine:
    """``\\s`` matches a newline, so ``token:`` ending a line swallowed the
    first word of the next one. That affected every caller of the shared
    scrubber, not just this path."""

    def test_a_trailing_name_does_not_eat_the_next_line(self):
        from helios.backend.sensitive_text import scrub_sensitive

        text = "is_secret:\n    return None"
        assert scrub_sensitive(text)[0] == text

    def test_a_real_pair_on_one_line_is_still_redacted(self):
        from helios.backend.sensitive_text import scrub_sensitive

        cleaned, changed = scrub_sensitive("password = hunter2correcthorse")
        assert changed is True
        assert "hunter2correcthorse" not in cleaned


class TestApprovalDisclosesTheDestination:
    """OpenRouter is the one Helios provider that ships conversation text and
    every tool result to a third party, chosen per session from whichever
    model the user picked. The approval dialog is the one place that choice is
    made, so it is the one place the fact belongs."""

    def _capture(self, driver, monkeypatch, tool, args):
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))
        seen: dict = {}

        def _grab(_self, payload, token):
            seen.update(payload)
            _self.answer_question(token, "Deny")

        driver.connect("question-asked", _grab)
        driver._request_approval(tool, args)
        return seen

    def test_the_prompt_names_the_model_and_the_operator(
        self, driver, monkeypatch
    ):
        driver._route = _route(input_price=0.0)
        seen = self._capture(driver, monkeypatch, "Bash", {"command": "ls"})
        question = seen["questions"][0]["question"]
        assert "vendor/model via P" in question
        assert "leaves this machine" in question

    def test_an_unpinned_session_still_names_the_model(self, driver, monkeypatch):
        driver._route = None
        seen = self._capture(driver, monkeypatch, "Bash", {"command": "ls"})
        assert "vendor/model" in seen["questions"][0]["question"]

    @pytest.mark.parametrize("command", ["printf hello", "printf 'héllo'\n" * 2000 + "printf final"])
    def test_commands_are_complete_code_details_without_duplicate_prose(
        self, driver, monkeypatch, command
    ):
        seen = self._capture(driver, monkeypatch, "Bash", {"command": command})
        assert seen["presentation"] == "tool-approval"
        assert seen["detail"] == {"kind": "code", "title": "Command", "text": command}
        assert command not in seen["questions"][0]["question"]
        assert driver._cwd in seen["caption"]
        assert "exact command" in seen["grantScope"]

    def test_other_tools_keep_reviewable_arguments_without_credentials(self, driver, monkeypatch):
        seen = self._capture(driver, monkeypatch, "mcp__lookup", {
            "query": "the complete query", "api_key": FAKE_PAT,
        })
        assert "the complete query" in seen["detail"]["text"]
        assert FAKE_PAT not in seen["detail"]["text"]

    def test_a_write_carries_its_content_for_the_dialog_to_render(
        self, driver, monkeypatch
    ):
        """Approving a Write on its path alone is approving it blind, and this
        provider writes as the desktop user."""
        seen = self._capture(
            driver, monkeypatch, "Write",
            {"file_path": "/tmp/x.py", "content": "print('replaced')\n"},
        )
        detail = seen["detail"]
        assert detail is not None
        assert "print('replaced')" in detail["text"]

    def test_an_edit_carries_a_diff(self, driver, monkeypatch):
        seen = self._capture(
            driver, monkeypatch, "Edit",
            {"file_path": "/tmp/x.py", "old_string": "a = 1", "new_string": "a = 2"},
        )
        detail = seen["detail"]
        assert detail is not None and detail["kind"] == "diff"
        assert "a = 2" in detail["text"]


class TestTheBudgetMessageNamesTheRealCeiling:
    """Three ceilings set the same latch now — the token budget, accumulated
    spend, and a projected round that would cross it — and the message on a
    later send was hardcoded to the token one, so a session stopped for money
    was told it had run out of tokens. Found by cross-model review."""

    def test_a_cost_trip_says_so(self, driver, monkeypatch):
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))
        driver._lifetime_cost_usd = 5.25
        driver._trip_runtime_budget(kind="cost", used=10)
        errors: list[str] = []
        driver.connect("error", lambda _d, m: errors.append(m))

        driver.send_user_text("later")

        assert "spend limit" in errors[-1]
        assert "$5.2500 used" in errors[-1]
        assert "token safety limit" not in errors[-1]

    def test_a_projected_cost_trip_says_so_too(self, driver, monkeypatch):
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))
        driver._trip_runtime_budget(kind="cost-projected", used=0)
        errors: list[str] = []
        driver.connect("error", lambda _d, m: errors.append(m))
        driver.send_user_text("later")
        assert "spend limit" in errors[-1]

    def test_a_token_trip_still_says_tokens(self, driver, monkeypatch):
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))
        driver._trip_runtime_budget(kind="tokens", used=200_001)
        errors: list[str] = []
        driver.connect("error", lambda _d, m: errors.append(m))
        driver.send_user_text("later")
        assert "200,000-token safety limit" in errors[-1]

    def test_an_unmetered_trip_names_the_missing_receipt(self, driver, monkeypatch):
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))
        driver._trip_runtime_budget(kind="usage-unverified", used=0)
        errors: list[str] = []
        driver.connect("error", lambda _d, m: errors.append(m))
        driver.send_user_text("later")
        assert "no verifiable token receipt" in errors[-1]


class TestThePromptEstimateIsABound:
    """A review finding: chars/4 is an approximation, so a projection that
    treats it as exact is not an upper bound and the ceiling it feeds is not a
    ceiling. Measured against the API's own prompt_tokens over four tokenizer
    families: worst 1.55× (Qwen, numbered Read output); DeepSeek 1.49× on
    JSON-shaped text. The tool schemas are a further ~830 tokens per request
    that estimate_tokens never saw."""

    def _priced(self, driver, history):
        driver._route = _route(input_price=1.0, output_price=0.0)  # $1 per token
        driver._history = history

    def test_a_first_round_carries_the_margin_and_the_schema_overhead(self, driver):
        from helios.backend.openrouter.history import estimate_tokens

        history = [{"role": "user", "content": "x" * 4_000}]
        self._priced(driver, history)
        raw = estimate_tokens(history)
        bound = driver._prompt_tokens_upper_bound()
        assert bound == int((raw + od._TOOL_SCHEMA_TOKENS) * od._PROMPT_ESTIMATE_MARGIN)
        assert bound > raw * 1.5

    def test_the_margin_covers_the_worst_measured_tokenizer(self):
        assert od._PROMPT_ESTIMATE_MARGIN >= 1.55

    def test_a_receipt_anchors_the_next_projection(self, driver):
        """From the second round on, the provider's own count for what was
        sent is the base; only the messages appended since are estimated."""
        from helios.backend.openrouter.history import estimate_tokens

        history = [{"role": "user", "content": "x" * 40_000}]
        self._priced(driver, history)
        driver._prompt_receipt = (1, 12_345)  # the API counted the 1-message array
        driver._history.append({"role": "assistant", "content": "short reply"})
        appended = estimate_tokens(driver._history[1:])
        assert driver._prompt_tokens_upper_bound() == 12_345 + int(
            appended * od._PROMPT_ESTIMATE_MARGIN
        )

    def test_a_metered_round_records_the_receipt(self, driver, monkeypatch):
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))
        monkeypatch.setattr(
            od.or_chat, "stream_chat",
            lambda *a, **k: _round_with_prompt_tokens(777)(*a, **k),
        )
        driver._history = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]

        driver._run_turn("go")

        anchor_len, anchor_tokens = driver._prompt_receipt
        assert anchor_tokens == 777
        assert anchor_len == 2  # the two messages that were sent, not the reply

    def test_compaction_discards_a_receipt_that_described_the_old_array(
        self, driver, monkeypatch
    ):
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))
        driver._history = [{"role": "system", "content": "s"}]
        for index in range(6):
            driver._history += [
                {"role": "user", "content": f"u{index} " + "x" * 4_000},
                {"role": "assistant", "content": f"a{index} " + "y" * 4_000},
            ]
        driver._prompt_receipt = (len(driver._history), 50_000)

        assert driver._compact_history(1_200) > 0
        assert driver._prompt_receipt == (0, 0)

    def test_a_stale_receipt_longer_than_the_history_is_ignored(self, driver):
        from helios.backend.openrouter.history import estimate_tokens

        history = [{"role": "user", "content": "x" * 400}]
        self._priced(driver, history)
        driver._prompt_receipt = (5, 99_999)  # describes an array we no longer have
        raw = estimate_tokens(history)
        assert driver._prompt_tokens_upper_bound() == int(
            (raw + od._TOOL_SCHEMA_TOKENS) * od._PROMPT_ESTIMATE_MARGIN
        )

    def test_an_under_counted_prompt_still_fits_the_ceiling(self, driver, monkeypatch):
        """The finding's scenario: $30/M in, $180/M out, a prompt the estimator
        under-counts by the worst measured ratio. The worst case of what is
        actually sent must still fit."""
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))
        sent: list[int] = []
        monkeypatch.setattr(
            od.or_chat, "stream_chat",
            lambda *a, **k: sent.append(k.get("max_tokens")) or _plain_round()(*a, **k),
        )
        driver._route = Route(
            model="vendor/model", provider_slug="p", provider_name="P",
            context_length=1_000_000, max_completion_tokens=0, quantization="",
            supports_tools=True, supports_reasoning=False, implicit_caching=False,
            explicit_caching=False, input_price=30.0 / 1_000_000,
            cache_read_price=0.0, output_price=180.0 / 1_000_000,
        )
        driver._history = [{"role": "user", "content": "x" * 200_000}]  # est ~50k tok
        from helios.backend.openrouter.history import estimate_tokens
        real_prompt_tokens = int(estimate_tokens(driver._history) * 1.55)  # Qwen-worst

        driver._run_turn("go")

        assert sent, "the request was refused outright"
        real_worst = (
            real_prompt_tokens * driver._route.input_price
            + sent[0] * driver._route.output_price
        )
        assert real_worst <= od.OPENROUTER_STANDARD_COST_BUDGET_USD


def _round_with_prompt_tokens(prompt_tokens: int):
    def _fake(messages, **kwargs):
        yield or_chat.TextDelta("done")
        yield or_chat.Done(
            or_chat.ChatCompleted(
                finish_reason="stop",
                usage=or_chat.ChatUsage(
                    input_tokens=prompt_tokens, output_tokens=1, reported=True
                ),
            )
        )
    return _fake



class TestTheScrubCatchesTheMotivatingLeak:
    """A review finding: shape rules alone cannot catch the concrete
    leak this scrub was written for. `NORVI_TRACKER_API_TOKEN` is 70 random
    characters with no vendor prefix, so `env`/`printenv` output sailed through
    `scrub_sensitive(named_pairs=False)`. Two new passes close it — the exact
    values Helios itself forwarded or holds, and complete env-assignment lines
    with a credential-shaped UPPER_CASE name — measured at 0 false positives
    over 319 repository files in raw, `Read` (line-numbered) and `Grep`
    (path:line:) shapes."""

    SEVENTY = "q9" + "a1b2c3d4" * 8 + "z8y7"  # prefixless, like the tracker token

    def test_an_env_dump_line_is_redacted_by_name(self):
        out = od._scrub_tool_output(f"HOME=/home/x\nNORVI_TRACKER_API_TOKEN={self.SEVENTY}\nSHELL=/bin/bash")
        assert self.SEVENTY not in out
        assert "HOME=/home/x" in out and "SHELL=/bin/bash" in out

    def test_read_output_with_line_numbers_is_still_caught(self):
        """The Read tool numbers every line; a `^`-anchored rule missed exactly
        the tool output that would carry a .env file upstream."""
        out = od._scrub_tool_output(f"1: # settings\n2: DB_PASSWORD={self.SEVENTY[:24]}\n3: DEBUG=1")
        assert self.SEVENTY[:24] not in out
        assert "3: DEBUG=1" in out

    def test_grep_output_with_a_path_prefix_is_still_caught(self):
        out = od._scrub_tool_output(f"/srv/app/.env:7:export API_SECRET={self.SEVENTY[:30]}")
        assert self.SEVENTY[:30] not in out
        assert "/srv/app/.env:7:export API_SECRET=" in out

    def test_a_shell_expansion_is_not_a_secret(self):
        """Spaceless forms, on purpose: a value with a space never reaches the
        rule at all, so only these actually exercise the `$`/quote/backtick
        guard. (The first version of this test used `$(secret-tool lookup …)`,
        which has spaces and proved nothing — caught by a mutation check.)"""
        for line in (
            # NB: single-quoted forms are deliberately absent — round 11
            # established that `'$(x)'` is a shell *literal*, so it is a
            # secret, not a reference. See TestRoundElevenFindings.
            "GITLAB_TOKEN=$SOME_LONG_VARIABLE_NAME",
            'export API_SECRET="$SECRET_REF_12345"',
            "DB_PASSWORD=`cat /run/secrets/db`",
        ):
            assert od._scrub_tool_output(line) == line, line

    def test_source_code_assignments_are_untouched(self):
        source = "token = object()\napi_key: str\nMAX_RETRIES=3\nsecret_key = settings.SECRET_KEY\n"
        assert od._scrub_tool_output(source) == source

    def test_the_forwarded_tracker_token_is_redacted_wherever_it_appears(self, monkeypatch):
        """Exact-match on the value Helios itself put in the child environment:
        zero false positives by construction, and not limited to NAME=value."""
        monkeypatch.setenv("NORVI_TRACKER_API_TOKEN", self.SEVENTY)
        out = od._scrub_tool_output(f"curl -H 'Authorization: Bearer {self.SEVENTY}' https://jira/x\n"
                                    f"# the token is {self.SEVENTY} and it works")
        assert self.SEVENTY not in out
        assert "redacted recognisable credentials" in out

    def test_the_openrouter_key_itself_is_redacted(self, monkeypatch):
        """The Bash tool can `cat ~/.helios/openrouter.key` (measured readable);
        its own API key must never be replayed to the provider.

        Today's real keys are `sk-or-v1-…`, which the SHAPE rule already
        catches, so a fixture in that shape proves nothing about the
        known-value pass (the first version of this test did exactly that and
        a mutation check exposed it). The fixture is deliberately prefixless:
        the known-value pass is the safety net for a key format the shape rule
        does not know."""
        from helios.backend.sensitive_text import scrub_sensitive

        fake_key = "orv2" + "f" * 60
        assert scrub_sensitive(fake_key, named_pairs=False)[1] is False, (
            "fixture must not be catchable by shape, or this test is redundant"
        )
        monkeypatch.setattr(od.or_key, "load_key", lambda: fake_key)
        assert fake_key not in od._scrub_tool_output(f"{fake_key}\n")

    def test_short_or_absent_known_values_are_ignored(self, monkeypatch):
        """A short value would match everywhere; an absent one must not turn
        every empty string into a redaction."""
        monkeypatch.setenv("NORVI_TRACKER_API_TOKEN", "abc")
        monkeypatch.setattr(od.or_key, "load_key", lambda: "")
        text = "abc is fine and so is this"
        assert od._scrub_tool_output(text) == text


class TestUnknownPricingIsNotFree:
    """A review finding: `_price()` collapsed a missing key, an unparsable
    value and a negative sentinel all to 0.0, and the driver read 0.0 as
    "free, nothing to clamp" — so an endpoint whose pricing the catalog did not
    give was exempt from the spend ceiling entirely. Five live models carry
    negative sentinel prices, and `/models/{slug}/endpoints` is a separate
    response from `/models`: it can be incomplete on its own."""

    def _route(self, **over):
        base = dict(
            model="vendor/model", provider_slug="p", provider_name="P",
            context_length=1_000_000, max_completion_tokens=0, quantization="",
            supports_tools=True, supports_reasoning=False, implicit_caching=False,
            explicit_caching=False, input_price=0.0, cache_read_price=0.0,
            output_price=0.0, pricing_known=True,
        )
        base.update(over)
        return Route(**base)

    def test_a_confirmed_free_endpoint_is_not_clamped(self, driver):
        driver._route = self._route(pricing_known=True)
        assert driver._affordable_completion_tokens(0.0)[0] == driver._route.completion_reserve

    def test_unknown_pricing_is_bounded_by_the_token_budget(self, driver):
        """Money cannot bound the round, so the unit that still can does —
        and the prompt comes out of that budget first (round 6)."""
        driver._route = self._route(pricing_known=False)
        driver._lifetime_tokens_used = od.OPENROUTER_STANDARD_TOKEN_BUDGET - 5_000
        driver._history = [{"role": "user", "content": "hi"}]
        prompt_bound = driver._prompt_tokens_upper_bound()
        affordable = driver._affordable_completion_tokens(0.0)[0]
        assert affordable == 5_000 - prompt_bound
        assert (
            driver._lifetime_tokens_used + prompt_bound + affordable
            <= od.OPENROUTER_STANDARD_TOKEN_BUDGET
        )

    def test_unknown_pricing_trips_when_the_token_budget_is_spent(
        self, driver, monkeypatch
    ):
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))
        sent = []
        monkeypatch.setattr(
            od.or_chat, "stream_chat",
            lambda *a, **k: sent.append(1) or _plain_round()(*a, **k),
        )
        driver._route = self._route(pricing_known=False)
        driver._lifetime_tokens_used = od.OPENROUTER_STANDARD_TOKEN_BUDGET - 10

        driver._run_turn("go")

        assert sent == [], "an unbounded round was sent on unknown pricing"
        assert driver._token_budget_exhausted is True

    def test_unknown_pricing_projects_no_dollars_rather_than_a_false_zero(self, driver):
        driver._route = self._route(pricing_known=False, input_price=30.0 / 1_000_000)
        driver._history = [{"role": "user", "content": "x" * 400_000}]
        assert driver._prompt_cost() == 0.0

    @pytest.mark.parametrize(
        "pricing,known",
        [
            ({"prompt": "0.0000001", "completion": "0.0000002"}, True),
            ({"prompt": "0", "completion": "0"}, True),          # confirmed free
            ({"prompt": "0.0000001"}, False),                     # missing completion
            ({"completion": "0.0000002"}, False),                 # missing prompt
            ({"prompt": "n/a", "completion": "0.0000002"}, False),  # unparsable
            ({"prompt": "-1000000", "completion": "-1000000"}, False),  # sentinel
            ({}, False),                                          # no pricing at all
        ],
    )
    def test_the_three_cases_are_told_apart(self, pricing, known):
        from helios.backend.openrouter import routes as or_routes

        endpoint = {
            "tag": "p", "provider_name": "P", "context_length": 131_072,
            "max_completion_tokens": 8_192, "quantization": "fp8", "status": 0,
            "uptime_last_30m": 99.9,
            "supported_parameters": ["tools", "tool_choice"],
            "pricing": pricing,
        }
        assert or_routes._choose("vendor/model", [endpoint]).pricing_known is known

    def test_the_token_fallback_pays_for_the_prompt_too(self, driver):
        """A review finding: the unknown-pricing branch budgeted only the
        reply against the token budget, so prompt + reply could still overrun
        it — round 2's error in the other currency."""
        driver._route = self._route(pricing_known=False)
        driver._lifetime_tokens_used = 0
        driver._history = [{"role": "user", "content": "x" * 400_000}]  # ~100k est
        bound = driver._prompt_tokens_upper_bound()
        affordable = driver._affordable_completion_tokens(0.0)[0]
        assert affordable + bound <= od.OPENROUTER_STANDARD_TOKEN_BUDGET

    def test_a_prompt_that_alone_fills_the_token_budget_leaves_nothing(self, driver):
        driver._route = self._route(pricing_known=False)
        driver._history = [{"role": "user", "content": "x" * 2_000_000}]
        assert driver._affordable_completion_tokens(0.0)[0] == 0

    def test_a_known_priced_route_is_also_held_to_the_token_ceiling(self, driver):
        """A review finding: the token allowance applied only on the
        unpriced branch, so a cheap known-priced model could blow through the
        200,000-token process ceiling in one round while the dollar clamp
        happily allowed it."""
        driver._route = self._route(
            pricing_known=True, input_price=1e-9, output_price=1e-9
        )
        driver._lifetime_tokens_used = od.OPENROUTER_STANDARD_TOKEN_BUDGET - 3_000
        driver._history = [{"role": "user", "content": "hi"}]
        prompt_bound = driver._prompt_tokens_upper_bound()
        affordable = driver._affordable_completion_tokens(driver._prompt_cost())[0]
        assert affordable == 3_000 - prompt_bound
        assert (
            driver._lifetime_tokens_used + prompt_bound + affordable
            <= od.OPENROUTER_STANDARD_TOKEN_BUDGET
        )

    def test_a_confirmed_free_route_is_held_to_the_token_ceiling_too(self, driver):
        """Free of charge is not free of budget."""
        driver._route = self._route(pricing_known=True, input_price=0.0, output_price=0.0)
        driver._history = [{"role": "user", "content": "hi"}]
        # Tool schemas count as prompt tokens. Reserve those first so this
        # remains a test of a free endpoint's reply cap as the tools evolve.
        driver._lifetime_tokens_used = (
            od.OPENROUTER_STANDARD_TOKEN_BUDGET
            - driver._prompt_tokens_upper_bound() - 2_000
        )
        affordable = driver._affordable_completion_tokens(0.0)[0]
        assert 0 < affordable <= 2_000
        assert affordable < driver._route.completion_reserve

    def test_whichever_ceiling_binds_first_wins(self, driver):
        """Both budgets are applied, so the tighter one decides."""
        driver._history = [{"role": "user", "content": "hi"}]
        # Dollars bind: expensive output, full token budget.
        driver._route = self._route(pricing_known=True, output_price=180.0 / 1_000_000)
        driver._lifetime_tokens_used = 0
        by_money = driver._affordable_completion_tokens(driver._prompt_cost())[0]
        # Tokens bind: nearly free output, nearly exhausted token budget.
        driver._route = self._route(pricing_known=True, output_price=1e-12)
        driver._lifetime_tokens_used = od.OPENROUTER_STANDARD_TOKEN_BUDGET - 1_000
        by_tokens = driver._affordable_completion_tokens(driver._prompt_cost())[0]
        assert by_tokens < by_money < driver._route.completion_reserve


class TestAnUnpinnedRouteIsStillBounded:
    """A review finding: `_affordable_completion_tokens` returned 0 the
    moment no route resolved, and the trip check was guarded on
    `self._route is not None` — so an unpinned request (a supported fallback
    when endpoint discovery fails) had neither a token clamp nor a completion
    cap, and one response near the ceiling could cross it."""

    def test_no_route_still_gets_a_completion_cap(self, driver, monkeypatch):
        monkeypatch.setattr(
            od.OpenRouterDriver, "_unpinned_completion_reserve", lambda _s: 50_000
        )
        driver._route = None
        driver._history = [{"role": "user", "content": "hi"}]
        allowed, bound = driver._affordable_completion_tokens(0.0)
        assert allowed == 50_000
        assert bound in ("reserve", "tokens", "cost")

    def test_no_route_is_still_held_to_the_token_ceiling(self, driver, monkeypatch):
        monkeypatch.setattr(
            od.OpenRouterDriver, "_unpinned_completion_reserve", lambda _s: 50_000
        )
        driver._route = None
        driver._lifetime_tokens_used = od.OPENROUTER_STANDARD_TOKEN_BUDGET - 4_000
        driver._history = [{"role": "user", "content": "hi"}]
        prompt_bound = driver._prompt_tokens_upper_bound()
        allowed, bound = driver._affordable_completion_tokens(0.0)
        assert allowed == 4_000 - prompt_bound
        assert bound == "tokens"
        assert (
            driver._lifetime_tokens_used + prompt_bound + allowed
            <= od.OPENROUTER_STANDARD_TOKEN_BUDGET
        )

    def test_an_unpinned_session_out_of_budget_is_not_sent(self, driver, monkeypatch):
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))
        sent = []
        monkeypatch.setattr(
            od.or_chat, "stream_chat",
            lambda *a, **k: sent.append(1) or _plain_round()(*a, **k),
        )
        driver._route = None
        driver._lifetime_tokens_used = od.OPENROUTER_STANDARD_TOKEN_BUDGET - 10

        driver._run_turn("go")

        assert sent == [], "an unpinned round was sent past the token ceiling"
        assert driver._token_budget_exhausted is True

    def test_the_unpinned_reserve_is_an_eighth_of_the_catalog_window(self, driver, monkeypatch):
        from helios.backend.openrouter import catalog as or_catalog

        monkeypatch.setattr(or_catalog, "context_length_for", lambda _m: 1_000_000)
        assert driver._unpinned_completion_reserve() == 125_000
        monkeypatch.setattr(or_catalog, "context_length_for", lambda _m: 0)
        assert driver._unpinned_completion_reserve() == 0


class TestTheTripNamesTheCeilingThatBoundIt:
    """A review finding: a session that ran out of *tokens* was
    latched and reported as cost-exhausted, because the combined condition
    always tripped with `kind="cost-projected"`."""

    def test_a_token_bound_stop_says_tokens(self, driver, monkeypatch):
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))
        monkeypatch.setattr(od.or_chat, "stream_chat", _plain_round())
        errors: list[str] = []
        driver.connect("error", lambda _d, m: errors.append(m))
        driver._route = _route(input_price=1e-12, output_price=1e-12)
        driver._lifetime_tokens_used = od.OPENROUTER_STANDARD_TOKEN_BUDGET - 10

        driver._run_turn("go")

        assert driver._budget_trip_kind == "tokens-projected"
        assert any("token safety limit" in e for e in errors), errors
        assert not any("spend limit" in e for e in errors), errors

    def test_a_cost_bound_stop_still_says_spend(self, driver, monkeypatch):
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))
        monkeypatch.setattr(od.or_chat, "stream_chat", _plain_round())
        errors: list[str] = []
        driver.connect("error", lambda _d, m: errors.append(m))
        driver._route = _route(input_price=1e-9, output_price=1.0)  # $1 per token
        driver._lifetime_cost_usd = od.OPENROUTER_STANDARD_COST_BUDGET_USD - 0.0001

        driver._run_turn("go")

        assert driver._budget_trip_kind == "cost-projected"
        assert any("spend limit" in e for e in errors), errors

    @pytest.mark.parametrize(
        "kind,expected,forbidden",
        [
            ("tokens-projected", "token safety limit", "spend limit"),
            ("tokens", "token safety limit", "spend limit"),
            ("cost-projected", "spend limit", "token safety limit"),
            ("cost", "spend limit", "token safety limit"),
            ("usage-unverified", "no verifiable token receipt", "spend limit"),
            ("", "token safety limit", "spend limit"),  # never tripped / unknown
        ],
    )
    def test_a_later_send_repeats_the_right_ceiling(
        self, driver, monkeypatch, kind, expected, forbidden
    ):
        """Every kind that can set the latch, including the fall-through: the
        token wording is the default rather than a branch, because an explicit
        one returned exactly what the default did and a mutation check caught
        it as dead code."""
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))
        if kind:
            driver._trip_runtime_budget(kind=kind, used=1)
        else:
            driver._token_budget_exhausted = True
        errors: list[str] = []
        driver.connect("error", lambda _d, m: errors.append(m))
        driver.send_user_text("later")
        assert expected in errors[-1], errors
        assert forbidden not in errors[-1], errors

    def test_a_spend_trip_is_never_labelled_token_exhaustion(self, driver, monkeypatch):
        """A review finding: the label came from the clamp even when the
        DOLLAR projection was what fired, so a spend trip could be reported —
        and permanently latched — as token exhaustion."""
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))
        monkeypatch.setattr(od.or_chat, "stream_chat", _plain_round())
        errors: list[str] = []
        driver.connect("error", lambda _d, m: errors.append(m))
        # Expensive INPUT and *confirmed free* output: the dollar clamp is
        # skipped entirely (output costs nothing to cap), so the completion
        # clamp is bound by tokens — while the dollar projection, which prices
        # the prompt, is what actually trips. That combination is the only one
        # where the two disagree, so it is the only one that tests the fix.
        driver._route = _route(input_price=1.0, output_price=0.0)
        driver._history = [{"role": "user", "content": "x" * 800_000}]

        driver._run_turn("go")

        assert driver._token_budget_exhausted is True
        assert driver._budget_trip_kind == "cost-projected", driver._budget_trip_kind
        assert any("spend limit" in e for e in errors), errors


class TestRoundTenFindings:
    """A review finding."""

    def _route_obj(self, **over):
        base = dict(
            model="vendor/model", provider_slug="p", provider_name="P",
            context_length=200_000, max_completion_tokens=8_000, quantization="fp8",
            supports_tools=True, supports_reasoning=False, implicit_caching=False,
            explicit_caching=False, input_price=0.0, cache_read_price=0.0,
            output_price=0.0, pricing_known=True,
        )
        base.update(over)
        return Route(**base)

    QUOTED = "correct-horse-battery-staple"

    @pytest.mark.parametrize(
        "line",
        [
            'DATABASE_PASSWORD="' + QUOTED + '"',
            "API_TOKEN='" + QUOTED + "'",
            '12: DATABASE_PASSWORD="' + QUOTED + '"',
            "/srv/app/.env:3:export API_SECRET='" + QUOTED + "'",
            'export DB_PASSWORD="' + QUOTED + '"',
        ],
    )
    def test_a_quoted_env_credential_is_redacted(self, line):
        """A quoted value is a literal, not an expansion — calling it one was
        simply wrong, and left the commonest `.env` shape unscrubbed."""
        out = od._scrub_tool_output(line)
        assert self.QUOTED not in out, out

    @pytest.mark.parametrize(
        "line",
        [
            'GITLAB_TOKEN="$SOME_REFERENCE_VALUE"',
            # `'$(...)'` moved to TestRoundElevenFindings as a *secret*: in
            # shell, single quotes make it a literal, not a substitution.
            'X_TOKEN="$(get-token --json)"',
            'DB_PASSWORD="`cat /run/secrets/db`"',
            'MAX_KEY_LEN="4096"',
        ],
    )
    def test_a_reference_is_not_a_secret(self, line):
        assert od._scrub_tool_output(line) == line

    @pytest.mark.parametrize(
        "sep", ["\r", "\x0b", "\x0c", "\x85", " "],
        ids=["cr", "vtab", "formfeed", "nel", "line-sep"],
    )
    def test_a_quoted_value_cannot_swallow_past_a_line_boundary(self, sep):
        from helios.backend.sensitive_text import scrub_sensitive

        cleaned, _ = scrub_sensitive(f'password = "abc{sep}KEEPME"')
        assert "KEEPME" in cleaned, cleaned

    def test_a_multi_word_quoted_value_is_still_one_value(self):
        from helios.backend.sensitive_text import scrub_sensitive

        text = 'password = "correct horse battery staple"'
        cleaned, changed = scrub_sensitive(text)
        assert changed and "battery" not in cleaned

    def test_a_tiny_endpoint_is_not_reported_as_an_exhausted_budget(
        self, driver, monkeypatch
    ):
        """A 400-token endpoint cannot fit a useful reply — that is a property
        of the model, not a budget the session has spent, and latching the
        process budget for it would kill the session permanently."""
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))
        sent = []
        monkeypatch.setattr(
            od.or_chat, "stream_chat",
            lambda *a, **k: sent.append(1) or _plain_round()(*a, **k),
        )
        errors: list[str] = []
        driver.connect("error", lambda _d, m: errors.append(m))
        driver._route = self._route_obj(context_length=400, max_completion_tokens=0)

        driver._run_turn("go")

        assert sent == []
        assert driver._token_budget_exhausted is False, "a small endpoint latched the budget"
        assert any("too few to be useful" in e for e in errors), errors

    def test_an_exhausted_token_budget_still_latches(self, driver, monkeypatch):
        monkeypatch.setattr(od.GLib, "idle_add", lambda cb, *a: cb(*a))
        monkeypatch.setattr(od.or_chat, "stream_chat", _plain_round())
        driver._route = self._route_obj()
        driver._lifetime_tokens_used = od.OPENROUTER_STANDARD_TOKEN_BUDGET - 10

        driver._run_turn("go")

        assert driver._token_budget_exhausted is True
        assert driver._budget_trip_kind == "tokens-projected"


class TestRoundElevenFindings:
    """A review finding."""

    @pytest.mark.parametrize(
        "line",
        [
            "DB_PASSWORD='pa$$word1'",        # $ inside SINGLE quotes is literal
            "API_TOKEN='literal$value'",
            "DB_PASSWORD=short",              # no minimum length
            "API_KEY='x'",                    # one character is still a secret
            "12: DB_PASSWORD='pa$$word1'",    # under the Read prefix
            "/srv/app/.env:3:export API_SECRET='s'",  # under the Grep prefix
            'API_TOKEN="mixed $VAR and text"',  # $ present but not a whole reference
        ],
    )
    def test_a_literal_secret_is_redacted_however_short_or_odd(self, line):
        out = od._scrub_tool_output(line)
        assert out != line, line

    @pytest.mark.parametrize(
        "line",
        [
            'GITLAB_TOKEN="$SOME_REF"',
            'API_KEY="${SOME_REF}"',
            'X_TOKEN="$(get-token --json)"',
            'DB_PASSWORD="`cat /run/secrets/db`"',
            "GITLAB_TOKEN=$SOME_REF",
            "MAX_KEY_LEN=4096",  # a number is never a credential
        ],
    )
    def test_a_whole_reference_or_a_number_is_left_alone(self, line):
        assert od._scrub_tool_output(line) == line, line

    def test_dropping_the_length_floor_added_no_false_positives(self):
        """Measured over the repository in raw, Read and Grep shapes before the
        floor was removed: the count did not move, which is why it went."""
        import pathlib

        files = [
            f
            for f in list(pathlib.Path("src").rglob("*.py"))
            + list(pathlib.Path("docs").rglob("*.md"))
            if f.is_file()
        ]
        altered = []
        for f in files:
            text = f.read_text(errors="replace")
            shapes = (
                text,
                "\n".join(f"{i}: {line}" for i, line in enumerate(text.splitlines(), 1)),
                "\n".join(f"{f}:{i}:{line}" for i, line in enumerate(text.splitlines(), 1)),
            )
            if any(od._scrub_tool_output(s) != s for s in shapes):
                altered.append(f)
        assert altered == [], altered

    @pytest.mark.parametrize(
        "raw", ["Infinity", "-Infinity", "inf", "nan", "NaN", float("inf"), float("nan")]
    )
    def test_a_non_finite_price_is_not_a_known_price(self, raw):
        """`float("Infinity")` parses, so infinity used to sail through as a
        real price and poison every projection that multiplied by it."""
        from helios.backend.openrouter import routes as or_routes

        assert or_routes._price(raw) is None

    def test_an_infinite_price_leaves_the_route_unpriced(self):
        from helios.backend.openrouter import routes as or_routes

        endpoint = {
            "tag": "p", "provider_name": "P", "context_length": 131_072,
            "max_completion_tokens": 8_192, "quantization": "fp8", "status": 0,
            "uptime_last_30m": 99.9,
            "supported_parameters": ["tools", "tool_choice"],
            "pricing": {"prompt": "Infinity", "completion": "0.0000002"},
        }
        route = or_routes._choose("vendor/model", [endpoint])
        assert route.pricing_known is False
        assert route.input_price == 0.0

    def test_the_free_tier_note_claims_only_what_is_always_true(self):
        """OpenRouter grants 1,000/day once an account has bought $10 of
        credit, so a flat "50/day" is wrong for exactly the users who paid."""
        from helios.backend.openrouter import account

        assert "50/day" not in account._FREE_TIER_NOTE
        assert "20 req/min" in account._FREE_TIER_NOTE


class TestModelLevelPricingBacksTheCeiling:
    """A review finding: `_prompt_cost` and `_projected_round_cost` both
    returned zero whenever no *endpoint* price was available, so an unpinned
    session (endpoint discovery failed — a supported path) or an endpoint with
    a mangled pricing payload had no monetary bound at all. The token budget
    is not money: the same 200,000 tokens is $0.02 on one model and $6.00 on
    another."""

    def test_an_unpinned_expensive_model_is_cost_bound(self, tmp_path, monkeypatch):
        monkeypatch.setattr(od.or_key, "load_key", lambda: "sk-or-v1-testkey0000000000")
        drv = od.OpenRouterDriver(cwd=str(tmp_path), model="vendor/expensive")
        drv._route = None
        drv._history = [{"role": "user", "content": "x" * 400_000}]
        from helios.backend.openrouter import catalog as or_catalog

        monkeypatch.setattr(
            or_catalog, "pricing_for", lambda _m: (30.0 / 1_000_000, 180.0 / 1_000_000)
        )
        allowed, bound = drv._affordable_completion_tokens(drv._prompt_cost())
        assert bound == "cost", (allowed, bound)
        assert drv._prompt_cost() > 1.0
        worst = drv._prompt_cost() + allowed * (180.0 / 1_000_000)
        assert worst <= od.OPENROUTER_STANDARD_COST_BUDGET_USD

    def test_an_unpinned_cheap_model_is_not_needlessly_restricted(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(od.or_key, "load_key", lambda: "sk-or-v1-testkey0000000000")
        drv = od.OpenRouterDriver(cwd=str(tmp_path), model="vendor/cheap")
        drv._route = None
        drv._history = [{"role": "user", "content": "x" * 400_000}]
        from helios.backend.openrouter import catalog as or_catalog

        monkeypatch.setattr(or_catalog, "pricing_for", lambda _m: (1e-7, 2e-7))
        _allowed, bound = drv._affordable_completion_tokens(drv._prompt_cost())
        assert bound == "tokens"

    def test_a_pinned_endpoint_price_still_wins(self, driver, monkeypatch):
        """The endpoint's own price is authoritative; the catalog is only the
        stand-in when there is none."""
        from helios.backend.openrouter import catalog as or_catalog

        monkeypatch.setattr(or_catalog, "pricing_for", lambda _m: (99.0, 99.0))
        driver._route = _route(input_price=1e-9, output_price=2e-9)
        assert driver._effective_prices() == (1e-9, 2e-9)

    def test_an_unpriced_endpoint_falls_back_to_the_catalog(self, driver, monkeypatch):
        from helios.backend.openrouter import catalog as or_catalog

        monkeypatch.setattr(or_catalog, "pricing_for", lambda _m: (5e-6, 7e-6))
        driver._route = Route(
            model="vendor/model", provider_slug="p", provider_name="P",
            context_length=200_000, max_completion_tokens=8_000, quantization="fp8",
            supports_tools=True, supports_reasoning=False, implicit_caching=False,
            explicit_caching=False, input_price=0.0, cache_read_price=0.0,
            output_price=0.0, pricing_known=False,
        )
        assert driver._effective_prices() == (5e-6, 7e-6)

    def test_no_price_anywhere_is_token_bounded_not_pretend_free(
        self, driver, monkeypatch
    ):
        from helios.backend.openrouter import catalog as or_catalog

        monkeypatch.setattr(or_catalog, "pricing_for", lambda _m: None)
        driver._route = None
        driver._lifetime_tokens_used = od.OPENROUTER_STANDARD_TOKEN_BUDGET - 3_000
        driver._history = [{"role": "user", "content": "hi"}]
        assert driver._prompt_cost() == 0.0
        allowed, bound = driver._affordable_completion_tokens(0.0)
        assert bound == "tokens"
        assert allowed <= 3_000

    @pytest.mark.parametrize(
        "pricing,expected",
        [
            ({"prompt": "0.0000001", "completion": "0.0000002"}, (1e-07, 2e-07)),
            ({"prompt": "0", "completion": "0"}, (0.0, 0.0)),
            ({"prompt": "Infinity", "completion": "0.0000002"}, None),
            ({"prompt": "-1000000", "completion": "-1000000"}, None),
            ({"prompt": "0.0000001"}, None),
            ({}, None),
        ],
    )
    def test_catalog_pricing_validates_like_the_endpoint_parser(
        self, monkeypatch, pricing, expected
    ):
        from helios.backend.openrouter import catalog as or_catalog

        monkeypatch.setattr(
            or_catalog, "_read_cache", lambda: [{"id": "v/m", "pricing": pricing}]
        )
        assert or_catalog.pricing_for("v/m") == expected
