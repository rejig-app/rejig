"""Configured widgets keep their settings (patches/widget-ids.patch).

A configured single widget's settings hang off its element id. A read shows that id only
as ``userSelectedElementIdentifier``; a write without it as ``uniqueIdentifier`` makes
SpringBoard save the widget under a fresh id and the settings are lost (homescreen-mcp,
iOS 27.0.1; on the iOS 26.5 simulator a single widget got a new id on every write). In a
stack the same key names the element on show, so stacks are left alone.
"""

import asyncio
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from ios_homescreen.device import Device
from ios_homescreen.model import describe, to_document
from ios_homescreen.planner import plan_layout
from ios_homescreen.snapshots import make_read
from ios_homescreen.state import fingerprint, normalize_for_write, safe_payload, same_layout
from ios_homescreen.workflow import APPLIED, ROLLED_BACK, WorkflowError, apply_state

from tests.test_offloaded_apps import FAST, Installer, Lockdown, Phone, SpringBoard

CONFIGURED = {
    "bundleIdentifier": "dk.simonbs.Scriptable.ScriptableWidget",
    "displayIdentifier": "dk.simonbs.Scriptable.ScriptableWidget",
    "elementType": "widget",
    "gridSize": "small",
    "iconType": "custom",
    "userSelectedElementIdentifier": "ELEMENT-1",
    "widgetIdentifier": "ScriptableWidget",
}
STACK = {
    "displayIdentifier": "STACK-1",
    "elementType": "widget",
    "gridSize": "small",
    "iconType": "custom",
    "userSelectedElementIdentifier": "E2",
    "elements": [
        {"elementType": "widget", "uniqueIdentifier": "E1", "widgetIdentifier": "a"},
        {"elementType": "widget", "uniqueIdentifier": "E2", "widgetIdentifier": "b"},
    ],
}


class NormalizeForWrite(unittest.TestCase):
    def test_a_configured_widget_is_written_with_its_element_id(self):
        written = normalize_for_write(CONFIGURED)
        self.assertEqual(written["uniqueIdentifier"], "ELEMENT-1")
        self.assertEqual(written["userSelectedElementIdentifier"], "ELEMENT-1")

    def test_an_existing_id_is_kept(self):
        widget = {**CONFIGURED, "uniqueIdentifier": "OWN-ID"}
        self.assertEqual(normalize_for_write(widget)["uniqueIdentifier"], "OWN-ID")

    def test_a_stack_and_an_unconfigured_widget_are_unchanged(self):
        plain = {k: v for k, v in CONFIGURED.items() if k != "userSelectedElementIdentifier"}
        self.assertEqual(normalize_for_write(STACK), STACK)
        self.assertEqual(normalize_for_write(plain), plain)
        self.assertEqual(normalize_for_write({**CONFIGURED, "userSelectedElementIdentifier": ""}),
                         {**CONFIGURED, "userSelectedElementIdentifier": ""})

    def test_the_payload_carries_the_id(self):
        payload = safe_payload([[], [CONFIGURED]], {})
        self.assertEqual(payload["iconLists"][0][0]["uniqueIdentifier"], "ELEMENT-1")


class Readback(unittest.TestCase):
    def test_a_single_widgets_id_does_not_decide_readback(self):
        sent = [[], [normalize_for_write(CONFIGURED)]]
        self.assertTrue(same_layout([[], [CONFIGURED]], sent))
        dropped = {k: v for k, v in CONFIGURED.items() if k != "userSelectedElementIdentifier"}
        self.assertTrue(same_layout([[], [dropped]], sent))

    def test_a_different_widget_id_read_back_fails_readback(self):
        sent = [[], [normalize_for_write(CONFIGURED)]]
        self.assertFalse(same_layout([[], [{**CONFIGURED, "uniqueIdentifier": "WRONG-ID",
                                             "userSelectedElementIdentifier": "WRONG-ID"}]], sent))

    def test_the_fingerprint_keeps_the_exact_id_a_read_shows(self):
        before = [[], [{**CONFIGURED, "uniqueIdentifier": "ORIGINAL-ID"}]]
        after = [[], [{**CONFIGURED, "uniqueIdentifier": "NEW-ID"}]]
        self.assertNotEqual(fingerprint(before), fingerprint(after))

    def test_a_stack_element_id_still_decides_readback(self):
        changed = deepcopy(STACK)
        changed["elements"][0]["uniqueIdentifier"] = "OTHER"
        self.assertFalse(same_layout([[], [changed]], [[], [STACK]]))


class Apply(unittest.TestCase):
    def setUp(self):
        self.phone = phone = Phone()
        phone.state[1].insert(0, deepcopy(CONFIGURED))
        self.device = Device(Lockdown(), springboard_factory=lambda _: SpringBoard(phone),
                             installer_factory=lambda _: Installer(phone))
        self.backups = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def apply(self, target):
        return asyncio.run(apply_state(self.device, target, backup_dir=self.backups, timing=FAST))

    def target(self):
        return {"buttonBar": deepcopy(self.phone.state[0]), "iconLists": [self.phone.state[1][::-1]],
                "ignored": []}

    def test_a_phone_that_reads_back_another_widget_id_gets_rolled_back(self):
        original = self.phone.set_icon_state

        def new_id(payload):  # the first write comes back under another id; the rollback does not
            original(payload)
            self.phone.set_icon_state = original
            for item in self.phone.state[1]:
                if item.get("elementType") == "widget":
                    item["uniqueIdentifier"] = "WRONG-ID"

        self.phone.set_icon_state = new_id
        self.assertEqual(self.apply(self.target()).status, ROLLED_BACK)

    def test_a_plan_made_before_the_widget_id_changed_is_refused(self):
        self.phone.state[1][0]["uniqueIdentifier"] = "ORIGINAL-ID"
        state = asyncio.run(self.device.read_state())
        read = make_read(state, asyncio.run(self.device.installed_apps()), device=self.device.info)
        plan = plan_layout(to_document(describe(read)), read)
        self.phone.state[1][0]["uniqueIdentifier"] = "NEW-ID-AFTER-PLANNING"
        with self.assertRaises(WorkflowError):
            self.apply(plan.payload)
        self.assertEqual(self.phone.writes, [])

    def test_a_phone_that_reads_the_id_back_only_as_the_selected_element_applies(self):
        phone = self.phone
        original = phone.set_icon_state

        def reads_without_unique_id(payload):  # what a read shows: no uniqueIdentifier
            original(payload)
            for page in phone.state[1:]:
                for item in page:
                    if isinstance(item, dict) and item.get("elementType") == "widget":
                        item.pop("uniqueIdentifier", None)

        phone.set_icon_state = reads_without_unique_id
        self.assertEqual(self.apply(self.target()).status, APPLIED)
        [widget] = [x for x in phone.writes[-1]["iconLists"][0] if x.get("elementType") == "widget"]
        self.assertEqual(widget["uniqueIdentifier"], "ELEMENT-1")


if __name__ == "__main__":
    unittest.main()
