"""Promotion evidence, endpoint verification, and inline context authorization.

The property every test here defends: **with no promotion record present,
nothing dispatches.** That is the production state, and it must stay the
default no matter what else changes in this module.
"""

from __future__ import annotations

import json
import os

import pytest

from helios.backend import router_promotion as promo
from helios.backend.openrouter import PROFILES, ProfileRef


def _record(tmp_path, body, mode=0o600, name="promotions.json"):
    path = tmp_path / name
    path.write_text(body if isinstance(body, str) else json.dumps(body))
    os.chmod(path, mode)
    return path


def _valid_body():
    ref = next(iter(PROFILES))
    return {"promoted": [{"profile_id": ref.profile_id, "version": ref.version}]}


def _owned_by(monkeypatch, uid: int) -> None:
    """Report a specific owner for every fstat, in both directions.

    Ownership cannot be asserted from the ambient user: CI runs as root, so a
    test that writes a file and expects it to be non-root-owned is meaningless
    there — and one that expects root ownership is meaningless on a developer
    machine. Simulating both ways makes each test mean the same thing
    everywhere it runs.
    """
    real_fstat = os.fstat

    class Owned:
        def __init__(self, info):
            self._info = info
        def __getattr__(self, name):
            return getattr(self._info, name)
        st_uid = uid

    monkeypatch.setattr(os, "fstat", lambda fd: Owned(real_fstat(fd)))


class TestPromotionRecord:
    def test_absent_record_promotes_nothing(self, tmp_path):
        """The default, and the current production state."""
        assert promo.load_promotions(tmp_path / "nope.json") == frozenset()

    @pytest.mark.parametrize("uid", [1000, 999, 65534])
    def test_a_record_the_service_could_author_is_refused(
        self, tmp_path, monkeypatch, uid
    ):
        """The hole this closes. A mode-0600 file owned by the service passes
        every permission check while being exactly what the gate exists to
        exclude — evidence the service wrote about itself. Root ownership is
        what establishes separation, since the service runs unprivileged."""
        _owned_by(monkeypatch, uid)
        assert promo.load_promotions(_record(tmp_path, _valid_body())) == frozenset()

    def test_a_symlink_is_refused(self, tmp_path):
        """Otherwise an unprivileged writer could point the ownership check at
        a root-owned file and the read somewhere else."""
        target = _record(tmp_path, _valid_body(), name="real.json")
        link = tmp_path / "link.json"
        link.symlink_to(target)
        assert promo.load_promotions(link) == frozenset()

    def test_a_directory_is_refused(self, tmp_path):
        assert promo.load_promotions(tmp_path) == frozenset()

    def test_an_oversized_record_is_refused(self, tmp_path):
        assert promo.load_promotions(_record(tmp_path, "x" * 70_000)) == frozenset()

    def test_a_root_owned_record_admits_named_profiles(self, tmp_path, monkeypatch):
        """The positive case. Ownership is simulated so this means the same
        thing on a developer machine and in a root CI container."""
        ref = next(iter(PROFILES))
        _owned_by(monkeypatch, 0)
        assert promo.load_promotions(_record(tmp_path, _valid_body())) == frozenset({ref})

    @staticmethod
    def _as_root(monkeypatch):
        _owned_by(monkeypatch, 0)

    # Every case below simulates root ownership so it is refused for *its own*
    # reason. Without this they would all pass because the ownership check
    # rejects them first — testing nothing about the property each names.

    @pytest.mark.parametrize("mode", [0o660, 0o606, 0o666])
    def test_a_writable_record_is_refused_even_when_root_owned(
        self, tmp_path, monkeypatch, mode
    ):
        self._as_root(monkeypatch)
        path = _record(tmp_path, _valid_body(), mode=mode)
        assert promo.load_promotions(path) == frozenset()

    def test_the_record_cannot_introduce_a_profile(self, tmp_path, monkeypatch):
        """It names profiles from the frozen table; it cannot invent a route."""
        self._as_root(monkeypatch)
        path = _record(tmp_path, {"promoted": [
            {"profile_id": "attacker.invented", "version": 1}
        ]})
        assert promo.load_promotions(path) == frozenset()

    @pytest.mark.parametrize("body", [
        "not json at all",
        "{}",
        '{"promoted": "everything"}',
        '{"promoted": [{"profile_id": 7, "version": "1"}]}',
        '{"promoted": [null, 3]}',
    ])
    def test_malformed_records_promote_nothing(self, tmp_path, monkeypatch, body):
        self._as_root(monkeypatch)
        assert promo.load_promotions(_record(tmp_path, body)) == frozenset()

    def test_the_positive_case_really_is_reachable(self, tmp_path, monkeypatch):
        """Guards the guards: if root simulation stopped working, every test
        above would pass vacuously again."""
        self._as_root(monkeypatch)
        assert promo.load_promotions(_record(tmp_path, _valid_body())) != frozenset()


