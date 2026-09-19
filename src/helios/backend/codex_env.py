"""OpenAI Codex CLI introspection — binary discovery, auth, key access.

Mirrors claude_env's role for the second backend. GTK-free.

Auth model (codex-cli ≥ 0.139): the native CLI/App Server no longer honors a
bare ``OPENAI_API_KEY``. Keys go through ``codex login --with-api-key`` (stdin),
which persists through Codex's configured credential store (file, keyring,
auto, or ephemeral). Helios therefore asks ``codex login status`` for current
login truth instead of treating ``auth.json`` metadata as authoritative.

Helios never reads credentials back from ``auth.json``. ``codex login status``
proves only that a mode is live, not which exact file/keyring/ephemeral
credential owns it; the model picker therefore asks App Server directly rather
than coupling generic status to a possibly stale file key.

``CODEX_API_KEY`` is a separate, exec-only credential. It is forwarded only
by the legacy ``codex exec`` fallback and is intentionally irrelevant here.
Direct env-only ``CODEX_ACCESS_TOKEN`` auth is outside Helios's H1 boundary;
persist it first with ``codex login --with-access-token`` when needed.
"""

from __future__ import annotations

import json
import os
import selectors
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from helios.backend.process.env_scrub import scrubbed_child_env


class CodexBinaryNotFound(RuntimeError):
    pass


class CodexAppServerError(RuntimeError):
    """The local Codex App Server could not satisfy a JSON-RPC request."""


@dataclass(frozen=True, slots=True)
class CodexAppServerCapabilities:
    """Authoritative picker capabilities from one initialized App Server."""

    models: tuple[dict, ...]
    collaboration_modes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CodexBinary:
    path: Path
    source: str  # "env" | "PATH" | "local-bin"

    def __str__(self) -> str:
        return f"{self.path} (via {self.source})"


def find_codex_binary() -> CodexBinary:
    """Same lookup discipline as claude_binary: explicit override, PATH,
    then ~/.local/bin (which GUI sessions don't have on PATH)."""
    env = os.environ.get("HELIOS_CODEX_BINARY")
    if env:
        p = Path(env).expanduser()
        if _is_runnable(p):
            return CodexBinary(path=p, source="env")

    on_path = shutil.which("codex")
    if on_path:
        p = Path(on_path).resolve()
        if _is_runnable(p):
            return CodexBinary(path=p, source="PATH")

    local_bin = Path.home() / ".local" / "bin" / "codex"
    if _is_runnable(local_bin):
        return CodexBinary(path=local_bin.resolve(), source="local-bin")

    raise CodexBinaryNotFound(
        "OpenAI Codex CLI not found. Install it (`npm install -g --prefix "
        "~/.local @openai/codex`) or set $HELIOS_CODEX_BINARY."
    )


def _is_runnable(p: Path) -> bool:
    try:
        return p.is_file() and os.access(p, os.X_OK)
    except OSError:
        return False


# ── auth ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CodexAuth:
    ok: bool
    logged_in: bool = False
    detail: str = ""  # safe human label (never a raw credential) or error
    # "chatgpt" | "apikey" | "access_token" | "authenticated" | ""
    mode: str = ""


def auth_mode(status: CodexAuth | None = None) -> str:
    """Return the CLI-confirmed current auth mode, or ``""`` when logged out.

    ``auth.json`` is intentionally not consulted: Codex may store credentials
    in a keyring/ephemeral backend, and a stale file may retain an ``auth_mode``
    after its usable credentials are gone. Callers that already queried status
    can pass it to avoid spawning ``codex login status`` twice.
    """
    current = status if status is not None else fetch_auth_status()
    return current.mode if current.ok and current.logged_in else ""


# Auth modes that bill by SUBSCRIPTION, not per token. Mirrors
# claude_env._SUBSCRIPTION_AUTH_METHODS; see is_subscription_billing.
_SUBSCRIPTION_AUTH_MODES = frozenset({"chatgpt"})

_billing_cache: dict[str, bool] = {}


