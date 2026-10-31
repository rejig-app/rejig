"""Draft edits of the MCP companion: the same moves the editor page offers, on its view."""

import unittest

from rejig_mcp import draft
from rejig_mcp.draft import DraftError


def app(ident, name=None):
    return {"type": "app", "id": ident, "name": name or ident.split(".")[-1], "cells": 1}


VIEW = {
    "dock": [app("com.apple.mobilesafari", "Safari"), app("com.apple.mobilemail", "Mail")],
    "pages": [
        {
            "items": [
                app("com.apple.Maps", "Maps"),
                app("com.apple.mobilenotes", "Notes"),
                {
                    "type": "folder",
                    "id": "folder-1",
                    "name": "Work",
                    "cells": 1,
                    "pages": [[app("com.slack", "Slack"), app("us.zoom", "Zoom")]],
                },
                {"type": "widget", "id": "widget-1", "name": "Weather", "cells": 4},
            ]
        },
        {"items": [app("com.apple.Photos", "Photos")]},
    ],
}
APPS = {
    a["id"]: a["name"]
    for a in [
        *VIEW["dock"],
        *VIEW["pages"][0]["items"][:2],
        *VIEW["pages"][0]["items"][2]["pages"][0],
        *VIEW["pages"][1]["items"],
        app("com.apple.chess", "Chess"),
        app("com.apple.stocks", "Stocks"),
    ]
}


def ids(items):
    return [item["id"] for item in items]


class DraftEditTest(unittest.TestCase):
    def setUp(self):
        self.layout = draft.from_view(VIEW)

    def edit(self, *operations):
        return draft.apply_operations(self.layout, list(operations), APPS)

    def test_from_view_and_to_layout_round_trip(self):
        out = draft.to_layout(self.layout)
        self.assertEqual(ids(out["dock"]), ids(VIEW["dock"]))
        self.assertEqual([ids(p["items"]) for p in out["pages"]], [ids(p["items"]) for p in VIEW["pages"]])

    def test_move_within_and_between_places(self):
        out = self.edit(
            {"op": "move", "item": "com.apple.Photos", "to": {"section": "dock", "index": 1}},
            {"op": "move", "item": "com.apple.Maps", "to": {"section": "page", "page": 2}},
        )
        self.assertEqual(ids(out["dock"])[0], "com.apple.Photos")
        self.assertEqual(ids(out["pages"][1]), ["com.apple.Maps"])
        self.assertEqual(ids(out["pages"][0])[0], "com.apple.mobilenotes")

    def test_move_into_and_out_of_a_folder_by_name_or_id(self):
        out = self.edit(
            {"op": "move", "item": "com.apple.Maps", "to": {"section": "folder", "folder": "Work"}},
            {"op": "move", "item": "com.slack", "to": {"section": "page", "page": 1, "index": 1}},
        )
        folder = out["pages"][0][2]
        self.assertEqual(ids(folder["pages"][0]), ["us.zoom", "com.apple.Maps"])
        self.assertEqual(ids(out["pages"][0])[0], "com.slack")

    def test_move_from_the_app_library_and_send_back(self):
        out = self.edit(
            {"op": "move", "item": "com.apple.chess", "to": {"section": "page", "page": 2, "index": 1}},
            {"op": "send_to_library", "items": ["com.apple.Photos"]},
        )
        self.assertEqual(ids(out["pages"][1]), ["com.apple.chess"])
        self.assertEqual(out["pages"][1][0]["name"], "Chess")
        self.assertNotIn("com.apple.Photos", draft.shown_apps(out))

    def test_folder_create_rename_and_dissolve(self):
        out = self.edit(
            {"op": "folder_create", "name": "Go", "items": ["com.apple.Maps", "com.apple.Photos"]},
            {"op": "folder_rename", "folder": "Go", "name": "Travel"},
            {"op": "folder_dissolve", "folder": "folder-1"},
        )
        travel = out["pages"][0][0]
        self.assertEqual((travel["type"], travel["name"]), ("folder", "Travel"))
        self.assertEqual(ids(travel["pages"][0]), ["com.apple.Maps", "com.apple.Photos"])
        self.assertEqual(ids(out["pages"][0])[2:4], ["com.slack", "us.zoom"])
        self.assertEqual(out["pages"][1], [])

    def test_a_folder_emptied_by_moves_disappears(self):
        out = self.edit(
            {"op": "send_to_library", "items": ["com.slack"]},
            {"op": "move", "item": "us.zoom", "to": {"section": "page", "page": 2}},
        )
        self.assertNotIn("folder-1", ids(out["pages"][0]))

    def test_pages_add_move_and_remove(self):
        out = self.edit(
            {"op": "page_add", "at": 1},
            {"op": "page_move", "from": 3, "to": 2},
            {"op": "page_remove", "page": 1},
        )
        self.assertEqual(ids(out["pages"][0]), ["com.apple.Photos"])
        self.assertEqual(ids(out["pages"][1])[0], "com.apple.Maps")

    def test_refusals(self):
        cases = [
            ({"op": "move", "item": "nope", "to": {"section": "dock"}}, "item_not_found"),
            ({"op": "move", "item": "widget-1", "to": {"section": "dock"}}, "widget_in_dock"),
            ({"op": "move", "item": "widget-1", "to": {"section": "folder", "folder": "Work"}}, "widget_in_folder"),
            ({"op": "move", "item": "folder-1", "to": {"section": "folder", "folder": "Work"}}, "nested_folder"),
            ({"op": "send_to_library", "items": ["widget-1"]}, "not_an_app"),
            ({"op": "page_remove", "page": 1}, "page_not_empty"),
            ({"op": "page_move", "from": 9, "to": 1}, "bad_page"),
            ({"op": "folder_rename", "folder": "Work", "name": "a\nb"}, "bad_name"),
            ({"op": "folder_rename", "folder": "Work", "name": "x" * 41}, "bad_name"),
            ({"op": "folder_create", "name": "W", "items": ["widget-1"]}, "not_foldable"),
            ({"op": "explode"}, "bad_operation"),
        ]
        for operation, code in cases:
            with self.subTest(code=code), self.assertRaises(DraftError) as caught:
                self.edit({"op": "page_add"}, operation)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(caught.exception.index, 1)

    def test_a_failed_batch_leaves_the_layout_alone(self):
        before = draft.to_layout(self.layout)
        with self.assertRaises(DraftError):
            self.edit({"op": "send_to_library", "items": ["com.apple.Maps"]}, {"op": "explode"})
        self.assertEqual(draft.to_layout(self.layout), before)


class DraftRevisionsTest(unittest.TestCase):
    def test_revisions_conflicts_and_undo(self):
        store = draft.Drafts()
        current = store.create(VIEW, APPS, base={"pull": "p1"})
        self.assertEqual(current.revision, 0)
        current = store.edit(current.id, 0, [{"op": "send_to_library", "items": ["com.apple.Maps"]}])
        self.assertEqual(current.revision, 1)
        with self.assertRaises(DraftError) as caught:
            store.edit(current.id, 0, [{"op": "page_add"}])
        self.assertEqual(caught.exception.code, "revision_conflict")
        current = store.edit(current.id, 1, [{"op": "undo", "to_revision": 0}])
        self.assertEqual(current.revision, 2)
        self.assertIn("com.apple.Maps", draft.shown_apps(current.layout))
        with self.assertRaises(DraftError) as caught:
            store.get("missing")
        self.assertEqual(caught.exception.code, "draft_not_found")


if __name__ == "__main__":
    unittest.main()
