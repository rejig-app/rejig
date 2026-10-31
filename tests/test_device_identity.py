"""Restore and Apply write only to the iPhone the backup or layout came from."""

import tempfile
import unittest
from pathlib import Path

from ios_homescreen.snapshots import make_backup, timestamp
from ios_homescreen.state import same_layout, save_plist
from ios_homescreen.ui.demo import DemoDevice
from ios_homescreen.ui.session import Session, Settings
from ios_homescreen.workflow import Timing

FAST = Timing(read_timeout=2, write_timeout=1, verify_attempts=2, verify_interval=0)


class Phone(DemoDevice):
    def __init__(self, udid):
        super().__init__(delay=0)
        self.udid = udid
        self.closed = 0

    @property
    def info(self):
        return {**super().info, "udid": self.udid}

    async def close(self):
        self.closed += 1


def moved_dock(phone):
    dock, *pages = phone._state
    return [list(reversed(dock)), *pages]


class DeviceIdentityTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.phone_a, self.phone_b = Phone("phone-a"), Phone("phone-b")
        self.phone_b._state = moved_dock(self.phone_b)
        self.connected = self.phone_a
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.backups = root / "data" / "backups"

        async def opener(emit):
            return self.connected

        settings = Settings(
            data_dir=root / "data", backup_dir=self.backups, timing=FAST, cache_root=root / "cache"
        )
        self.session = Session(settings, opener=opener)
        # Runs before the temporary directory goes: stops background icon fetches.
        self.addAsyncCleanup(self.session.shutdown)

    async def run_job(self, job):
        await job.task
        return job

    def backup_of(self, phone):
        self.backups.mkdir(parents=True, exist_ok=True)
        path = self.backups / f"backup-{timestamp()}.plist"
        save_plist(path, make_backup(phone._state, device=phone.info))
        return path

    async def assert_refused(self, job, phone):
        await self.run_job(job)
        self.assertEqual(job.error["code"], "other_device")
        self.assertEqual(phone.writes, 0)
        self.assertEqual(phone.closed, 1)

    async def test_restore_refuses_a_backup_from_another_phone(self):
        backup = self.backup_of(self.phone_a)
        self.connected = self.phone_b
        await self.assert_refused(self.session.start_restore(backup.name), self.phone_b)
        self.assertEqual(sorted(self.backups.iterdir()), [backup])

    async def test_restore_refuses_a_backup_without_a_recorded_phone(self):
        backup = self.backup_of(Phone(""))
        await self.assert_refused(self.session.start_restore(backup.name), self.phone_a)

    async def test_restore_refuses_a_phone_without_identity(self):
        backup = self.backup_of(self.phone_a)
        self.connected = Phone("")
        await self.assert_refused(self.session.start_restore(backup.name), self.connected)

    async def test_restore_writes_to_the_phone_the_backup_came_from(self):
        backup = self.backup_of(self.phone_a)
        wanted = self.phone_a._state
        self.phone_a._state = moved_dock(self.phone_a)
        job = await self.run_job(self.session.start_restore(backup.name))
        self.assertIsNone(job.error)
        self.assertEqual(self.phone_a.writes, 1)
        self.assertTrue(same_layout(self.phone_a._state, wanted))

    async def apply_moved_dock(self, *, force=False):
        await self.run_job(self.session.start_read())
        view = self.session.view
        layout = {
            "dock": list(reversed(view["dock"])),
            "pages": [{"items": page["items"]} for page in view["pages"]],
        }
        return self.session.start_apply(layout, force=force)

    async def test_apply_refuses_a_layout_read_from_another_phone_even_with_force(self):
        for force in (False, True):
            with self.subTest(force=force):
                self.connected = self.phone_a
                start = await self.apply_moved_dock(force=force)
                # Same layout on both phones, so only the identity check can tell them apart.
                self.phone_b = Phone("phone-b")
                self.phone_b._state = self.phone_a._state
                self.connected = self.phone_b
                await self.assert_refused(start, self.phone_b)

    async def test_apply_refuses_a_layout_read_from_a_phone_without_identity(self):
        self.connected = Phone("")
        start = await self.apply_moved_dock()
        self.connected = self.phone_a
        await self.assert_refused(start, self.phone_a)

    async def test_apply_writes_to_the_phone_the_layout_was_read_from(self):
        wanted = moved_dock(self.phone_a)
        job = await self.run_job(await self.apply_moved_dock())
        self.assertIsNone(job.error)
        self.assertEqual(self.phone_a.writes, 1)
        self.assertTrue(same_layout(self.phone_a._state, wanted))