def is_subscription_billing(*, refresh: bool = False) -> bool:
    """Whether this Codex account is billed by subscription, not per token.

    The exact counterpart of :func:`helios.backend.claude_env.
    is_subscription_billing`, and it exists for the same reason. On a ChatGPT
    subscription a token is not money, so a lifetime *token* cap measures
    nothing billable — it is the 2026-08-05 dollar-cap defect in
    token units. The real ceiling on these accounts is the 5-hour/weekly
    meter, which the driver already receives as ``account/rateLimits/updated``.

    An explicit ``CODEX_API_KEY`` wins: that is real per-token billing even
    when a ChatGPT login also exists. Fails CLOSED to False (= keep the token
    cap) when auth cannot be read, so an unreadable status never silently
    removes a real spend control.
    """

    if os.environ.get("CODEX_API_KEY"):
        return False
    if not refresh and "subscription" in _billing_cache:
        return _billing_cache["subscription"]
    subscription = auth_mode() in _SUBSCRIPTION_AUTH_MODES
    _billing_cache["subscription"] = subscription
    return subscription


def _mode_from_status_text(text: str) -> str:
    """Classify only auth modes explicitly named by ``codex login status``."""
    lower = text.casefold()
    if "api key" in lower:
        return "apikey"
    if "access token" in lower:
        return "access_token"
    if "chatgpt" in lower:
        return "chatgpt"
    return "authenticated"


def _safe_auth_detail(mode: str) -> str:
    """A credential-free label for Settings, independent of CLI formatting."""
    return {
        "apikey": "OpenAI API key",
        "chatgpt": "ChatGPT account",
        "access_token": "OpenAI access token",
        "authenticated": "Codex account",
    }.get(mode, "Codex account")


def fetch_auth_status(timeout: float = 15.0) -> CodexAuth:
    """`codex login status` — exit 0 + a "Logged in using …" line when authed."""
    try:
        binary = find_codex_binary()
    except CodexBinaryNotFound as e:
        return CodexAuth(ok=False, detail=str(e))
    try:
        out = subprocess.run(
            [str(binary.path), "login", "status"],
            capture_output=True, text=True, timeout=timeout,
            # Native auth reads ~/.codex; no env key is honored, so forward none.
            env=scrubbed_child_env(),
        )
    except (OSError, subprocess.SubprocessError) as e:
        return CodexAuth(ok=False, detail=f"codex login status failed: {e}")
    text = ((out.stdout or "") + (out.stderr or "")).strip()
    lower = text.casefold()
    logged_in = (
        out.returncode == 0
        and "logged in" in lower
        and "not logged in" not in lower
    )
    if logged_in:
        mode = _mode_from_status_text(text)
        return CodexAuth(
            ok=True,
            logged_in=True,
            detail=_safe_auth_detail(mode),
            mode=mode,
        )
    return CodexAuth(ok=True, logged_in=False, detail=text or "Not logged in")


def login_with_api_key(api_key: str, timeout: float = 30.0) -> tuple[bool, str]:
    """Persist a pasted key via `codex login --with-api-key` (key on stdin,
    never argv — argv is world-readable in /proc)."""
    api_key = (api_key or "").strip()
    if not api_key:
        return False, "Empty key."
    try:
        binary = find_codex_binary()
    except CodexBinaryNotFound as e:
        return False, str(e)
    try:
        out = subprocess.run(
            [str(binary.path), "login", "--with-api-key"],
            input=api_key, capture_output=True, text=True, timeout=timeout,
            # Key arrives on stdin; no env key needed.
            env=scrubbed_child_env(),
        )
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"codex login failed: {e}"
    if out.returncode == 0:
        return True, "Key saved by Codex's configured credential store."
    err = (out.stderr or out.stdout or "").strip()
    return False, err.splitlines()[-1] if err else "codex login failed."


def codex_version(timeout: float = 10.0) -> str:
    try:
        binary = find_codex_binary()
        out = subprocess.run(
            [str(binary.path), "--version"],
            capture_output=True, text=True, timeout=timeout,
            env=scrubbed_child_env(),  # --version needs no credentials
        )
        return (out.stdout or "").strip()
    except (CodexBinaryNotFound, OSError, subprocess.SubprocessError):
        return ""


# ── Native App Server snapshots ───────────────────────────────────────────


def _helios_data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "helios"


_MCP_SNAPSHOT_PATH = _helios_data_dir() / "codex_mcp_snapshot.json"


def save_mcp_snapshot(servers: list[dict]) -> None:
    """Persist the latest shared App Server MCP inventory, best effort."""

    try:
        directory = _helios_data_dir()
        directory.mkdir(parents=True, exist_ok=True)
        payload = {"servers": servers or [], "saved_at": time.time()}
        tmp = _MCP_SNAPSHOT_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(_MCP_SNAPSHOT_PATH)
    except OSError:
        pass


