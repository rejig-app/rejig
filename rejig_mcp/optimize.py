"""Fewer swipes from usage the person supplies (for example Screen Time minutes).

The most-used apps go first on page 1, out of their folders if needed; every other item
keeps its order and pages fill again from there. The dock stays as it is, App Library-only
apps stay in the App Library and nothing is removed. Usage that matches no app, or several,
is reported, never guessed, and an app without usage is "no data", never "unused".
"""

from __future__ import annotations

import copy
import math
from collections.abc import Iterable, Mapping
from typing import Any

from .draft import DraftError, all_items

PAGE_CELLS = 24
MAX_ROWS = 1000


def _rows(usage: Any) -> list[tuple[str, float]]:
    if not isinstance(usage, list) or len(usage) > MAX_ROWS:
        raise DraftError("usage_invalid", f"usage is a list of at most {MAX_ROWS} {{app, minutes}} rows.")
    rows = []
    for row in usage:
        app = row.get("app") if isinstance(row, Mapping) else None
        minutes = row.get("minutes") if isinstance(row, Mapping) else None
        if (
            not isinstance(app, str)
            or not app.strip()
            or isinstance(minutes, bool)
            or not isinstance(minutes, int | float)
            or not math.isfinite(minutes)
            or minutes < 0
        ):
            raise DraftError("usage_invalid", "Each usage row is {app: id or exact name, minutes >= 0}.")
        rows.append((app.strip(), float(minutes)))
    return rows


def _places(layout: Mapping[str, Any]) -> dict[str, float]:
    """Swipes plus taps to reach each app: page n costs n - 1, a folder one tap more."""
    cost: dict[str, float] = {}
    lists = [(0, layout["dock"]), *((n, page) for n, page in enumerate(layout["pages"]))]
    for swipes, items in lists:
        for item in items:
            if item.get("type") == "app":
                cost[item["id"]] = swipes
            elif item.get("type") == "folder":
                for page in item["pages"]:
                    for inner in page:
                        cost[inner["id"]] = swipes + 1
    return cost


def _estimate(layout: Mapping[str, Any], minutes: Mapping[str, float]) -> float | None:
    cost = _places(layout)
    total = sum(minutes[i] for i in minutes if i in cost)
    if not total:
        return None
    return round(sum(minutes[i] * cost[i] for i in minutes if i in cost) / total, 2)


def _cells(item: Mapping[str, Any]) -> int:
    cells = item.get("cells", 1)
    return cells if isinstance(cells, int) and cells > 0 else 1


def _paginate(items: Iterable[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    pages: list[list[dict[str, Any]]] = [[]]
    used = 0
    for item in items:
        if used + _cells(item) > PAGE_CELLS and pages[-1]:
            pages.append([])
            used = 0
        pages[-1].append(item)
        used += _cells(item)
    return pages


def optimize(
    layout: Mapping[str, Any],
    usage: Any,
    apps: Mapping[str, str],
    library: Iterable[str],
    options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    options = options or {}
    first_page = options.get("max_first_page", PAGE_CELLS)
    if not isinstance(first_page, int) or not 0 <= first_page <= PAGE_CELLS:
        raise DraftError("usage_invalid", f"max_first_page is 0 to {PAGE_CELLS}.")
    rows = _rows(usage)
    library = set(library)
    shown = {item["id"] for item in all_items(layout) if item.get("type") == "app"}
    by_name: dict[str, list[str]] = {}
    for ident, name in apps.items():
        by_name.setdefault(name, []).append(ident)

    minutes: dict[str, float] = {}
    unmatched, ambiguous, library_only = [], [], []
    for app, spent in rows:
        candidates = [app] if app in apps else sorted(by_name.get(app, []))
        if len(candidates) > 1:
            ambiguous.append({"row": app, "candidates": candidates})
        elif not candidates:
            unmatched.append(app)
        elif candidates[0] in shown:
            minutes[candidates[0]] = minutes.get(candidates[0], 0.0) + spent
        elif candidates[0] in library:
            library_only.append(candidates[0])
        else:
            unmatched.append(app)

    # The dock stays whole, its folders' apps too.
    dock = {item["id"] for item in all_items({"dock": layout["dock"], "pages": []}) if item.get("type") == "app"}
    order = {item["id"]: n for n, item in enumerate(all_items(layout))}
    ranked = sorted(
        (ident for ident, spent in minutes.items() if spent > 0 and ident not in dock),
        key=lambda ident: (-minutes[ident], order[ident]),
    )[:first_page]

    result = copy.deepcopy(dict(layout))
    picked = set(ranked)
    found: dict[str, dict[str, Any]] = {}
    rest: list[dict[str, Any]] = []
    for page in result["pages"]:
        for item in page:
            if item.get("id") in picked:
                found[item["id"]] = item
                continue
            if item.get("type") == "folder":
                kept = []
                for inner in (x for p in item["pages"] for x in p):
                    if inner.get("id") in picked:
                        found[inner["id"]] = inner
                    else:
                        kept.append(inner)
                if not kept:
                    continue
                item["pages"] = [kept[i : i + 9] for i in range(0, len(kept), 9)]
            rest.append(item)
    # Rejig: the pages flow again from the top, so gaps Rejig placed are not kept.
    result["pages"] = _paginate(
        {k: v for k, v in item.items() if k != "cell"} for item in [found[ident] for ident in ranked] + rest
    )

    return {
        "layout": result,
        "coverage": {
            "matched": sorted(minutes),
            "unmatched_rows": unmatched,
            "ambiguous": ambiguous,
            "library_only": sorted(set(library_only)),
            "apps_without_data": sorted(shown - set(minutes)),
            "period": options.get("period"),
        },
        "swipe_estimate": {"before": _estimate(layout, minutes), "after": _estimate(result, minutes)},
    }
