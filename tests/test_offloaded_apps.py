"""Apps that live only in the App Library stay there, offloaded ones included.

Installation Proxy lists placeholders (offloaded apps, apps still downloading) only when
asked with ``ShowPlaceholders``. SpringBoard puts every app a write leaves out of both
the pages and ``ignored`` back on the Home Screen, so a write that misses them surfaces
them on a page.
"""

import asyncio
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from ios_homescreen.device import Device
from ios_homescreen.planner import plan_layout
from ios_homescreen.report import app_places
from ios_homescreen.snapshots import make_read
from ios_homescreen.state import visible_apps
from ios_homescreen.workflow import APPLIED, Timing, apply_state

FAST = Timing(read_timeout=2, write_timeout=1, verify_attempts=2, verify_interval=0)

# Library-only: one installed app and one offloaded app. On a page: one offloaded app.
INSTALLED = {
    "com.example.dock": {"CFBundleDisplayName": "Dock", "ApplicationType": "User"},
    "com.example.one": {"CFBundleDisplayName": "One", "ApplicationType": "User"},
    "com.example.two": {"CFBundleDisplayName": "Two", "ApplicationType": "User"},
    "com.example.library": {"CFBundleDisplayName": "Library", "ApplicationType": "User"},
}
PLACEHOLDERS = {
    "com.example.offloaded": {
        "CFBundleDisplayName": "Offloaded",
        "ApplicationType": "User",
        "IsPlaceholder": True,
        "IsDemotedApp": True,
    },
    "com.example.offpage": {
        "CFBundleDisplayName": "Off Page",
        "ApplicationType": "User",
        "IsPlaceholder": True,
        "IsDemotedApp": True,
    },
    "com.example.downloading": {
        "CFBundleDisplayName": "Downloading",
        "ApplicationType": "User",
        "IsPlaceholder": True,
        "IsDemotedApp": False,
    },
}
LIBRARY_ONLY = {"com.example.library", "com.example.offloaded", "com.example.downloading"}


def app(ident):
    return {"bundleIdentifier": ident, "displayIdentifier": ident}


class Phone:
    """SpringBoard and Installation Proxy as an iPhone answers them."""

    def __init__(self):
        self.state = [
            [app("com.example.dock")],
            [app("com.example.one"), app("com.example.two"), {"displayIdentifier": "com.example.offpage"}],
        ]
        self.writes = []
        self.lookups = []

    def lookup(self, options):
        self.lookups.append(dict(options))
        found = dict(INSTALLED)
        if options.get("ShowPlaceholders"):
            found.update(PLACEHOLDERS)
        wanted = options.get("ReturnAttributes")
        if wanted is not None:
            found = {k: {a: v for a, v in meta.items() if a in wanted} for k, meta in found.items()}
        return deepcopy(found)

    def set_icon_state(self, payload):
        self.writes.append(deepcopy(payload))
        state = [deepcopy(payload["buttonBar"]), *deepcopy(payload["iconLists"])]
        placed = visible_apps(payload) | set(payload["ignored"])
        for ident in sorted({**INSTALLED, **PLACEHOLDERS}):
            if ident not in placed:
                form = {"displayIdentifier": ident} if ident in PLACEHOLDERS else app(ident)
                state[-1].append(form)
        self.state = state


class SpringBoard:
    def __init__(self, phone):
        self.phone = phone
        self.service = self

    async def connect(self):
        return None

    async def close(self):
        return None

    async def get_icon_state(self, format_version="2"):
        return deepcopy(self.phone.state)

    async def send_plist(self, message):
        assert message["command"] == "setIconState"
        self.phone.set_icon_state(message["iconState"])

    async def recv_plist(self):
        return None


class Installer:
    def __init__(self, phone):
        self.phone = phone

    async def connect(self):
        return None

    async def close(self):
        return None

    async def lookup(self, options):
        return self.phone.lookup(options)


class Lockdown:
    def __init__(self):
        self.all_values = {"DeviceName": "Test iPhone", "UniqueDeviceID": "phone-a"}

    async def close(self):
        return None


def on_any_page(state):
    return visible_apps({"buttonBar": state[0], "iconLists": state[1:], "ignored": []})


class OffloadedAppsTest(unittest.TestCase):
    def setUp(self):
        self.phone = Phone()
        self.device = Device(
            Lockdown(),
            springboard_factory=lambda _: SpringBoard(self.phone),
            installer_factory=lambda _: Installer(self.phone),
        )
        self.backups = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def read(self):
        state = asyncio.run(self.device.read_state())
        apps = asyncio.run(self.device.installed_apps())
        return make_read(state, apps, device=self.device.info)

    def test_the_app_list_includes_offloaded_apps_and_marks_them(self):
        apps = asyncio.run(self.device.installed_apps())
        self.assertTrue(self.phone.lookups[-1].get("ShowPlaceholders"))
        self.assertTrue(apps["com.example.offloaded"]["offloaded"])
        self.assertTrue(apps["com.example.offpage"]["offloaded"])
        self.assertNotIn("offloaded", apps["com.example.one"])
        # Still downloading: listed (so kept in ignored), but not offloaded.
        self.assertNotIn("offloaded", apps["com.example.downloading"])
        library = {a["id"]: a for a in app_places(self.read()) if a["where"]["section"] == "library"}
        self.assertEqual(set(library), LIBRARY_ONLY)
        self.assertTrue(library["com.example.offloaded"]["offloaded"])
        self.assertFalse(library["com.example.library"]["offloaded"])
        self.assertFalse(library["com.example.downloading"]["offloaded"])

    def test_a_reorder_keeps_every_app_library_only_app_off_the_pages(self):
        read = self.read()
        layout = {
            "dock": {"items": ["com.example.dock"]},
            "pages": [{"items": ["com.example.two", "com.example.one", "com.example.offpage"]}],
        }
        plan = plan_layout(layout, read)
        result = asyncio.run(
            apply_state(self.device, plan.payload, backup_dir=self.backups, timing=FAST)
        )
        self.assertEqual(result.status, APPLIED)
        self.assertEqual(len(self.phone.writes), 1)
        self.assertLessEqual(LIBRARY_ONLY, set(self.phone.writes[0]["ignored"]))
        self.assertEqual(on_any_page(self.phone.state) & LIBRARY_ONLY, set())
        self.assertEqual(
            [item.get("displayIdentifier") for item in self.phone.state[1]],
            ["com.example.two", "com.example.one", "com.example.offpage"],
        )

    def test_an_offloaded_app_moved_home_is_written_in_springboards_own_form(self):
        layout = {
            "dock": {"items": ["com.example.dock"]},
            "pages": [
                {"items": ["com.example.one", "com.example.two", "com.example.offpage", "com.example.offloaded"]}
            ],
        }
        plan = plan_layout(layout, self.read())
        self.assertEqual(plan.payload["iconLists"][0][-1], {"displayIdentifier": "com.example.offloaded"})

    def test_a_downloading_app_moved_home_keeps_the_normal_form(self):
        layout = {
            "dock": {"items": ["com.example.dock"]},
            "pages": [
                {"items": ["com.example.one", "com.example.two", "com.example.offpage", "com.example.downloading"]}
            ],
        }
        plan = plan_layout(layout, self.read())
        self.assertEqual(plan.payload["iconLists"][0][-1], app("com.example.downloading"))


if __name__ == "__main__":
    unittest.main()
