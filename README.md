# Helios

A native Ubuntu AI workbench where Claude Code and OpenAI Codex collaborate on
the same durable work, built with GTK4 + libadwaita.

**Codename — rename freely.** Helios is the working name; nothing in the code depends on it semantically.

Repo: [spencercnorton/helios](https://github.com/spencercnorton/helios)

## What it is

Helios is an all-in-one desktop app for building and completing work with
Claude Code and OpenAI Codex. A task is a provider-neutral **Work**. Standard
Work keeps each provider's context isolated. Provider switching may attach
both opaque native bindings to one Work, but no cross-provider packet is
delivered unless a Tandem contract has an objective and definition of done;
the current UI does not yet enable that contract. Helios drives the local
`claude` CLI for Anthropic sessions and one
shared persistent Codex App Server for OpenAI threads, reusing each CLI's
normal auth and native context. Codex App Server is fail-closed: Helios does
not silently fall back to `codex exec`, because the fallback cannot prove
equivalent instruction, delegation, approval, and lifetime-budget controls.
The target architecture and rollout
are defined in [docs/HELIOS-WORK-GRAPH.md](docs/HELIOS-WORK-GRAPH.md); the
truth projection and Work Room implementation contract is in
[docs/STATE-RECONCILER-WORK-ROOM.md](docs/STATE-RECONCILER-WORK-ROOM.md). The
provider control model and rollout gates are in
[docs/PROVIDER-EXECUTION-CONTROL.md](docs/PROVIDER-EXECUTION-CONTROL.md).

## What it does today

- **Project & session browser** — every cwd under `~/.claude/projects/`, with
  provider chips, per-session status dots (working / ready / awaiting / errored
  / idle), local-ollama titles by default (metered Claude is explicit opt-in), rename, and
  multi-select delete.
- **Live chat and bounded multi-session Work** — spawn `claude` in stream-json
  mode or bind lightweight GPT drivers to native App Server threads and turns;
  keep independent sessions working in the background; resume provider-native
  history. Cross-provider context is off for ordinary chat.
- **Tandem contract substrate (held)** — storage/tests can represent a Work
  with mode `tandem`, a non-empty objective, and a definition of done. Only
  that contract receives a credential-scrubbed ledger delta, and a durable
  epoch blocks pre-acceptance replay. Required collaboration context fails
  closed before provider I/O; only a verified single-provider Work may degrade
  to Goal-only context, and that requires a visible warning plus a durable
  model-excluded audit event. No production UI enables Tandem yet.
- **Execution containment** — retired Bypass records narrow to Ask, HOME is
  read-only, Bypass enables native full access across providers, GPT standard Work disables
  agents/delegation and gives each
  native root family a 200k aggregate breaker across root/child usage observed
  by the live App Server hub. That child topology is not yet a durable global
  ledger or a confirmed recursive-kill protocol. Claude defaults to its
  standard 200K context and requires a finite per-process native dollar
  breaker, and OpenRouter has a 200k per-process billed-token breaker. A trip
  makes the Work terminal, preserves queues, and requests cancellation of its
  live tagged drivers. Every interactive Work turn routed through the Claude,
  GPT App Server, or OpenRouter driver must acquire a durable execution lane
  before its provider boundary; terminal accounting commits before results or
  queued follow-ups are released. Schema v10 permits one running attempt per
  Work. Claude is otherwise uncapped, each
  canonical non-Claude provider has its own bounded concurrent lane, and every
  unrecognized provider id shares one fail-closed slot. Conflicts resolve
  most-specific-first: Work, then provider. Ordinary Works may share a checkout;
  pre-turn checkpoints make a collision recoverable instead of forbidding work
  in advance. A
  Helios client crash after reservation or an ambiguous provider
  acknowledgement keeps only the owning Work/provider lane blocked instead of
  guessing that execution stopped. The durable row retains only scrubbed
  recovery identifiers/receipts and a prompt digest, never the raw prompt; its
  audit events are not replayed into collaborator context. This ledger is not
  a cross-surface/global quota, and same-Work Tandem turns remain serialized
  rather than receiving Tandem's intended concurrent contract.
  Direct external CLI sessions, provider-native descendants, and Helios's
  bounded opt-in title/archive model helpers are outside this attempt ledger
  and still require the planned supervisor and quota reconciler. The app menu's
  stronger Emergency Stop All action also tears down the shared Codex transport.
- **Goal Mode** — keep one user-owned Work objective, definition of done, and
  acceptance checklist shared by Claude and GPT/Codex, with
  pause/resume/complete controls. Provider TodoWrite/native plans never rewrite
  that accepted state.
- **Streaming transcript** — markdown + GtkSourceView5 code blocks, thinking
  expanders, and one card per tool call with a running/done/error chip, its
  paired result, elapsed time and "show full output"; file edits render as
  unified diffs and a bubble lists the files it changed. Provider failures
  stay in the live transcript as copyable error rows for the session,
  blocking or failing hooks leave a notice row, and a jump-to-latest button
  appears whenever you scroll up. The activity strip shows what the current agent is doing.
- **Observed agent activity** — an in-conversation Agent Dock stays hidden until
  the active provider reports actors for the exact Work and root turn, then
  shows stable compact nodes and accessible Active, Needs You, Done, and
  Observed details. Its accessible summary distinguishes starting, working,
  input, complete, error, stopped, and merely observed states; ambiguous
  `waiting`/`blocked` reports are never presented as user input requests. The
  same neutral projection appears in the Plan pane, while proven
  descendant-only local and remote-pool transcripts stay out of the primary
  SessionList. This is an observational surface: it does not enable GPT
  delegation or weaken the standard `agents.enabled=false` contract, does not
  claim authoritative topology or cancellation, and does not synthesize Claude
  lifecycle state before its causal adapter is available.
- **Plan review for Claude** — in Plan mode Claude investigates read-only,
  writes its plan, and submits it through `ExitPlanMode`; Helios renders the
  plan and offers approve, approve with auto-accepted edits, or keep planning
  with feedback. Approval switches the conversation out of Plan and the
  toolbar and durable settings follow.
- **Approvals carry what the CLI knows** — the tool's own description, why it
  asked, the CLI's suggested session rules or `acceptEdits` switch, and the
  full content or unified diff for file writes and long commands. *Other*
  denies with the typed reason. Claude questions are answered natively
  through `updatedInput.answers`.
- **Per-agent stop and slash discovery** — the Agent Dock can stop one
  subagent (`stop_task`) without interrupting the session, and typing `/` in
  the composer lists the CLI's commands and skills with descriptions. A
  subagent's own question or approval names the subagent that asked and
  marks it *needs input* in the dock; a background chat's row says how many
  subagents it is running. Typed `/model` and `/effort` keep the toolbar and
  the durable conversation settings in sync.
- **Claude's task list is the durable plan** — interactive Claude sessions
  have the CLI's task tools enabled; TaskCreate/TaskUpdate/TaskList (or
  TodoWrite on older CLIs) feed the same durable execution plan and
  `X/Y tasks complete` chip that Codex's native plan does, and survive
  restart and session switching.
