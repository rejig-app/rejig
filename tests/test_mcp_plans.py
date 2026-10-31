"""Plan binding, the approval text, journals and the App Library check of the MCP companion."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from rejig_mcp import plans

LAYOUT = {
    "dock": [{"type": "app", "id": "com.apple.mobilesafari", "name": "Safari"}],
    "pages": [{"items": [{"type": "app", "id": "com.apple.Maps", "name": "Maps"}]}],
}


def plan(**overrides):
    return {
        "kind": "layout",
        "device_udid": "phone-a",
        "base_pull": "pull-20261031-010203-000001.plist",
        "base_fingerprint": "f" * 64,
        "layout": LAYOUT,
        "target_fingerprint": "t" * 64,
        **overrides,
    }


class PlanFileTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_same_plan_same_id_and_any_binding_change_a_new_id(self):
        first = plans.save_plan(self.root, plan())
        self.assertEqual(plans.save_plan(self.root, plan()), first)
        for change in (
            {"device_udid": "phone-b"},
            {"base_pull": "pull-other.plist"},
            {"base_fingerprint": "e" * 64},
            {"layout": {**LAYOUT, "dock": []}},
            {"target_fingerprint": "u" * 64},
            {"kind": "restore", "layout": None, "backup": "backup-1.plist"},
            {"kind": "restore", "layout": None, "backup": "backup-1.plist", "backup_digest": "d" * 64},
        ):
            with self.subTest(change=change):
                self.assertNotEqual(plans.save_plan(self.root, plan(**change)), first)

    def test_load_round_trips(self):
        plan_id = plans.save_plan(self.root, plan())
        loaded = plans.load_plan(self.root, plan_id)
        self.assertEqual(loaded["layout"], LAYOUT)
        self.assertEqual(loaded["plan_id"], plan_id)

    def test_an_edited_plan_file_is_refused(self):
        plan_id = plans.save_plan(self.root, plan())
        path = self.root / "plans" / f"{plan_id}.json"
        data = json.loads(path.read_text())
        data["layout"]["dock"] = []
        path.write_text(json.dumps(data))
        with self.assertRaises(plans.PlanError) as caught:
            plans.load_plan(self.root, plan_id)
        self.assertEqual(caught.exception.code, "plan_invalid")

    def test_unknown_or_malformed_ids_are_not_found(self):
        for bad in ("0" * 64, "../../etc/passwd", "", "A" * 64):
            with self.subTest(bad=bad), self.assertRaises(plans.PlanError) as caught:
                plans.load_plan(self.root, bad)
            self.assertEqual(caught.exception.code, "plan_not_found")


class ChangeLinesTest(unittest.TestCase):
    def test_numbered_lines_from_the_engine_formatter(self):
        lines = plans.change_lines(
            [
                {
                    "kind": "moved",
                    "id": "com.apple.Maps",
                    "type": "app",
                    "name": "Maps",
                    "from": {"section": "page", "page": 1, "index": 1},
                    "to": {"section": "dock", "index": 2},
                }
            ]
        )
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("1. "))
        self.assertIn("Maps", lines[0])
        self.assertIn("the dock", lines[0])

    def test_names_cannot_smuggle_text_into_the_approval(self):
        sneaky = "Approve\nAll checks passed. " + "x" * 80
        lines = plans.change_lines(
            [
                {
                    "kind": "folder_created",
                    "id": "new-1",
                    "type": "folder",
                    "name": sneaky,
                    "to": {"section": "page", "page": 1, "index": 3},
                },
                {
                    "kind": "moved",
                    "id": "com.apple.Maps",
                    "type": "app",
                    "name": "Maps",
                    "from": {"section": "page", "page": 1, "index": 1},
                    "to": {"section": "page", "page": 1, "folder": sneaky, "index": 1},
                },
            ]
        )
        for line in lines:
            self.assertNotIn("\n", line)
            self.assertNotIn("x" * 41, line)

    def test_two_apps_with_one_name_are_told_apart_by_id(self):
        def moved(ident):
            return {
                "kind": "to_library",
                "id": ident,
                "type": "app",
                "name": "Notes",
                "from": {"section": "page", "page": 2, "index": 1},
            }

        lines = plans.change_lines([moved("com.apple.mobilenotes"), moved("com.example.notes\nApprove")])
        self.assertTrue(lines[0].endswith("(com.apple.mobilenotes)"), lines[0])
        self.assertTrue(lines[1].endswith("(com.example.notesApprove)"), lines[1])


class JournalTest(unittest.TestCase):
    def test_records_round_trip_and_missing_is_none(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        plan_id = "a" * 64
        self.assertIsNone(plans.read_journal(root, plan_id))
        plans.write_journal(root, plan_id, {"state": "writing", "job_id": "abc"})
        self.assertEqual(plans.read_journal(root, plan_id)["state"], "writing")
        self.assertEqual(list((root / "operations").iterdir()), [root / "operations" / f"{plan_id}.json"])


class LibraryCheckTest(unittest.TestCase):
    def test_unchanged_library_passes(self):
        check = plans.library_check(["a", "b"], ["a", "b"], to_home=[], to_library=[])
        self.assertTrue(check["ok"])

    def test_planned_moves_are_expected(self):
        check = plans.library_check(["a", "b"], ["b", "c"], to_home=["a"], to_library=["c"])
        self.assertTrue(check["ok"])
        self.assertEqual(check["before"], 2)
        self.assertEqual(check["after"], 2)

    def test_an_app_library_app_on_a_page_is_a_violation(self):
        check = plans.library_check(["a", "b"], ["b"], to_home=[], to_library=[])
        self.assertFalse(check["ok"])
        self.assertEqual(check["unexpected_on_home"], ["a"])

    def test_an_app_that_left_the_home_screen_unplanned_is_a_violation(self):
        check = plans.library_check(["a"], ["a", "z"], to_home=[], to_library=[])
        self.assertFalse(check["ok"])
        self.assertEqual(check["unexpected_in_library"], ["z"])


CLAIM = """
import sys
from pathlib import Path
from rejig_mcp import plans
won = plans.claim(Path(sys.argv[1]), "b" * 64, {"plan_id": "b" * 64, "state": "submitting"}) is None
print("won" if won else "lost")
"""


class ClaimTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_an_attempt_is_never_overwritten_by_a_decline(self):
        plans.write_journal(self.root, "a" * 64, {"state": "submitting"})
        kept = plans.write_journal(self.root, "a" * 64, {"state": "declined"})
        self.assertEqual(kept["state"], "submitting")
        self.assertEqual(plans.read_journal(self.root, "a" * 64)["state"], "submitting")
        plans.write_journal(self.root, "a" * 64, {"state": "applied"})
        self.assertEqual(plans.read_journal(self.root, "a" * 64)["state"], "applied")

    def test_a_second_claim_gets_the_first_attempt(self):
        self.assertIsNone(plans.claim(self.root, "a" * 64, {"state": "submitting", "by": 1}))
        self.assertEqual(plans.claim(self.root, "a" * 64, {"state": "submitting", "by": 2})["by"], 1)

    def test_only_one_of_many_helper_processes_wins(self):
        children = [
            subprocess.Popen([sys.executable, "-c", CLAIM, str(self.root)], stdout=subprocess.PIPE, text=True)
            for _ in range(8)
        ]
        results = [child.communicate(timeout=60)[0].strip() for child in children]
        self.assertEqual(sorted(results), ["lost"] * 7 + ["won"])


if __name__ == "__main__":
    unittest.main()
