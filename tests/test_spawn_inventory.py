"""Per-call static guard for every direct child-process spawn.

A model/tool child that inherits Helios's whole environment leaks credentials
(see helios.backend.process.env_scrub). The AST inventory enforces the policy at
each call site, not merely once per module:

* ``subprocess.run``/``Popen`` and the App Server's injected Popen factory must
  pass an explicit non-None ``env=``;
* every ``Gio.SubprocessLauncher.spawnv`` receiver must have been passed to
  ``scrub_helios_env`` earlier in the same lexical scope.

This remains a static structural check, not a proof that each purpose selected
the right provider keep-set; focused runtime tests enforce those exact sets.
OS URI activation (Gtk.UriLauncher/Gio.AppInfo) is intentionally not classified
as a Helios child: the desktop owns that application's process/environment.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src" / "helios"

_SUBPROCESS_CALLS = {"subprocess.run", "subprocess.Popen"}


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _expr_key(node: ast.expr) -> str:
    """Stable identity for a simple Name/Attribute launcher expression."""
    return _call_name(node)


def _is_subprocess_call(name: str) -> bool:
    leaf = name.rsplit(".", 1)[-1]
    return name in _SUBPROCESS_CALLS or leaf in {
        "popen_factory",
        "_popen_factory",
    }


def _calls_in_scope(scope: ast.AST) -> list[ast.Call]:
    """Calls in one lexical scope, excluding nested function/class bodies."""
    calls: list[ast.Call] = []

    class Visitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 — ast API
            calls.append(node)
            self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
            if node is scope:
                self.generic_visit(node)

        def visit_AsyncFunctionDef(  # noqa: N802
            self,
            node: ast.AsyncFunctionDef,
        ) -> None:
            if node is scope:
                self.generic_visit(node)

        def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
            if node is scope:
                self.generic_visit(node)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
            if node is scope:
                self.generic_visit(node)

    visitor = Visitor()
    if isinstance(scope, ast.Module):
        for statement in scope.body:
            if not isinstance(
                statement,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
            ):
                visitor.visit(statement)
    else:
        visitor.visit(scope)
    return calls


def _scopes(tree: ast.Module) -> list[ast.AST]:
    return [
        tree,
        *[
            node
            for node in ast.walk(tree)
            if isinstance(
                node,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef),
            )
        ],
    ]


def _spawn_calls(path: Path) -> list[tuple[ast.Call, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[ast.Call, str]] = []
    for scope in _scopes(tree):
        for call in _calls_in_scope(scope):
            name = _call_name(call.func)
            if _is_subprocess_call(name):
                found.append((call, "subprocess"))
            elif isinstance(call.func, ast.Attribute) and call.func.attr == "spawnv":
                found.append((call, "gio"))
    return found


def _spawn_modules() -> list[str]:
    mods = []
    for py in _SRC.rglob("*.py"):
        if py.name == "env_scrub.py":
            continue  # defines the policy; not a spawn site
        if _spawn_calls(py):
            mods.append(str(py.relative_to(_SRC)))
    return sorted(mods)


def _policy_offenders(path: Path, *, label: str | None = None) -> list[str]:
    rel = label or str(path.relative_to(_SRC))
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[str] = []
    for scope in _scopes(tree):
        calls = _calls_in_scope(scope)
        scrubbed_launchers = {
            _expr_key(call.args[0]): call.lineno
            for call in calls
            if _call_name(call.func).endswith("scrub_helios_env")
            and call.args
            and _expr_key(call.args[0])
        }
        for call in calls:
            name = _call_name(call.func)
            if _is_subprocess_call(name):
                env_kw = next((kw for kw in call.keywords if kw.arg == "env"), None)
                if env_kw is None or (
                    isinstance(env_kw.value, ast.Constant)
                    and env_kw.value.value is None
                ):
                    offenders.append(f"{rel}:{call.lineno} missing env=")
            elif isinstance(call.func, ast.Attribute) and call.func.attr == "spawnv":
                receiver = _expr_key(call.func.value)
                scrub_line = scrubbed_launchers.get(receiver, 0)
                if not scrub_line or scrub_line >= call.lineno:
                    offenders.append(
                        f"{rel}:{call.lineno} {receiver}.spawnv before scrub"
                    )
    return offenders


def test_spawn_sites_are_discovered():
    # Sanity: the guard actually finds the known spawn modules (not a no-op).
    mods = set(_spawn_modules())
    for expected in (
        "backend/process/cli_driver.py",
        "backend/process/codex_driver.py",
        "backend/process/codex_app_server.py",
        "backend/process/title_generator.py",
        "backend/codex_env.py",
        "backend/claude_env.py",
        "backend/claude_binary.py",
        "backend/model_catalog.py",
        "backend/session_archiver.py",
        "widgets/mission_pane.py",
    ):
        assert expected in mods, f"{expected} not detected as a spawn site"


def test_class_body_spawn_is_inventoried(tmp_path: Path):
    source = tmp_path / "class_spawn.py"
    source.write_text(
        "import subprocess\n"
        "class Unsafe:\n"
        "    child = subprocess.run(['true'])\n",
        encoding="utf-8",
    )

    assert _policy_offenders(source, label="class_spawn.py") == [
        "class_spawn.py:3 missing env="
    ]


def test_every_spawn_call_applies_env_scrub_policy():
    offenders: list[str] = []
    for rel in _spawn_modules():
        offenders.extend(_policy_offenders(_SRC / rel, label=rel))
    assert not offenders, (
        "spawn calls not applying env_scrub at the call site: "
        f"{offenders}"
    )


def test_exec_only_codex_key_has_one_runtime_consumer():
    """CODEX_API_KEY may reach the real ``codex exec`` fallback and no other
    runtime module. Native App Server/discovery and non-model tandem approval
    must not grow an accidental keep exception."""
    consumers = []
    for py in _SRC.rglob("*.py"):
        if py.name == "env_scrub.py":
            continue
        if "CODEX_EXEC_ENV" in py.read_text(encoding="utf-8"):
            consumers.append(str(py.relative_to(_SRC)))
    assert consumers == ["backend/process/codex_driver.py"]
