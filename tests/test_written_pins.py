"""Gaps Rejig placed (patches/written-pins.patch): remembered per phone, re-sent, restored.

A read never shows pins, so Rejig keeps the pins it wrote, bound to each page as the
verifying read showed it, and puts them back on a later read only while that page still
reads back the same.
"""

import asyncio
import json
import shutil
import subprocess
import tempfile
import unittest
import unittest.mock
from copy import deepcopy
from pathlib import Path

import ios_homescreen
from ios_homescreen import api
from ios_homescreen.device import Device
from ios_homescreen.model import describe_changes
from ios_homescreen.planner import plan_layout
from ios_homescreen.report import change_lines, change_summary
from ios_homescreen.snapshots import make_backup, make_read
from ios_homescreen.state import (
    StateError,
    load_plist,
    page_key,
    pin_record,
    place_page,
    reconcile_pins,
    save_plist,
    validate_backup,
)
from ios_homescreen.ui.demo import DemoDevice
from ios_homescreen.ui.session import PINS_FILE, PinStoreError, PlanChanged, ReadChanged, Session, Settings
from ios_homescreen.workflow import APPLIED, ROLLED_BACK, Timing, apply_state

from tests.test_offloaded_apps import FAST, Installer, Lockdown, Phone, SpringBoard, app

ONE, TWO, OFF = "com.example.one", "com.example.two", "com.example.offpage"


def page_ids(page):
    return [x.get("displayIdentifier") for x in page]


class Reconcile(unittest.TestCase):
    page = [app(TWO), app(ONE)]  # screen order with ONE pinned at 23

    def record(self):
        return pin_record([[], self.page], [{ONE: 23}])

    def test_an_unchanged_page_gets_its_pins_back(self):
        self.assertEqual(reconcile_pins(self.record(), [[], deepcopy(self.page)]), [{ONE: 23}])

    def test_a_page_edited_on_the_phone_loses_them(self):
        self.assertIsNone(reconcile_pins(self.record(), [[], [app(ONE), app(TWO)]]))
        self.assertIsNone(reconcile_pins(self.record(), [[], [app(TWO)]]))

    def test_pins_that_no_longer_give_the_read_order_are_dropped(self):
        record = [{"key": page_key(self.page), "pins": {TWO: 23}}]  # TWO at 23 would read last
        self.assertIsNone(reconcile_pins(record, [[], self.page]))

    def test_two_entries_for_one_page_are_ambiguous(self):
        record = [*self.record(), {"key": page_key(self.page), "pins": {ONE: 22}}]
        self.assertIsNone(reconcile_pins(record, [[], self.page]))

    def test_other_pages_stay_and_no_record_means_none(self):
        record = self.record()
        self.assertEqual(reconcile_pins(record, [[], [app(OFF)], self.page]), [{}, {ONE: 23}])
        self.assertIsNone(reconcile_pins(None, [[], self.page]))
        self.assertEqual(pin_record([[], self.page], [{}]), [])


def widget(ident, size):
    return {"displayIdentifier": ident, "bundleIdentifier": ident, "elementType": "widget",
            "gridSize": size, "iconType": "custom", "widgetIdentifier": "w"}