- **Questions and approvals** — Claude questions/tool permissions plus Codex's
  one-to-three questions and command/file/expanded-permission approvals render
  as required, explicit-choice serialized dialogs, including for background
  sessions and server-side resolution races. Incomplete Submit stays open with
  inline validation; blocking state and provider-owned auto-resolution timing
  are visible.
- **Native GPT work state** — App Server plans are persisted with immutable
  revisions and drive a durable `X/Y tasks complete` chip. Default and native
  Plan workflows are separate from permission profiles, capability-discovered,
  and restart-safe. Aggregate diffs, child-thread status, MCP inventory, token
  usage, rate limits, goals, and interrupt/resume lifecycle are rendered without
  exposing raw hidden reasoning. A generated Codex 0.152.0 protocol registry
  names handled, ignored, unsupported, and denied method surfaces in CI.
  Native context compaction is available from the context popover or the
  capability-driven `Ctrl+Shift+P` agent-command launcher; provider item
  lifecycle and a summary-free local boundary make each compaction visible
  without treating its maintenance turn as model work. The same launcher maps
  Review to native read-only model work and Fork to a persisted, source-safe
  branch with an independent Work and fresh budget. Revert is deliberately
  visible-but-disabled while Helios's legacy native histories cannot satisfy
  stable `thread/revert`; deprecated rollback is never substituted silently.
- **Context pane** — global + project CLAUDE.md, MEMORY.md index, and a full
  memory-file editor (frontmatter form + markdown body, atomic saves, rotating
  backups under `~/.helios/backups/`, external-change detection). Claude loads
  its native CLAUDE.md/settings context. Codex loads AGENTS.md natively and
  receives Helios policy as typed App Server application context that composes
  with built-in workflow instructions; user messages are never rewritten as
  Claude role briefs.
- **Toolbar** — capability-driven model picker, a combined current-conversation
  Execution control for workflow + reasoning + permissions, a circular
  context-window meter with native Compact action, a keyboard-first agent
  command launcher, and provider-partitioned live Claude/Codex rate-limit
  details.
- **Search** — full-text across all local transcripts (Ctrl+F).
- **Settings** — Claude, OpenAI, and OpenRouter provider status, resolved CLI
  binaries, safe permission defaults, MCP server health, and built-in tool
  inventory.
- **OpenRouter as a third chat provider** — select OpenRouter in the header
  toggle to drive any model in the live `/models` catalog directly. Unlike
  Claude and Codex (where a CLI subprocess executes tools), Helios runs the
  agent loop itself and executes `Read`/`Write`/`Edit`/`Bash`/`Grep`/`Glob`
  in-process under the same permission modes, workspace-scoped. It holds its
  own user-level key, so conversation text and tool results *do* leave the
  machine — the opposite posture from the Smart Routing broker below. See
  [OpenRouter operations](docs/OPENROUTER-OPERATIONS.md).
