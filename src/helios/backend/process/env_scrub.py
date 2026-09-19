"""Filter the environment handed to a spawned CLI down to a scoped policy.

`claude`/`codex` spawn arbitrary Bash tools, so ANY variable in the child
environment is readable by a model-driven tool call (`env`, `printenv`, reading
`/proc/self/environ`). Helios must therefore hand each provider CLI only the
variables it legitimately needs.

**What this is and is NOT.** This is an interim NAME-based *scrub* (a denylist of
credential-shaped names plus a small scoped allow-list of each provider's own
auth), not a positive minimum-environment builder and not a sandbox. It reduces
casual credential inheritance between unrelated services and between the two
providers. It does **not** confine file-backed credentials (`~/.aws`,
`~/.netrc`, `~/.ssh/id_*`, `~/.config/**`), `HOME`, the D-Bus session, or the
filesystem — a model-driven tool can still read those. Real confinement is the
future supervisor/lease sandbox (tracked as residual boundary work); do not read
this module as providing it.

Removal layers (all NAME-based — a value is never read, logged, or stored):

1. **Helios-internal vars** — config/secrets meaningful only to Helios itself
   (the scratchpad API key, debug flags, binary overrides). Always removed.

2. **Credential-shaped vars** — any name that looks like it carries a secret
   (``TOKEN``/``SECRET``/``PASSWORD``/``PASSPHRASE``/``_KEY``/``_PWD``/``_PAT``/
   ``ASKPASS``/…). Removed by name pattern so an unrelated credential never
   reaches the agent WITHOUT Helios enumerating each name up front. Errs toward
   over-dropping (a benign ``TOKENIZERS_PARALLELISM`` goes too) — the safe
   failure direction for a security filter.

3. **Cross-provider and unnecessary auth** — the ``keep=`` allow-list is scoped
   by PURPOSE, not just provider, so a child receives only the auth env that its
   exact command can actually use:

   * Claude model spawns get :data:`CLAUDE_AUTH_ENV` (Anthropic env auth is a
     real supported path on the installed CLI). A Codex child never sees it.
   * Codex ``exec`` gets :data:`CODEX_EXEC_ENV` (``CODEX_API_KEY`` only, which is
     exec-only per Codex docs). The native App Server, discovery, and
     login/status/version paths deliberately authenticate only from
     ``~/.codex``, so they forward ``keep=()`` — no key at all.
     ``OPENAI_API_KEY`` is ignored by this build and is deliberately never
     forwarded or treated as a login. Direct env-only ``CODEX_ACCESS_TOKEN`` is
     also outside this H1 contract; persist it with ``codex login
     --with-access-token`` before using Helios.
   * Anything that isn't a model command (``--help``/``--version``) forwards
     ``keep=()``.

   HONEST SCOPE: keeping a provider's OWN env auth means a tool-capable child of
   that provider CAN read its own forwarded auth variable via a tool call. That
   is accepted — H1.2 protects UNRELATED and CROSS-provider env credentials, not
   the active provider's own env-only auth (the CLI needs it to run). Claude's
   auth detection and launch use the same keep set. Codex native
   detection/launch use the Codex-managed credential store; only the explicit
   exec fallback gets its exec-only key.

   * The interactive Claude session additionally gets
     :data:`INFISICAL_ENV` — the machine identity it uses to fetch
     every other secret at runtime (Spencer, 2026-08-05). Housekeeping
     Claude spawns (title generation, archival, auth/mcp/version probes) share
     ``CLAUDE_AUTH_ENV`` but deliberately do NOT get this.

4. **Agent-forwarding sockets** — ``SSH_AUTH_SOCK`` is deliberately RETAINED
   (:data:`POLICY_KEEP`) so agents can ssh and push autonomously (Spencer,
   2026-07-15) — a considered trust grant. ``GPG_AGENT_INFO`` is dropped as
   minor hygiene, NOT as a GPG boundary: modern GnuPG locates its agent socket
   via gpgconf regardless of this var, so dropping it does not isolate GPG.

Documented residual gap: a secret in a non-credential-shaped name (e.g.
``DATABASE_URL`` with an inline password) is not caught by name matching. Closing
that needs the positive allowlist / minimum-environment builder — the principled
next step, consciously deferred here.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from collections.abc import Iterable

_log = logging.getLogger("helios.env-scrub")

# Layer 1 — Helios-internal config/secret vars, meaningless to the child.
HELIOS_INTERNAL_ENV = (
    "APOLLO_SCRATCHPAD_KEY",  # secret — the shared-context pane's API key
    "APOLLO_SCRATCHPAD_URL",  # internal endpoint
    "HELIOS_DEBUG",
    "HELIOS_LOG_LEVEL",
    "HELIOS_CLAUDE_BINARY",
    "HELIOS_CODEX_BINARY",
    "HELIOS_CODEX_TRANSPORT",
    "HELIOS_ROUTER_SOCKET",
    "HELIOS_TANDEM_BINARY",
)

# Layer 4 — sockets DROPPED by explicit policy (see module doc). SSH_AUTH_SOCK is
# intentionally NOT here — it is retained via POLICY_KEEP. GPG_AGENT_INFO is a
# hygiene drop, not a GPG isolation boundary.
AGENT_SOCKET_ENV = ("GPG_AGENT_INFO",)

# Always RETAINED, overriding the credential markers — an explicit, greppable
# trust grant (agents may ssh and push autonomously; Spencer, 2026-07-15).
POLICY_KEEP = frozenset({"SSH_AUTH_SOCK"})

# Purpose-scoped auth allow-lists (see module doc). A child gets ONLY the auth
# env its exact command can use; a tool-capable child of that provider can read
# its own forwarded auth var — that is the accepted own-provider boundary.

# Claude model spawns (interactive driver, title-gen, archive summary, and the
# auth/mcp/login probes). Anthropic env auth is a real path on the installed CLI
# (`claude --help`: ANTHROPIC_API_KEY; the binary also honors ANTHROPIC_AUTH_TOKEN
# and CLAUDE_CODE_OAUTH_TOKEN). Never forwarded to a Codex child.
CLAUDE_AUTH_ENV = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
    }
)

# Codex `exec` ONLY. Per Codex docs CODEX_API_KEY is honored only by `codex exec`;
# Helios's native App Server / discovery / login / status / version contract is
# Codex-managed file/keyring/etc. auth, so they forward keep=() instead.
# OPENAI_API_KEY is ignored by codex-cli (>=0.139), and direct
# CODEX_ACCESS_TOKEN env compatibility is intentionally deferred; both are
# absent.
CODEX_EXEC_ENV = frozenset({"CODEX_API_KEY"})

# Operator-granted workload credential (Spencer, 2026-08-05). The
# Infisical machine identity the INTERACTIVE agent uses to fetch every other
# secret at runtime. Same class of considered, greppable trust grant as
# SSH_AUTH_SOCK in POLICY_KEEP.
#
# Deliberately ONE bootstrap credential, not a pile of service tokens: with
# this, the agent pulls GitLab / tracker / Portainer secrets from Infisical on
# demand, so GITLAB_TOKEN, JIRA_API_KEY, APOLLO_SCRATCHPAD_KEY and friends stay
# scrubbed by name. Widening this set is a policy decision, not a convenience —
# add a secret here only when nothing can derive it from Infisical.
#
# Scoped to the interactive session, NOT folded into CLAUDE_AUTH_ENV: title
# generation, session archival, and the auth/mcp/version probes also spawn with
# that keep set and have no legitimate need for a secrets-manager credential.
# Every extra holder widens what a prompt-injection reaching a Bash tool can do.
#
# `INFISICAL_CLIENT_ID` is not credential-shaped and would survive anyway; it is
# named here so the pair is one greppable unit, and because a keep entry for an
# unscrubbed name is a harmless no-op.
INFISICAL_WORKLOAD_ENV = frozenset(
    {
        "INFISICAL_CLIENT_ID",
        "INFISICAL_CLIENT_SECRET",
    }
)

# Which Infisical to talk to. NOT a credential — separate from the pair above so
# that stays "the secret", but load-bearing all the same: the CLI's default is
# Infisical **Cloud**, so a session without this silently authenticates against
# app.infisical.com instead of the self-hosted server and gets an unrelated
# failure. `--domain` on `infisical login` does not carry over to later
# commands, which is exactly how this was found.
#
# Not credential-shaped, so it already survives the scrub; named here for the
# same reason `INFISICAL_CLIENT_ID` is — one greppable unit, and so that adding
# a marker like `URL` to :data:`_CREDENTIAL_MARKERS` later cannot silently start
# dropping it.
INFISICAL_CONFIG_ENV = frozenset({"INFISICAL_API_URL"})

# Everything a session needs to resolve a secret: the identity plus its target.
INFISICAL_ENV = INFISICAL_WORKLOAD_ENV | INFISICAL_CONFIG_ENV

# Operator-granted tracker credential (Spencer, 2026-08-22). Norvi Tracker
# (OpenProject) is the estate's durable work record, and
# Spencer wants a Helios session to reach it the way it reaches GitLab: read
# current work, history, blockers and what a project even is, and record its
# own checkpoints against the work package its objective names.
#
# The sanctioned client is the `norvi-work` CLI from the tracker gateway
# repository, already installed on the workstation. Helios deliberately ships NO OpenProject client
# of its own — `docs/helios-adapter.md` in that repo forbids a second
# credential, API client, binding registry or outbox, and the gateway's write
# path is allow-listed to `PATCH /work_packages/{id}` and `POST .../activities`
# with no create and no delete. Forwarding the token rather than shipping a
# client is what keeps that boundary intact.
#
# Same class of considered, greppable trust grant as SSH_AUTH_SOCK and the
# Infisical pair, and it carries the same honest cost: a tool-capable session
# CAN read this value, and today it is the `kleos` account key, which is an
# ADMIN key. Nothing scopes an OpenProject API token — only a separate user
# can — so a least-privilege tracker identity is still unprovisioned. The
# gateway's own fixtures name that gap ("Least-privilege identity is not
# provisioned"); narrowing this grant is that ticket's job, not this module's.
#
# Scoped to the INTERACTIVE session like INFISICAL_WORKLOAD_ENV: title
# generation, session archival and the auth/mcp/version probes have no work to
# document and must not hold it.
NORVI_TRACKER_ENV = frozenset({"NORVI_TRACKER_API_TOKEN"})

# Credentials Helios adopts from the systemd user manager at startup (see
# :func:`import_workload_identity`). Still a fixed allow-list of exact names —
# adding one is a policy decision; "import interesting vars from systemd" is
# not on the table.
ADOPTED_ENV = INFISICAL_ENV | NORVI_TRACKER_ENV

# Layer 2 — substrings that mark a credential-bearing variable NAME (uppercased).
# `_KEY`/`_PWD`/`_PAT` use a leading underscore to avoid benign collisions
# (PATH, OLDPWD, KEYBOARD). `AUTH_CONFIG`/`ASKPASS`/`JWT` cover common carriers.
_CREDENTIAL_MARKERS = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "PASSPHRASE",
    "_PASS",  # DB_PASS / REDIS_PASS / SMTP_PASS (not BYPASS / CLASSPATH / PATH)
    "CREDENTIAL",
    "APIKEY",
    "_KEY",
    "_PWD",
    "_PAT",
    "JWT",
    "AUTH_CONFIG",
    "ASKPASS",
    "KEYRING",
)


def _is_credential_name(name: str) -> bool:
    upper = name.upper()
    return any(marker in upper for marker in _CREDENTIAL_MARKERS)


def names_to_scrub(
    env_names: Iterable[str], *, keep: Iterable[str] = ()
) -> list[str]:
    """Names to remove from a child env, computed from the parent's var names.

    ``keep`` is this spawn's provider auth allow-list (e.g. :data:`CLAUDE_AUTH_ENV`
    for a Claude child); those names and :data:`POLICY_KEEP` are never dropped
    even though they are credential-shaped. Everything else credential-shaped —
    including the *other* provider's auth — is removed. Sorted + deterministic so
    the launcher and dict paths stay identical and tests can assert exact sets.
    """
    keeps = POLICY_KEEP | set(keep)
    drop = set(HELIOS_INTERNAL_ENV) | set(AGENT_SOCKET_ENV)
    for name in env_names:
        if name in keeps:
            continue
        if _is_credential_name(name):
            drop.add(name)
    return sorted(drop - keeps)


def scrub_helios_env(launcher, *, keep: Iterable[str] = ()) -> None:
    """Remove internal + credential-shaped vars from a Gio.SubprocessLauncher.

    The launcher inherits the parent (Helios) environment; ``unsetenv`` drops
    each named var from what the child receives. Unsetting an absent name is a
    harmless no-op, so the fixed internal/socket names are always cleared even
    when this process didn't have them set. Pass ``keep`` = the child's provider
    auth allow-list.
    """
    for name in names_to_scrub(os.environ.keys(), keep=keep):
        launcher.unsetenv(name)


def import_workload_identity(
    source: dict[str, str] | None = None,
) -> tuple[str, ...]:
    """Adopt the workload credentials from the systemd user environment.

    Covers the Infisical machine identity and the Norvi Tracker token — see
    :data:`ADOPTED_ENV`.

    The Claude driver forwards :data:`INFISICAL_ENV` so an interactive
    session can fetch every other secret at runtime — but it can only
    forward what Helios itself has. `gio launch` from the desktop entry gives the
    new process the *caller's* environment, not the systemd user manager's, so
    restarting Helios from a shell without the identity silently produced a
    Helios with nothing to forward. Observed 2026-08-06: the live process had
    neither variable while `systemctl --user show-environment` had both. The
    recorded restart procedure `source`s the conf file, which sets shell
    variables without exporting them, so it does not fix this either.

    Reads names only from a fixed allow-list — this can adopt the workload
    identity and nothing else, so a polluted user environment cannot smuggle
    anything in through it. An already-set value always wins, and the value is
    never logged. Returns the names adopted, for the caller's startup log.
    """

    env = os.environ if source is None else source
    missing = sorted(n for n in ADOPTED_ENV if not env.get(n))
    if not missing:
        return ()
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show-environment"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError) as e:
        _log.warning("could not read the systemd user environment: %s", e)
        return ()

    adopted = []
    for line in out.splitlines():
        name, sep, raw = line.partition("=")
        if not sep or name not in missing:
            continue
        # systemd prints this block "suitable for sourcing by a shell", so the
        # value may be quoted or escaped. shlex unquotes it the way a shell
        # would instead of leaving literal quotes in the credential.
        try:
            parts = shlex.split(raw)
        except ValueError:
            _log.warning("could not parse %s from the systemd user env", name)
            continue
        if len(parts) != 1 or not parts[0]:
            continue
        env[name] = parts[0]
        adopted.append(name)
    return tuple(sorted(adopted))


def scrubbed_child_env(
    source: dict[str, str] | None = None, *, keep: Iterable[str] = ()
) -> dict[str, str]:
    """Return a subprocess environment with internal + credential vars removed.

    ``subprocess.Popen``/``subprocess.run`` paths (App Server transport, model
    discovery, auth/help probes) need the same policy as the Gio launchers in
    plain ``dict`` form. Pass ``keep`` = the child's provider auth allow-list.
    """
    env = dict(os.environ if source is None else source)
    for name in names_to_scrub(env.keys(), keep=keep):
        env.pop(name, None)
    return env
