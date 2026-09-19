"""Promotion evidence and context authorization for the routing broker.

Two of the broker's four dispatch gates were unreachable rather than merely
unset: ``main()`` supplied no ``promoted_profiles``, ``verified_endpoints`` or
``context_authorizer``, and no flag, environment variable or config file could.
This module supplies the two that can be satisfied by evidence the broker does
not author, and leaves the third — accepted paired-quality evidence — where it
belongs: an explicit, human-reviewed, root-owned record.

Nothing here promotes anything on its own. With no promotion file present every
gate stays exactly as shut as before, which is the property to preserve when
editing this module.

GTK-free.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from helios.backend.openrouter import PROFILES, ProfileRef, get_profile
from helios.log import get_logger

_log = get_logger("router-promotion")

__all__ = [
    "DEFAULT_PROMOTIONS_PATH",
    "authorize_inline_context",
    "load_promotions",
    "verified_endpoints",
]

#: Root-owned, service-readable. Deliberately alongside the credential rather
#: than in the service's own state directory: a service that can write its own
#: promotion record can promote itself, which makes the gate decorative.
DEFAULT_PROMOTIONS_PATH = Path("/etc/helios-router/promotions.json")

#: A promotion record is a short list of profile refs. Anything larger is not
#: one, and bounding the read keeps a hostile or corrupt file from being
#: slurped whole.
_MAX_RECORD_BYTES = 64 * 1024


def load_promotions(path: Path = DEFAULT_PROMOTIONS_PATH) -> frozenset[ProfileRef]:
    """Profiles a human has recorded as carrying accepted quality evidence.

    Absent file → empty set → nothing dispatches, which is the current
    production state and must remain the default.

    The record must be a **regular file owned by root** and not writable by
    group or other. Mode alone is not enough: a mode-0600 file owned by the
    router service passes every permission check while being exactly the thing
    the gate exists to exclude — evidence the service authored itself. Root
    ownership is what establishes the separation, since the service runs
    unprivileged.

    Opened once with ``O_NOFOLLOW`` and inspected via ``fstat`` on that same
    descriptor, so the bytes parsed are the bytes whose ownership was checked.
    Checking by pathname and reading by pathname are two different files if
    anything swaps the path in between, and a symlink would let an unprivileged
    writer point the check at a root-owned file and the read somewhere else.

    The record names profiles; it cannot invent them. A ref that is not in the
    frozen profile table is ignored, so the file cannot introduce a route.
    """
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return frozenset()
    except OSError as e:
        _log.warning("could not open promotion record %s: %s", path, e)
        return frozenset()

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            _log.error("refusing non-regular promotion record %s", path)
            return frozenset()
        if info.st_uid != 0:
            _log.error(
                "refusing promotion record %s owned by uid %d, not root — "
                "a record the service can author is not evidence",
                path, info.st_uid,
            )
            return frozenset()
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            _log.error(
                "refusing group/world-writable promotion record %s (mode %o)",
                path, stat.S_IMODE(info.st_mode),
            )
            return frozenset()
        try:
            payload_bytes = os.read(fd, _MAX_RECORD_BYTES + 1)
        except OSError as e:
            _log.warning("could not read promotion record %s: %s", path, e)
            return frozenset()
    finally:
        os.close(fd)

    # Bounded in bytes, and checked before decoding. Checking the decoded
    # length would count characters: multibyte UTF-8 can exceed the byte limit
    # while staying under the same number of characters.
    if len(payload_bytes) > _MAX_RECORD_BYTES:
        _log.error("refusing oversized promotion record %s", path)
        return frozenset()
    try:
        raw = payload_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        _log.error("promotion record %s is not valid UTF-8: %s", path, e)
        return frozenset()

    try:
        payload = json.loads(raw)
    except ValueError as e:
        _log.error("malformed promotion record %s: %s", path, e)
        return frozenset()
    entries = payload.get("promoted") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        _log.error("promotion record %s has no 'promoted' array", path)
        return frozenset()

    promoted: set[ProfileRef] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        profile_id = entry.get("profile_id")
        version = entry.get("version")
        if not isinstance(profile_id, str) or type(version) is not int:
            continue
        ref = ProfileRef(profile_id, version)
        if ref not in PROFILES:
            _log.error("promotion record names unknown profile %s v%s", profile_id, version)
            continue
        promoted.add(ref)
    if promoted:
        _log.info("promotion record admits %d profile(s)", len(promoted))
    return frozenset(promoted)


def verified_endpoints(
    refs=None,
    *,
    verifier=None,
) -> frozenset[ProfileRef]:
    """Profiles whose exact endpoint variant the live catalog confirms.

    Checked against OpenRouter's own endpoints listing, which is evidence the
    broker did not author — the distinction ``EndpointRef`` draws when it says a
    receipt cannot claim independent confirmation "without an external catalog
    gate". Any profile that cannot be positively confirmed is simply absent,
    leaving its gate shut.
    """
    if verifier is None:
        from helios.backend.openrouter.routes import verify_endpoint as verifier

    confirmed: set[ProfileRef] = set()
    for ref in (refs if refs is not None else PROFILES):
        profile = get_profile(ref)
        try:
            ok = verifier(
                profile.requested_model,
                profile.endpoint.provider_slug,
                profile.endpoint.quantization,
            )
        except Exception as e:  # noqa: BLE001 — unverifiable is unverified
            _log.info("endpoint verification errored for %s: %s", ref.profile_id, e)
            ok = False
        if ok:
            confirmed.add(ref)
    return frozenset(confirmed)


def authorize_inline_context(binding: dict[str, str], task: dict[str, Any]) -> bool:
    """Authorize a task whose context is entirely inline and already screened.

    This is not a general context compiler and must not be mistaken for one. It
    authorizes exactly the case the routing policy has already narrowed to by
    the time this runs: a task with at least one context ref, every ref inline,
    total inline content within the serving profile's own input ceiling, and
    admission having already rejected anything that looked like a credential.

    It re-checks those invariants rather than trusting that they held earlier.
    That is the whole value — the policy could grow a path that reaches this
    gate without them, and a rubber stamp would not notice. Workspace paths,
    diffs and work-event ranges still need a real compiler and are refused
    before this point.
    """
    if not isinstance(binding, dict) or not binding.get("kind"):
        return False
    if not isinstance(task, dict):
        return False
    refs = task.get("context_refs")
    if not isinstance(refs, list) or not refs:
        return False
    total = 0
    for ref in refs:
        if not isinstance(ref, dict) or ref.get("kind") != "inline":
            return False
        value = ref.get("ref")
        if not isinstance(value, str) or not value:
            return False
        total += len(value)

    from helios.backend.router_policy import CANARY_PROFILE

    ceiling = get_profile(CANARY_PROFILE).max_input_chars
    if total > ceiling:
        return False
    return True
