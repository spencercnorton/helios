# Helios user guide

Everything the README's pictures do not say. Helios is a desktop for coding
agents you already have: it drives the official `claude` CLI, binds GPT
sessions to a Codex App Server, and runs its own agent loop for OpenRouter
models. Nothing here replaces those tools' own sign-in, settings or context
files — Helios reads them and shows you what they are doing.

## First run

**The window.** Left, the sessions sidebar; centre, the transcript and the
composer; right, an optional workspace pane. The header holds the provider
toggle (**Claude · GPT · OpenRouter**), *New chat* (`Ctrl+N`, or `Ctrl+Shift+N`
to pick the folder first), search (`Ctrl+F`), reload, Settings, and the
workspace-pane switcher.

**Where sessions come from.** Claude sessions are the transcripts Claude Code
writes under `~/.claude/projects/<encoded-cwd>/`, so everything you have ever
run in the terminal is already in the sidebar, grouped by project and newest
first. GPT sessions are Codex App Server threads; OpenRouter sessions live
under `~/.helios/openrouter-sessions/`. The sidebar filter narrows to one
project, to this machine, or — when a shared session pool is mounted — to
another machine's sessions (read-only).

**Status dots.** Every row carries a dot: working, ready, awaiting your
input, errored or idle. A background session that asks a question or needs an
approval turns its dot and raises the dialog; you never have to be looking at
it. Sessions idle for four days move to `~/.helios/session-archive/` — out of
the sidebar, never deleted.

**Titles.** Session titles are generated locally with Ollama when one is
running (Settings → Behavior); otherwise the first message is the title.
Rename any session from its row.

**Starting a chat.** Pick the provider in the header, choose a folder (the
default working directory is in Settings → Defaults), type, and send with
`Ctrl+Enter`. `Enter` is a newline. Typing `/` lists the CLI's own commands
and skills with descriptions; the attach button inserts a file as `@path`.
While an agent works you can keep typing — the message queues and goes when
the turn ends. `Esc` or `Ctrl+.` stops the current turn; queued messages come
back to the composer.

## Permissions

The permission mode is the chip beside the composer, set per conversation.
The default for new conversations is in Settings → Defaults.

| Mode | Claude | GPT (Codex) |
|---|---|---|
| **Ask** | Every tool that needs approval asks | Workspace-write sandbox; asks before broader or unsandboxed access |
| **Accept edits** | File edits are approved automatically; commands still ask | Workspace-write sandbox with network; in-workspace edits run unprompted, escalation still asks |
| **Auto** | Guarded automation; prompts only for escalation | As Accept edits |
| **Bypass** | No approval prompts, unsandboxed | Full access, no prompts |
| **Plan** | Read-only investigation; Claude submits a plan for approval | Read-only sandbox |
| **Never ask** | Stays workspace-scoped; refuses anything that would need escalation | Workspace-write sandbox; refuses escalation |

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
agent process exits — nothing is written to disk. *Deny* stops it, and *Other*
lets you type why, which the agent reads.

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
| GPT | Aggregate token budget across the root thread and its subagents, measured by the App Server | 200,000 tokens |
| OpenRouter | Token budget, a per-Work dollar allowance, and a tool-round ceiling | 200,000 tokens · $5 · 25 rounds, extended up to 100 while the turn stays productive; repeated identical tool rounds pause with a resumable handoff |

The context meter in the toolbar shows how full the model's window is and
what fills it (system prompt, tools, memory files, messages); its *Compact*
action runs the provider's native compaction. Claude sessions also
auto-compact, and Helios shows the boundary in the transcript when that
happens. Live rate-limit details for Claude and Codex are in the same
toolbar.

## Works, goals and plans

A **Work** is Helios's unit of durable work: one objective, one definition of
done, one acceptance checklist, owned by you and shared by whichever provider
is executing. Set or edit it from the app menu → *Set a goal…* or from the
goal strip above the transcript; pause, resume and complete it there. The
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
- **Missions** and **Shared** — cross-provider work (a *tandem* contract with
  its own objective and definition of done) and the context it shares; the
  substrate exists, the user-facing contract is not enabled yet.

**Rewind.** Before a turn Helios takes a checkpoint of the checkout; app
menu → *Rewind files…* restores one. When a snapshot cannot be taken in time
the message is sent without one and a notice says that rewind is unavailable
for it.

**The Agent Dock.** When the active provider reports subagents for the
current turn, a dock appears in the conversation with one node per agent —
what it is doing, which one needs you — and a stop button per agent
(`stop_task`) that does not interrupt the session.

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
| `Ctrl+Shift+P` | Agent commands: compact, review, fork |
| `/` in the composer | The CLI's commands and skills |
| `Ctrl+1` | Toggle the sessions sidebar |
| `Ctrl+3` | Toggle the workspace pane |
| `Ctrl+R` | Reload sessions from disk |
| `Ctrl+Q` | Quit |

Typed `/model` and `/effort` keep the toolbar and the conversation's saved
settings in sync, the same as choosing them from the toolbar.

## Settings

**Settings.** *Account* — the Claude account and the resolved `claude` binary.
*Defaults* — permission mode, model and working directory for new chats.
*Appearance* — colour scheme (auto, light, dark) and a translucent
sessions sidebar. *Behavior* — notifications when a background session
finishes or a tandem mission is gated, local Ollama titles, other machines'
sessions. *Diagnostics* — the log file.

**Providers.** *OpenRouter · Chat* — the API key (stored at
`~/.helios/openrouter.key`, mode 0600), the live model catalog, and which
models appear in the picker. *OpenAI · Codex* — sign-in status, or an API key
saved through the Codex CLI. *OpenRouter · Smart Routing* — a preview that is
off by default.

**Tools.** The MCP servers each CLI has configured, their health, and the
built-in tool inventory the connected CLI reported.

## Environment variables

| Variable | Effect |
|---|---|
| `HELIOS_CLAUDE_BINARY` | Path of the `claude` binary to use, ahead of `PATH` |
| `HELIOS_CODEX_BINARY` | Path of the `codex` binary |
| `HELIOS_STATE_DIR` | Where Helios keeps its own state (default `~/.helios`) |
| `HELIOS_DEBUG=1` | Wire-level driver tracing in the log |
| `HELIOS_LOG_LEVEL` | Python logging level; `WARNING` quiets the default |

## Troubleshooting

**"No claude binary" in Settings.** Helios looks at `$HELIOS_CLAUDE_BINARY`,
then `PATH`, then `~/.local/bin/claude` and the standalone installer's
`~/.local/share/claude/versions/`. A desktop session often has a shorter
`PATH` than your terminal: set `HELIOS_CLAUDE_BINARY` in the launcher, or
symlink the binary into `~/.local/bin`.

**The startup message says GTK, libadwaita or GtkSourceView is too old.**
Helios needs GTK 4.10, libadwaita 1.5 and GtkSourceView 5; Ubuntu 24.04 and
newer have them. The check runs before the window opens so the failure is a
sentence, not a crash later.

**A control is missing — no effort slider, no Claude spend cap.** The
connected CLI is older than the feature; the log's first lines list what was
degraded and why. Update the CLI.

**GPT shows "Not signed in".** Run `codex login` in a terminal; Helios reads
the CLI's own credentials and never copies them.

**Where is the log?** Settings → Diagnostics, or `~/.helios/logs/helios.log`.
`HELIOS_DEBUG=1 helios` from a terminal adds the wire traffic to and from each
CLI, which is what a bug report needs.
