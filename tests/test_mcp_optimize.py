"""The usage optimizer: most-used apps first, honest about what the usage covers."""

import unittest

from rejig_mcp import draft, optimize
from rejig_mcp.draft import DraftError


def app(ident, name):
    return {"type": "app", "id": ident, "name": name, "cells": 1}


def layout():
    return draft.from_view(
        {
            "dock": [app("safari", "Safari")],
            "pages": [
                {"items": [app("maps", "Maps"), {"type": "widget", "id": "w1", "name": "W", "cells": 4}]},
                {
                    "items": [
                        app("notes", "Notes"),
                        {
                            "type": "folder",
                            "id": "folder-1",
                            "name": "Social",
                            "cells": 1,
                            "pages": [[app("chat", "Chat"), app("wallet1", "Wallet")]],
                        },
                        app("wallet2", "Wallet"),
                    ]
                },
            ],
        }
    )


APPS = {
    "safari": "Safari",
    "maps": "Maps",
    "notes": "Notes",
    "chat": "Chat",
    "wallet1": "Wallet",
    "wallet2": "Wallet",
    "chess": "Chess",
}
LIBRARY = ["chess"]


def run(usage, **options):
    return optimize.optimize(layout(), usage, APPS, LIBRARY, options)


class OptimizeTest(unittest.TestCase):
    def test_most_used_apps_go_to_page_one_and_out_of_folders(self):
        result = run([{"app": "chat", "minutes": 300}, {"app": "Notes", "minutes": 120}])
        first = [item["id"] for item in result["layout"]["pages"][0]]
        self.assertEqual(first[:2], ["chat", "notes"])
        self.assertEqual(result["layout"]["dock"][0]["id"], "safari")
        folder = next(i for p in result["layout"]["pages"] for i in p if i["type"] == "folder")
        self.assertEqual([x["id"] for x in folder["pages"][0]], ["wallet1"])

    def test_everything_else_keeps_its_order(self):
        result = run([{"app": "notes", "minutes": 5}])
        rest = [i["id"] for p in result["layout"]["pages"] for i in p][1:]
        self.assertEqual(rest, ["maps", "w1", "folder-1", "wallet2"])

    def test_coverage_is_honest(self):
        result = run(
            [
                {"app": "Wallet", "minutes": 9},
                {"app": "Unknown App", "minutes": 3},
                {"app": "Chess", "minutes": 30},
                {"app": "maps", "minutes": 1},
            ]
        )
        coverage = result["coverage"]
        self.assertEqual(coverage["matched"], ["maps"])
        self.assertEqual(coverage["ambiguous"], [{"row": "Wallet", "candidates": ["wallet1", "wallet2"]}])
        self.assertEqual(coverage["unmatched_rows"], ["Unknown App"])
        self.assertEqual(coverage["library_only"], ["chess"])
        self.assertEqual(sorted(coverage["apps_without_data"]), ["chat", "notes", "safari", "wallet1", "wallet2"])

    def test_app_library_apps_stay_there(self):
        result = run([{"app": "chess", "minutes": 999}])
        self.assertNotIn("chess", draft.shown_apps(result["layout"]))

    def test_swipe_estimate(self):
        result = run([{"app": "chat", "minutes": 100}, {"app": "maps", "minutes": 100}])
        # Before: Chat page 2 in a folder (1 swipe + 1 tap), Maps page 1 (0). After: both on page 1.
        self.assertEqual(result["swipe_estimate"], {"before": 1.0, "after": 0.0})

    def test_bad_usage_is_refused(self):
        for usage in ([{"app": "maps"}], [{"app": "", "minutes": 1}], [{"app": "maps", "minutes": -1}], "x"):
            with self.subTest(usage=usage), self.assertRaises(DraftError) as caught:
                run(usage)
            self.assertEqual(caught.exception.code, "usage_invalid")

    def test_no_positive_matched_usage_gives_no_estimate(self):
        for usage in ([], [{"app": "Unknown App", "minutes": 5}], [{"app": "maps", "minutes": 0}]):
            with self.subTest(usage=usage):
                result = run(usage)
                self.assertEqual(result["swipe_estimate"], {"before": None, "after": None})
                self.assertEqual(result["layout"]["dock"], layout()["dock"])

    def test_apps_in_a_dock_folder_stay_in_the_dock(self):
        start = layout()
        start["dock"].append(
            {"type": "folder", "id": "folder-2", "name": "Tools", "cells": 1, "pages": [[app("calc", "Calc")]]}
        )
        apps = {**APPS, "calc": "Calc"}
        result = optimize.optimize(start, [{"app": "calc", "minutes": 50}, {"app": "notes", "minutes": 5}], apps, LIBRARY)
        self.assertEqual(result["layout"]["dock"], start["dock"])
        self.assertEqual(result["layout"]["pages"][0][0]["id"], "notes")

    def test_never_removes_anything(self):
        before = draft.all_items(layout())
        result = run([{"app": "chat", "minutes": 1}, {"app": "wallet2", "minutes": 2}])
        self.assertEqual(
            sorted(i["id"] for i in draft.all_items(result["layout"])), sorted(i["id"] for i in before)
        )


if __name__ == "__main__":
    unittest.main()
