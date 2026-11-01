"""Gap pins (patches/pinned-gaps.patch): items placed at a grid cell with gaps around them.

SpringBoard takes pins in setIconState as listUniqueIdentifiers + listMetadata and reads a
pinned page back in screen order (iOS 26.5 simulator), so a pinned page
must be written in that order or readback rolls the write back.
"""

import asyncio
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from ios_homescreen.device import Device
from ios_homescreen.model import describe, to_document
from ios_homescreen.planner import LayoutError, plan_layout
from ios_homescreen.snapshots import make_read
from ios_homescreen.state import (
    PAYLOAD_KEYS,
    PINS_KEY,
    StateError,
    fingerprint,
    place_page,
    safe_payload,
    validate_plan,
    validate_write_payload,
    wire_payload,
)
from ios_homescreen.workflow import APPLIED, ROLLED_BACK, apply_state

from tests.test_offloaded_apps import FAST, Installer, Lockdown, Phone, SpringBoard


def app(ident):
    return {"bundleIdentifier": ident, "displayIdentifier": ident, "displayName": ident}


WIDGET = {
    "bundleIdentifier": "com.example.widget",
    "elementType": "widget",
    "gridSize": "small",
    "iconType": "custom",
    "uniqueIdentifier": "W1",
    "widgetIdentifier": "w",
}
FOLDER = {"displayName": "F", "listType": "folder", "iconLists": [[app("com.example.f1")]]}
IDS = ["com.example.a", "com.example.b", "com.example.c", "com.example.d"]
APPS = {i: {"CFBundleDisplayName": i, "ApplicationType": "User"} for i in [*IDS, "com.example.f1"]}


def ids(items):
    return [x.get("bundleIdentifier") or x.get("displayName") for x in items]


class PlacePage(unittest.TestCase):
    def test_pinned_item_takes_its_cell_and_the_page_comes_back_in_screen_order(self):
        page = [app(i) for i in IDS]
        placed = place_page(page, {"com.example.a": 23})
        self.assertEqual([c for c, _ in placed], [0, 1, 2, 23])
        self.assertEqual(ids(x for _, x in placed), IDS[1:] + IDS[:1])

    def test_unpinned_items_flow_around_pins_and_widgets(self):
        page = [WIDGET, app("com.example.a"), app("com.example.b")]
        placed = place_page(page, {"com.example.b": 2})
        # widget covers 0,1,4,5; b is pinned at 2; a takes the next free cell, 3.
        self.assertEqual([(c, x.get("bundleIdentifier")) for c, x in placed],
                         [(0, "com.example.widget"), (2, "com.example.b"), (3, "com.example.a")])

    def test_bad_pins_are_refused(self):
        page = [app(i) for i in IDS] + [WIDGET, FOLDER]
        for pins in ({"com.example.a": 24}, {"com.example.a": -1}, {"com.example.a": True},
                     {"com.example.a": "3"}, {"com.example.a": 5, "com.example.b": 5},
                     {"com.example.zzz": 1}, {"com.example.widget": 8}, {"com.example.f1": 8}):
            with self.subTest(pins=pins), self.assertRaises(StateError) as ctx:
                place_page(page, pins)
            self.assertEqual(ctx.exception.code, "invalid_pins")

    def test_a_page_that_no_longer_fits_around_its_pins_is_refused(self):
        page = [app(f"com.example.n{i}") for i in range(23)] + [{**WIDGET}]
        with self.assertRaises(StateError):
            place_page(page, {})


