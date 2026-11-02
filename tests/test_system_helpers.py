"""System helpers without hidden tags must not be offered as placeable apps."""

import tempfile
import unittest
import asyncio
from copy import deepcopy
from pathlib import Path
from unittest import mock

from ios_homescreen.device import Device
from ios_homescreen.model import describe
from ios_homescreen.planner import LayoutError, plan_layout
from ios_homescreen.report import app_places, library_apps
from ios_homescreen.snapshots import make_read
from ios_homescreen.state import StateError, safe_payload, validate_write_payload
from ios_homescreen.ui.session import Session, Settings
from tests.test_offloaded_apps import INSTALLED, Installer, Lockdown, Phone

HELPERS = {"com.apple.NanoUniverse.AegirProxyApp": "Astronomy", "com.apple.sidecar": "Continuity"}
APPS = {ident: {"name": name, "type": "System"} for ident, name in HELPERS.items()}
APPS.update({"com.example.regular": {"name": "Regular", "type": "User"},
             "com.apple.Magnifier": {"name": "Magnifier", "type": "System"},
             "com.example.astronomy": {"name": "Astronomy", "type": "User"}})
READ = make_read([[], []], APPS)


class SystemHelpers(unittest.TestCase):
    def test_device_marks_the_helpers_hidden_even_without_hidden_tags(self):
        phone = Phone()
        device = Device(Lockdown(), installer_factory=lambda _: Installer(phone))
        additions = {ident: {"CFBundleDisplayName": name, "ApplicationType": "System"}
                     for ident, name in HELPERS.items()}
        with mock.patch.dict(INSTALLED, additions):
            apps = asyncio.run(device.installed_apps())
        self.assertTrue(all(apps[ident]["hidden"] for ident in HELPERS))

    def test_old_reads_without_hidden_flags_filter_only_the_known_helpers(self):
        expected = set(APPS) - set(HELPERS)
        self.assertEqual(set(library_apps(READ["state"], APPS)), expected)
        self.assertEqual({a["id"] for a in app_places(READ)}, expected)
        self.assertEqual({a["id"] for a in describe(READ)["library"]}, expected)

    def test_editor_app_list_also_filters_old_metadata(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        session = Session(Settings(data_dir=root, backup_dir=root / "backups", cache_root=root / "cache"))
        session.read, session.view = deepcopy(READ), describe(READ)
        self.assertEqual({a["id"] for a in session.state()["read"]["apps"]}, set(APPS) - set(HELPERS))

    def test_explicit_helper_ids_are_rejected_in_pages_dock_and_folders(self):
        for ident in HELPERS:
            for entry in (ident, {"type": "app", "id": ident},
                          {"type": "folder", "name": "Helpers", "items": [ident]}):
                for section in ("dock", "page"):
                    target = {"dock": {"items": [entry] if section == "dock" else []},
                              "pages": [{"items": [entry]}] if section == "page" else []}
                    with self.subTest(ident=ident, entry=entry, section=section), self.assertRaises(LayoutError) as caught:
                        plan_layout(target, READ)
                    self.assertEqual(caught.exception.issues[0].code, "app_not_placeable")

    def test_same_named_regular_app_and_placeable_system_apps_still_work(self):
        plan = plan_layout({"dock": {"items": []}, "pages": [{"items": ["Astronomy", "Magnifier", "Regular"]}]}, READ)
        self.assertEqual(set(plan.to_home_screen), set(APPS) - set(HELPERS))
        self.assertTrue(set(HELPERS) <= set(plan.payload["ignored"]))
        validate_write_payload({k: plan.payload[k] for k in ("buttonBar", "iconLists", "ignored")})

    def test_write_gate_rejects_helpers_even_when_the_planner_is_bypassed(self):
        for ident in HELPERS:
            for item in (ident, {"bundleIdentifier": ident, "displayIdentifier": ident}):
                payload = safe_payload({"buttonBar": [], "iconLists": [[item]], "ignored": []}, APPS)
                with self.subTest(item=item), self.assertRaises(StateError) as caught:
                    validate_write_payload(payload)
                self.assertEqual(caught.exception.code, "app_not_placeable")


if __name__ == "__main__":
    unittest.main()
