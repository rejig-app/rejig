"""Draft layouts and the edits the model can make to them.

A draft layout is the editor page's own shape: ``{"dock": [item], "pages": [[item]]}``,
with items as the editor's view gives them (``type``, ``id``, ``name``...), a folder being
``{"type": "folder", "id", "name", "pages": [[item]]}``. The edits mirror the page's
``ops.js`` (move, folders, pages); whether the result can be written is decided by the
editor itself (``/api/plan``), with the same checks the page gets.
"""

from __future__ import annotations

import copy
import secrets
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

FOLDER_SLOTS = 9
FOLDABLE = frozenset({"app", "webclip", "webapp"})
NAME_LIMIT = 40


class DraftError(Exception):
    def __init__(self, code: str, message: str, *, index: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.index = index


def from_view(view: Mapping[str, Any]) -> dict[str, Any]:
    pages = view.get("pages", []) or []
    return copy.deepcopy(
        {
            "dock": list(view.get("dock", []) or []),
            "pages": [list(p.get("items", []) if isinstance(p, Mapping) else p) for p in pages],
        }
    )


def to_layout(layout: Mapping[str, Any]) -> dict[str, Any]:
    """The ``/api/plan`` and ``/api/apply`` body shape."""
    return {"dock": layout["dock"], "pages": [{"items": items} for items in layout["pages"]]}


def _top(layout: Mapping[str, Any]) -> list[tuple[list[Any], int]]:
    lists = [layout["dock"], *layout["pages"]]
    return [(items, index) for items in lists for index in range(len(items))]


def all_items(layout: Mapping[str, Any]) -> list[dict[str, Any]]:
    found = []
    for items, index in _top(layout):
        item = items[index]
        found.append(item)
        if item.get("type") == "folder":
            found.extend(x for page in item["pages"] for x in page)
    return found


def shown_apps(layout: Mapping[str, Any]) -> set[str]:
    return {item["id"] for item in all_items(layout) if item.get("type") == "app"}


def _locate(layout: Mapping[str, Any], ident: str) -> tuple[list[Any], int] | None:
    for items, index in _top(layout):
        item = items[index]
        if item.get("id") == ident:
            return items, index
        if item.get("type") == "folder":
            for page in item["pages"]:
                for inner, entry in enumerate(page):
                    if entry.get("id") == ident:
                        return page, inner
    return None


def _folder(layout: Mapping[str, Any], ref: Any) -> dict[str, Any]:
    """A top-level folder by its id, else by its exact (unique) name."""
    folders = [items[i] for items, i in _top(layout) if items[i].get("type") == "folder"]
    for folder in folders:
        if folder.get("id") == ref:
            return folder
    named = [folder for folder in folders if folder.get("name") == ref]
    if len(named) > 1:
        raise DraftError("ambiguous_folder", f"Several folders are named {ref!r}; use its id.")
    if not named:
        raise DraftError("folder_not_found", f"There is no folder {ref!r}.")
    return named[0]


def _name(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > NAME_LIMIT
        or any(unicodedata.category(c)[0] == "C" for c in value)
    ):
        raise DraftError("bad_name", f"A folder name is 1 to {NAME_LIMIT} characters on one line.")
    return value.strip()


def _take(layout: dict[str, Any], ident: Any, apps: Mapping[str, str]) -> dict[str, Any]:
    """Take an item out of the layout; an installed app not on it comes from the App Library."""
    if not isinstance(ident, str):
        raise DraftError("item_not_found", "Name an item by its id.")
    found = _locate(layout, ident)
    if found is not None:
        items, index = found
        return items.pop(index)
    if ident in apps:
        return {"type": "app", "id": ident, "name": apps[ident], "cells": 1}
    raise DraftError("item_not_found", f"{ident!r} is not on the Home Screen or in the App Library.")


def _page_number(layout: Mapping[str, Any], number: Any, *, extra: int = 0) -> int:
    count = len(layout["pages"]) + extra
    if not isinstance(number, int) or isinstance(number, bool) or not 1 <= number <= count:
        raise DraftError("bad_page", f"Pages are numbered 1 to {count}.")
    return number - 1


def _insert(items: list[Any], index: Any, item: Mapping[str, Any]) -> None:
    at = len(items) if index is None else index - 1 if isinstance(index, int) else len(items)
    items.insert(max(0, min(at, len(items))), item)


def _put(layout: dict[str, Any], item: dict[str, Any], to: Any) -> None:
    if not isinstance(to, Mapping):
        raise DraftError("bad_operation", "to must be {section, page?, index?, folder?}.")
    section = to.get("section")
    if section == "dock":
        if item.get("type") == "widget":
            raise DraftError("widget_in_dock", "Widgets cannot go in the dock.")
        _insert(layout["dock"], to.get("index"), item)
    elif section == "page":
        page = _page_number(layout, to.get("page", len(layout["pages"])), extra=1)
        if page == len(layout["pages"]):
            layout["pages"].append([])
        _insert(layout["pages"][page], to.get("index"), item)
    elif section == "folder":
        if item.get("type") == "folder":
            raise DraftError("nested_folder", "A folder cannot go inside a folder.")
        if item.get("type") not in FOLDABLE:
            raise DraftError("widget_in_folder", "Widgets cannot go in a folder.")
        folder = _folder(layout, to.get("folder"))
        inside = [x for page in folder["pages"] for x in page]
        _insert(inside, to.get("index"), item)
        folder["pages"] = [inside]
    else:
        raise DraftError("bad_operation", "section is dock, page or folder.")


def _tidy(layout: dict[str, Any]) -> None:
    """Folders: no empty folders, at most FOLDER_SLOTS items a page (the rest flows on)."""
    for items in [layout["dock"], *layout["pages"]]:
        for item in list(items):
            if item.get("type") != "folder":
                continue
            inside = [x for page in item["pages"] for x in page]
            if not inside:
                items.remove(item)
                continue
            if any(len(page) > FOLDER_SLOTS or not page for page in item["pages"]):
                item["pages"] = [
                    inside[i : i + FOLDER_SLOTS] for i in range(0, len(inside), FOLDER_SLOTS)
                ]


def _operation(layout: dict[str, Any], op: Mapping[str, Any], apps: Mapping[str, str]) -> None:
    kind = op.get("op")
    if kind == "move":
        _put(layout, _take(layout, op.get("item"), apps), op.get("to"))
    elif kind == "send_to_library":
        for ident in op.get("items") or []:
            found = _locate(layout, ident) if isinstance(ident, str) else None
            if found is None:
                raise DraftError("item_not_found", f"{ident!r} is not on the Home Screen.")
            items, index = found
            if items[index].get("type") != "app":
                raise DraftError("not_an_app", "Only apps go to the App Library; nothing is removed.")
            items.pop(index)
    elif kind == "folder_create":
        name = _name(op.get("name"))
        refs = op.get("items") or []
        if not isinstance(refs, list) or not refs:
            raise DraftError("bad_operation", "folder_create needs items.")
        to = op.get("to")
        if to is None:
            first = _locate(layout, refs[0]) if isinstance(refs[0], str) else None
            if first is None or first[0] not in [layout["dock"], *layout["pages"]]:
                raise DraftError("bad_operation", "Say where the folder goes: to {section, page, index}.")
            where, index = first
            to = (
                {"section": "dock", "index": index + 1}
                if where is layout["dock"]
                else {"section": "page", "page": layout["pages"].index(where) + 1, "index": index + 1}
            )
        taken = []
        for ref in refs:
            item = _take(layout, ref, apps)
            if item.get("type") not in FOLDABLE:
                raise DraftError("not_foldable", "Only apps, web clips and web apps go in folders.")
            taken.append(item)
        folder = {"type": "folder", "id": f"new-{secrets.token_hex(4)}", "name": name, "cells": 1}
        folder["pages"] = [taken]
        _put(layout, folder, to)
    elif kind == "folder_rename":
        _folder(layout, op.get("folder"))["name"] = _name(op.get("name"))
    elif kind == "folder_dissolve":
        folder = _folder(layout, op.get("folder"))
        items, index = _locate(layout, folder["id"])
        items[index : index + 1] = [x for page in folder["pages"] for x in page]
    elif kind == "page_add":
        at = op.get("at", len(layout["pages"]) + 1)
        layout["pages"].insert(_page_number(layout, at, extra=1), [])
    elif kind == "page_remove":
        page = _page_number(layout, op.get("page"))
        if layout["pages"][page]:
            raise DraftError("page_not_empty", "Move everything off a page before removing it.")
        del layout["pages"][page]
    elif kind == "page_move":
        source = _page_number(layout, op.get("from"))
        target = _page_number(layout, op.get("to"))
        layout["pages"].insert(target, layout["pages"].pop(source))
    else:
        raise DraftError("bad_operation", f"Unknown operation {kind!r}.")


def apply_operations(
    layout: Mapping[str, Any], operations: list[Mapping[str, Any]], apps: Mapping[str, str]
) -> dict[str, Any]:
    """A new layout with every operation done in order; all or nothing."""
    result = copy.deepcopy(dict(layout))
    for index, op in enumerate(operations):
        try:
            if not isinstance(op, Mapping):
                raise DraftError("bad_operation", "Each operation is an object with an op.")
            _operation(result, op, apps)
            _tidy(result)
        except DraftError as error:
            error.index = index
            raise
    return result


@dataclass
class Draft:
    id: str
    revisions: list[dict[str, Any]]
    apps: dict[str, str]
    base: dict[str, Any] = field(default_factory=dict)

    @property
    def revision(self) -> int:
        return len(self.revisions) - 1

    @property
    def layout(self) -> dict[str, Any]:
        return self.revisions[-1]


class Drafts:
    """The drafts of one MCP client session, in memory."""

    def __init__(self) -> None:
        self._drafts: dict[str, Draft] = {}

    def create(self, view: Mapping[str, Any], apps: Mapping[str, str], *, base: Mapping[str, Any]) -> Draft:
        made = Draft(f"d-{secrets.token_hex(4)}", [from_view(view)], dict(apps), dict(base))
        self._drafts[made.id] = made
        return made

    def get(self, ident: Any) -> Draft:
        found = self._drafts.get(ident) if isinstance(ident, str) else None
        if found is None:
            raise DraftError("draft_not_found", "No draft has that id; call read_layout first.")
        return found

    def edit(self, ident: Any, base_revision: Any, operations: list[Mapping[str, Any]]) -> Draft:
        current = self.get(ident)
        if base_revision != current.revision:
            raise DraftError(
                "revision_conflict", f"The draft is at revision {current.revision}; edit that one."
            )
        if len(operations) == 1 and operations[0].get("op") == "undo":
            target = operations[0].get("to_revision")
            if not isinstance(target, int) or not 0 <= target <= current.revision:
                raise DraftError("bad_operation", "undo needs to_revision of an earlier revision.", index=0)
            current.revisions.append(copy.deepcopy(current.revisions[target]))
            return current
        current.revisions.append(apply_operations(current.layout, operations, current.apps))
        return current

    def add_revision(self, ident: str, layout: Mapping[str, Any]) -> Draft:
        current = self.get(ident)
        current.revisions.append(copy.deepcopy(dict(layout)))
        return current