class Payload(unittest.TestCase):
    def state(self, pins):
        page1 = [app(i) for i in IDS]
        page2 = [app("com.example.f1")]
        out = {"buttonBar": [], "iconLists": [page1, page2], "ignored": []}
        if pins is not None:
            out[PINS_KEY] = pins
        return out

    def test_only_pinned_pages_are_reordered_and_fingerprints_ignore_pins(self):
        payload = safe_payload(self.state([{"com.example.a": 23}, {}]), APPS)
        self.assertEqual(ids(payload["iconLists"][0]), IDS[1:] + IDS[:1])
        self.assertEqual(ids(payload["iconLists"][1]), ["com.example.f1"])
        self.assertEqual(payload[PINS_KEY], [{"com.example.a": 23}, {}])
        unpinned = deepcopy(payload)
        del unpinned[PINS_KEY]
        self.assertEqual(fingerprint(payload), fingerprint(unpinned))

    def test_no_pins_leaves_the_payload_exactly_as_before(self):
        self.assertEqual(safe_payload(self.state(None), APPS), safe_payload(self.state([{}, {}]), APPS))
        self.assertNotIn(PINS_KEY, safe_payload(self.state([{}, {}]), APPS))
        self.assertEqual(set(wire_payload(safe_payload(self.state(None), APPS))), PAYLOAD_KEYS)

    def test_wire_payload_sends_page_ids_and_fixed_locations_and_passes_the_write_gate(self):
        wire = wire_payload(safe_payload(self.state([{"com.example.a": 23}, {}]), APPS))
        page_ids = wire["listUniqueIdentifiers"]
        self.assertEqual(len(page_ids), 2)
        self.assertEqual(wire["listMetadata"], {page_ids[0]: {
            "fixedLocations": {"com.example.a": 23},
            "fixedLocationsGridColumns": 4,
            "fixedLocationsGridRows": 6,
        }})
        self.assertNotIn(PINS_KEY, wire)
        validate_write_payload(wire)

    def test_write_gate_refuses_a_pinned_page_in_list_order_or_unknown_page_ids(self):
        wire = wire_payload(safe_payload(self.state([{"com.example.a": 23}, {}]), APPS))
        wrong_order = deepcopy(wire)
        wrong_order["iconLists"][0] = [app(i) for i in IDS]
        stray = deepcopy(wire)
        stray["listMetadata"]["NOT-A-PAGE"] = stray["listMetadata"].popitem()[1]
        half = deepcopy(wire)
        del half["listMetadata"]
        for bad in (wrong_order, stray, half):
            with self.subTest(), self.assertRaises(StateError):
                validate_write_payload(bad)

    def test_write_gate_refuses_other_grids_extra_fields_and_odd_page_ids(self):
        wire = wire_payload(safe_payload(self.state([{"com.example.a": 23}, {}]), APPS))
        page_id = wire["listUniqueIdentifiers"][0]

        def mutate(change):
            bad = deepcopy(wire)
            change(bad, bad["listMetadata"][page_id])
            return bad

        cases = [
            mutate(lambda w, m: m.update(fixedLocationsGridColumns=1)),
            mutate(lambda w, m: m.update(fixedLocationsGridRows=1)),
            mutate(lambda w, m: m.update(fixedLocationsGridRows=True)),
            mutate(lambda w, m: m.update(rotatedFixedLocations={"com.example.a": 999})),
            mutate(lambda w, m: m.update(fixedLocations={})),
            mutate(lambda w, m: w["listUniqueIdentifiers"].__setitem__(1, 7)),
            mutate(lambda w, m: w["listUniqueIdentifiers"].__setitem__(1, "")),
        ]
        for bad in cases:
            with self.subTest(bad=bad["listMetadata"]), self.assertRaises(StateError):
                validate_write_payload(bad)