class TestEndpointVerification:
    def test_only_positively_confirmed_profiles_are_returned(self):
        ref = ProfileRef("bulk_extract.parasail", 1)
        confirmed = promo.verified_endpoints(
            [ref], verifier=lambda model, slug, quant: True)
        assert confirmed == frozenset({ref})

    def test_an_unconfirmed_profile_is_absent(self):
        ref = ProfileRef("bulk_extract.parasail", 1)
        assert promo.verified_endpoints(
            [ref], verifier=lambda *a: False) == frozenset()

    def test_a_verifier_error_is_unverified_not_verified(self):
        """Confirmation must be positive evidence, never the absence of a
        contradiction."""
        ref = ProfileRef("bulk_extract.parasail", 1)
        def boom(*_a):
            raise RuntimeError("catalog unreachable")
        assert promo.verified_endpoints([ref], verifier=boom) == frozenset()

    def test_the_verifier_receives_the_profile_declaration(self):
        seen = []
        ref = ProfileRef("bulk_extract.parasail", 1)
        promo.verified_endpoints(
            [ref], verifier=lambda m, s, q: seen.append((m, s, q)) or True)
        model, slug, quant = seen[0]
        assert "/" in model
        assert slug == "parasail"
        assert quant == "fp8"


class TestInlineContextAuthorizer:
    BINDING = {"kind": "claude", "client_binding": "c", "call_id": "1"}

    def _task(self, refs):
        return {"context_refs": refs}

    def test_authorizes_bounded_inline_context(self):
        assert promo.authorize_inline_context(
            self.BINDING, self._task([{"kind": "inline", "ref": "hello"}])) is True

    def test_refuses_a_non_inline_ref(self):
        """Workspace paths, diffs and work-event ranges need a real compiler.
        The policy rejects them earlier, but this must not be the thing that
        stops noticing if that ever changes."""
        for kind in ("workspace_path", "workspace_diff", "artifact", "work_event_range"):
            assert promo.authorize_inline_context(
                self.BINDING, self._task([{"kind": kind, "ref": "x"}])) is False

    def test_refuses_a_mixed_ref_list(self):
        assert promo.authorize_inline_context(self.BINDING, self._task([
            {"kind": "inline", "ref": "ok"},
            {"kind": "workspace_path", "ref": "/etc/passwd"},
        ])) is False

    def test_refuses_empty_or_missing_context(self):
        assert promo.authorize_inline_context(self.BINDING, self._task([])) is False
        assert promo.authorize_inline_context(self.BINDING, {}) is False

    def test_refuses_content_over_the_profile_ceiling(self):
        from helios.backend.openrouter import get_profile
        from helios.backend.router_policy import CANARY_PROFILE

        ceiling = get_profile(CANARY_PROFILE).max_input_chars
        assert promo.authorize_inline_context(
            self.BINDING, self._task([{"kind": "inline", "ref": "x" * (ceiling + 1)}])
        ) is False

    def test_refuses_without_a_caller_binding(self):
        task = self._task([{"kind": "inline", "ref": "hello"}])
        assert promo.authorize_inline_context({}, task) is False
        assert promo.authorize_inline_context({"kind": ""}, task) is False

    def test_never_raises_on_garbage(self):
        for junk in (None, [], "text", {"context_refs": "no"},
                     {"context_refs": [None, 7]}):
            assert isinstance(
                promo.authorize_inline_context(self.BINDING, junk), bool)


class TestRecordSizeIsBytesNotCharacters:
    """The limit is stated in bytes. Checking the decoded length would count
    characters, so multibyte UTF-8 could carry more than the stated limit while
    staying under the same character count."""

    def test_a_multibyte_record_over_the_byte_limit_is_refused(
        self, tmp_path, monkeypatch
    ):
        _owned_by(monkeypatch, 0)
        # Each of these is 3 bytes but 1 character: well under the limit by
        # characters, well over it by bytes.
        blob = "中" * 40_000
        assert len(blob) < promo._MAX_RECORD_BYTES
        assert len(blob.encode("utf-8")) > promo._MAX_RECORD_BYTES
        path = _record(tmp_path, json.dumps({"promoted": [], "pad": blob}))
        assert promo.load_promotions(path) == frozenset()

    def test_invalid_utf8_is_refused_not_crashed(self, tmp_path, monkeypatch):
        _owned_by(monkeypatch, 0)
        path = tmp_path / "promotions.json"
        path.write_bytes(b'{"promoted": []} \xff\xfe')
        os.chmod(path, 0o600)
        assert promo.load_promotions(path) == frozenset()

    def test_a_record_just_under_the_limit_still_loads(self, tmp_path, monkeypatch):
        _owned_by(monkeypatch, 0)
        ref = next(iter(PROFILES))
        body = {"promoted": [{"profile_id": ref.profile_id, "version": ref.version}],
                "note": "x" * 1000}
        assert promo.load_promotions(_record(tmp_path, body)) == frozenset({ref})
