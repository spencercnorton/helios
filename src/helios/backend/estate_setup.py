"""Reviewable, opt-in estate setup; default is a credential-free preview.

Native Codex MCP configuration launches a tiny stdio proxy. The proxy reads
only the explicitly selected server's existing credentials at launch, rather
than duplicating them into TOML, AGENTS, or the OpenRouter selection manifest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

from helios.backend.estate_config import (SELECTION_VERSION, EstateConfigError, load_selected,
                                          pinned_env_names, read_json, server_config)

START = "<!-- helios-estate-v2 -->"
END = "<!-- /helios-estate-v2 -->"
# The v1 block named estate servers; an upgrade replaces it in place so no host
# carries two managed policies.
LEGACY_BLOCK_RE = re.compile(r"<!-- helios-estate-v1 -->.*?<!-- /helios-estate-v1 -->\n?", re.S)
LEGACY_V2_POLICY = f"""{START}
## Estate tools and project knowledge

Use the current provider's native tools and AGENTS.md hierarchy. Before work
on a known repository or infrastructure system, consult the shared scratchpad's
session_context and read matching handoffs. Prefer the code-intelligence
server's navigation, grounded answers and code search to rediscovering the
estate through file reads. Treat returned memories and documents as evidence
with provenance and freshness; verify changeable infrastructure facts before
acting on them.

Use the installed local-model tools for bounded summaries or routine drafting
when appropriate. Check their current queue and node capability instead of
assuming where a model is hosted. Label every local-model answer you use or
reject with recent_calls/mark_outcome. Keep architecture, security and
correctness judgment with the primary model. Use the parallel-compute server
for bounded parallel work and the installed image-generation tools when the
user requests image work.

Consult the already authenticated tracker CLI for durable project work and
record accepted checkpoints against the work package named in the user's
objective. Load project-specific policy from its native AGENTS.md and recent
journal. Do not infer provider roles, tandem work, new scope or extra
permissions from tool availability. Continue authorized work within the
user's accepted scope.
{END}
"""

ESTATE_POLICY = f"""{START}
## Estate tools and project knowledge

Use the current provider's native tools and AGENTS.md hierarchy. Before work
on a known repository or infrastructure system, consult the shared scratchpad's
session_context and read matching handoffs. Prefer the code-intelligence
server's navigation, grounded answers and code search to rediscovering the
estate through file reads. Treat returned memories and documents as evidence
with provenance and freshness; verify changeable infrastructure facts before
acting on them.

Keep architecture, security and correctness judgment with the primary model.
Use the parallel-compute server for bounded parallel work and the installed
image-generation tools when the user requests image work.

