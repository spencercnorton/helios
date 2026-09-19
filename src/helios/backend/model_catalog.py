"""Dynamic model catalog — the picker stays current without code changes.

Two providers, two discovery paths:

**Anthropic** — scraped from the installed `claude` binary. The CLI bundle
embeds every model id it supports (`claude-fable-5`, `claude-opus-4-8`, …)
plus the quoted alias strings (`"fable[1m]"`, `"opus[1m]"`). When the CLI
self-updates, the binary's mtime changes, the scan re-runs, and newly
released models appear in the picker — including brand-new *families*,
which the old hardcoded list could never track (Fable had to be patched in
by hand; that edit is what motivated this module).

Family gating: the binary also embeds ids for unreleased/internal families
(e.g. `claude-mythos-5` today), so raw ids can't be trusted alone. A family
is shown when ANY of:
  * it's in the known seed (fable/opus/sonnet/haiku),
  * `claude --help` mentions it as a `--model` alias example (the help text
    gained 'fable' when Fable shipped, so this tracks launches),
  * the binary contains a quoted `"<family>[1m]"` alias string (only real,
    selectable families get the 1M-context alias plumbing).

**OpenAI** — discovered through Codex App Server ``model/list``. This is the
same authenticated, version-aware catalog Codex clients use and includes the
supported reasoning efforts, default, modalities, and service tiers. A
last-known-good subscription list remains as a ChatGPT-only informational
fallback for diagnosing older/broken Codex installs. Those curated rows are
never account-entitlement or activation proof; callers must only make models
selectable when the diagnostic status is ``app-server``. API-key and opaque
auth modes fail closed when App Server cannot prove the current account's
capabilities.

Everything here is GTK-free so the CI test image (python-slim) can exercise
it. Callers run the slow paths (binary scan ~1-2 s, network fetch) on worker
threads and re-apply entries via GLib.idle_add.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from helios.backend.process.env_scrub import scrubbed_child_env

from helios.log import get_logger

if TYPE_CHECKING:
    from helios.backend.codex_env import CodexAuth

_log = get_logger("models")

_CATALOG_CACHE = Path.home() / ".helios" / "model-catalog.json"
# Bump whenever the Anthropic picker policy changes, so a catalog cached under
# the old policy is rebuilt instead of served. rev 2 restores the 1M aliases and
# the settings-owned default that rev 1 (P0 containment) filtered out.
_ANTHROPIC_POLICY_REV = 2

# Priority order for display; also the seed of families known to be real.
KNOWN_FAMILIES: tuple[str, ...] = ("fable", "opus", "sonnet", "haiku")

PROVIDER_ANTHROPIC = "anthropic"
PROVIDER_OPENAI = "openai"
PROVIDER_OPENROUTER = "openrouter"

#: User-facing provider names for toasts, scope chips, and notifications.
PROVIDER_LABELS: dict[str, str] = {
    PROVIDER_ANTHROPIC: "Claude",
    PROVIDER_OPENAI: "GPT",
    PROVIDER_OPENROUTER: "OpenRouter",
}

# Last capabilities proved by the same live App Server identity check as the
# OpenAI model catalog. Default is always safe; Plan is offered only after the
# installed server advertises it. The driver independently rechecks at bind.
_OPENAI_WORKFLOW_MODES: tuple[str, ...] = ("default",)


@dataclass(frozen=True, slots=True)
class ModelEntry:
    """One row in the model picker."""

    id: str  # alias or full model id; "" = claude settings default
    label: str
    group: str  # cosmetic section header
    provider: str = PROVIDER_ANTHROPIC
    description: str = ""
    reasoning_efforts: tuple[tuple[str, str], ...] = ()
    default_effort: str = ""
    input_modalities: tuple[str, ...] = ()
    service_tiers: tuple[tuple[str, str, str], ...] = ()
    is_default: bool = False


# Last-known-good list, used only when the binary can't be found/scanned.
# Mirrors what discovery produces today so the picker never goes empty.
FALLBACK_ANTHROPIC: list[ModelEntry] = [
    ModelEntry("fable[1m]", "Fable (latest · 1M context)", "Latest"),
    ModelEntry("fable", "Fable (latest)", "Latest"),
    ModelEntry("opus[1m]", "Opus (latest · 1M context)", "Latest"),
    ModelEntry("opus", "Opus (latest)", "Latest"),
    ModelEntry("sonnet[1m]", "Sonnet (latest · 1M context)", "Latest"),
    ModelEntry("sonnet", "Sonnet (latest)", "Latest"),
    ModelEntry("haiku", "Haiku (latest)", "Latest"),
    ModelEntry("", "Default (from Claude settings)", "Other"),
    ModelEntry("claude-fable-5", "Fable 5", "Fable"),
    ModelEntry("claude-opus-4-8", "Opus 4.8", "Opus"),
    ModelEntry("claude-opus-4-7", "Opus 4.7", "Opus"),
    ModelEntry("claude-opus-4-6", "Opus 4.6", "Opus"),
    ModelEntry("claude-opus-4-5", "Opus 4.5", "Opus"),
    ModelEntry("claude-sonnet-4-6", "Sonnet 4.6", "Sonnet"),
    ModelEntry("claude-sonnet-4-5", "Sonnet 4.5", "Sonnet"),
    ModelEntry("claude-haiku-4-5", "Haiku 4.5", "Haiku"),
]


# ── Anthropic: scan the claude binary ──────────────────────────────────────

# Full ids embedded in the CLI bundle. Family is alphabetic, version starts
# with a digit: claude-fable-5, claude-opus-4-8, claude-haiku-4-5-20251001…
_ID_RE = re.compile(rb"claude-([a-z]{3,16})-(\d[a-zA-Z0-9-]{0,40})")
# Quoted 1M-context alias strings: "fable[1m]", "opus[1m]"…
_ONEM_RE = re.compile(rb'"([a-z]{3,16})\[1m\]"')
# Alias examples inside `claude --help`'s --model section: 'fable', 'opus'…
_HELP_ALIAS_RE = re.compile(r"'([a-z]{3,16})'")

_SCAN_CHUNK = 8 * 1024 * 1024
_SCAN_OVERLAP = 64  # longest match straddling a chunk boundary


def scan_claude_binary(path: Path) -> dict:
    """Single pass over the binary: every embedded model id + every family
    with a quoted `[1m]` alias. Returns {"ids": [...], "onem": [...]}."""
    ids: set[bytes] = set()
    onem: set[bytes] = set()
    with open(path, "rb") as f:
        tail = b""
        while True:
            chunk = f.read(_SCAN_CHUNK)
            if not chunk:
                break
            buf = tail + chunk
            ids.update(m.group(0) for m in _ID_RE.finditer(buf))
            onem.update(m.group(1) for m in _ONEM_RE.finditer(buf))
            tail = buf[-_SCAN_OVERLAP:]
    return {
        "ids": sorted(i.decode("ascii", "ignore") for i in ids),
        "onem": sorted(o.decode("ascii", "ignore") for o in onem),
    }


def parse_help_aliases(help_text: str) -> set[str]:
    """Families named as alias examples in the `--model` help section."""
    m = re.search(r"--model\b(.{0,400})", help_text, re.DOTALL)
    if not m:
        return set()
    found = set(_HELP_ALIAS_RE.findall(m.group(1)))
    # Drop full-id examples like 'claude-fable-5' (regex already excludes
    # hyphenated matches) and obvious non-alias words.
    return {f for f in found if f != "claude"}


def fetch_help_aliases(binary: Path, timeout: float = 10.0) -> set[str]:
    try:
        out = subprocess.run(
            [str(binary), "--help"],
            capture_output=True, text=True, timeout=timeout,
            env=scrubbed_child_env(),  # --help needs no credentials
        )
        return parse_help_aliases(out.stdout or "")
    except (OSError, subprocess.SubprocessError):
        return set()


def family_gate(scanned_families: set[str], onem: set[str], help_aliases: set[str]) -> list[str]:
    """Which scraped families to show, in display order. Seed ∪ help ∪ [1m]."""
    allowed = set(KNOWN_FAMILIES) | help_aliases | onem
    gated = [f for f in scanned_families if f in allowed]
    prio = {f: i for i, f in enumerate(KNOWN_FAMILIES)}
    return sorted(gated, key=lambda f: (prio.get(f, len(prio)), f))


@dataclass(frozen=True, slots=True)
class _PinnedId:
    family: str
    version: tuple[int, ...]
    date: str  # "20251001" or ""
    id: str


def _parse_pinned(model_id: str) -> _PinnedId | None:
    m = _ID_RE.fullmatch(model_id.encode("ascii", "ignore"))
    if not m:
        return None
    family = m.group(1).decode()
    rest = m.group(2).decode()
    parts = rest.split("-")
    version: list[int] = []
    date = ""
    for i, p in enumerate(parts):
        if re.fullmatch(r"20\d{6}", p):
            date = p
            trailing = parts[i + 1:]
            # Anything after the date (-v1, -fast, regional variants) is a
            # non-canonical alias of the same model — skip the whole id.
            if trailing:
                return None
            break
        if p.isdigit():
            version.append(int(p))
        else:
            # Non-numeric token (fast, v1, latest…) → variant id, skip.
            return None
    if not version:
        return None
    return _PinnedId(family=family, version=tuple(version), date=date, id=model_id)


def clean_pinned_ids(ids: list[str], families: list[str]) -> list[_PinnedId]:
    """Filter scraped ids down to one canonical entry per (family, version).

    Drops variant suffixes (-v1, -fast), collapses date-stamped twins onto
    the short form, and drops a bare `fam-N` when `fam-N-0` exists (they
    name the same model)."""
    fam_set = set(families)
    parsed = [p for p in (_parse_pinned(i) for i in ids) if p is not None]
    parsed = [p for p in parsed if p.family in fam_set]

    # Prefer the short (undated) form per (family, version).
    by_key: dict[tuple[str, tuple[int, ...]], _PinnedId] = {}
    for p in parsed:
        key = (p.family, p.version)
        cur = by_key.get(key)
        if cur is None or (cur.date and not p.date):
            by_key[key] = p

    # Bare major (4,) duplicates an explicit (4, 0) — keep the explicit one.
    keys = set(by_key)
    for fam, ver in list(keys):
        if len(ver) == 1 and (fam, (*ver, 0)) in keys:
            del by_key[(fam, ver)]

    prio = {f: i for i, f in enumerate(KNOWN_FAMILIES)}

    def sort_key(p: _PinnedId):
        pad = p.version + (0,) * (4 - len(p.version))
        return (prio.get(p.family, len(prio)), p.family, tuple(-v for v in pad))

    return sorted(by_key.values(), key=sort_key)


def _label_for_pinned(p: _PinnedId) -> str:
    name = p.family.capitalize()
    ver = ".".join(str(v) for v in p.version)
    if p.date:
        d = f"{p.date[:4]}-{p.date[4:6]}-{p.date[6:]}"
        return f"{name} {ver} ({d})"
    return f"{name} {ver}"


def build_anthropic_entries(scan: dict, help_aliases: set[str]) -> list[ModelEntry]:
    """Assemble picker rows from a binary scan: alias rows first (these are
    the never-stale part), then the settings default, then pinned versions
    grouped per family."""
    parsed_families = {p.family for p in
                       (_parse_pinned(i) for i in scan.get("ids", []))
                       if p is not None}
    onem = set(scan.get("onem", []))
    families = family_gate(parsed_families, onem, help_aliases)
    if not families:
        return list(FALLBACK_ANTHROPIC)

    entries: list[ModelEntry] = []
    for fam in families:
        name = fam.capitalize()
        if fam in onem:
            entries.append(ModelEntry(f"{fam}[1m]", f"{name} (latest · 1M context)", "Latest"))
        entries.append(ModelEntry(fam, f"{name} (latest)", "Latest"))

    entries.append(ModelEntry("", "Default (from Claude settings)", "Other"))

    for p in clean_pinned_ids(scan.get("ids", []), families):
        entries.append(ModelEntry(p.id, _label_for_pinned(p), p.family.capitalize()))
    return entries


def _binary_fingerprint(path: Path) -> dict:
    st = path.stat()
    return {"path": str(path), "mtime_ns": st.st_mtime_ns, "size": st.st_size}


def anthropic_entries(*, force: bool = False) -> list[ModelEntry]:
    """Cached-by-binary-fingerprint discovery. Fast (one stat) when the CLI
    hasn't changed; full rescan (~1-2 s) when it has — i.e. exactly when an
    update may have introduced new models."""
    try:
        from helios.backend.claude_binary import find_claude_binary

        binary = find_claude_binary().path
        fp = _binary_fingerprint(binary)
    except Exception:
        return list(FALLBACK_ANTHROPIC)

    cached = _read_json(_CATALOG_CACHE)
    if (
        not force
        and cached.get("fingerprint") == fp
        and cached.get("anthropic_policy_rev") == _ANTHROPIC_POLICY_REV
        and cached.get("entries")
    ):
        return _entries_from_cache(cached["entries"])

    try:
        scan = scan_claude_binary(binary)
        help_aliases = fetch_help_aliases(binary)
        entries = build_anthropic_entries(scan, help_aliases)
    except Exception as e:  # noqa: BLE001 — discovery must never break the app
        _log.warning("model discovery failed (%s); using fallback list", e)
        return list(FALLBACK_ANTHROPIC)

    _write_json(_CATALOG_CACHE, {
        "fingerprint": fp,
        "anthropic_policy_rev": _ANTHROPIC_POLICY_REV,
        "scanned_at": int(time.time()),
        "entries": [[e.id, e.label, e.group] for e in entries],
    })
    _log.info("model catalog rescanned: %d entries from %s", len(entries), binary)
    return entries


def _entries_from_cache(rows: list) -> list[ModelEntry]:
    out = []
    for r in rows:
        try:
            out.append(ModelEntry(str(r[0]), str(r[1]), str(r[2])))
        except (IndexError, TypeError):
            continue
    return out or list(FALLBACK_ANTHROPIC)


def claude_binary_changed() -> bool:
    """One stat — used by the periodic in-app check to decide whether a
    rescan is worth scheduling (the app runs for days between restarts)."""
    try:
        from helios.backend.claude_binary import find_claude_binary

        fp = _binary_fingerprint(find_claude_binary().path)
    except Exception:
        return False
    cached = _read_json(_CATALOG_CACHE)
    return (
        cached.get("fingerprint") != fp
        or cached.get("anthropic_policy_rev") != _ANTHROPIC_POLICY_REV
    )


# ── OpenAI: Codex App Server model/list ───────────────────────────────────

# Agent-capable families only. Everything else (embeddings, audio, images,
# moderation, generic ChatGPT aliases…) is either unusable in Codex or creates
# the "regular chatbot" experience Helios is deliberately avoiding.
_OPENAI_INCLUDE_RE = re.compile(r"^(gpt-\d|o\d|codex)")
_OPENAI_PROVIDER_RE = re.compile(r"^(gpt-\d|o\d|codex|chatgpt)")
_OPENAI_EXCLUDE = (
    "embedding", "audio", "tts", "whisper", "dall-e", "moderation",
    "realtime", "transcribe", "image", "search-preview", "search-api",
    "instruct", "chat-latest", "deep-research",
)


# Last-known-good subscription catalog. This is informational resilience only:
# the rows help diagnose older/broken Codex installs but MUST NOT authorize or
# activate a model. Callers may expose selectable GPT rows only when the source
# status is ``app-server``. Keep the concrete Codex variants rather than the
# API's generic ``gpt-5.6`` alias because capability metadata differs by
# variant.
FALLBACK_OPENAI_SUBSCRIPTION: tuple[str, ...] = (
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.3-codex-spark",
)


_OPENAI_DATE_SUFFIX_RE = re.compile(r"-20\d{2}-\d{2}-\d{2}$")


def preferred_openai_model(entries: list[ModelEntry]) -> str:
    """Best default for the GPT header toggle: App Server default, then first."""
    return next((entry.id for entry in entries if entry.is_default), entries[0].id if entries else "")


def _openai_label(mid: str) -> str:
    # Trailing snapshot date → parenthesized suffix on the base label.
    date = ""
    m = _OPENAI_DATE_SUFFIX_RE.search(mid)
    if m:
        date = f" ({m.group(0)[1:]})"
        mid = _OPENAI_DATE_SUFFIX_RE.sub("", mid)
    if mid.startswith("gpt-"):
        rest = mid[4:]
        parts = rest.split("-")
        label = "GPT-" + parts[0]
        if len(parts) > 1:
            label += " " + " ".join(p.capitalize() for p in parts[1:])
        return label + date
    if mid.startswith("chatgpt"):
        return mid.replace("chatgpt", "ChatGPT", 1) + date
    return mid + date  # o-series and codex ids read best verbatim


def build_openai_entries(model_ids: list[tuple[str, int]]) -> list[ModelEntry]:
    return [
        ModelEntry(mid, _openai_label(mid), "OpenAI", PROVIDER_OPENAI)
        for mid, _created in model_ids
    ]


def build_app_server_openai_entries(models: list[dict]) -> list[ModelEntry]:
    """Translate Codex App Server ``model/list`` rows into picker entries."""
    entries: list[ModelEntry] = []
    seen: set[str] = set()
    for row in models:
        mid = str(row.get("id") or row.get("model") or "").strip()
        if not mid or mid in seen or not _OPENAI_INCLUDE_RE.match(mid):
            continue
        if bool(row.get("hidden")) or any(bad in mid for bad in _OPENAI_EXCLUDE):
            continue
        seen.add(mid)

        effort_rows: list[tuple[str, str]] = []
        for effort in row.get("supportedReasoningEfforts") or []:
            if not isinstance(effort, dict):
                continue
            key = str(effort.get("reasoningEffort") or "").strip()
            if key:
                effort_rows.append((key, str(effort.get("description") or key)))

        tier_rows: list[tuple[str, str, str]] = []
        for tier in row.get("serviceTiers") or []:
            if not isinstance(tier, dict):
                continue
            tier_id = str(tier.get("id") or "").strip()
            if tier_id:
                tier_rows.append((
                    tier_id,
                    str(tier.get("name") or tier_id),
                    str(tier.get("description") or ""),
                ))

        modalities = tuple(
            str(value) for value in (row.get("inputModalities") or ("text", "image"))
            if value
        )
        entries.append(ModelEntry(
            id=mid,
            label=str(row.get("displayName") or _openai_label(mid)),
            group="OpenAI · Recommended" if bool(row.get("isDefault")) else "OpenAI",
            provider=PROVIDER_OPENAI,
            description=str(row.get("description") or ""),
            reasoning_efforts=tuple(effort_rows),
            default_effort=str(row.get("defaultReasoningEffort") or ""),
            input_modalities=modalities,
            service_tiers=tuple(tier_rows),
            is_default=bool(row.get("isDefault")),
        ))
    return entries


def subscription_openai_entries() -> list[ModelEntry]:
    """Return informational rows when App Server discovery is unavailable.

    These rows are not tied to the active account and therefore are never
    entitlement or activation proof. Consumers must keep them out of model
    choices unless a separate authoritative App Server result provides them.
    """
    return build_openai_entries([(mid, 0) for mid in FALLBACK_OPENAI_SUBSCRIPTION])


def openai_entries(
    *,
    force: bool = False,
    auth: CodexAuth | None = None,
) -> tuple[list[ModelEntry], str]:
    """Return Codex-compatible models and a diagnostic source status.

    App Server is authoritative under every auth mode. ``codex login status``
    proves that *some* credential is live, but exposes no stable account or
    credential-store identity. Consequently a mode-only cache could cross
    ChatGPT account A -> B, and a readable ``auth.json`` key could be stale
    while the active key lives in a keyring. Every call therefore performs a
    fresh App Server identity check and never couples live status to raw file
    credentials or a persistent model cache.

    If the App Server request itself fails, only explicit ChatGPT auth gets the
    curated subscription fallback with status ``chatgpt-fallback``. Those rows
    are informational diagnostics only and MUST NOT be treated as selectable,
    entitled, or activated by callers. A successful empty or fully filtered
    response is authoritative and stays empty. API-key, access-token, and
    unknown authenticated modes fail closed because Helios cannot prove their
    exact entitlement.

    This function performs subprocess/network I/O and must run on a worker
    thread. Both MainWindow and Settings already do so; ``auth=`` lets Settings
    reuse the status it fetched in that same worker rather than spawning twice.
    """
    from helios.backend import codex_env

    global _OPENAI_WORKFLOW_MODES
    _OPENAI_WORKFLOW_MODES = ("default",)

    current_auth = auth if auth is not None else codex_env.fetch_auth_status()
    if not current_auth.ok or not current_auth.logged_in:
        return [], "not-logged-in"

    mode = codex_env.auth_mode(current_auth) or "authenticated"

    # ``force`` remains part of the public caller contract, but opaque auth
    # identity makes every lookup authoritative; there is no safe cache hit to
    # bypass when it is false.
    del force
    try:
        capabilities = codex_env.fetch_app_server_capabilities()
        proved_modes = tuple(
            mode
            for mode in capabilities.collaboration_modes
            if mode in {"default", "plan"}
        )
        _OPENAI_WORKFLOW_MODES = tuple(dict.fromkeys(("default", *proved_modes)))
        app_entries = build_app_server_openai_entries(list(capabilities.models))
        if app_entries:
            return app_entries, "app-server"
        return [], "app-server-empty"
    except (codex_env.CodexBinaryNotFound, codex_env.CodexAppServerError) as exc:
        _log.warning("Codex App Server model discovery failed: %s", exc)

    if mode == "chatgpt":
        return subscription_openai_entries(), "chatgpt-fallback"
    return [], f"{mode}-catalog-unavailable"


def openai_workflow_modes() -> tuple[str, ...]:
    """Return the latest authoritatively discovered Codex workflow modes."""

    return _OPENAI_WORKFLOW_MODES


# ── OpenRouter: fetched /models catalog ────────────────────────────────────

# Last-known-good rows so the picker is never empty before the first fetch.
# The live /models fetch replaces these within seconds on a networked host.
FALLBACK_OPENROUTER: list[ModelEntry] = [
    ModelEntry("google/gemini-2.5-pro", "Gemini 2.5 Pro", "Google", PROVIDER_OPENROUTER),
    ModelEntry("moonshotai/kimi-k2", "Kimi K2", "Moonshotai", PROVIDER_OPENROUTER),
    ModelEntry("deepseek/deepseek-chat", "DeepSeek Chat", "Deepseek", PROVIDER_OPENROUTER),
]


def openrouter_model_selectable(model_id: str) -> bool:
    """Keep native Claude/GPT models out of OpenRouter's model choices.

    Provider classification stays unchanged so existing OpenRouter histories
    retain their actual provider. This is a catalog/selection policy, not a
    migration of a conversation to another API or credential.
    """
    vendor, separator, name = str(model_id or "").strip().partition("/")
    return bool(separator and vendor and name) and vendor.casefold() not in {
        "anthropic", "openai",
    }


def preferred_openrouter_model(entries: list[ModelEntry]) -> str:
    """Best default for the OpenRouter header toggle: first eligible entry."""
    return next((entry.id for entry in entries if openrouter_model_selectable(entry.id)), "")


def openrouter_entries(*, force: bool = False) -> tuple[list[ModelEntry], str]:
    """Return OpenRouter models and a diagnostic source status.

    The catalog comes from OpenRouter's public ``/models`` endpoint, cached on
    disk by ``openrouter.catalog``. Without a saved API key the provider stays
    unselectable (``no-key``) even though the endpoint itself is public —
    there is nothing to chat with until Settings has a credential. With a key,
    cached rows are returned immediately; ``force=True`` performs the network
    refresh synchronously (callers run it on a worker thread) and falls back
    to the previous cache when the fetch fails.
    """
    from helios.backend.openrouter import catalog as or_catalog
    from helios.backend.openrouter import key as or_key

    if not or_key.load_key():
        return [], "no-key"
    if force:
        try:
            entries = or_catalog.refresh_models()
            if entries:
                return entries, "fetched"
        except Exception as e:  # noqa: BLE001 — discovery must never break the app
            _log.warning("OpenRouter catalog fetch failed (%s); using cache", e)
    cached = or_catalog.cached_models()
    if cached:
        return cached, "cached" if not force else "cached-stale"
    return list(FALLBACK_OPENROUTER), "fallback"


# ── Cross-provider helpers ─────────────────────────────────────────────────


def provider_for(model_id: str) -> str:
    """Classify a model id.

    OpenRouter ids are always ``vendor/model`` — a slash never appears in a
    native Anthropic or OpenAI id. Failing that, unmistakably OpenAI naming
    wins; everything else is Anthropic."""
    m = model_id or ""
    if "/" in m:
        return PROVIDER_OPENROUTER
    if _OPENAI_PROVIDER_RE.match(m):
        return PROVIDER_OPENAI
    return PROVIDER_ANTHROPIC


# Best-effort context-window sizes for the gauge denominator. OpenAI's API
# doesn't expose windows; these are the published figures per family.
_OPENAI_WINDOWS: tuple[tuple[str, int], ...] = (
    ("gpt-5.6", 1_050_000),
    ("gpt-5", 400_000),
    ("gpt-4.1", 1_000_000),
    ("gpt-4", 128_000),
    ("codex", 400_000),
    ("o3", 200_000),
    ("o4", 200_000),
)


#: Anthropic ids measured at a 1M window WITHOUT the `[1m]` alias suffix,
#: 2026-08-07 against claude 2.1.224 via `modelUsage.contextWindow`.
#: Matched as a SUBSTRING, so entries must carry their version: plain "opus"
#: would also match `claude-opus-4-5`, which really is 200k.
_MEASURED_1M_IDS: frozenset[str] = frozenset({"sonnet-5", "opus-5", "fable-5"})

#: Bare family aliases, matched EXACTLY. `--model sonnet` resolved to
#: claude-sonnet-5 (1M) on 2.1.224 — the alias means "latest in this family",
#: and the latest of these three is 1M. Exact-match only: a future 200k model
#: in one of these families would make this wrong, which is survivable because
#: this whole function is the last resort behind get_context_usage.
_LATEST_1M_ALIASES: frozenset[str] = frozenset({"sonnet", "opus", "fable"})


def context_window_for(model_id: str, default_anthropic: str = "") -> int:
    m = model_id or default_anthropic
    provider = provider_for(m)
    if provider == PROVIDER_OPENROUTER:
        from helios.backend.openrouter import catalog as or_catalog

        return or_catalog.context_length_for(m) or 200_000
    if provider == PROVIDER_OPENAI:
        for prefix, window in _OPENAI_WINDOWS:
            if m.startswith(prefix):
                return window
        return 200_000
    # LAST-RESORT ESTIMATE ONLY. The authoritative window is
    # `get_context_usage.maxTokens`, which the driver requests before the
    # first turn and after every result; `modelUsage[...].contextWindow` fills
    # in when that frame is unavailable. This function is reached only when
    # neither has answered — a pre-spawn picker label, or an older CLI.
    #
    # The `[1m]` rule under-reports: measured 2026-08-07 on claude 2.1.224,
    # `--model sonnet` (a PLAIN alias, no `[1m]`) resolves to claude-sonnet-5
    # with contextWindow 1,000,000, so this returned 200,000 for it — wrong by
    # 5x. The alias is a Helios spelling for "the 1M variant", not a statement
    # about the base model's window.
    #
    # Deliberately NOT replaced with a wider guess: `initialize`'s models[]
    # does not carry contextWindow (verified — the keys are value,
    # resolvedModel, displayName, description, supportsEffort,
    # supportedEffortLevels, supportsAdaptiveThinking, supportsFastMode,
    # supportsAutoMode), so there is no authoritative per-model source to read
    # here, and inventing one per family is how the old hardcoded list rotted.
    # The `_CURRENT_1M_FAMILIES` set records what was actually measured.
    if "[1m]" in m:
        return 1_000_000
    if m in _LATEST_1M_ALIASES:
        return 1_000_000
    if any(measured in m for measured in _MEASURED_1M_IDS):
        return 1_000_000
    return 200_000


# ── tiny JSON cache I/O (0600, atomic) ─────────────────────────────────────


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)
    except OSError as e:
        _log.warning("could not write %s: %s", path, e)
