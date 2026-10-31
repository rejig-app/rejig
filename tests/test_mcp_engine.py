"""The editor changes the MCP companion relies on (patches/mcp-companion.patch)."""

import tempfile
import unittest
from pathlib import Path

from ios_homescreen.snapshots import make_backup, timestamp
from ios_homescreen.state import save_plist
from ios_homescreen.ui.demo import DemoDevice
from ios_homescreen.api import load_backup
from ios_homescreen.ui.session import BackupChanged, ReadChanged, Session, Settings, backup_digest
from ios_homescreen.workflow import Timing

from rejig_mcp import plans

FAST = Timing(read_timeout=2, write_timeout=1, verify_attempts=2, verify_interval=0)


class ExpectReadTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.phone = DemoDevice(delay=0)
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.backups = root / "data" / "backups"

        async def opener(emit):
            return self.phone

        settings = Settings(
            data_dir=root / "data", backup_dir=self.backups, timing=FAST, cache_root=root / "cache"
        )
        self.session = Session(settings, opener=opener)
        self.addAsyncCleanup(self.session.shutdown)

    async def read(self):
        job = self.session.start_read()
        await job.task
        return self.session.read_file.name

    def reversed_dock(self):
        view = self.session.view
        return {
            "dock": list(reversed(view["dock"])),
            "pages": [{"items": page["items"]} for page in view["pages"]],
        }

    async def test_apply_proceeds_on_the_expected_read(self):
        first = await self.read()
        job = self.session.start_apply(self.reversed_dock(), expect_read=first)
        await job.task
        self.assertIsNone(job.error)
        self.assertEqual(self.phone.writes, 1)

    async def test_apply_refuses_after_another_read(self):
        first = await self.read()
        layout = self.reversed_dock()
        await self.read()
        with self.assertRaises(ReadChanged) as caught:
            self.session.start_apply(layout, expect_read=first)
        self.assertEqual(caught.exception.code, "read_changed")
        self.assertEqual(self.phone.writes, 0)
        self.assertEqual(self.session.job.kind, "read")

    async def test_restore_refuses_after_another_read(self):
        first = await self.read()
        self.backups.mkdir(parents=True, exist_ok=True)
        backup = self.backups / f"backup-{timestamp()}.plist"
        save_plist(backup, make_backup(self.phone._state, device=self.phone.info))
        await self.read()
        with self.assertRaises(ReadChanged):
            self.session.start_restore(backup.name, expect_read=first)
        self.assertEqual(self.phone.writes, 0)

    async def test_state_names_the_folders_this_run_uses(self):
        state = self.session.state()
        self.assertEqual(state["data_dir"], str(self.session.settings.data_dir))
        self.assertEqual(state["backup_dir"], str(self.backups))

    async def test_without_expect_read_nothing_changes(self):
        await self.read()
        layout = self.reversed_dock()
        await self.read()
        job = self.session.start_apply(layout)
        await job.task
        self.assertIsNone(job.error)
        self.assertEqual(self.phone.writes, 1)


    def save_backup(self, state):
        self.backups.mkdir(parents=True, exist_ok=True)
        backup = self.backups / f"backup-{timestamp()}.plist"
        save_plist(backup, make_backup(state, device=self.phone.info))
        return backup

    def other_layout(self):
        dock, *pages = self.phone._state
        return [list(reversed(dock)), *pages]

    async def test_companion_and_editor_agree_on_a_backup_digest(self):
        backup = self.save_backup(self.other_layout())
        self.assertEqual(plans.backup_digest_of(backup), backup_digest(load_backup(backup)))

    async def test_restore_refuses_a_changed_backup(self):
        first = await self.read()
        backup = self.save_backup(self.other_layout())
        digest = plans.backup_digest_of(backup)
        save_plist(backup, make_backup(self.phone._state, device=self.phone.info))
        with self.assertRaises(BackupChanged) as caught:
            self.session.start_restore(backup.name, expect_read=first, expect_digest=digest)
        self.assertEqual(caught.exception.code, "backup_changed")
        self.assertEqual(self.phone.writes, 0)

    async def test_restore_refuses_when_the_phone_changed_since_the_read(self):
        first = await self.read()
        backup = self.save_backup(self.other_layout())
        dock, *pages = self.phone._state
        self.phone._state = [dock, *reversed(pages)]  # someone edits the phone itself
        job = self.session.start_restore(backup.name, expect_read=first, expect_digest=plans.backup_digest_of(backup))
        await job.task
        self.assertEqual(job.error["code"], "layout_changed")
        self.assertEqual(self.phone.writes, 0)

    async def test_restore_against_the_read_writes(self):
        first = await self.read()
        backup = self.save_backup(self.other_layout())
        job = self.session.start_restore(backup.name, expect_read=first, expect_digest=plans.backup_digest_of(backup))
        await job.task
        self.assertIsNone(job.error)
        self.assertEqual(self.phone.writes, 1)

if __name__ == "__main__":
    unittest.main()