- **Smart Routing preview (held)** — standard Work does not expose Codex
  dynamic delegation. The credential-isolated OpenRouter broker remains on a
  shadow/manual-evaluation hold: task calls explain the candidate route but no
  task content is sent for inference. Dispatch remains locked until an explicit
  Parallel lease, family budget, trusted-context compilation, paired-quality
  evidence, and exact endpoint-variant verification all exist. See
  [OpenRouter operations](docs/OPENROUTER-OPERATIONS.md).
- **Remote session pool** *(read-only)* — browse other machines' sessions from a
  shared iCloud pool when mounted.
- **Automatic session archival** — local sessions older than four days are
  moved out of the active sidebar and preserved under Helios's archive
  directory. Automatic archival never starts a hidden cloud-model call; an
  internal explicit summarizer helper remains bounded if invoked by code.

## Requirements

- Ubuntu 24.04+ (or any distro with GTK 4.10+, libadwaita 1.5+, GtkSourceView 5).
  These minimums are enforced at startup with a clear message rather than a
  late crash.
- Python 3.11+
- PyGObject (`python3-gi`, `gir1.2-gtk-4.0`, `gir1.2-adw-1`, `gir1.2-gtksource-5`)
- A `claude` install. Lookup order: `$HELIOS_CLAUDE_BINARY` → PATH →
  `~/.local/bin/claude` → standalone installs in `~/.local/share/claude/versions/`
  → IDE-bundled binaries (VSCode / JetBrains) as a last resort. The resolved
  binary is shown in Settings → Account.
- Optional for GPT sessions: a `codex` install authenticated by the Codex CLI.

```bash
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-gtksource-5 libgtksourceview-5-0
```

## Run from source

```bash
cd ~/helios          # or wherever the clone lives
./scripts/helios     # launcher: adds src/ to PYTHONPATH, runs `python3 -m helios`
./scripts/install-desktop.sh   # optional: app-grid entry
```

Debugging: `HELIOS_DEBUG=1 ./scripts/helios` enables wire-level driver tracing;
`HELIOS_LOG_LEVEL=WARNING` quiets it down.

## Data layout

| Path | What Helios does with it |
|---|---|
| `~/.claude/projects/<encoded-cwd>/*.jsonl` | Sessions; read for transcripts/status, deleted on user request |
| `~/.claude/projects/<encoded-cwd>/memory/` | Memory files — read **and written** by the editor (atomic, backed up) |
| `~/.claude/CLAUDE.md`, `<cwd>/CLAUDE.md` | Shown in the context pane (read-only) |
| `~/.helios/` | Helios's private state: Work graph, title/provider indexes, goals, project names/perms, UI state, and editor backups (0600/0700) |
| `~/.helios/work/work.db` | SQLite Work, participant/native-binding, ordered event, and immutable artifact index (WAL) |
| `~/.helios/work/<work_id>/artifacts/` | Content-addressed immutable Work artifacts (0600) |
| `~/.helios/session-goals.json` | Goal Mode compatibility projection keyed by Work ID; legacy session keys migrate lazily |
| `~/.helios/session-archive/` | Raw JSONL transcripts moved out of the active sidebar by automatic 4-day archival |
| `~/.helios/openrouter.key` | OpenRouter chat-provider API key (0600), user-level — **separate** from the root-owned Smart Routing broker credential |
| `~/.helios/openrouter-models.json` | Cached OpenRouter `/models` catalog backing the model picker and context-window sizing |
| `~/.helios/openrouter-sessions/<id>.json` | Authoritative OpenAI message array per OpenRouter session (0600); holds verbatim conversation and tool output. Each array is compacted by whole exchanges to fit the model window, and the directory is pruned at driver start (older than 30 days, keeping the 200 most recent) |
| `~/.local/share/helios/` | Tool/MCP snapshot from the last session init |
| `~/.cache/helios/titlegen/` | Scratch cwd for title-generation subprocesses |

## Development

```bash
python3 -m pytest -q     # backend tests (GTK-free; CI runs these)
```

- Branches: `claude/<topic>` · `codex/<topic>` · `spencer/<topic>`; MRs reviewed by Jeeves.
- Docs: [ROADMAP.md](ROADMAP.md) (phases),
  [docs/STATE-RECONCILER-WORK-ROOM.md](docs/STATE-RECONCILER-WORK-ROOM.md)
  (truth projection and Work Room),
  [docs/gtk4-gotchas.md](docs/gtk4-gotchas.md) (13 PyGObject/GTK4 failure modes
  worth reading before touching widgets), review history in `REVIEW-*.md`,
  per-phase history in [HANDOFF.md](HANDOFF.md).

## Layout

```
helios/
├── src/helios/
│   ├── app.py, main_window.py      # Adw application + window (driver registry lives here)
│   ├── backend/                    # pure data + filesystem (GTK-free)
│   │   └── process/                # GLib/Gio subprocess drivers (cli_driver, title_generator)
│   ├── widgets/                    # GTK4/libadwaita widgets
│   └── resources/style/helios.css
├── tests/                          # pytest, GTK-free
├── deploy/                         # hardened system-service templates
├── scripts/                        # launchers + desktop/router installers
└── data/                           # .desktop + icons
```
