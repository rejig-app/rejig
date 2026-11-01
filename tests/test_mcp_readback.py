"""The companion's readback check (server.reads_as_target) against real engine plans.

A configured widget is written with its element id as ``uniqueIdentifier`` (widget-ids.patch),
and on an iPhone 16 Pro (iOS 27.0.1) the next read showed the widget without it, so the read's
fingerprint missed the plan's. The companion then follows the engine's own readback rule: a
missing single-widget id passes, a different one fails, and anything else must still match.
"""

import plistlib
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from ios_homescreen.model import describe
from ios_homescreen.snapshots import make_read
from ios_homescreen.state import fingerprint
from ios_homescreen.storage import PULLS

from rejig_mcp import draft as drafting
from rejig_mcp import plans, server

WIDGET = {
    "bundleIdentifier": "com.apple.weather.widget",
    "containerBundleIdentifier": "com.apple.weather",
    "displayIdentifier": "WIDGET-1",
    "elementType": "widget",
    "gridSize": "small",
    "iconType": "custom",
    "userSelectedElementIdentifier": "CONFIG-1",
    "widgetIdentifier": "com.apple.weather",
}
APPS = {ident: {"name": ident, "type": "User"} for ident in ("com.a", "com.b", "com.c", "com.d")}


def app(ident):
    return {"bundleIdentifier": ident, "displayIdentifier": ident}


class ReadsAsTarget(unittest.TestCase):
    def setUp(self):
        self.data = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.data / PULLS).mkdir()
        self.before = [[app("com.a")], [deepcopy(WIDGET), app("com.b"), app("com.c"), app("com.d")]]
        self.save("pull-before", self.before)
        state = {"data_dir": str(self.data), "read": {"file": "pull-before"}}
        # The plan swaps com.b and com.c, as preview_draft freezes it.
        layout = drafting.from_view(describe(make_read(self.before, APPS)))
        page = layout["pages"][0]
        page[1], page[2] = page[2], page[1]
        self.plan = {"kind": "layout", "base_pull": "pull-before", "base_fingerprint": fingerprint(self.before),
                     "layout": drafting.to_layout(layout), "target_fingerprint": server.target_fingerprint(state, layout)}
        self.state = {"data_dir": str(self.data)}

    def save(self, name, icon_state):
        (self.data / PULLS / name).write_bytes(plistlib.dumps(make_read(icon_state, APPS)))

    def reads(self, icon_state):
        self.save("pull-after", icon_state)
        read = {"file": "pull-after", "view": {"fingerprint": fingerprint(icon_state)}}
        return server.reads_as_target(self.plan, self.state, read)

    def planned(self, **widget):
        return [[app("com.a")], [{**WIDGET, **widget}, app("com.c"), app("com.b"), app("com.d")]]

    def test_the_write_reads_back_exactly(self):
        self.assertTrue(self.reads(self.planned(uniqueIdentifier="CONFIG-1")))

    def test_the_widget_reads_back_without_the_id_it_was_written_with(self):
        self.assertNotEqual(fingerprint(self.planned()), self.plan["target_fingerprint"])
        self.assertTrue(self.reads(self.planned()))

    def test_a_different_widget_id_is_a_mismatch(self):
        self.assertFalse(self.reads(self.planned(uniqueIdentifier="OTHER", userSelectedElementIdentifier="OTHER")))

    def test_another_order_is_a_mismatch(self):
        self.assertFalse(self.reads(self.before))

    def test_a_plan_whose_layout_no_longer_gives_its_fingerprint_is_not_loosened(self):
        self.plan["target_fingerprint"] = "0" * 64
        self.assertFalse(self.reads(self.planned()))

    def test_a_plan_whose_frozen_read_is_gone_stays_exact(self):
        # The editor restarted on fresh data, or the pull was pruned: no crash, no loosening.
        (self.data / PULLS / "pull-before").unlink()
        self.assertFalse(self.reads(self.planned()))
        self.assertTrue(self.reads(self.planned(uniqueIdentifier="CONFIG-1")))

    def test_a_plan_whose_frozen_read_was_changed_stays_exact(self):
        self.save("pull-before", [[app("com.a")], [deepcopy(WIDGET), app("com.c"), app("com.b"), app("com.d")]])
        self.assertFalse(self.reads(self.planned()))

    def test_a_restore_plan_stays_exact(self):
        self.plan["kind"] = "restore"
        self.assertFalse(self.reads(self.planned()))


