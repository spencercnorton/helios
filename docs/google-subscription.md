# Google account sessions

Helios runs Google sessions through **Antigravity CLI (`agy`)**. Models and
labels come from `agy models`; Helios does not invent model names, context
sizes or subscription entitlements. The separate Gemini CLI uses a different
protocol and is not a fallback for this route.

## Install and sign in

Use Google's [official installation instructions](https://antigravity.google/docs/cli/install/).
On Linux:

```bash
curl -fsSL https://antigravity.google/cli/install.sh | bash
agy --version
agy
```

Complete the Google-account sign-in in the interactive CLI, then exit it.
Antigravity keeps its credentials in the operating system's keyring. Open
`agy` and use `/usage` to inspect the account's current quotas. Model discovery
can be checked separately:

```bash
agy models
```

Helios finds `agy` on `PATH`, in `~/.local/bin`, or through
`HELIOS_GOOGLE_BINARY`. An app-grid launch may have a different `PATH` from
your terminal. If discovery fails, check that the app can find `agy`, then
press the refresh button under **Settings → Providers → Google · Gemini**.

That page distinguishes a discovered catalog from verified account sign-in.
A failed quota check leaves sign-in unverified; an existing directory or API
key never counts as account authentication. The CLI determines which models
your account can actually run.

This route uses the Google account's allowance. Helios rejects API/custom
providers and automatic AI-credit overages, and strips API/cloud overrides
from its Google child environment. In
`~/.gemini/antigravity-cli/settings.json`, remove an API `modelProvider`
selection and keep `useG1Credits` absent or explicitly `false`. Google's
[CLI reference](https://antigravity.google/docs/cli/reference) describes
the AI-credit fallback. Helios does not edit these settings for you.

## Start with a separate worktree

From your existing repository, create a trial branch and checkout:

```bash
git worktree add -b trial/google-summary ../project-google-trial HEAD
```

In Helios, press `Ctrl+Shift+N`, choose `project-google-trial`, select a
discovered model under **Google (Gemini)**, and choose your desired permission mode
and reasoning effort level. Google sessions cannot start in your home folder. If the native session is
still initializing when you send, the message remains a draft; send it again
after startup completes.

Give the model a small task with an answer you can check. A useful first
prompt is:

```text
Read only src/example.py and tests/test_example.py.
Explain the input validation path in at most 300 words.
Cite exact file names and line numbers for each claim.
List uncertainties separately. Do not edit files, run commands,
use external tools, commit, push or deploy. Stop after the report.
```

Replace the two paths with files in your project. Compare the cited code
with the report before using its conclusions. For an editing trial, allow
one named file and one specific behavior, review the resulting diff, and
run the relevant checks yourself before bringing changes into another branch:

```bash
git -C ../project-google-trial status --short
git -C ../project-google-trial diff --check
git -C ../project-google-trial diff
```

A worktree starts from committed `HEAD`; it does not copy uncommitted edits.
It also shares Git history and configured remotes with the original checkout.
It keeps trial file changes separate, but does not isolate credentials or
external services. This workflow needs no production access.

## Permission modes and effort levels

Helios integrates Google models directly with the execution toolbar's effort
selector and permission picker:

- **Reasoning effort**: Models such as `gemini-3.8-flash` and `gemini-3.1-pro` advertise
  supported effort levels (e.g. Low, Medium, High). Helios lists the base model in the
  model catalog and configures effort via the toolbar's reasoning slider rather than
  separate redundant catalog rows. The selected level is passed to `agy --effort <level>`.
- **Permission modes**: All standard Helios permission modes are supported:
  - **Bypass**: Full agentic access via `--dangerously-skip-permissions` (unsandboxed).
  - **Plan**: Read-only planning mode via `--mode plan --sandbox`.
  - **Accept edits**: Auto-approve trusted workspace edits via `--mode accept-edits --sandbox`.
  - **Ask / Auto / Never ask**: Run within the terminal sandbox (`--sandbox`). In headless
    mode, tools requiring interactive approvals that cannot be granted are denied by the CLI.

The [terminal sandbox](https://antigravity.google/docs/sandbox/) restricts
terminal execution; native file and MCP tools follow Antigravity's own
permission policy. Review existing allow rules and connected tools before
giving the session a task. Keep trial changes in the separate worktree
until you have checked them.

## Transport and recovery

The bridge was verified with Antigravity CLI **1.3.3**, using Google's
[documented persistent streaming protocol](https://antigravity.google/docs/cli/headless/#stream-prompts-from-stdin).
Helios checks the native conversation identity, model, working directory
and approval policy before accepting input. Native result events finish
turns; a local pipe write or process exit is not proof of completion.

If delivery becomes uncertain, Helios keeps the Work blocked and does not
automatically replay the prompt. Inspect the native conversation before
starting replacement work that may already have run.
