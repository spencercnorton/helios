# Native CLI data and presentation audit

Audited on 2026-10-06 against Helios 0.99.8, source base
`ecc4599d5c27f71568836456bbe37a3598a2c683`, Claude Code 2.1.291,
Claude Agent SDK 0.3.291, and Codex CLI 0.160.1.

## Evidence

- Installed Claude `--help` and the matching distributed SDK `sdk.d.ts`:
  `SDKToolProgressMessage`, `SDKRateLimitInfo`, `SDKStatusMessage`,
  `SDKToolUseSummaryMessage` and `SDKUsageReport`.
- Claude's installed formatter multiplies `utilization` by 100 before
  displaying percentages. A missing fraction is distinct from a reported zero.
- Codex's local `app-server generate-json-schema --experimental` output.
  The compact generated manifest is committed in
  [protocol/codex-app-server-0.160.1.json](protocol/codex-app-server-0.160.1.json).
- [Codex App Server documentation](https://developers.openai.com/codex/app-server/),
  [Claude streaming output](https://code.claude.com/docs/en/agent-sdk/streaming-output),
  [Claude CLI reference](https://code.claude.com/docs/en/cli-reference), and the
  [published Claude Agent SDK](https://www.npmjs.com/package/@anthropic-ai/claude-agent-sdk/v/0.3.291).

Schema generation and inspection did not run model turns. The SDK package was
inspected without installing or changing either CLI. Available schema fields
prove a transport contract, not that every account/model emits every event.

## Existing integration to retain

| Data | Existing projection | Assessment |
| --- | --- | --- |
| Claude text, thinking and tool-input deltas | One accumulated assistant message, finalized at `message_stop` | Correctly avoids one bubble per canonical content-block snapshot. |
| Claude child messages and task lifecycle | Causal agent dock keyed by parent tool-use id | Keep child traffic outside root conversation, context and accounting. |
| Native task plans | Durable execution plan and progress chip | Keep separate from the user's acceptance checklist. |
| Codex commentary/final-answer phases | Ordered public updates and prominent final answer | Already consumes the native phase instead of guessing from prose. |
| Codex reasoning | Public summaries only | Raw reasoning remains excluded. |
| Codex commands, patches, MCP progress and review | Native items, activity, Changes and Plan panes | Preserve item/turn correlation and authoritative completion. |
| Usage and compaction | Per-request context, terminal accounting, visible compaction boundary | Lifetime token counters must not become context-window fill. |
| Models and control requests | Native model catalog, serialized approvals and questions | Preserve native policy and authentication ownership. |

## Changes in this branch

| Finding | Resulting behavior |
| --- | --- |
| Claude supplied `utilization`, but the quota UI expected `usedPercent`. | Fractional utilization becomes a validated percentage; zero displays as zero, missing/invalid values stay unknown. Existing high-usage notifications can now consume Claude's actual measurement. |
| Current Claude `seven_day*`/`overage` keys appeared as raw identifiers. | Weekly, model-specific weekly and extra-usage labels are readable, with older names retained. |
| Claude `tool_progress` was dropped. | A known active root tool updates the activity strip with its native elapsed time. Tool results, a new turn and terminal results retire that identity. Heartbeats do not claim execution; child progress only promotes an existing eligible actor. |
| Codex merged every quota notification into one snapshot before its driver split buckets. | Initial reads and notifications share one cache scoped by native `limitId`, preserving sparse metadata within that bucket. Other buckets cannot inherit its label, secondary window or rejection. |
| A prior Codex rejection survived a later explicit null verdict; removed windows survived in the UI. | Explicit recovery clears the rejection, and explicit null windows remove obsolete rows. Omitted fields retain the last reported state. |
| Missing Codex percentages were fabricated as zero. | The UI shows a percentage only for a finite nonnegative native measurement. |
| Identical quota updates rebuilt all popover widgets. | Unchanged rows skip rebuilding; removal updates rebuild only when a row existed. |
| Codex hook completions produced a generic capability-gap warning. | Blocked/failed hooks and explicit warning/error/stop entries produce concise transcript notices. Clean hooks are quiet. Notices are thread/turn scoped, deduplicated by run id, scrubbed and bounded; context/feedback entries and source paths are excluded. |
| Protocol classifications targeted 0.152.0. | All 83 notifications and 11 server requests in 0.160.1 have explicit classifications. Its two new notification methods are identified: CLI-owned gateway auth is ignored; thread attachments remain a visible unsupported capability. |

The new manifest has 167 client requests. The native rollback request is absent;
the integration continues to avoid rollback as a fallback. Notification coverage
is **30 handled, 35 deliberately ignored, 18 explicitly unsupported**; server
requests are **6 supported, 5 explicitly denied**. These counts describe method
classification, not full feature parity or coverage of every item field.

## Remaining integration work

| Priority | Gap and next implementation boundary |
| --- | --- |
| High | **Model rerouting and identity changes:** `model/rerouted` is still explicitly unsupported. Reconcile actual provider identity with model selection, context sizing and persisted metadata before displaying a replacement model. |
| High | **Recovery and review states:** authentication recovery, safety buffering and auto-approval review retain visible capability gaps. Add typed activity that respects existing approval and delivery boundaries. |
| Medium | **Structured Claude account usage:** the new `SDKUsageReport` has server-provided usage rows, labels and extra-usage spend. Integrate through capability-detected native control replies; do not add a second credential/API client or present unavailable data as zero. |
| Medium | **Tools and timeline presentation:** Claude's `tool_use_summary` remains unprojected. Evaluate a summary on the existing activity group, correlated by `preceding_tool_use_ids`, without duplicating the assistant answer or replacing detailed receipts. |
| Medium | **Attachments and native item variants:** thread attachments, `functionCallOutput`, `sleep` and hook-prompt items need explicit product treatment and restoration tests. The method manifest alone does not validate all native item variants. |
| Medium | **Skills and provenance:** live skill changes and native memory status do not have complete UI projections. Refresh authoritative inventories and show source/freshness in the existing context surface. |
| Lower | **Account history, earned quota resets and realtime:** available transport methods do not automatically warrant UI controls. Add them only as intentional workflows with their own consent and failure behavior. |

## Validation

- Full real-GTK suite under Xvfb: **3,250 passed**.
- CI-style run with GTK hidden: **1,841 passed, 88 skipped**.
- Focused provider-data suite: **255 passed**.
- Ruff and diff whitespace checks pass.

Regression coverage includes bucket isolation, quota recovery, removed windows,
unknown/invalid measurements, old Claude records, known versus unknown tool
identities, heartbeats, foreign sessions, child isolation, terminal actors,
hook context exclusion/redaction, and duplicate popover updates.

Validation used isolated test state and a virtual display. This branch is a
source change pending review/release; it is not evidence of installation,
desktop visual acceptance or a measured end-to-end performance improvement.
