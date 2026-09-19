"""Tail-scan a session's JSONL to derive its current state for the sidebar.

For each session we want two facts:

  * `last_response_at`  — Unix timestamp of the most recent assistant message
                          that produced visible output. Used to sort the
                          session list "by last response."
  * `state`             — one of:
        "working"           -- a `claude` process is bound to this session id
                                (seen in /proc/*/cmdline) AND it is mid-turn:
                                the transcript tail is a user/assistant record
                                with no terminating `result` yet. This is the
                                only state that means "actually doing work."
        "ready"             -- a `claude` process is bound but the last record
                                is a clean `result` — the process is alive and
                                waiting for the next message (persistent
                                stream-json mode does NOT exit between turns),
                                so it is idle, not working. Distinguishing this
                                from "working" is the whole point: a bound
                                process is NOT evidence of activity.
        "awaiting_response" -- no bound process; last meaningful record is a
                                user message with no `result` after it (claude
                                was interrupted/stopped, or the user just typed)
        "errored"           -- last result/tool_result has is_error=true
        "idle"              -- no bound process; last record is a clean
                                `result`, conversation finished

We only read the last ~64 KB of each JSONL — sessions can be megabytes and
we just need to look at the tail. The full scan would block the UI.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from helios.backend.projects import Session


# How much of each transcript we'll scan from the end. We read backward in
# chunks until we have at least `TAIL_MIN_RECORDS` complete records OR we hit
# the cap, whichever comes first. The previous implementation read the last
# 64KB and discarded the (presumed-partial) leading line, which silently lost
# the entire status signal when the FINAL JSONL line was >64KB (huge
# `tool_result` records can easily exceed that).
TAIL_CHUNK_BYTES = 16 * 1024     # how much to grab per backward step
TAIL_MIN_RECORDS = 6             # stop scanning when we have this many
TAIL_MAX_BYTES = 1 * 1024 * 1024 # absolute ceiling per file


STATE_WORKING = "working"
STATE_READY = "ready"
STATE_AWAITING = "awaiting_response"
STATE_ERRORED = "errored"
STATE_IDLE = "idle"

# Back-compat alias: "active" used to mean "a claude process is bound" without
# distinguishing working from idle-waiting. It now maps to the working state.
STATE_ACTIVE = STATE_WORKING


@dataclass(slots=True)
class SessionStatus:
    state: str  # one of the STATE_* constants
    last_response_at: float  # 0.0 if unknown


def status_for(session: Session, active_ids: set[str]) -> SessionStatus:
    """Derive a Status for a Session.

    `active_ids` is the set of session ids currently bound to a running
    `claude` process. Pass {} to skip active-process detection.
    """
    bound = session.session_id in active_ids
    state, last_resp = _scan_tail(session.path, bound=bound)
    if bound and not last_resp:
        # Working/ready sessions still need a sort timestamp even if the tail
        # has no visible assistant text yet (e.g. just-sent turn).
        last_resp = _parse_tail_response_ts(session.path)
    return SessionStatus(state=state, last_response_at=last_resp)


def context_fill_for(session: Session) -> int:
    """Tokens of the model context window this session currently occupies,
    read from the LAST real assistant turn in the transcript tail. 0 if unknown.

    Each turn re-sends the whole conversation as input, so the most recent
    request's input (prompt + cached prefix) plus its output ≈ what's resident
    in the window right now. This lets the context meter show a session's real
    fill the moment it's opened, instead of 0% until the next turn runs.

    Note: the saved transcript records the model WITHOUT the `[1m]` variant
    marker and carries no contextWindow, so callers must derive the window
    separately (and bump to 1M when the fill exceeds the base 200K window).
    """
    for rec in reversed(_read_tail_records(session.path)):
        if rec.get("type") != "assistant":
            continue
        msg = rec.get("message") or {}
        if msg.get("model") in (None, "<synthetic>"):
            continue
        u = msg.get("usage") or {}
        used = (
            int(u.get("input_tokens") or 0)
            + int(u.get("cache_read_input_tokens") or 0)
            + int(u.get("cache_creation_input_tokens") or 0)
            + int(u.get("output_tokens") or 0)
        )
        if used > 0:
            return used
    return 0


class ActiveSessionProbe(Protocol):
    """Detects which session ids are currently bound to a running `claude`
    process.

    Linux-on-the-host is the only implementation today (via /proc), but
    Phase 5+ work (Flatpak sandboxing, macOS/Windows ports) will need
    different strategies. Implementations can be passed into
    `discover_active_session_ids()` to override the default."""

    def detect(self) -> set[str]:
        ...


class LinuxProcActiveSessionProbe:
    """Walk /proc/*/comm + /proc/*/cmdline. Linux-only.

    Cheap: ~500 entries on a typical desktop, opens cmdline only on
    processes whose `comm` starts with "claude".
    """

    def detect(self) -> set[str]:
        ids: set[str] = set()
        proc = Path("/proc")
        if not proc.is_dir():
            return ids

        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                comm = (entry / "comm").read_text(errors="replace").strip()
            except OSError:
                continue
            if not comm.startswith("claude"):
                continue
            try:
                raw = (entry / "cmdline").read_bytes()
            except OSError:
                continue
            argv = [a for a in raw.split(b"\x00") if a]
            argv_s = [a.decode("utf-8", errors="replace") for a in argv]
            for i, a in enumerate(argv_s):
                if a in ("--resume", "--session-id") and i + 1 < len(argv_s):
                    ids.add(argv_s[i + 1])
                elif a.startswith("--resume=") or a.startswith("--session-id="):
                    ids.add(a.split("=", 1)[1])
        return ids


# Process-wide default probe — substitute via dependency injection for
# tests, sandboxed builds, or alternate platforms.
DEFAULT_ACTIVE_PROBE: ActiveSessionProbe = LinuxProcActiveSessionProbe()


def discover_active_session_ids(
    extra: set[str] | None = None,
    *,
    probe: ActiveSessionProbe | None = None,
) -> set[str]:
    """Return session ids belonging to currently-running `claude` processes,
    unioned with any caller-known live ids.

    `extra` is typically the live Helios driver's own session id (which
    isn't passed via --resume so the probe can't see it).
    `probe` overrides the default Linux-/proc impl for non-Linux or
    sandboxed builds.
    """
    ids: set[str] = set(extra or ())
    ids |= (probe or DEFAULT_ACTIVE_PROBE).detect()
    return ids


# ── Internals ────────────────────────────────────────────────────────────


def _scan_tail(path: Path, *, bound: bool = False) -> tuple[str, float]:
    """Return (state, last_response_timestamp).

    `bound` is True when a live `claude` process is attached to this session.
    It changes how the terminal record is classified:

      * bound + clean `result` tail    -> READY   (alive, waiting for input)
      * bound + user/assistant tail     -> WORKING (mid-turn, actually busy)
      * unbound + clean `result` tail   -> IDLE    (finished, no process)
      * unbound + user tail             -> AWAITING (typed, never answered)

    The errored classification is the same either way.
    """
    records = _read_tail_records(path)
    if not records:
        # A bound process with an empty/just-created transcript is spinning up
        # a turn; treat it as working rather than idle.
        return (STATE_WORKING if bound else STATE_IDLE), 0.0

    # last_response: most recent assistant turn with text content
    last_response_ts = 0.0
    for rec in reversed(records):
        if rec.get("type") != "assistant":
            continue
        msg = rec.get("message") or {}
        if _has_visible_text(msg.get("content")):
            ts = _parse_ts(rec.get("timestamp"))
            if ts > last_response_ts:
                last_response_ts = ts
                break

    # "No-process" resting state for each decisive tail event. When a live
    # process IS bound, a non-errored resting state means the opposite:
    #   * a clean `result`  -> READY  (alive, finished the turn, awaiting input)
    #   * a pending user msg -> WORKING (claude has the message and is on it)
    #   * a trailing assistant msg -> WORKING (mid-turn, result not flushed yet)
    # State: scan from the end for the most recent decisive event.
    for rec in reversed(records):
        t = rec.get("type")
        if t == "result":
            # claude emits errored results two ways:
            #   * {"type":"result", "is_error": true, ...}
            #   * {"type":"result", "subtype":"error", ...}
            # The old code missed the latter and reported idle.
            errored = bool(rec.get("is_error")) or rec.get("subtype") == "error"
            if errored:
                return STATE_ERRORED, last_response_ts
            return (STATE_READY if bound else STATE_IDLE), last_response_ts
        if t == "user" and not rec.get("isReplay"):
            msg = rec.get("message") or {}
            content = msg.get("content")
            # If the user-message envelope is a tool_result wrapper with
            # is_error, it's an error state. Otherwise it's a real user
            # message with no follow-up yet => awaiting (or working if a
            # process is on it).
            if isinstance(content, list):
                for blk in content:
                    if not isinstance(blk, dict):
                        continue
                    if blk.get("type") == "tool_result" and blk.get("is_error"):
                        return STATE_ERRORED, last_response_ts
                # If the envelope is purely user text (no tool_result), it's
                # a real pending user message.
                if any(isinstance(b, dict) and b.get("type") == "text" for b in content):
                    return (STATE_WORKING if bound else STATE_AWAITING), last_response_ts
            elif isinstance(content, str) and content.strip():
                return (STATE_WORKING if bound else STATE_AWAITING), last_response_ts
        if t == "assistant":
            # Most reliable working/ready signal in stream-json mode: claude
            # NEVER writes a `result` record to the .jsonl, so every finished
            # turn just ends on an assistant message. Disambiguate by the
            # assistant's FINAL content block:
            #   * ends in `tool_use`  -> claude emitted a tool call; if a
            #     process is bound it's running it => WORKING. (Unbound: the
            #     turn was cut after a tool call with no result => IDLE.)
            #   * ends in `text`      -> the visible response is complete, the
            #     turn is done => READY when a process is still alive and
            #     waiting, else IDLE.
            msg = rec.get("message") or {}
            if _assistant_ends_with_tool_use(msg.get("content")):
                return (STATE_WORKING if bound else STATE_IDLE), last_response_ts
            return (STATE_READY if bound else STATE_IDLE), last_response_ts

    return (STATE_WORKING if bound else STATE_IDLE), last_response_ts


def _read_tail_records(path: Path) -> list[dict]:
    """Return up to the last `TAIL_MIN_RECORDS` complete JSONL records from
    the file, scanning backward in chunks so we never accidentally drop the
    final line just because it's huge.

    Strategy:
      1. Read `TAIL_CHUNK_BYTES` from the end into a buffer.
      2. Walk the buffer back-to-front; collect complete lines (delimited by
         `\\n`) until we have enough complete records.
      3. If the first line in the buffer doesn't start at a newline AND we
         haven't hit `TAIL_MAX_BYTES`, prepend another chunk and retry.

    This is the "tac-with-budget" pattern. ~50µs for a 2MB file on SSD.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if size == 0:
        return []

    buf = b""
    bytes_read = 0
    leading_complete = False  # True once the buffer starts right after a \n

    try:
        with path.open("rb") as f:
            while bytes_read < TAIL_MAX_BYTES and bytes_read < size:
                step = min(TAIL_CHUNK_BYTES, size - bytes_read)
                seek_to = size - bytes_read - step
                f.seek(seek_to, os.SEEK_SET)
                chunk = f.read(step)
                buf = chunk + buf
                bytes_read += step

                # If our buffer either starts at byte 0 of the file OR the
                # char before it (in the file) is a newline, the first line
                # in `buf` is complete.
                if seek_to == 0:
                    leading_complete = True
                else:
                    f.seek(seek_to - 1, os.SEEK_SET)
                    if f.read(1) == b"\n":
                        leading_complete = True

                # Count complete records (anything between two \n that
                # parses as JSON). We don't actually parse here in the loop
                # — that's the next step — but counting newlines gives us a
                # cheap "do we have enough?" check.
                #
                # If the first byte of the buffer is *not* preceded by a
                # newline, the first line is partial; subtract one from the
                # newline count.
                newlines = buf.count(b"\n")
                complete_lines = newlines if leading_complete else max(0, newlines - 1)
                if complete_lines >= TAIL_MIN_RECORDS:
                    break
    except OSError:
        return []

    # Now parse. If the leading line is partial, discard it; otherwise keep
    # everything.
    raw = buf
    if not leading_complete:
        nl = raw.find(b"\n")
        raw = raw[nl + 1:] if nl != -1 else b""

    records: list[dict] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def _parse_tail_response_ts(path: Path) -> float:
    """For sessions in the ACTIVE state we still want a sort timestamp."""
    records = _read_tail_records(path)
    for rec in reversed(records):
        if rec.get("type") != "assistant":
            continue
        msg = rec.get("message") or {}
        if _has_visible_text(msg.get("content")):
            return _parse_ts(rec.get("timestamp"))
    # Fall back to file mtime.
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _assistant_ends_with_tool_use(content) -> bool:
    """True if the assistant message's last content block is a `tool_use` —
    i.e. the turn isn't a finished text answer, claude is invoking a tool (and
    if a process is bound, actively running it). False for a text-final
    message (a completed response) or anything we can't read."""
    if not isinstance(content, list):
        return False
    for blk in reversed(content):
        if isinstance(blk, dict) and blk.get("type"):
            return blk.get("type") == "tool_use"
    return False


def _has_visible_text(content) -> bool:
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                if (blk.get("text") or "").strip():
                    return True
    return False


def _parse_ts(ts: str | None) -> float:
    if not ts or not isinstance(ts, str):
        return 0.0
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0