class EditorPlacement(unittest.TestCase):
    """The editor draws a pinned page where the server's place_page writes it."""

    CASES = [
        (["a", "b", "c", "d"], {"a": 23}),
        (["a", ("w", "small"), "b", "c"], {"b": 1}),
        ([("w", "medium"), "a", "b"], {"a": 0}),
        (["a", "b", "c", "d", "e", "f"], {"b": 5, "e": 10}),
        ([("w", "large"), "a", "b", ("v", "small")], {"b": 20}),
    ]

    def test_layout_js_packs_pinned_pages_like_place_page(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        js_cases, expected = [], []
        for spec, pins in self.CASES:
            page = [widget(x[0], x[1]) if isinstance(x, tuple) else app(x) for x in spec]
            expected.append({item["displayIdentifier"]: cell for cell, item in place_page(page, pins)})
            js_cases.append([
                {"id": x[0], "type": "widget", "size": x[1]} if isinstance(x, tuple)
                else {"id": x, "type": "app", **({"cell": pins[x]} if x in pins else {})}
                for x in spec
            ])
        layout_js = Path(ios_homescreen.__file__).parent / "ui" / "static" / "js" / "layout.js"
        script = ("const L = await import(process.argv[1]);"
                  "const cases = JSON.parse(process.argv[2]);"
                  "console.log(JSON.stringify(cases.map((items) => Object.fromEntries("
                  "L.pack(items).map((p, i) => [items[i].id, p.row * 4 + p.col])))));")
        done = subprocess.run([node, "--input-type=module", "-e", script, layout_js.as_uri(), json.dumps(js_cases)],
                              capture_output=True, text=True, timeout=30, check=True)
        self.assertEqual(json.loads(done.stdout), expected)


class Backups(unittest.TestCase):
    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_a_backup_keeps_pins_beside_the_state_and_restore_reads_them(self):
        state = [[app("com.example.dock")], [app(TWO), app(ONE)]]
        backup = make_backup(state, device={"udid": "u"}, pins=[{ONE: 23}])
        self.assertEqual(backup["pins"], [{ONE: 23}])
        self.assertIs(backup["state"], state)
        path = save_plist(self.dir / "backup-1.plist", backup)
        target = api.load_backup(path)
        self.assertEqual(target["pins"], [{ONE: 23}])
        self.assertEqual(page_ids(target["iconLists"][0]), [TWO, ONE])

    def test_backups_without_pins_load_exactly_as_before(self):
        state = [[app("com.example.dock")], [app(ONE)]]
        self.assertNotIn("pins", make_backup(state, pins=None))
        self.assertNotIn("pins", make_backup(state, pins=[{}]))
        path = save_plist(self.dir / "backup-2.plist", make_backup(state))
        self.assertEqual(set(api.load_backup(path)), {"buttonBar", "iconLists", "ignored"})

    def test_bad_backup_pins_are_refused(self):
        state = {"buttonBar": [], "iconLists": [[app(ONE)]], "ignored": []}
        for pins in ([{"com.example.nope": 3}], [{ONE: 24}], [{ONE: 1}, {}], "x"):
            with self.subTest(pins=pins), self.assertRaises(StateError):
                validate_backup({**state, "pins": pins})
        validate_backup({**state, "pins": [{ONE: 5}]})

    def test_a_backup_with_empty_or_false_pins_is_refused_not_read_as_none(self):
        state = [[app("com.example.dock")], [app(ONE)]]
        for i, pins in enumerate((False, [], {}, "")):
            path = save_plist(self.dir / f"bad-{i}.plist", {**make_backup(state), "pins": pins})
            with self.subTest(pins=pins), self.assertRaises(StateError):
                api.load_backup(path)

    def test_the_mcp_companion_digests_a_pinned_backup_like_the_editor(self):
        from ios_homescreen.ui.session import backup_digest
        from rejig_mcp.plans import backup_digest_of

        state = [[app("com.example.dock")], [app(TWO), app(ONE)]]
        path = save_plist(self.dir / "backup-3.plist", make_backup(state, pins=[{ONE: 23}]))
        self.assertEqual(backup_digest_of(path), backup_digest(api.load_backup(path)))


class Changes(unittest.TestCase):
    def view(self, pins=None, order=(ONE, TWO)):
        page = {"items": [{"id": i, "type": "app", "name": i.split(".")[-1]} for i in order]}
        if pins:
            page["pins"] = pins
        return {"dock": [], "pages": [{"items": []}, page]}

    def test_pin_and_unpin_are_changes_with_their_own_words(self):
        changes = describe_changes(self.view(), self.view({ONE: 7}))
        self.assertEqual(changes, [{"kind": "pinned", "id": ONE, "type": "app", "name": "one",
                                    "to": {"section": "page", "page": 1, "cell": 7}}])
        self.assertIn("keeps its own spot: row 2, column 4 of page 1", change_lines(changes)[0])
        self.assertEqual(change_summary(changes), "1 placed in a spot.")
        changes = describe_changes(self.view({ONE: 7}), self.view())
        self.assertEqual([c["kind"] for c in changes], ["unpinned"])
        self.assertIn("no longer keeps its own spot on page 1", change_lines(changes)[0])
        self.assertEqual(describe_changes(self.view({ONE: 7}), self.view({ONE: 7})), [])
        self.assertEqual([c["kind"] for c in describe_changes(self.view({ONE: 7}), self.view({ONE: 0}))], ["pinned"])


class Apply(unittest.TestCase):
    def setUp(self):
        self.phone = Phone()
        self.device = Device(
            Lockdown(),
            springboard_factory=lambda _: SpringBoard(self.phone),
            installer_factory=lambda _: Installer(self.phone),
        )
        self.backups = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def plan(self, pins):
        state = asyncio.run(self.device.read_state())
        read = make_read(state, asyncio.run(self.device.installed_apps()), device=self.device.info)
        page = {"items": [ONE, TWO, OFF]}
        if pins:
            page["pins"] = pins
        return plan_layout({"dock": {"items": ["com.example.dock"]}, "pages": [page]}, read)

    def apply(self, pins, known=None):
        return asyncio.run(apply_state(self.device, self.plan(pins).payload, backup_dir=self.backups,
                                       timing=FAST, known_pins=known))

    def test_the_result_binds_the_written_pins_to_the_verified_read(self):
        result = self.apply({ONE: 23})
        self.assertEqual(result.status, APPLIED)
        self.assertEqual(reconcile_pins(result.pins, self.phone.state), [{ONE: 23}])
        self.assertNotIn("pins", load_plist(result.backup))  # nothing known before
        self.assertNotIn("pins", result.to_dict())

    def test_a_later_write_backs_up_the_known_pins_and_an_unpinned_write_clears_them(self):
        known = self.apply({ONE: 23}).pins
        result = self.apply(None, known)
        self.assertEqual(load_plist(result.backup)["pins"], [{ONE: 23}])
        self.assertEqual(result.pins, [])

    def test_a_rollback_sends_the_known_pins_back(self):
        known = self.apply({ONE: 23}).pins
        original = self.phone.set_icon_state

        def refuse_once(payload):  # the first write lands in another order
            self.phone.set_icon_state = original
            payload = deepcopy(payload)
            payload["iconLists"][0].reverse()
            original(payload)

        self.phone.set_icon_state = refuse_once
        result = self.apply(None, known)
        self.assertEqual(result.status, ROLLED_BACK)
        [locations] = [m["fixedLocations"] for m in self.phone.writes[-1]["listMetadata"].values()]
        self.assertEqual(locations, {ONE: 23})
        self.assertEqual(reconcile_pins(result.pins, self.phone.state), [{ONE: 23}])


class Recorder(DemoDevice):
    """The demo phone with a short page 1, recording what each write sent."""

    def __init__(self):
        super().__init__(delay=0)
        dock, page1, *rest = self._state
        apps = [x for x in page1 if "elementType" not in x and "listType" not in x]
        self._state = [dock, apps[:3], *rest]
        self.sent = []

    async def write_state(self, payload):
        self.sent.append(deepcopy(payload))
        await super().write_state(payload)


class SessionPins(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.phone = Recorder()
        self.first, self.second, self.third = page_ids(self.phone._state[1])
        self.session = self.new_session()

    def new_session(self):
        async def opener(emit):
            return self.phone

        settings = Settings(data_dir=self.root / "data", backup_dir=self.root / "data" / "backups",
                            timing=Timing(read_timeout=2, write_timeout=1, verify_attempts=2, verify_interval=0),
                            cache_root=self.root / "cache")
        session = Session(settings, opener=opener)
        self.addAsyncCleanup(session.shutdown)
        return session

    async def finish(self, job):
        await job.task
        self.assertIsNone(job.error, job.error)
        return job

    async def read(self, session=None):
        return await self.finish((session or self.session).start_read())

    def layout(self, pins=None, session=None):
        view = (session or self.session).view
        pages = [{"items": p["items"], **({"pins": p["pins"]} if p.get("pins") else {})} for p in view["pages"]]
        if pins is not None:
            items = pages[0]["items"]
            pinned = [x for x in items if x["id"] in pins]
            pages[0] = {"items": [x for x in items if x["id"] not in pins] + pinned, "pins": pins}
        return {"dock": view["dock"], "pages": pages}

    async def apply(self, layout):
        checked = self.session.check(layout)
        return await self.finish(self.session.start_apply(layout, expect_read=self.session.read_file.name,
                                                       expect_plan=checked["approval"]))

    async def test_a_pin_only_edit_is_a_change_and_is_remembered_after_apply(self):
        await self.read()
        layout = self.layout({self.first: 23})
        checked = self.session.check(layout)
        self.assertTrue(checked["dirty"])
        self.assertEqual([c["kind"] for c in checked["changes"]], ["pinned"])
        await self.apply(layout)
        [meta] = self.phone.sent[-1]["listMetadata"].values()
        self.assertEqual(meta["fixedLocations"], {self.first: 23})
        self.assertEqual(self.session.view["pages"][0]["pins"], {self.first: 23})
        self.assertFalse(self.session.check(self.layout())["dirty"])
        # A restarted editor still knows them; the store is per phone.
        later = self.new_session()
        await self.read(later)
        self.assertEqual(later.view["pages"][0]["pins"], {self.first: 23})
        stored = json.loads((self.root / "data" / PINS_FILE).read_text())
        self.assertEqual(list(stored), ["demo"])

    async def test_a_page_changed_on_the_phone_loses_its_pins(self):
        await self.read()
        await self.apply(self.layout({self.first: 23}))
        self.phone._state[1].reverse()
        await self.read()
        self.assertNotIn("pins", self.session.view["pages"][0])

    async def test_apply_refuses_a_layout_other_than_the_reviewed_one(self):
        await self.read()
        approved = self.session.check(self.layout({self.first: 23}))["approval"]
        with self.assertRaises(PlanChanged):
            self.session.start_apply(self.layout({self.first: 22}), expect_plan=approved)
        self.assertEqual(self.phone.sent, [])

    async def test_restore_puts_the_backed_up_gaps_back(self):
        await self.read()
        await self.apply(self.layout({self.first: 23}))
        job = await self.apply(self.layout({}))  # unpin: its backup holds the pin
        self.assertNotIn("pins", self.session.view["pages"][0])
        backup = Path(job.result["backup"]).name
        await self.finish(self.session.start_restore(backup))
        [meta] = self.phone.sent[-1]["listMetadata"].values()
        self.assertEqual(meta["fixedLocations"], {self.first: 23})
        self.assertEqual(self.session.view["pages"][0]["pins"], {self.first: 23})

    async def test_a_damaged_store_refuses_writes_instead_of_dropping_gaps(self):
        (self.root / "data").mkdir(parents=True, exist_ok=True)
        (self.root / "data" / PINS_FILE).write_text("{not json")
        await self.read()
        with self.assertRaises(PinStoreError):
            self.session.start_apply(self.layout({self.first: 23}))
        self.assertEqual(self.phone.sent, [])
        (self.root / "data" / PINS_FILE).unlink()  # what the refusal tells the user to do
        await self.finish(self.session.start_apply(self.layout({self.first: 23})))

    async def test_a_failed_read_after_a_write_blocks_writes_until_the_phone_is_read(self):
        async def lost(device):
            raise OSError("the phone went away")

        await self.blocks_until_read(unittest.mock.patch.object(api, "read_device", lost))

    async def test_a_failed_save_of_the_read_after_a_write_blocks_writes_too(self):
        full = unittest.mock.patch("ios_homescreen.ui.session.save_pull", side_effect=OSError("disk full"))
        await self.blocks_until_read(full)

    async def blocks_until_read(self, failure):
        await self.read()
        with failure:
            job = await self.apply(self.layout({self.third: 23}))  # the last app: same icon order
        self.assertEqual(job.result["status"], "applied")
        self.assertIn("reread_error", job.result)
        sent = len(self.phone.sent)
        with self.assertRaises(ReadChanged):
            self.session.start_apply(self.layout())  # the stale read would drop the new pin
        with self.assertRaises(ReadChanged):
            self.session.start_restore(Path(job.result["backup"]).name, expect_read=self.session.read_file.name)
        self.assertEqual(len(self.phone.sent), sent)
        await self.read()
        self.assertEqual(self.session.view["pages"][0]["pins"], {self.third: 23})
        self.session.start_apply(self.layout())
        await self.session.job.task

    async def test_a_failed_save_is_reported_and_blocks_the_next_write(self):
        await self.read()
        store = self.root / "data" / PINS_FILE
        store.mkdir(parents=True)  # a directory: os.replace onto it fails
        job = await self.apply(self.layout({self.first: 23}))
        self.assertEqual(job.result["status"], "applied")
        self.assertIn("pins_unsaved", [e["code"] for e in job.events])
        with self.assertRaises(PinStoreError):
            self.session.start_apply(self.layout())
        store.rmdir()
        self.session.start_apply(self.layout())  # the save goes through now
        await self.session.job.task


if __name__ == "__main__":
    unittest.main()