class Planner(unittest.TestCase):
    def read(self):
        state = [[], [app(i) for i in IDS]]
        return make_read(state, APPS, device={"UniqueDeviceID": "u", "ProductVersion": "26.7.1"})

    def test_a_page_pin_reaches_the_plan_in_screen_order(self):
        layout = {"dock": {"items": []}, "pages": [{"items": IDS, "pins": {"com.example.a": 23}}]}
        plan = plan_layout(layout, self.read())
        self.assertEqual(plan.payload[PINS_KEY], [{"com.example.a": 23}])
        self.assertEqual(ids(plan.payload["iconLists"][0]), IDS[1:] + IDS[:1])
        validate_plan(plan.payload)

    def test_a_pinless_page_with_widgets_plans_as_before(self):
        large = {**WIDGET, "gridSize": "large", "uniqueIdentifier": "W2", "bundleIdentifier": "com.example.big"}
        state = [[], [app("com.example.a"), large, WIDGET]]  # 21 cells, greedy flow can't fit it
        read = make_read(state, APPS, device={"UniqueDeviceID": "u", "ProductVersion": "26.7.1"})
        plan = plan_layout(to_document(describe(read)), read)
        self.assertNotIn(PINS_KEY, plan.payload)
        self.assertEqual(len(plan.payload["iconLists"][0]), 3)

    def test_pins_outside_pages_or_of_the_wrong_type_are_refused(self):
        dock_pins = {"dock": {"items": ["com.example.a"], "pins": {"com.example.a": 0}},
                     "pages": [{"items": IDS[1:]}]}
        with self.assertRaises(LayoutError):
            plan_layout(dock_pins, self.read())
        for bad in (False, [], "x", {1: 2}):
            layout = {"dock": {"items": []}, "pages": [{"items": IDS, "pins": bad}]}
            with self.subTest(pins=bad), self.assertRaises(LayoutError) as ctx:
                plan_layout(layout, self.read())
            self.assertIn("invalid_pins", [i.code for i in ctx.exception.issues])
            view = {"dock": [], "pages": [{"items": IDS, "pins": bad}]}
            with self.subTest(view=bad), self.assertRaises(LayoutError):
                plan_layout(to_document(view), self.read())

    def test_an_invalid_pin_is_a_layout_issue(self):
        layout = {"dock": {"items": []}, "pages": [{"items": IDS, "pins": {"com.example.a": 30}}]}
        with self.assertRaises(LayoutError) as ctx:
            plan_layout(layout, self.read())
        self.assertIn("invalid_pins", [issue.code for issue in ctx.exception.issues])


class Apply(unittest.TestCase):
    def setUp(self):
        self.phone = Phone()
        self.device = Device(
            Lockdown(),
            springboard_factory=lambda _: SpringBoard(self.phone),
            installer_factory=lambda _: Installer(self.phone),
        )
        self.backups = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def plan(self):
        state = asyncio.run(self.device.read_state())
        read = make_read(state, asyncio.run(self.device.installed_apps()), device=self.device.info)
        layout = {"dock": {"items": ["com.example.dock"]},
                  "pages": [{"items": ["com.example.one", "com.example.two", "com.example.offpage"],
                             "pins": {"com.example.one": 23}}]}
        return plan_layout(layout, read)

    def test_a_pinned_write_sends_fixed_locations_and_warns_the_spot_is_unchecked(self):
        result = asyncio.run(apply_state(self.device, self.plan().payload, backup_dir=self.backups, timing=FAST))
        self.assertEqual(result.status, APPLIED)
        sent = self.phone.writes[-1]
        [locations] = [m["fixedLocations"] for m in sent["listMetadata"].values()]
        self.assertEqual(locations, {"com.example.one": 23})
        self.assertNotIn(PINS_KEY, sent)
        self.assertIn("pins_unverified", [e.code for e in result.events])

    def test_a_phone_that_reads_back_another_order_gets_rolled_back(self):
        original = self.phone.set_icon_state

        def list_order(payload):  # pretend SpringBoard kept list order, not screen order
            payload = deepcopy(payload)
            if "listMetadata" in payload:
                payload["iconLists"][0].sort(key=lambda x: x.get("displayIdentifier") != "com.example.one")
            original(payload)

        self.phone.set_icon_state = list_order
        before = deepcopy(self.phone.state)
        result = asyncio.run(apply_state(self.device, self.plan().payload, backup_dir=self.backups, timing=FAST))
        self.assertEqual(result.status, ROLLED_BACK)
        self.assertEqual(self.phone.state, before)


if __name__ == "__main__":
    unittest.main()
