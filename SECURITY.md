# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub:
**[Report a vulnerability](https://github.com/spencercnorton/helios/security/advisories/new)**.
Do not open a public issue, and do not include real credentials, transcripts
or project paths in the report — a description and a minimal reproduction
are enough. A `HELIOS_DEBUG=1` log records each `claude` spawn's full
command line, working directory and stderr, so scrub those lines before
attaching one.

You will get an acknowledgement within a week. Fixes ship as a tagged
release; the advisory is published once the release is out.

## Supported versions

Only the latest tagged release is supported. Helios has no LTS line.

## What Helios does with credentials and data

Understanding the trust model helps you judge what is and is not a finding:

- **Claude and GPT sessions** run through the official `claude` and `codex`
  CLIs, which own sign-in and hold the credentials; Helios keeps no copy.
  Three exceptions you should know about. If `ANTHROPIC_API_KEY`,
  `ANTHROPIC_AUTH_TOKEN` or `CLAUDE_CODE_OAUTH_TOKEN` is set in Helios's own
  environment it is passed through to `claude` processes, and only to them;
  `codex` processes receive no provider credential, and `OPENAI_API_KEY` is
  never forwarded to anything. The presence of `ANTHROPIC_API_KEY` or
  `CODEX_API_KEY` is read to decide per-token versus subscription billing.
  An OpenAI key pasted in Settings → Providers is handed once to
  `codex login --with-api-key` on stdin and not kept.
- **Every agent process gets a scrubbed environment.** Any variable whose
  name contains `TOKEN`, `SECRET`, `PASSWORD`, `PASSWD`, `PASSPHRASE`,
  `_PASS`, `CREDENTIAL`, `APIKEY`, `_KEY`, `_PWD`, `_PAT`, `JWT`,
  `AUTH_CONFIG`, `ASKPASS` or `KEYRING` is removed unless it is on that
  process's allow-list above; so are Helios's own `HELIOS_*` settings
  variables (`HELIOS_STATE_DIR` and `HELIOS_SESSION_POOL` pass through), the
  two variables the optional shared-context pane reads
  (`APOLLO_SCRATCHPAD_URL`/`APOLLO_SCRATCHPAD_KEY`, named for the
  maintainer's own scratchpad service and listed in
  `src/helios/backend/process/env_scrub.py`; unset and inert on a standard
  install, where the pane only ever tries `http://127.0.0.1:9101`), and
  `GPG_AGENT_INFO`.
  Names without those markers (for example `DATABASE_URL`) pass through.
  `SSH_AUTH_SOCK` is deliberately kept, so any session — *Bypass* above
  all — can ssh and push as you. Two optional integrations are also
  allow-listed when set: `INFISICAL_CLIENT_ID`/`INFISICAL_CLIENT_SECRET`/
  `INFISICAL_API_URL` for interactive Claude sessions, and
  `NORVI_TRACKER_API_TOKEN` for Claude sessions, the Codex App Server and
  OpenRouter Bash tools; Helios also adopts those four names from
  `systemctl --user show-environment` at startup when they are missing. On a
  standard install they are unset everywhere and inert.
- **OpenRouter sessions** use a key you paste into Settings, stored at
  `~/.helios/openrouter.key` (mode 0600; this path ignores
  `HELIOS_STATE_DIR`). The key is sent only as a bearer token to
  `https://openrouter.ai/api/v1/` (`chat/completions`, `key` and `credits`;
  the `models` and `models/<model>/endpoints` catalog fetches carry no key);
  redirects are refused so it never changes origin. Helios runs the agent
  loop itself, so conversation text and tool output leave the machine for
  the model you picked, and the message history is kept in plain text at
  `$HELIOS_STATE_DIR/openrouter-sessions/<session_id>.json`.
- **Spend breakers are fixed constants, not settings.** Claude:
  `--max-budget-usd 10` per process on API billing, none on a Claude
  subscription. GPT: 200,000 tokens per session on API-key billing, none on
  a ChatGPT login. OpenRouter: $5 per Work, and a model whose price the
  catalog does not publish is refused before any request is sent. No
  setting or file changes the amounts; the only lever, for Claude and GPT,
  is billing mode — a subscription login lifts the cap, and an exported
  `ANTHROPIC_API_KEY` or `CODEX_API_KEY` forces it back on.
- **Permissions are not a sandbox.** *Ask* prompts before tool calls that
  need approval; *Plan* and *Never ask* refuse them instead of prompting;
  *Bypass* never prompts and, for GPT, lifts the Codex sandbox
  (`danger-full-access`). "Outside the working directory" is enforced
  differently per provider. GPT sessions in every mode but *Bypass* run in
  Codex's `workspace-write` sandbox pinned to the working directory
  (`read-only` in *Plan*). OpenRouter tools lose their automatic standing on
  any path outside the working directory and ask instead (in *Plan* and
  *Never ask* they are refused); there is no CLI, so Helios's own rule table
  decides (Read, Grep and Glob inside the working directory run unprompted,
  Write and Edit run unprompted only in *Accept edits* and *Auto*, Bash never
  runs unprompted outside *Bypass* except for an exact command you have
  already allowed for the session — and Bash commands are not path-checked).
  Claude sessions have no Helios-side path check: Claude Code's own rules
  decide, and its settings still apply (Helios launches it with
  `--setting-sources user,project,local`). A chat whose working directory is
  your home folder is always read-only. Approval grants (*Allow for this
  session*) live only in memory for that conversation; on GPT the equivalent
  *Approve for session* is held by the Codex App Server, not by Helios.
  Helios never writes permission rules to `~/.claude/settings.json` or any
  other file.

  A security bug is anything that lets a session act outside the mode you
  selected — a prompt skipped in *Ask*, a write in *Plan* or *Never ask*, a
  GPT session escaping its `workspace-write` sandbox, an OpenRouter tool
  touching a path outside the working directory without asking — or past
  its provider's spend breaker. *Bypass* is deliberately unsandboxed, and
  Claude sessions are path-scoped only by Claude Code's own rules, so
  neither is a finding on its own. Please report everything else.
- **Local state** lives under `~/.helios/` (0700). Setting
  `HELIOS_STATE_DIR` moves logs, the Work database, per-conversation
  permissions, checkpoints, goals and OpenRouter session history there, but
  `openrouter.key`, `ui-state.json`, `project-names.json`, the caches
  (`model-catalog.json`, `openrouter-models.json`, `title-cache.json`),
  `backups/` and `session-archive/` always stay in `~/.helios/`. Small
  non-secret caches also live at `~/.local/share/helios/`
  (`$XDG_DATA_HOME/helios/`);
  `~/.cache/helios/titlegen/` is created only as the working directory for
  title-generation subprocesses, and Helios writes nothing into it.
  Transcripts are read from `~/.claude/projects/`
  (`$CLAUDE_HOME/projects/`), and Helios writes there too: GPT and OpenRouter
  conversations are mirrored as `<encoded-cwd>/<thread_id>.jsonl`, memory
  files are edited in place, and housekeeping moves idle transcripts to
  `~/.helios/session-archive/` and stale throwaway projects to
  `~/.claude/projects-archive/`. Housekeeping moves transcripts and never
  deletes them; the only automatic removals are empty project directories,
  Helios's own title-generation stub transcripts, memory-file backups under
  `~/.helios/backups/` beyond the last five, and OpenRouter session
  histories older than 30 days or beyond the newest 200 (pruned when a new
  OpenRouter session starts).
- **No telemetry is sent anywhere.** What does leave by default: session
  titles are generated by POSTing the first 1,500 characters of a new chat's
  first message and 800 of the reply to the Ollama URL in Settings →
  Behavior (`http://localhost:11434` unless you change it); with
  *Generate session titles with Ollama* switched off, the same text goes to
  Anthropic through `claude --print --model haiku` instead. The OpenRouter
  model list is fetched from `openrouter.ai` only once a key is saved.
  Everything else Helios contacts is local to this machine, and the
  provider CLIs handle their own connectivity. The optional shared-context
  pane talks only to a local endpoint unless configured otherwise (its
  default is `http://127.0.0.1:9101`). The optional Smart Routing broker is
  different: it is a separate system service reached over a Unix socket
  (`/run/helios-router/router.sock`) that holds its own OpenRouter key at
  `/etc/helios-router/openrouter.key` and sends the delegated task text to
  `https://openrouter.ai/api/v1/chat/completions` on the session's behalf.
  It is absent unless you install it; once installed it checks that key
  against `https://openrouter.ai/api/v1/key`, and task text goes upstream
  only with *Enable Smart Routing preview* switched on in Settings →
  Providers.