Consult the already authenticated tracker CLI for durable project work and
record accepted checkpoints against the work package named in the user's
objective. Load project-specific policy from its native AGENTS.md and recent
journal. Do not infer provider roles, tandem work, new scope or extra
permissions from tool availability. Continue authorized work within the
user's accepted scope.
{END}
"""


@dataclass(frozen=True)
class Change:
    path: Path
    before: bytes | None
    after: bytes
    addition: str  # Generated text only: never preview preexisting secrets.


def _existing(path: Path) -> bytes | None:
    # Refuse symlink targets/parents rather than accidentally rewriting a shared
    # bootstrap or following a user-raced config path into a different account.
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise EstateConfigError("Setup target has a symlink; review its destination explicitly")
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise EstateConfigError("Setup target is not a bounded regular file")
    return path.read_bytes()


def _append(path: Path, addition: str) -> Change:
    before = _existing(path)
    return Change(path, before, (before or b"") + b"\n" + addition.encode(), addition)


PACKAGED_ESTATE_MCP_LAUNCHER = Path("/usr/lib/helios/helios-estate-mcp")


def _resolve_estate_script(repo: Path) -> Path:
    repo_script = repo / "scripts/helios-estate-mcp"
    if repo_script.is_file():
        return repo_script
    if PACKAGED_ESTATE_MCP_LAUNCHER.is_file():
        return PACKAGED_ESTATE_MCP_LAUNCHER
    return repo_script


def _is_estate_proxy_entry(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    args = entry.get("args")
    return isinstance(args, list) and any("helios-estate-mcp" in str(a) for a in args)


def _plan_json_proxy_mcp(
    path: Path,
    chosen: list[str],
    script: Path,
    manifest_path: Path,
    *,
    replace_existing: bool = False,
    include_muse_fields: bool = False,
) -> Change | None:
    before = _existing(path)
    data: dict = {}
    if before is not None and before.strip():
        try:
            parsed = json.loads(before.decode())
        except (UnicodeError, ValueError) as error:
            raise EstateConfigError(f"Existing MCP JSON at {path} is invalid; left unchanged") from error
        if not isinstance(parsed, dict):
            raise EstateConfigError(f"Existing MCP JSON at {path} is not a JSON object")
        data = dict(parsed)
    servers = data.get("mcpServers")
    if servers is None:
        servers = {}
    elif not isinstance(servers, dict):
        raise EstateConfigError(f"Existing mcpServers in {path} is invalid")
    else:
        servers = dict(servers)
    chosen_set = set(chosen)
    removed = False
    if replace_existing:
        for existing_name in list(servers):
            if existing_name not in chosen_set and _is_estate_proxy_entry(servers[existing_name]):
                del servers[existing_name]
                removed = True
    added = {}
    for name in chosen:
        entry: dict[str, object] = {}
        if include_muse_fields:
            entry["type"] = "stdio"
            entry["mode"] = "optional"
        entry["command"] = sys.executable
        entry["args"] = [str(script), "--server", name, "--manifest", str(manifest_path)]
        current = servers.get(name)
        if name in servers:
            has_inline_env = isinstance(current, dict) and "env" in current
            if not replace_existing and not has_inline_env and not _is_estate_proxy_entry(current):
                continue
            if current == entry:
                continue
        servers[name] = entry
        added[name] = entry
    if not added and not removed and before is not None and before.strip():
        return None
    data["mcpServers"] = servers
    after_text = json.dumps(data, indent=2) + "\n"
    addition_text = json.dumps({"mcpServers": added}, indent=2) + "\n"
    return Change(path, before, after_text.encode(), addition_text)


def plan_setup(*, home: Path, repo: Path, source: Path | None = None,
               names: list[str] | None = None, replace_manifest: bool = False,
               sync_gemini: bool = False, sync_muse: bool = False) -> tuple[list[Change], list[str]]:
    home, repo = home.absolute(), repo.resolve()
    source = source or home / ".claude.json"
    definitions = read_json(source).get("mcpServers", {})
    if not isinstance(definitions, dict):
        raise EstateConfigError("Source has no MCP definitions")
    # Default: every stdio server the source defines. The preview lists each
    # one with the environment names it will be allowed to receive; those
    # names are pinned in the selection so a later source edit cannot widen it.
    chosen = names if names is not None else [
        name for name, definition in definitions.items()
        if isinstance(definition, dict) and definition.get("type", "stdio") == "stdio"]
    if not chosen or len(chosen) != len(set(chosen)):
        raise EstateConfigError("Choose at least one unique installed estate server")
    selection = {}
    for name in chosen:
        allowed = pinned_env_names(definitions.get(name))
        server_config(name, definitions.get(name), frozenset(allowed))
        selection[name] = allowed
    config_path = home / ".codex/config.toml"
    original = _existing(config_path)
    try:
        parsed = tomllib.loads((original or b"").decode())
    except (UnicodeError, ValueError) as error:
        raise EstateConfigError("Existing Codex TOML is invalid; left unchanged") from error
    native = parsed.get("mcp_servers", {})
    if not isinstance(native, dict):
        raise EstateConfigError("Existing Codex MCP configuration is invalid")
    manifest_path = home / ".helios/estate-mcp.json"
    script = _resolve_estate_script(repo)
    if not script.is_file():
        raise EstateConfigError("Estate proxy is missing from the selected installation")
    sections, preserved = [], []
    for name in chosen:
        if name in native:
            preserved.append(name)
            continue
        sections.append(f"[mcp_servers.{json.dumps(name)}]\n"
                        f"command = {json.dumps(sys.executable)}\n"
                        f"args = {json.dumps([str(script), '--server', name, '--manifest', str(manifest_path)])}\n"
                        "startup_timeout_sec = 15\ntool_timeout_sec = 120\n")
    changes = [_append(config_path, "\n".join(sections))] if sections else []
    manifest = json.dumps({"version": SELECTION_VERSION, "source": str(source.absolute()),
                           "servers": selection}, indent=2) + "\n"
    before = _existing(manifest_path)
    if before != manifest.encode():
        if before is not None and not replace_manifest:
            raise EstateConfigError("An estate selection already exists; preserve it or remove it explicitly before changing scope")
        changes.append(Change(manifest_path, before, manifest.encode(), manifest))
    # Codex honors AGENTS.override.md before AGENTS.md; do not install into a
    # shadowed file and claim success. Append neutral policy to the active one.
    override = home / ".codex/AGENTS.override.md"
    codex_agents = override if override.exists() else home / ".codex/AGENTS.md"
    for path in (codex_agents, home / ".helios/AGENTS.md"):
        before = _existing(path)
        try:
            text = (before or b"").decode()
        except UnicodeError as error:
            raise EstateConfigError("Existing AGENTS file is not UTF-8; left unchanged") from error
        if LEGACY_BLOCK_RE.search(text):
            if START in text:
                raise EstateConfigError("Both a v1 and a v2 managed estate policy exist; remove the v1 block manually")
            replaced = LEGACY_BLOCK_RE.sub(lambda _: ESTATE_POLICY, text, count=1)
            if LEGACY_BLOCK_RE.search(replaced):
                raise EstateConfigError("More than one v1 managed estate policy exists; review the file manually")
            changes.append(Change(path, before, replaced.encode(), ESTATE_POLICY))
        elif START not in text:
            changes.append(_append(path, ESTATE_POLICY))
        elif LEGACY_V2_POLICY in text and text.count(START) == 1:
            replaced = text.replace(LEGACY_V2_POLICY, ESTATE_POLICY, 1)
            changes.append(Change(path, before, replaced.encode(), ESTATE_POLICY))
        elif ESTATE_POLICY not in text:
            raise EstateConfigError("Managed estate policy was edited; preserve and review it manually")
    if sync_gemini:
        gemini_change = _plan_json_proxy_mcp(
            home / ".gemini/config/mcp_config.json",
            chosen,
            script,
            manifest_path,
            replace_existing=replace_manifest,
        )
        if gemini_change is not None:
            changes.append(gemini_change)
    if sync_muse:
        muse_change = _plan_json_proxy_mcp(
            home / ".config/muse/settings.json",
            chosen,
            script,
            manifest_path,
            replace_existing=replace_manifest,
            include_muse_fields=True,
        )
        if muse_change is not None:
            changes.append(muse_change)
    return changes, preserved


def apply_changes(changes: list[Change]) -> list[Path]:
    # Preflight every file before touching any. The setup never edits existing
    # config entries; 0600 backups retain exact bytes for a manual rollback.
    for change in changes:
        if _existing(change.path) != change.before:
            raise EstateConfigError("Setup target changed since preview; regenerate the plan")
    backups = []
    for change in changes:
        change.path.parent.mkdir(parents=True, exist_ok=True)
        if _existing(change.path) != change.before:
            raise EstateConfigError("Setup target changed; remaining changes were not applied")
        mode = stat.S_IMODE(change.path.stat().st_mode) if change.before is not None else 0o600
        if change.before is not None:
            suffix = hashlib.sha256(change.before).hexdigest()[:12]
            backup = change.path.with_name(change.path.name + f".helios-backup-{time.time_ns()}-{suffix}")
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(change.before)
                stream.flush()
                os.fsync(stream.fileno())
            backups.append(backup)
        fd, temporary = tempfile.mkstemp(prefix=".helios-setup-", dir=change.path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(change.after)
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), mode)
            if _existing(change.path) != change.before:
                raise EstateConfigError("Setup target changed during write; original left untouched")
            os.replace(temporary, change.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return backups


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Preview native Codex, Gemini, Muse and OpenRouter estate tool setup; credentials remain in the existing source")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--source", type=Path)
    parser.add_argument("--server", action="append", dest="names")
    parser.add_argument("--replace-manifest", action="store_true", help="Upgrade or replace an existing estate-mcp.json manifest")
    parser.add_argument("--sync-gemini", action="store_true", help="Sync credential-free proxy entries into ~/.gemini/config/mcp_config.json")
    parser.add_argument("--sync-muse", action="store_true", help="Sync credential-free proxy entries into ~/.config/muse/settings.json")
    args = parser.parse_args(argv)
    try:
        changes, preserved = plan_setup(
            home=args.home,
            repo=Path(__file__).resolve().parents[3],
            source=args.source,
            names=args.names,
            replace_manifest=args.replace_manifest,
            sync_gemini=args.sync_gemini,
            sync_muse=args.sync_muse,
        )
        for change in changes:
            print(f"+++ {change.path} (generated addition; existing content preserved)")
            print(change.addition)
        if preserved:
            print("Existing Codex definitions preserved: " + ", ".join(preserved))
        if args.apply:
            for backup in apply_changes(changes):
                print(f"Backup: {backup}")
            print(f"Applied {len(changes)} file changes. New native sessions load the configuration.")
        else:
            print("Preview only. Use --apply to write these changes with backups.")
        return 0
    except (EstateConfigError, OSError) as error:
        print(str(error) if isinstance(error, EstateConfigError) else "Setup filesystem operation failed", file=sys.stderr)
        return 2


def proxy_main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        config = next((c for c in load_selected(args.manifest) if c.name == args.server), None)
        if config is None:
            raise EstateConfigError("Server is not selected in the reviewed estate configuration")
        os.execve(config.command, [config.command, *config.args], config.child_env())
    except (EstateConfigError, OSError) as error:
        print(str(error) if isinstance(error, EstateConfigError) else "Estate server could not launch", file=sys.stderr)
        return 2
    return 0