class LostUnpin(unittest.IsolatedAsyncioTestCase):
    """An unpin-only write leaves the order as it was, and so does a failed one; the copied widget
    id must not make the plan look as if it changed the order."""

    async def outcome(self, phone, source=None):
        """The journal after get_operation reconciles a lost unpin-only write against ``phone``;
        ``source`` replaces the frozen read before that ("gone" deletes it)."""
        support = Path(self.enterContext(tempfile.TemporaryDirectory()))
        data = support / "data"
        (data / PULLS).mkdir(parents=True)
        before = [[app("com.a")], [deepcopy(WIDGET), app("com.b"), app("com.c"), app("com.d")]]
        (data / PULLS / "pull-before").write_bytes(plistlib.dumps(make_read(before, APPS)))
        layout = drafting.from_view(describe(make_read(before, APPS)))  # same order, no cells: clears a spot
        base = {"data_dir": str(data), "read": {"file": "pull-before", "view": {}}}
        plan = {"kind": "layout", "device_udid": "PHONE-A", "base_pull": "pull-before",
                "base_fingerprint": fingerprint(before), "layout": drafting.to_layout(layout),
                "target_fingerprint": server.target_fingerprint(base, layout), "backup": None,
                "backup_digest": None, "approval": "a" * 64, "target_pins": [{}],
                "library_moves": {"to_home": [], "to_library": []}}
        self.assertNotEqual(plan["target_fingerprint"], plan["base_fingerprint"])  # the copied id
        (data / PULLS / "pull-now").write_bytes(plistlib.dumps(make_read(phone, APPS)))
        if source == "gone":
            (data / PULLS / "pull-before").unlink()
        elif source is not None:
            (data / PULLS / "pull-before").write_bytes(plistlib.dumps(make_read(source, APPS)))
        view = {"device": {"udid": "PHONE-A"}, "fingerprint": fingerprint(phone), "library": [],
                "pages": [{"items": layout["pages"][0]}]}
        state = {"data_dir": str(data), "read": {"file": "pull-now", "view": view}}
        stub = mock.Mock(support=support)
        stub.current_job.return_value = None
        stub.request.return_value = {"backups": []}
        self.enterContext(mock.patch.object(server, "editor", stub))
        self.enterContext(mock.patch.object(server, "read_now", mock.AsyncMock(return_value=state)))
        root = support / "mcp"
        plan_id = plans.save_plan(root, plan)
        plans.write_journal(root, plan_id, {"plan_id": plan_id, "state": "writing", "backups_before": [], "library_before": []})
        await server.get_operation(None, plan_id)
        return plan, plans.read_journal(root, plan_id)

    def written(self):
        return [[app("com.a")], [{**WIDGET, "uniqueIdentifier": "CONFIG-1"}, app("com.b"), app("com.c"), app("com.d")]]

    async def test_a_lost_unpin_beside_a_configured_widget_stays_unknown(self):
        before = [[app("com.a")], [deepcopy(WIDGET), app("com.b"), app("com.c"), app("com.d")]]
        _, record = await self.outcome(before)
        self.assertEqual(record["state"], "outcome_unknown", record)
        self.assertTrue(record["phone_matches_base"])
        self.assertTrue(record["matches_plan"])  # the read shows the plan too: that is the ambiguity

    async def test_an_exact_read_of_a_lost_unpin_stays_unknown(self):
        plan, record = await self.outcome(self.written())
        self.assertEqual(fingerprint(self.written()), plan["target_fingerprint"])
        self.assertEqual(record["state"], "outcome_unknown", record)

    async def test_a_lost_unpin_whose_frozen_read_is_gone_or_changed_stays_unknown(self):
        changed = [[app("com.a")], [deepcopy(WIDGET), app("com.c"), app("com.b"), app("com.d")]]
        for source in ("gone", changed):
            with self.subTest(source="gone" if source == "gone" else "changed"):
                _, record = await self.outcome(self.written(), source)
                self.assertEqual(record["state"], "outcome_unknown", record)


if __name__ == "__main__":
    unittest.main()
