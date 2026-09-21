# Helios user guide

Everything the README's pictures do not say. Helios is a desktop for coding
agents you already have: it drives the official `claude` CLI, binds GPT
sessions to a Codex App Server, and runs its own agent loop for OpenRouter
models. Nothing here replaces those tools' own sign-in, settings or context
files — Helios reads them and shows you what they are doing. Under
`~/.claude/projects/` it writes only the folder for a chat's working
directory, mirrors of GPT and OpenRouter transcripts in Claude's format, the
memory files you edit in the Claude memory pane, and the copy made when you
fork a GPT conversation. Its housekeeping moves transcripts — idle sessions
to `~/.helios/session-archive/`, dead temporary projects to
`~/.claude/projects-archive/` — and never deletes them; the only thing that
deletes a session is the sidebar's *Delete session* (the row's trash button
or the right-click item) or the multi-select *Delete*, after a confirmation
that says it cannot be undone. The short list of things Helios does remove
on its own — empty project folders, its own title-generation stubs, old
editor backups, old OpenRouter histories — is in
[SECURITY.md](../SECURITY.md#what-helios-does-with-credentials-and-data).

**Install and launch.** Follow [Install](../README.md#install) in the README:
the `helios` package from the APT repository on Ubuntu 26.04, or
`./scripts/helios` from a checkout on any other distribution. The `helios`
command (or `./scripts/helios`) takes no options of its own; a second launch
activates the running window instead of opening another.

## First run

**The window.** Left, the sessions sidebar; centre, the transcript and the
composer; right, an optional workspace pane. The header holds the provider
toggle (**Claude · GPT · OpenRouter**), *New chat* (`Ctrl+N`, or `Ctrl+Shift+N`
to pick the folder first), search (`Ctrl+F`), reload, Settings, and the
workspace-pane switcher.

**Before the first chat.** Set Settings → Defaults → *Default working
directory* (or `default_cwd` in `~/.helios/ui-state.json`). With nothing set,
a new chat opens in the most recent project folder Helios knows about, or in
`$HOME` on a fresh install — and a chat whose folder *is* `$HOME` is forced
read-only whatever mode you pick, with a toast that says so. Subfolders of
`$HOME` are not affected. `Ctrl+Shift+N` picks a folder for one chat.

**Where sessions come from.** Claude sessions are the transcripts Claude Code
writes under `~/.claude/projects/<encoded-cwd>/`, so the last four days of
terminal sessions are already in the sidebar, as one list ordered by what
needs attention (working, stuck, errored, then finished) and newest first.
GPT sessions are Codex App Server threads; OpenRouter sessions live under
`~/.helios/openrouter-sessions/`. The sidebar filter narrows the list to *All
sessions*, *This machine*, one folder, one other machine, or *Temporary*.

Sessions whose folder is under `/tmp` or has a path component starting with
`_tmp_` are *temporary*: they appear only under the **Temporary** filter, and
once such a project has at most one transcript, 14 days of inactivity and a
folder that no longer exists, it is moved to `~/.claude/projects-archive/`
at startup (move it back to restore). Other machines' sessions appear only
with Settings → Behavior → *Show other machines' sessions* on and
`HELIOS_SESSION_POOL` pointing at a session pool root; they are read-only.

**Status dots.** Every row carries a dot: working, complete (the last turn
finished, whether or not the process is still open), stuck (your last message
got no reply and nothing is running), errored, and a distinct *needs you* dot
when a background session is waiting on an answer or an approval. That dialog
is not raised over the chat you are in: the row's dot changes, a desktop
notification is posted, and the dialog opens when you switch to that session.

**Archive.** Local sessions whose transcript is older than four days are moved
(never deleted) from `~/.claude/projects/<encoded-cwd>/` to
`~/.helios/session-archive/<encoded-cwd>/<id>.jsonl`, 15 s after launch and
every 24 h, at most 8 per run; a session with a live agent process is skipped.
Claude Code's own `claude --resume` no longer sees an archived session; `mv`
the file back into `~/.claude/projects/<encoded-cwd>/` to restore it.

**Titles.** Titles come from a local Ollama by default
(`http://localhost:11434`, model `qwen2.5-coder:14b` — pull it, or change
*Title model* in Settings → Behavior and press *Save and check*). Each sidebar
reload queues titles for the 30 most recent untitled sessions; when Ollama is
not running the failure is silent and the first message stays the title.
Turning the *Generate session titles with Ollama* switch **off** does not
disable titling: it switches to metered Claude titles (`claude --print --model
haiku`, capped at $0.05 per title). Right-click a row → *Rename…* sets a
title by hand.

**Starting a chat.** Pick the provider in the header, choose a folder, type,
and send with `Ctrl+Enter`. `Enter` is a newline. Typing `/` lists Helios's
agent commands and, in Claude chats, the CLI's own commands and skills with
descriptions; the attach button inserts a file as `@path`. While an agent
works you can keep typing — the message queues and goes when the turn ends.
`Esc` or `Ctrl+.` stops the current turn; queued messages come back to the
composer.

## Permissions

Permissions are the third value of the **Execution** capsule beside the model
picker in the chat toolbar (`Default · High · Ask` = workflow · reasoning ·
permissions), set per conversation. The default for new conversations is in
Settings → Defaults.

- **Workflow** is *Default* for every provider, and additionally *Plan* for
  GPT when the Codex App Server advertises native plan mode. The Plan
  workflow forces read-only permissions for that thread.
- **Reasoning** is `off`, `low`, `medium`, `high` (default), `xhigh` or
  `max`. Claude gets `--effort <level>` (on a CLI without `--effort`, an
  equivalent `--max-thinking-tokens` value); `off` is always
  `--max-thinking-tokens 0`. OpenRouter offers `off` to `high` only for
  models whose catalog row advertises reasoning. GPT uses the levels the App
  Server lists for the model.
- **Permissions** are the six modes below. The key is what the CLI receives
  and what you write to a state file (see [Settings keys](#settings-keys)).

| Mode | Key | Claude | GPT (Codex) | OpenRouter |
|---|---|---|---|---|
| **Ask** | `default` | Every tool that needs approval asks | Workspace-write sandbox, network off; asks before broader or unsandboxed access | In-folder reads run; everything else asks |
| **Accept edits** | `acceptEdits` | File edits are approved automatically; commands still ask | Workspace-write sandbox with network; in-workspace edits run unprompted, escalation still asks | In-folder reads and edits run; commands and paths outside the folder ask |
| **Auto** | `auto` | Guarded automation; prompts only for escalation | As Accept edits | As Accept edits |
| **Bypass** | `bypassPermissions` | No approval prompts, unsandboxed | Full access, no prompts | Everything runs |
| **Plan** | `plan` | Read-only investigation; Claude submits a plan for approval | Read-only sandbox, network off | In-folder reads run; everything else is refused |
| **Never ask** | `dontAsk` | Stays workspace-scoped; refuses anything that would need escalation | Workspace-write sandbox, network off; refuses escalation | As Plan |

Where the Codex network is off, a command that needs it fails inside the
sandbox: in Ask that raises an approval prompt, in Plan and Never ask it is
refused.

Two rules hold whatever you pick:

- A conversation whose working directory is your **home folder is
  read-only** (Plan). The picker tells you when it has narrowed a choice.
- **Bypass is unsandboxed.** Helios scrubs unrelated and cross-provider
  credentials from the agent's environment, but your files, file-backed
  secrets and a forwarded SSH agent are not sandboxed. Use it in a checkout
  you can afford to have rewritten.

**The approval dialog** shows what the CLI itself reported: the tool, its
description, the full command or the unified diff, and the rule the CLI would
remember. *Allow once* approves this request. *Allow for this session* adds
that rule (for example `Bash(python3 -m pytest:*)`) until this conversation's
agent process exits — nothing is written to disk. *Allow and auto-accept
edits* (offered for file writes) approves and switches this conversation to
Accept edits. *Deny* stops it, and *Other* lets you type why, which the agent
reads.

**Plan review.** In Plan mode Claude investigates read-only, writes its plan
and submits it. Helios renders the plan and offers *approve*, *approve with
edits auto-accepted*, or *keep planning* with your feedback. Approving
switches the conversation out of Plan; the toolbar and the saved settings
follow.

**Questions.** When an agent asks a question (Claude's `AskUserQuestion`,
Codex's one-to-three questions) it arrives as an explicit dialog with the
options the agent offered plus a free-text answer. A subagent's question names
the subagent that asked.

## Budgets

Every provider has a breaker, and a trip is a hard stop: the Work becomes
terminal, the live agent is asked to cancel, and anything you had queued stays
in the composer. Reopen a Work the breaker closed from the app menu → *Clear
budget block*; it is a menu choice on purpose, with no shortcut.

| Provider | Breaker | Default |
|---|---|---|
| Claude | The CLI's own `--max-budget-usd`, per process, covering subagents the process spawns | $10 on API (per-token) billing; none on a subscription, where the CLI's cost figure is an estimate rather than money |
| GPT | Aggregate token budget across the root thread and its subagents, measured by the App Server | 200,000 tokens on API-key billing; none on a ChatGPT subscription |
| OpenRouter | A per-Work dollar allowance reserved before every request, and a tool-round ceiling | $5 per Work · 25 tool rounds per turn, extended up to 100 while the turn stays productive; an endpoint that publishes no price is priced from the model's catalog row (a warning is logged once at session start); a model with no usable price anywhere is refused before any request is sent — "OpenRouter could not verify this model's prices"; repeated identical tool rounds pause with a resumable handoff |

None of these limits is configurable — there is no setting, file key or
environment variable for them. The only lever is which billing mode the CLI
is signed in with: an exported `ANTHROPIC_API_KEY` or `CODEX_API_KEY` in
Helios's environment counts as per-token billing even when a subscription
login exists. On per-token Claude billing the cap requires Claude Code
2.1.217 or newer advertising `--max-budget-usd`; an older CLI refuses to
start a chat (see [Troubleshooting](#troubleshooting)).

The context meter in the toolbar shows how full the model's window is and
what fills it (system prompt, tools, memory files, messages); its *Compact
now* button runs the provider's native compaction. Claude sessions also
auto-compact, and Helios shows the boundary in the transcript when that
happens. Live rate-limit details for Claude and Codex are in the same
toolbar.

## Works, goals and plans

A **Work** is Helios's unit of durable work: one objective, one definition of
done, one acceptance checklist, owned by you and shared by whichever provider
is executing. Set or edit it from the app menu → *Set a goal…* or from the
goal strip between the transcript and the composer; pause, resume and
complete it there. The
model's own plan — Claude's task list, Codex's native plan — is tracked
separately as *the execution plan* and drives the `X/Y tasks complete` chip;
it can never rewrite the goal you accepted.

The **workspace pane** (header, right) switches between:

- **Plan** — the execution plan, the tasks done, and quality signals such as
  whether a passing check actually covered the files that changed.
- **Changes** — the aggregate diff of the current conversation.
- **Capabilities** — what the connected provider reported for this
  conversation: the tools it has, its MCP servers, and the instruction files
  it loaded.
- **Claude memory** — the global and project `CLAUDE.md`, the memory index,
  and a memory-file editor with atomic saves and rotating backups under
  `~/.helios/backups/`.
- **Missions** and **Shared** — optional integrations. Missions lists
  *tandem* missions found under `$TANDEM_STATE_DIR` (default `~/.tandem`) and
  is empty without the tandem engine; Shared reads a scratchpad service that
  is not part of Helios and reports "can't reach the scratchpad" when there
  is none. Neither affects chats.

**Rewind.** In a folder that is a git repository (`git` installed), Helios
snapshots the worktree as a dangling git tree before each message you send —
your index, refs and branches are untouched, and git-ignored files are not
captured. App menu → *Rewind files…* restores one; files a restore removes go
to `$HELIOS_STATE_DIR/checkpoint-trash/`, never deleted. Outside a
repository the dialog says "No checkpoints yet"; when the snapshot takes
longer than 4 s the message is sent without one and a notice says that rewind
is unavailable for it.

**The Agent Dock.** When the active provider reports subagents for the
current turn, a dock appears in the conversation with one node per agent —
what it is doing, which one needs you — and, in Claude chats, a stop button
per agent that stops that subagent without interrupting the session.

## Keyboard shortcuts

| Keys | Action |
|---|---|
| `Ctrl+Enter` | Send the message |
| `Esc` / `Ctrl+.` | Stop the current turn (queued messages return to the composer) |
| `Ctrl+Shift+.` | Emergency stop: every session, and the shared Codex transport |
| `Ctrl+N` | New chat in the default folder |
| `Ctrl+Shift+N` | New chat — choose the folder |
| `Ctrl+F` | Search all transcripts |
| `Ctrl+L` | Focus the composer |
| `Ctrl+Shift+P` | Agent commands: compact, review, fork, revert |
| `/` in the composer | Helios's agent commands; in Claude chats also the CLI's commands and skills |
| `Ctrl+1` | Toggle the sessions sidebar |
| `Ctrl+3` | Toggle the workspace pane |
| `Ctrl+R` | Reload sessions from disk |
| `Ctrl+Q` | Quit |

Typed `/model <alias>` and `/effort <level>` work in Claude chats and route
through the toolbar handlers: `/model` changes the sticky default model
(applied to the live process when the CLI allows it, otherwise at your next
chat), `/effort` sets this conversation's reasoning level.

## Settings

**Settings.** *Account* — the Claude account and the resolved `claude` binary
with a badge naming where it was found. *Defaults* — permission mode and
model for new chats (persisted only by the **Save chat defaults** button at
the bottom of the page) and the default working directory (saved as soon as
you choose it). *Appearance* — colour scheme (auto, light, dark) and a
translucent sessions sidebar. *Behavior* — notifications when a background
session finishes or a tandem mission is gated, Ollama titles (server URL and
model, persisted by *Save and check*), other machines' sessions.
*Diagnostics* — the log file.

**Providers.** *OpenRouter · Chat* — the API key (stored at
`~/.helios/openrouter.key`, mode 0600), the live model catalog, and which
models appear in the picker. The key must be at least 16 characters with no
whitespace; saving it enables the OpenRouter toggle and fetches the catalog
from `https://openrouter.ai/api/v1/models` (cached in
`~/.helios/openrouter-models.json`). Pre-seeding the file while Helios is
closed enables the toggle at the next launch, but a launch reads only the
cache: the picker shows a three-model fallback until Settings has been
opened once, which fetches and caches the catalog, and the picker reloads
it at the next launch or six-hourly refresh. `anthropic/*` and `openai/*` models are
excluded — use the Claude and GPT providers for those — and models that do
not advertise tool calling are marked *no tools*. *Models in the picker*
writes `openrouter_picker_models`; empty means the whole catalog.
*OpenAI · Codex* — sign-in status from `codex login status`, or an API key
handed to `codex login --with-api-key` (Helios keeps no copy).
*OpenRouter · Smart Routing* — an optional broker, inert unless its service
is installed; the switch stays disabled with "Helios Router unavailable".

**Tools.** *Helios Router* (the optional broker above), *Claude MCP servers*
(live, from `claude mcp list`, with health), *Codex MCP servers* (the
inventory captured by your last GPT chat — empty until one has run), and
*Built-in tools* (the tool set your last Claude session reported).

### Settings keys

Every control above is a key in `~/.helios/ui-state.json`: flat JSON, mode
0600, missing keys take their defaults. The file is read once at start and
rewritten whole on every change, so edit it only while Helios is closed.

| Control | Key | Values |
|---|---|---|
| Defaults → Default permissions | `permission_mode` | `default` · `acceptEdits` · `auto` · `bypassPermissions` · `plan` · `dontAsk` (default `default`); `bypassPermissions` is honoured only with `permission_mode_confirmed: true`, otherwise it is clamped to `default` |
| Defaults → Model | `model` | model id (default `fable[1m]`); an empty value is read back as `fable[1m]` at the next launch, so *Default (from Claude settings)* in the picker lasts only for the running session |
| Defaults → Default working directory | `default_cwd` | absolute path; unset, or no longer a directory → the most recent project folder Helios knows about, else `$HOME` (read-only) |
| Appearance → Theme | `color_scheme` | `auto` · `light` · `dark` |
| Appearance → Translucent sessions sidebar | `glass_sidebar` | `true` · `false` |
| Behavior → Notify when a background session finishes | `notify_on_finish` | `true` (default) · `false` |
| Behavior → Notify when a tandem mission is gated | `notify_on_mission_gate` | `true` (default) · `false` |
| Behavior → Generate session titles with Ollama | `title_backend` | `ollama` (on, default) · `claude` (off) |
| Behavior → Ollama server URL | `ollama_url` | default `http://localhost:11434` |
| Behavior → Title model | `ollama_title_model` | default `qwen2.5-coder:14b` |
| Behavior → Show other machines' sessions | `show_pool_sessions` | `true` · `false` (default) |
| Providers → Models in the picker | `openrouter_picker_models` | list of model ids; empty = whole catalog |
| Execution capsule → Reasoning (global default) | `effort_level` | `off` · `low` · `medium` · `high` (default) · `xhigh` · `max` |

Per-conversation overrides live in `$HELIOS_STATE_DIR/conversation-perms.json`
as `{provider: {session_id: {permission_mode, effort_key?, workflow_mode?}}}`
with provider `anthropic`, `openai` or `openrouter`. The full table, with the
files each key lives in, is in [agent-setup.md](agent-setup.md).

## Environment variables

| Variable | Effect |
|---|---|
| `HELIOS_CLAUDE_BINARY` | Path of the `claude` binary to use, ahead of `PATH`; must be an executable file, otherwise it is silently skipped |
| `HELIOS_CODEX_BINARY` | Path of the `codex` binary, ahead of `PATH` and `~/.local/bin/codex` |
| `HELIOS_STATE_DIR` | Root for `logs/`, `work/work.db`, `checkpoints.json`, `checkpoint-trash/`, `conversation-perms.json`, `session-providers.json`, `session-goals.json`, `openrouter-sessions/`, `openrouter-routes.json`, `openrouter-context/`, `AGENTS.md`, `estate-mcp.json` (optional input) and `project-perms.json` (legacy) (default `~/.helios`). `ui-state.json`, `openrouter.key`, `openrouter-models.json`, `model-catalog.json`, `title-cache.json`, `project-names.json`, `session-archive/` and `backups/` always live in `$HOME/.helios` regardless. To relocate everything, run with a different `HOME` |
| `HELIOS_DEBUG` | Any non-empty value: everything at DEBUG, including the Claude driver's spawn argv, the type of every record the CLI emits, its stderr and its exit code; overrides `HELIOS_LOG_LEVEL` |
| `HELIOS_LOG_LEVEL` | Python logging level name (`DEBUG` … `CRITICAL`); default `INFO`; an unknown name is treated as `INFO`. `WARNING` quiets startup |
| `HELIOS_CODEX_TRANSPORT` | Leave unset; `exec` refuses every GPT chat |
| `HELIOS_ROUTER_SOCKET` | Unix socket of an optional Smart Routing broker (default `/run/helios-router/router.sock`); inert unless the socket answers |
| `HELIOS_SESSION_POOL` | Root of an optional read-only session pool shared between machines; ignored when the path does not exist |
| `HELIOS_TANDEM_BINARY` | Path of the optional `tandem` binary for the Missions pane |
| `CLAUDE_HOME` | Where Claude Code transcripts are read (default `~/.claude`) |
| `XDG_DATA_HOME` | Tool inventories cached under `<dir>/helios/` (default `~/.local/share`) |
| `TANDEM_STATE_DIR` | Tandem mission state for the Missions pane (default `~/.tandem`) |
| `TERMINAL` | Terminal emulator for the Settings *Sign in…* button (else the first of `x-terminal-emulator`, `gnome-terminal`, `kgx`, `konsole`, `tilix`, `kitty`, `alacritty`, `foot`, `xterm`) |

A desktop launch does not read `~/.bashrc`; set these on the desktop entry's
`Exec=` line (`Exec=env HELIOS_CLAUDE_BINARY=/path/to/claude helios`).
`HELIOS_DEBUG`, `HELIOS_LOG_LEVEL`, `HELIOS_CLAUDE_BINARY`,
`HELIOS_CODEX_BINARY`, `HELIOS_CODEX_TRANSPORT`, `HELIOS_ROUTER_SOCKET` and
`HELIOS_TANDEM_BINARY` are stripped from the agents' own environment;
`HELIOS_STATE_DIR` and `HELIOS_SESSION_POOL` pass through.

**Credentials are not configured through Helios variables.** `claude` sign-in
is its own: `claude auth login`, or `ANTHROPIC_API_KEY`,
`ANTHROPIC_AUTH_TOKEN` or `CLAUDE_CODE_OAUTH_TOKEN` in Helios's environment,
which are the only auth variables forwarded to Claude — an exported
`ANTHROPIC_API_KEY` also makes Helios treat the account as per-token and
apply the $10 cap. `OPENAI_API_KEY` is not read and not forwarded: sign in
with `codex login`, or paste a key in Settings → Providers, which runs
`codex login --with-api-key`. There is no OpenRouter environment variable:
the key is read only from `~/.helios/openrouter.key` (one line, at least 16
characters, no whitespace, mode 0600); write that file while Helios is closed
or paste the key in Settings. Every other variable whose name contains
`TOKEN`, `SECRET`, `PASSWORD`, `PASSWD`, `PASSPHRASE`, `_PASS`, `CREDENTIAL`,
`APIKEY`, `_KEY`, `_PWD`, `_PAT`, `JWT`, `AUTH_CONFIG`, `ASKPASS` or
`KEYRING` is stripped from the agents' environment; `SSH_AUTH_SOCK` is kept.
Two credential-shaped names are forwarded on purpose when set, for optional
integrations that are inert without them: `NORVI_TRACKER_API_TOKEN` (an
optional work-tracker token) to Claude, GPT and OpenRouter, and
`INFISICAL_CLIENT_SECRET` (an optional secrets-manager identity) to Claude
only. Leave both unset if you do not use those services.

## Troubleshooting

**Settings says "Not found — set $HELIOS_CLAUDE_BINARY".** Helios looks at
`$HELIOS_CLAUDE_BINARY` (must be an executable file, otherwise it is silently
ignored), then `PATH`, `~/.local/bin/claude`, the newest
`~/.local/share/claude/versions/*`, then VS Code and JetBrains bundles — the
badge beside the path in Settings → Account says which one won. A desktop
session has a shorter `PATH` than your terminal: symlink the binary into
`~/.local/bin`, or set the variable on the desktop entry
(`Exec=env HELIOS_CLAUDE_BINARY=/path/to/claude helios`).

**The startup message says GTK, libadwaita or GtkSourceView is too old.**
Helios needs GTK 4.10, libadwaita 1.5 and GtkSourceView 5; Ubuntu 24.04 and
newer have them. The check runs before the window opens: the message is
printed to stderr, shown in a dialog when GTK can display one, and the
process exits with status 1. It names the packages to upgrade
(`gir1.2-gtk-4.0`, `gir1.2-adw-1`, `gir1.2-gtksource-5`).

**`ImportError: cannot import name 'UTC' from 'datetime'` (or a similar
`StrEnum`/`tomllib` import error) at launch.** Python is older than 3.11;
the startup check covers only the GTK libraries.

**A control is missing — no effort slider, no Claude spend cap.** The
connected CLI is older than the feature. At startup a toast reads "Claude CLI
<version> cannot do: …" and the log carries a `claude CLI <version>;
degraded: …` line naming the missing flag (`--effort` = the reasoning
slider, `--max-budget-usd` = Claude's spend cap, `--forward-subagent-text` =
live subagent text in the Agent Dock). Update the CLI.

**"Claude's required process-family budget breaker is unavailable" when
sending.** You are on per-token billing and the `claude` CLI is older than
2.1.217 or lacks `--max-budget-usd`; update Claude Code.

**GPT toggle disabled, Settings says "Codex CLI not available".** Install the
CLI (`npm install -g --prefix ~/.local @openai/codex`) or set
`HELIOS_CODEX_BINARY`; Helios looks at that variable, `PATH`, then
`~/.local/bin/codex`.

**GPT shows "Not signed in".** Run `codex login` in a terminal (ChatGPT
account), or paste an OpenAI API key in Settings → Providers → *OpenAI ·
Codex* — Helios hands it to `codex login --with-api-key` on stdin and keeps
no copy. Sign-in state comes from `codex login status`; Helios never reads
`~/.codex/auth.json`.

**Where is the log?** Settings → Diagnostics, or
`$HELIOS_STATE_DIR/logs/helios.log` (default `~/.helios/logs/helios.log`;
rotates at 1 MB, three backups). `HELIOS_DEBUG=1 helios` from a terminal
(`HELIOS_DEBUG=1 ./scripts/helios` from a checkout) adds the Claude CLI's
spawn argv, the type of every record it emits, its stderr and its exit code,
which is what a bug report needs. To check a start:

```bash
grep 'claude CLI' ~/.helios/logs/helios.log | tail -n 1
```

A healthy start prints one line ending `helios.window: claude CLI <version>;
degraded: nothing`; anything after `degraded:` names a feature the installed
CLI cannot provide.
