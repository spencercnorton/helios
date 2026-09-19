"""GTK-free compatibility gate for the pinned Codex App Server schema."""

from __future__ import annotations

import ast
import json
from pathlib import Path

from helios.backend.process import codex_protocol


_ROOT = Path(__file__).resolve().parent.parent
_MANIFEST = (
    _ROOT
    / "docs"
    / "protocol"
    / f"codex-app-server-{codex_protocol.CODEX_PROTOCOL_VERSION}.json"
)


def _methods() -> dict[str, set[str]]:
    payload = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    assert payload["schema"] == "codex-app-server-methods"
    assert payload["experimental"] is True
    assert payload["codex_version"] == codex_protocol.CODEX_PROTOCOL_VERSION
    return {name: set(values) for name, values in payload["methods"].items()}


def test_every_server_notification_has_exactly_one_disposition():
    generated = _methods()["server_notifications"]
    handled = set(codex_protocol.HANDLED_NOTIFICATION_METHODS)
    ignored = set(codex_protocol.IGNORED_NOTIFICATION_METHODS)
    unsupported = set(codex_protocol.UNSUPPORTED_NOTIFICATION_METHODS)

    assert not (handled & ignored)
    assert not (handled & unsupported)
    assert not (ignored & unsupported)
    assert handled | ignored | unsupported == generated
    assert codex_protocol.KNOWN_NOTIFICATION_METHODS == generated


def test_every_server_request_is_supported_or_explicitly_denied():
    generated = _methods()["server_requests"]
    supported = set(codex_protocol.SUPPORTED_SERVER_REQUEST_METHODS)
    denied = set(codex_protocol.DENIED_SERVER_REQUEST_METHODS)

    assert not (supported & denied)
    assert supported | denied == generated
    assert codex_protocol.KNOWN_SERVER_REQUEST_METHODS == generated
    assert all(codex_protocol.DENIED_SERVER_REQUEST_METHODS.values())


def test_every_helios_client_method_exists_in_the_generated_schema():
    generated = _methods()
    assert (
        codex_protocol.CLIENT_REQUEST_METHODS_USED
        <= generated["client_requests"]
    )
    assert (
        codex_protocol.CLIENT_NOTIFICATION_METHODS_USED
        <= generated["client_notifications"]
    )


def _literal_wire_methods(path: Path) -> set[str]:
    """Find literal methods sent through request/call or JSON-RPC objects."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    methods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"call", "request"} and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    methods.add(first.value)
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            if (
                isinstance(key, ast.Constant)
                and key.value == "method"
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                methods.add(value.value)
    return methods


def test_literal_production_wire_methods_are_declared_by_the_registry():
    process = _ROOT / "src" / "helios" / "backend" / "process"
    paths = (
        _ROOT / "src" / "helios" / "backend" / "codex_env.py",
        process / "codex_app_driver.py",
        process / "codex_app_hub.py",
        process / "codex_app_server.py",
    )
    observed = set().union(*(_literal_wire_methods(path) for path in paths))
    declared = (
        codex_protocol.CLIENT_REQUEST_METHODS_USED
        | codex_protocol.CLIENT_NOTIFICATION_METHODS_USED
    )
    assert observed <= declared


def test_classifier_distinguishes_schema_gaps_from_protocol_drift():
    assert codex_protocol.classify_notification("thread/started") == "handled"
    assert codex_protocol.classify_notification("warning") == "handled"
    assert codex_protocol.classify_notification("configWarning") == "handled"
    assert codex_protocol.classify_notification("thread/status/changed") == "ignored"
    assert codex_protocol.classify_notification("thread/compacted") == "handled"
    assert codex_protocol.classify_notification("thread/futureEvent") == "unknown"

    assert (
        codex_protocol.classify_server_request("item/tool/requestUserInput")
        == "supported"
    )
    assert (
        codex_protocol.classify_server_request("attestation/generate") == "denied"
    )
    assert codex_protocol.classify_server_request("future/request") == "unknown"