def load_mcp_snapshot() -> list[dict]:
    """Return the last live Codex MCP inventory without starting a server."""

    try:
        payload = json.loads(_MCP_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = payload.get("servers") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


# ── App Server capability discovery ─────────────────────────────────────


def _write_rpc(proc: subprocess.Popen, message: dict) -> None:
    if proc.stdin is None:
        raise CodexAppServerError("Codex App Server stdin is unavailable")
    proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
    proc.stdin.flush()


def _read_rpc_response(
    proc: subprocess.Popen,
    request_id: int,
    *,
    deadline: float,
) -> dict:
    """Read JSONL until ``request_id`` responds, ignoring notifications.

    App Server may emit status notifications between request and response.
    ``selectors`` keeps the discovery worker bounded even when a broken or
    incompatible CLI leaves stdout open without answering.
    """
    if proc.stdout is None:
        raise CodexAppServerError("Codex App Server stdout is unavailable")
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexAppServerError(
                    f"Codex App Server request {request_id} timed out"
                )
            if not selector.select(remaining):
                raise CodexAppServerError(
                    f"Codex App Server request {request_id} timed out"
                )
            line = proc.stdout.readline()
            if not line:
                raise CodexAppServerError(
                    f"Codex App Server exited before request {request_id} completed"
                )
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") != request_id:
                continue
            if message.get("error"):
                error = message["error"]
                detail = error.get("message") if isinstance(error, dict) else str(error)
                raise CodexAppServerError(detail or "Codex App Server request failed")
            result = message.get("result")
            if not isinstance(result, dict):
                raise CodexAppServerError(
                    f"Codex App Server request {request_id} returned no result"
                )
            return result
    finally:
        selector.close()


def fetch_app_server_capabilities(
    timeout: float = 15.0,
) -> CodexAppServerCapabilities:
    """Return models and collaboration modes from the same App Server.

    This is the authoritative catalog for the installed Codex build and its
    current authentication mode.  Unlike ``GET /v1/models``, it also tells a
    client which reasoning efforts, modalities, defaults, and service tiers
    Codex can actually use.
    """
    binary = find_codex_binary()
    try:
        proc = subprocess.Popen(
            [str(binary.path), "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            # Native discovery uses the Codex-managed login (file/keyring/etc.);
            # forward no provider env key into it or its tool children.
            env=scrubbed_child_env(),
            start_new_session=True,
        )
    except OSError as exc:
        raise CodexAppServerError(f"could not start Codex App Server: {exc}") from exc

    deadline = time.monotonic() + timeout
    try:
        _write_rpc(proc, {
            "method": "initialize",
            "id": 1,
            "params": {
                "clientInfo": {
                    "name": "helios",
                    "title": "Helios",
                    "version": "model-catalog",
                },
                "capabilities": {"experimentalApi": True},
            },
        })
        _read_rpc_response(proc, 1, deadline=deadline)
        _write_rpc(proc, {"method": "initialized", "params": {}})
        _write_rpc(proc, {
            "method": "model/list",
            "id": 2,
            "params": {"limit": 100, "includeHidden": False},
        })
        model_result = _read_rpc_response(proc, 2, deadline=deadline)
        model_data = model_result.get("data")
        if not isinstance(model_data, list):
            raise CodexAppServerError("Codex App Server returned an invalid model list")
        mode_data: list = []
        try:
            _write_rpc(proc, {
                "method": "collaborationMode/list",
                "id": 3,
                "params": {},
            })
            mode_result = _read_rpc_response(proc, 3, deadline=deadline)
            discovered_modes = mode_result.get("data")
            if not isinstance(discovered_modes, list):
                raise CodexAppServerError(
                    "Codex App Server returned an invalid collaboration mode list"
                )
            mode_data = discovered_modes
        except (CodexAppServerError, OSError, BrokenPipeError):
            # Workflow discovery is optional. A model catalog already returned
            # by this authenticated server remains authoritative; Plan simply
            # stays unavailable and the live driver rechecks before binding.
            mode_data = []
        modes: list[str] = []
        for row in mode_data:
            mode = row.get("mode") if isinstance(row, dict) else None
            if isinstance(mode, str) and mode and mode not in modes:
                modes.append(mode)
        return CodexAppServerCapabilities(
            models=tuple(row for row in model_data if isinstance(row, dict)),
            collaboration_modes=tuple(modes),
        )
    except (OSError, BrokenPipeError) as exc:
        raise CodexAppServerError(f"Codex App Server communication failed: {exc}") from exc
    finally:
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1.0)


def fetch_app_server_models(timeout: float = 15.0) -> list[dict]:
    """Compatibility wrapper returning only authoritative App Server models."""

    return list(fetch_app_server_capabilities(timeout).models)
