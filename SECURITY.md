# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub:
**[Report a vulnerability](https://github.com/spencercnorton/helios/security/advisories/new)**.
Do not open a public issue, and do not include real credentials, transcripts
or project paths in the report — a description and a minimal reproduction
are enough.

You will get an acknowledgement within a week. Fixes ship as a tagged
release; the advisory is published once the release is out.

## Supported versions

Only the latest tagged release is supported. Helios has no LTS line.

## What Helios does with credentials and data

Understanding the trust model helps you judge what is and is not a finding:

- **Claude and GPT sessions** run through the official `claude` and `codex`
  CLIs. Helios never reads, stores or forwards their credentials; sign-in is
  owned by each CLI.
- **OpenRouter sessions** use a key you paste into Settings, stored at
  `~/.helios/openrouter.key` (mode 0600). For these sessions Helios runs the
  agent loop itself, so conversation text and tool output leave the machine
  for the model you picked.
- **Permissions are not a sandbox.** *Ask* mode asks before tool calls;
  *Bypass* does not. The CLIs' own settings still apply. Anything that lets
  a session act outside the mode you selected, outside the working directory
  it was given, or past a spend cap you set, is a security bug — please
  report it.
- **Local state** lives under `~/.helios/` (0700). Transcripts are read from
  `~/.claude/projects/`. No telemetry is sent anywhere.
