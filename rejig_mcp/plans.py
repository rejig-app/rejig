"""Plans, operation journals, the approval text and the App Library check.

A plan is the exact thing a person approves: one layout (or one backup) for one phone,
planned against one read. Its id is the SHA-256 of those fields, so an edited plan file no
longer matches its name and is refused. A journal records what happened to a plan's write;
it is how a repeated or lost apply_plan call finds the outcome instead of writing again.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import plistlib
import re
import unicodedata
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

from ios_homescreen.report import change_lines as engine_change_lines
from ios_homescreen.snapshots import state_from_file
from ios_homescreen.state import to_payload

BINDING = (
    "kind",
    "device_udid",
    "base_pull",
    "base_fingerprint",
    "layout",
    "target_fingerprint",
    "backup",
    "backup_digest",
)
NAME_LIMIT = 40
ID_LIMIT = 80
_ID = re.compile(r"^[0-9a-f]{64}$")
# Journal states once a write may have reached the editor: the plan is spent, and a repeated
# apply_plan returns the record instead of writing again.
ATTEMPTED = frozenset(
    {
        "submitting",
        "writing",
        "refused",
        "applied",
        "applied_reconciled",
        "applied_unverified",
        "readback_mismatch",
        "rolled_back",
        "rollback_failed",
        "library_violation",
        "not_written",
        "outcome_unknown",
    }
)


class PlanError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def plan_id(plan: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical({key: plan.get(key) for key in BINDING})).hexdigest()


def _write(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=1, sort_keys=True))
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _read(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None


def save_plan(root: Path, plan: Mapping[str, Any]) -> str:
    """Write ``ROOT/plans/<id>.json`` and return the id."""
    ident = plan_id(plan)
    _write(root / "plans" / f"{ident}.json", {**plan, "plan_id": ident})
    return ident


def load_plan(root: Path, ident: str) -> dict[str, Any]:
    if not isinstance(ident, str) or not _ID.match(ident):
        raise PlanError("plan_not_found", "No plan has that id; call preview_draft first.")
    try:
        data = _read(root / "plans" / f"{ident}.json")
    except ValueError:
        data = {}
    if data is None:
        raise PlanError("plan_not_found", "No plan has that id; call preview_draft first.")
    if not isinstance(data, dict) or plan_id(data) != ident or data.get("plan_id") != ident:
        raise PlanError("plan_invalid", "This plan file was changed after it was made; preview again.")
    return data


def backup_digest(target: Any) -> str:
    """What a restore writes, as a SHA-256; the same as the editor's ``backup_digest``."""
    return hashlib.sha256(plistlib.dumps(target, sort_keys=True)).hexdigest()


def backup_digest_of(path: Path) -> str:
    return backup_digest(to_payload(state_from_file(path)))


def read_journal(root: Path, ident: str) -> dict[str, Any] | None:
    return _read(root / "operations" / f"{ident}.json")


@contextlib.contextmanager
def _locked(root: Path, ident: str) -> Iterator[None]:
    """One plan's journal, locked across every helper process on this Mac."""
    path = root / "locks" / f"{ident}.lock"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def write_journal(root: Path, ident: str, record: Mapping[str, Any]) -> dict[str, Any]:
    """Record ``record``, unless a write attempt is on record and ``record`` is not one:
    an attempt is never overwritten by a later decline, cancel or wait. The record kept."""
    with _locked(root, ident):
        existing = read_journal(root, ident)
        if existing and existing.get("state") in ATTEMPTED and record.get("state") not in ATTEMPTED:
            return existing
        _write(root / "operations" / f"{ident}.json", record)
        return dict(record)


def claim(root: Path, ident: str, record: Mapping[str, Any]) -> dict[str, Any] | None:
    """Record the start of this plan's one write attempt. ``None`` when this caller won;
    otherwise the attempt already on record (another call or helper got there first)."""
    with _locked(root, ident):
        existing = read_journal(root, ident)
        if existing and existing.get("state") in ATTEMPTED:
            return existing
        _write(root / "operations" / f"{ident}.json", record)
        return None


def _clean(text: Any) -> str:
    """One line, no control or format characters, at most NAME_LIMIT characters."""
    text = "".join(" " if unicodedata.category(c)[0] in "CZ" else c for c in str(text))
    text = " ".join(text.split())
    return text if len(text) <= NAME_LIMIT else text[: NAME_LIMIT - 1] + "…"


def _clean_change(change: Mapping[str, Any]) -> dict[str, Any]:
    cleaned = dict(change)
    for key in ("name", "old_name"):
        if key in cleaned:
            cleaned[key] = _clean(cleaned[key])
    for key in ("from", "to"):
        where = cleaned.get(key)
        if isinstance(where, Mapping) and "folder" in where:
            cleaned[key] = {**where, "folder": _clean(where["folder"])}
    return cleaned


def _item_id(change: Mapping[str, Any]) -> str:
    """The item's own id (a bundle id for an app), so two apps with one name differ."""
    if change.get("type") == "folder" or not change.get("id"):
        return ""
    text = "".join(c for c in str(change["id"]) if c.isprintable() and not c.isspace())
    return f" ({text[:ID_LIMIT]})" if text else ""


def change_lines(changes: Iterable[Mapping[str, Any]]) -> list[str]:
    """The numbered change list a person approves, from the engine's own wording.

    Names come from the phone or from the model (folder names), so each is flattened to one
    line and cut to NAME_LIMIT characters: a name cannot pose as part of the approval text.
    Each item's id follows its line.
    """
    cleaned = [_clean_change(change) for change in changes]
    lines = engine_change_lines(cleaned)
    return [
        f"{number}. {line.strip()}{_item_id(change)}"
        for number, (change, line) in enumerate(zip(cleaned, lines, strict=True), 1)
    ]


def library_moves(changes: Iterable[Mapping[str, Any]]) -> dict[str, list[str]]:
    """Apps a change list brings from the App Library and sends to it."""
    changes = list(changes)
    return {
        "to_home": sorted(
            c["id"] for c in changes if c["kind"] == "added" and c.get("type", "app") == "app"
        ),
        "to_library": sorted(c["id"] for c in changes if c["kind"] == "to_library"),
    }


def library_check(
    before: Iterable[str], after: Iterable[str], *, to_home: Iterable[str], to_library: Iterable[str]
) -> dict[str, Any]:
    """Did the App Library-only set change exactly as planned?

    ``before`` and ``after`` are the App Library-only app ids of the read the plan was made
    against and of the read after the write.
    """
    before, after = set(before), set(after)
    expected = (before - set(to_home)) | set(to_library)
    # shortcut: an app uninstalled during the write also counts as on the Home Screen; the
    # write is still reported with its backup, so the person checks it.
    on_home = sorted(expected - after)
    in_library = sorted(after - expected)
    return {
        "ok": not on_home and not in_library,
        "before": len(before),
        "after": len(after),
        "unexpected_on_home": on_home,
        "unexpected_in_library": in_library,
    }
