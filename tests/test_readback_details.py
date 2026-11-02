"""A failed Apply keeps its evidence even after a successful rollback."""

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from ios_homescreen import workflow
from ios_homescreen.planner import plan_layout
from ios_homescreen.snapshots import make_read
from ios_homescreen.state import load_plist, same_layout, split_state
from ios_homescreen.workflow import APPLIED, ROLLED_BACK, Timing, apply_state

APPS = {"com.a": {"name": "Already here", "type": "User"},
        "com.b": {"name": "Library App", "type": "User", "offloaded": True}}
FAST = Timing(read_timeout=1, write_timeout=1, verify_attempts=2, verify_interval=0)


class Phone:
    def __init__(self, reject=True, unreadable=False, reorder=False):
        self.state = [[], ["com.a"]]
        self.writes = 0
        self.reject, self.unreadable, self.reorder = reject, unreadable, reorder

    async def read_state(self):
        if self.unreadable and self.writes == 1:
            raise RuntimeError("Read connection closed")
        return deepcopy(self.state)

    async def installed_apps(self):
        return APPS

    async def write_state(self, payload):
        self.writes += 1
        dock, pages = split_state(deepcopy(payload))
        self.state = [dock, *pages]
        if self.writes == 1:
            if self.reject:
                self.state = [[], ["com.a"]]
            elif self.reorder:
                self.state[1].reverse()


class ReadbackDetails(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.plan = plan_layout({"dock": {"items": []}, "pages": [{"items": ["com.a", "com.b"]}]},
                                make_read([[], ["com.a"]], APPS)).payload

    async def apply(self, phone):
        return await apply_state(phone, self.plan, backup_dir=self.root, timing=FAST)

    async def test_missing_library_app_is_named_and_failed_reads_survive_rollback(self):
        phone = Phone()
        result = await self.apply(phone)
        self.assertEqual(result.status, ROLLED_BACK)
        self.assertEqual(phone.writes, 2)
        event = next(e for e in result.events if e.code == "readback_mismatch")
        self.assertIn("Library App", event.message)
        [path] = (self.root / "diagnostics").glob("*.plist")
        evidence = load_plist(path)
        self.assertTrue(same_layout(evidence["target"], self.plan))
        self.assertEqual(len(evidence["reads"]), 2)
        self.assertFalse(same_layout(evidence["reads"][-1]["state"], self.plan))
        self.assertEqual(phone.state, [[], ["com.a"]])

    async def test_read_failures_are_distinguished_from_missing_apps(self):
        result = await self.apply(Phone(unreadable=True))
        self.assertEqual(result.status, ROLLED_BACK)
        event = next(e for e in result.events if e.code == "readback_mismatch")
        self.assertIn("could not be read", event.message)
        [path] = (self.root / "diagnostics").glob("*.plist")
        self.assertEqual(load_plist(path)["reads"], [{"error": "Read connection closed"}] * 2)

    async def test_order_mismatch_is_not_misreported_as_missing_apps(self):
        result = await self.apply(Phone(reject=False, reorder=True))
        self.assertEqual(result.status, ROLLED_BACK)
        event = next(e for e in result.events if e.code == "readback_mismatch")
        self.assertIn("order", event.message)
        self.assertNotIn("Missing", event.message)

    async def test_diagnostic_disk_failure_cannot_stop_rollback(self):
        original = workflow.save_plist

        def save(path, data):
            if "diagnostics" in Path(path).parts:
                raise OSError("disk full")
            return original(path, data)

        with mock.patch.object(workflow, "save_plist", side_effect=save):
            result = await self.apply(Phone())
        self.assertEqual(result.status, ROLLED_BACK)
        self.assertTrue(result.rollback_verified)
        self.assertIn("readback_save_failed", [e.code for e in result.events])

    async def test_success_does_not_create_failure_diagnostics(self):
        result = await self.apply(Phone(reject=False))
        self.assertEqual(result.status, APPLIED)
        self.assertFalse((self.root / "diagnostics").exists())


if __name__ == "__main__":
    unittest.main()
