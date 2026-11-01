"""Spots kept through the MCP companion: a draft item's ``cell``.

An app, web clip or web app on a page can keep a cell (0-23); the rest flow around it. The
draft carries it like the editor's ``ops.js`` (``item.cell``), sends it as the page's
``pins``; the engine already writes a pinned page in screen order.
"""

import unittest

from ios_homescreen.model import describe_changes
from ios_homescreen.planner import plan_layout
from ios_homescreen.snapshots import make_read
from rejig_mcp import draft
from rejig_mcp.draft import DraftError, apply_operations
from rejig_mcp.optimize import optimize

from tests.test_offloaded_apps import app

A, B, S = "com.example.a", "com.example.b", "com.example.s"


def item(ident, kind="app"):
    return {"type": kind, "id": ident, "name": ident.rsplit(".", 1)[-1], "cells": 1}


def view_page(ids, pins=None):
    page = {"items": [item(i) for i in ids]}
    return {**page, "pins": pins} if pins else page


class Draft(unittest.TestCase):
    def layout(self, pins=None):
        return draft.from_view({"dock": [item("com.example.dock")], "pages": [view_page([A, B, S], pins)]})

    def move(self, layout, ident, to):
        return apply_operations(layout, [{"op": "move", "item": ident, "to": to}], {})

    def test_view_pins_become_cells_and_go_back_as_pins(self):
        layout = self.layout({S: 23})
        self.assertEqual([x.get("cell") for x in layout["pages"][0]], [None, None, 23])
        self.assertEqual(draft.to_layout(layout)["pages"][0]["pins"], {S: 23})
        self.assertNotIn("pins", draft.to_layout(self.layout())["pages"][0])

    def test_a_move_with_a_cell_pins_and_one_without_unpins(self):
        pinned = self.move(self.layout(), A, {"section": "page", "page": 1, "cell": 7})
        self.assertEqual(draft.to_layout(pinned)["pages"][0]["pins"], {A: 7})
        back = self.move(pinned, A, {"section": "page", "page": 1, "index": 1})
        self.assertNotIn("pins", draft.to_layout(back)["pages"][0])
        docked = self.move(pinned, A, {"section": "dock"})
        self.assertNotIn("cell", docked["dock"][-1])

    def test_only_apps_on_a_page_take_a_valid_cell(self):
        layout = self.layout()
        layout["pages"][0].append(item("com.example.w", "widget"))
        for to, code in (
            ({"section": "dock", "cell": 3}, "not_pinnable"),
            ({"section": "page", "page": 1, "cell": 24}, "bad_cell"),
            ({"section": "page", "page": 1, "cell": True}, "bad_cell"),
            ({"section": "page", "page": 1, "cell": "3"}, "bad_cell"),
        ):
            with self.subTest(to=to), self.assertRaises(DraftError) as caught:
                self.move(layout, A, to)
            self.assertEqual(caught.exception.code, code)
        with self.assertRaises(DraftError) as caught:
            self.move(layout, "com.example.w", {"section": "page", "page": 1, "cell": 3})
        self.assertEqual(caught.exception.code, "not_pinnable")

    def test_a_folder_never_holds_a_pinned_item(self):
        made = apply_operations(self.layout({S: 23}), [{"op": "folder_create", "name": "F", "items": [S, A]}], {})
        [folder] = [x for x in made["pages"][0] if x["type"] == "folder"]
        self.assertFalse(any("cell" in x for page in folder["pages"] for x in page))

    def test_the_optimizer_reflows_and_drops_kept_spots(self):
        result = optimize(self.layout({S: 23}), [{"app": S, "minutes": 50}], {A: "a", B: "b", S: "s"}, [])
        self.assertFalse(any("cell" in x for page in result["layout"]["pages"] for x in page))


class Engine(unittest.TestCase):
    def test_a_pinned_item_out_of_place_in_the_list_is_not_reordered(self):
        before = {"dock": [], "pages": [view_page([A, B, S])]}
        after = {"dock": [], "pages": [view_page([S, A, B], {S: 23})]}
        self.assertEqual([c["kind"] for c in describe_changes(before, after)], ["pinned"])
        read = {"dock": [], "pages": [view_page([A, B, S], {S: 23})]}
        self.assertEqual(describe_changes(read, after), [])

    def test_a_pinned_page_is_written_in_screen_order(self):
        state = [[app("com.example.dock")], [app(A), app(B), app(S)]]
        read = make_read(state, {i: {"CFBundleDisplayName": i} for i in (A, B, S, "com.example.dock")})
        layout = {"dock": {"items": ["com.example.dock"]}, "pages": [{"items": [S, A, B], "pins": {S: 23}}]}
        plan = plan_layout(layout, read)
        self.assertEqual([x["displayIdentifier"] for x in plan.payload["iconLists"][0]], [A, B, S])
        self.assertEqual(plan.payload["pins"], [{S: 23}])


if __name__ == "__main__":
    unittest.main()
