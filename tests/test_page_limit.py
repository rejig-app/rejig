"""Reject page 16 before writing instead of letting SpringBoard truncate and roll back."""

import unittest
import tempfile
from copy import deepcopy
from pathlib import Path

from ios_homescreen.planner import LayoutError, plan_layout
from ios_homescreen.snapshots import make_read
from ios_homescreen.state import StateError, validate_plan, validate_write_payload
from ios_homescreen.ui.demo import DemoDevice
from ios_homescreen.ui.session import LayoutProblems, Session, Settings

from rejig_mcp import draft
from tests.test_offloaded_apps import FAST


IDS = [f"com.example.app{i}" for i in range(40)]
APPS = {ident: {"name": ident, "type": "User"} for ident in IDS}
READ = make_read([[], *[[ident] for ident in IDS[:14]], IDS[14:16]], APPS)


def layout(count):
    return {"dock": {"items": []}, "pages": [{"items": [ident]} for ident in IDS[:count]]}


class PageLimit(unittest.TestCase):
    def test_library_apps_can_fill_the_existing_fifteenth_page(self):
        target = layout(14)
        target["pages"].append({"items": IDS[14:38]})
        plan = plan_layout(target, READ)
        self.assertEqual(len(plan.payload["iconLists"]), 15)
        self.assertEqual(len(plan.payload["iconLists"][-1]), 24)
        self.assertEqual(set(plan.to_home_screen), set(IDS[16:38]))
        validate_plan(plan.payload)

    def test_sixteenth_and_twentieth_pages_are_refused_by_the_planner(self):
        for count in (16, 20):
            with self.subTest(count=count), self.assertRaises(LayoutError) as caught:
                plan_layout(layout(count), READ)
            issue = caught.exception.issues[0]
            self.assertEqual(issue.code, "too_many_pages")
            self.assertEqual(issue.details, {"count": count, "capacity": 15})

    def test_imported_plan_and_direct_device_write_cannot_bypass_the_limit(self):
        payload = plan_layout(layout(15), READ).payload
        payload["iconLists"].append([IDS[15]])
        with self.assertRaises(StateError) as caught:
            validate_plan(payload)
        self.assertEqual(caught.exception.code, "too_many_pages")
        wire = {key: payload[key] for key in ("buttonBar", "iconLists", "ignored")}
        with self.assertRaises(StateError) as caught:
            validate_write_payload(wire)
        self.assertEqual(caught.exception.code, "too_many_pages")

    def test_folders_can_hold_library_apps_without_another_home_screen_page(self):
        target = layout(15)
        target["pages"][-1]["items"] = [{"type": "folder", "name": "More",
                                          "pages": [IDS[i:i + 9] for i in range(14, 40, 9)]}]
        self.assertEqual(len(plan_layout(target, READ).payload["iconLists"]), 15)

    def test_companion_page_add_and_move_to_new_page_are_atomic_at_the_limit(self):
        source = {"dock": [], "pages": [[{"type": "app", "id": ident, "name": ident}]
                                          for ident in IDS[:15]]}
        original = deepcopy(source)
        for operation in ({"op": "page_add"},
                          {"op": "move", "item": IDS[15], "to": {"section": "page", "page": 16}}):
            with self.subTest(operation=operation), self.assertRaises(draft.DraftError) as caught:
                draft.apply_operations(source, [operation], {ident: ident for ident in IDS})
            self.assertEqual(caught.exception.code, "too_many_pages")
            self.assertEqual(source, original)


class EditorPageLimit(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.phone = DemoDevice(delay=0)
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))

        async def opener(emit):
            return self.phone

        self.session = Session(Settings(data_dir=root / "data", backup_dir=root / "backups",
                                        cache_root=root / "cache", timing=FAST), opener=opener)
        self.addAsyncCleanup(self.session.shutdown)
        await self.session.start_read().task

    def layout(self):
        view = self.session.view
        return {"dock": view["dock"], "pages": [{"items": p["items"]} for p in view["pages"]]}

    async def test_over_limit_apply_is_refused_before_the_first_write(self):
        # Use only known apps: split the demo's existing items over too many pages.
        target = self.layout()
        items = [item for page in target["pages"] for item in page["items"]]
        self.assertGreaterEqual(len(items), 16)
        target["pages"] = [{"items": [item]} for item in items[:15]] + [{"items": items[15:]}]
        check = self.session.check(target)
        self.assertFalse(check["ok"])
        self.assertEqual(check["issues"][0]["code"], "too_many_pages")
        with self.assertRaises(LayoutProblems):
            self.session.start_apply(target)
        self.assertEqual(self.phone.writes, 0)

    async def test_empty_draft_pages_do_not_count_toward_the_write_limit(self):
        target = self.layout()
        target["pages"].extend({"items": []} for _ in range(20))
        self.assertTrue(self.session.check(target)["ok"])


if __name__ == "__main__":
    unittest.main()
