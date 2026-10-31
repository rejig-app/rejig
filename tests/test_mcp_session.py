"""The MCP companion through real MCP client sessions, against a real demo editor process.

Each test starts ``ios_homescreen editor --demo`` (a pretend iPhone), writes the bridge file
the Rejig app would write, and drives the companion with the MCP SDK's own Client: legacy
elicitation (initialize handshake) and the 2026-07-28 input-required round trip. The Mac
approval window is a small fake script; nothing here touches a real phone or the real app.
"""

import asyncio
import contextlib
import io
import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from mcp import Client, StdioServerParameters
from mcp_types import ElicitResult, Implementation, InputRequiredResult

from rejig_mcp import plans, server
from rejig_mcp.draft import Drafts
from rejig_mcp.editor import Editor, EditorError

OPEN = re.compile(r"Open: http://127\.0\.0\.1:(\d+)/\?token=(\S+)")
MODES = ("legacy", "2026-07-28")

FAKE_APPROVER = textwrap.dedent(
    """\
    import json, pathlib, sys, time
    here = pathlib.Path(sys.argv[1])
    (here / "asked.json").write_text(sys.stdin.read())
    answer = (here / "answer").read_text().strip()
    while answer == "wait":
        time.sleep(0.1)
        answer = (here / "answer").read_text().strip()
    sys.exit(int(answer))
    """
)


def data(result):
    return result.structured_content or {}


def record_of(result):
    """The operation record of an apply_plan or get_operation answer, error or not."""
    found = data(result)
    return found.get("operation") or (found.get("error") or {}).get("operation") or {}


class DemoEditor:
    """A real ``ios_homescreen editor --demo`` process and the bridge file for it."""

    def __init__(self, support: Path) -> None:
        self.support = support
        self.process = subprocess.Popen(
            [sys.executable, "-m", "ios_homescreen", "editor", "--demo", "--no-browser", "--port", "0"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.monotonic() + 30
        for line in self.process.stdout:
            found = OPEN.search(line)
            if found:
                self.port, self.token = int(found[1]), found[2]
                break
            if time.monotonic() > deadline:
                break
        else:
            raise RuntimeError("the demo editor did not start")
        self.write_bridge()

    def write_bridge(self, **extra) -> None:
        bridge = {"port": self.port, "token": self.token, "pid": self.process.pid, "connection": "demo", "dirty": False}
        (self.support / "mcp-bridge.json").write_text(json.dumps({**bridge, **extra}))

    def stop(self) -> None:
        self.process.terminate()
        self.process.wait(timeout=10)
        self.process.stdout.close()


class SessionTest(unittest.IsolatedAsyncioTestCase):
    mode = "legacy"

    async def asyncSetUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.support = root / "support"
        self.support.mkdir()
        self.approver_dir = root / "approver"
        self.approver_dir.mkdir()
        (self.approver_dir / "approve.py").write_text(FAKE_APPROVER)
        (self.approver_dir / "answer").write_text("0")
        self.demo = DemoEditor(self.support)
        self.addCleanup(self.demo.stop)
        command = [sys.executable, str(self.approver_dir / "approve.py"), str(self.approver_dir)]
        # The app path does not exist: a test can never launch a real Rejig.app.
        self.enterContext(mock.patch.object(server, "editor", Editor(support=self.support, app=root / "none.app")))
        self.enterContext(mock.patch.object(server, "drafts", Drafts()))
        self.enterContext(mock.patch.object(server, "approvals", {}))
        self.enterContext(mock.patch.object(server, "approver_command", lambda: command))
        self.asked = []
        self.answer = ElicitResult(action="accept", content={"approve": True})

    async def elicit(self, context, params):
        self.asked.append(params.message)
        return self.answer

    def client(self, *, forms=True):
        return Client(server.server, mode=self.mode, elicitation_callback=self.elicit if forms else None)

    def backups(self):
        return sorted((Path(server.editor.state()["backup_dir"])).glob("*"))

    async def plan(self, client):
        """read_layout, move the first dock app to the last page, preview: the plan's data."""
        layout = data(await client.call_tool("read_layout", {}))
        first = layout["dock"][0]["id"]
        edited = await client.call_tool(
            "edit_draft",
            {
                "draft_id": layout["draft_id"],
                "base_revision": 0,
                "operations": [
                    {"op": "move", "item": first, "to": {"section": "page", "page": len(layout["pages"]), "index": 1}}
                ],
            },
        )
        self.assertFalse(edited.is_error, edited.content)
        self.assertTrue(data(edited)["valid"], data(edited))
        preview = await client.call_tool("preview_draft", {"draft_id": layout["draft_id"], "revision": 1})
        self.assertFalse(preview.is_error, preview.content)
        return {**data(preview), "moved": first}

    # ------------------------------------------------------------------ reads

    async def test_tools_reads_are_marked_and_the_decision_is_hidden(self):
        async with self.client() as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        self.assertEqual(len(tools), 12)
        for name in ("get_status", "read_layout", "list_apps", "get_app_icons", "get_operation", "list_backups"):
            self.assertTrue(tools[name].annotations.read_only_hint, name)
        self.assertFalse(tools["apply_plan"].annotations.read_only_hint)
        self.assertEqual(set(tools["apply_plan"].input_schema["properties"]), {"plan_id", "approval"})
        for name, tool in tools.items():
            self.assertEqual(tool.output_schema.get("type"), "object", name)

    async def test_reads_never_carry_the_udid(self):
        async with self.client() as client:
            layout = await client.call_tool("read_layout", {})
            status = await client.call_tool("get_status", {})
            apps = await client.call_tool("list_apps", {"section": "library"})
        udid = server.udid_of(server.editor.state()["read"])
        self.assertTrue(udid)
        for result in (layout, status, apps):
            self.assertFalse(result.is_error, result.content)
            shown = json.dumps(data(result)) + str(result.content)
            self.assertNotIn(self.demo.token, shown)
            self.assertNotIn('"udid"', shown)
            self.assertNotIn(f'"{udid}"', shown.replace('"connection": "demo"', ""))
        self.assertEqual(data(status)["device"]["id"], server.device_id(udid))
        self.assertEqual(data(apps)["library_only_count"], len(data(layout)["library"]))

    async def test_text_content_carries_the_structured_result(self):
        # Clients that pass only text to the model (Claude Desktop) must still see ids and pages.
        async with self.client() as client:
            results = [
                await client.call_tool("read_layout", {}),
                await client.call_tool("get_status", {}),
                await client.call_tool("list_apps", {}),
                await client.call_tool("apply_plan", {"plan_id": "0" * 64}),
            ]
        self.assertTrue(results[-1].is_error)
        for result in results:
            self.assertEqual(json.loads(result.content[1].text), data(result), result.content[0].text)
        self.assertTrue(data(results[0])["pages"], "read_layout shows pages")

    # ------------------------------------------------------------------ approval

    async def test_approved_plan_is_written_and_read_back(self):
        async with self.client() as client:
            plan = await self.plan(client)
            result = await client.call_tool("apply_plan", {"plan_id": plan["plan_id"]})
            self.assertFalse(result.is_error, result.content)
            record = data(result)["operation"]
            self.assertEqual(record["state"], "applied")
            self.assertTrue(record["matches_plan"])
            self.assertTrue(record["library_check"]["ok"])
            self.assertEqual(len(self.asked), 1)
            for line in plan["changes"]:
                self.assertIn(line, self.asked[0])
            again = data(await client.call_tool("read_layout", {"use_last": True}))
            self.assertNotIn(plan["moved"], [item["id"] for item in again["dock"]])
            self.assertEqual(len(self.backups()), 1)
            # A repeated call returns the record and never writes twice.
            repeat = await client.call_tool("apply_plan", {"plan_id": plan["plan_id"]})
            self.assertEqual(data(repeat)["operation"]["state"], "applied")
            self.assertEqual(len(self.asked), 1)
            self.assertEqual(len(self.backups()), 1)

    async def test_declined_or_unapproved_or_cancelled_writes_nothing(self):
        answers = {
            "approval_declined": ElicitResult(action="decline"),
            "approval_declined ": ElicitResult(action="accept", content={"approve": False}),
            "approval_cancelled": ElicitResult(action="cancel"),
        }
        async with self.client() as client:
            for code, answer in answers.items():
                with self.subTest(code):
                    self.answer = answer
                    plan = await self.plan(client)
                    result = await client.call_tool("apply_plan", {"plan_id": plan["plan_id"]})
                    self.assertTrue(result.is_error)
                    self.assertEqual(data(result)["error"]["code"], code.strip())
                    self.assertEqual(self.backups(), [])
            status = data(await client.call_tool("get_status", {}))
            self.assertIsNone(status["last_operation"])

    async def test_stale_plan_is_refused_before_asking(self):
        async with self.client() as client:
            plan = await self.plan(client)
            await client.call_tool("read_layout", {})  # a newer read
            result = await client.call_tool("apply_plan", {"plan_id": plan["plan_id"]})
        self.assertEqual(data(result)["error"]["code"], "read_changed")
        self.assertEqual(self.asked, [])
        self.assertEqual(self.backups(), [])

    async def test_unsaved_edits_in_the_app_block_reads_and_writes(self):
        async with self.client() as client:
            plan = await self.plan(client)
            self.demo.write_bridge(dirty=True)
            applied = await client.call_tool("apply_plan", {"plan_id": plan["plan_id"]})
            read = await client.call_tool("read_layout", {})
        self.assertEqual(data(applied)["error"]["code"], "app_has_unsaved_edits")
        self.assertEqual(data(read)["error"]["code"], "app_has_unsaved_edits")
        self.assertEqual(self.asked, [])

    async def test_busy_editor_is_refused(self):
        async with self.client() as client:
            plan = await self.plan(client)
            server.editor.request("POST", "/api/read", {})
            result = await client.call_tool("apply_plan", {"plan_id": plan["plan_id"]})
        self.assertEqual(data(result)["error"]["code"], "busy")
        self.assertEqual(self.asked, [])

    async def test_tampered_or_unknown_plan_is_refused(self):
        async with self.client() as client:
            plan = await self.plan(client)
            path = self.support / "mcp" / "plans" / f"{plan['plan_id']}.json"
            saved = json.loads(path.read_text())
            saved["layout"]["dock"] = []
            path.write_text(json.dumps(saved))
            tampered = await client.call_tool("apply_plan", {"plan_id": plan["plan_id"]})
            unknown = await client.call_tool("apply_plan", {"plan_id": "0" * 64})
        self.assertEqual(data(tampered)["error"]["code"], "plan_invalid")
        self.assertEqual(data(unknown)["error"]["code"], "plan_not_found")
        self.assertEqual(self.asked, [])

    async def test_plan_for_another_phone_is_refused(self):
        async with self.client() as client:
            plan = await self.plan(client)
            saved = json.loads((self.support / "mcp" / "plans" / f"{plan['plan_id']}.json").read_text())
            other = plans.save_plan(self.support / "mcp", {**saved, "device_udid": "00008110-000000000000OTHER"})
            result = await client.call_tool("apply_plan", {"plan_id": other})
        self.assertEqual(data(result)["error"]["code"], "other_device")
        self.assertEqual(self.backups(), [])

    async def test_a_backup_changed_after_preview_is_refused(self):
        async with self.client() as client:
            plan = await self.plan(client)
            await client.call_tool("apply_plan", {"plan_id": plan["plan_id"]})
            name = data(await client.call_tool("list_backups", {}))["backups"][0]["name"]
            restore = data(await client.call_tool("preview_restore", {"backup": name}))
            path = Path(server.editor.state()["backup_dir"]) / name
            saved = plistlib.loads(path.read_bytes())
            saved["state"][0] = list(reversed(saved["state"][0]))
            path.write_bytes(plistlib.dumps(saved))
            result = await client.call_tool("apply_plan", {"plan_id": restore["plan_id"]})
        self.assertEqual(data(result)["error"]["code"], "backup_changed")
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(len(self.backups()), 1)

    async def test_two_helpers_applying_one_plan_write_once(self):
        async with self.client() as one, self.client() as two:
            plan = await self.plan(one)
            results = await asyncio.gather(
                one.call_tool("apply_plan", {"plan_id": plan["plan_id"]}),
                two.call_tool("apply_plan", {"plan_id": plan["plan_id"]}),
            )
            final = await self.settle(one, plan["plan_id"])
        self.assertEqual(final["state"], "applied")
        self.assertEqual(len(self.backups()), 1)
        states = sorted(record_of(r).get("state") or data(r)["error"]["code"] for r in results)
        self.assertIn("applied", states)
        self.assertTrue(set(states) <= {"applied", "busy", "submitting", "writing"}, states)

    async def settle(self, client, plan_id):
        """get_operation until the write has an outcome; the final record."""
        for _ in range(120):
            result = await client.call_tool("get_operation", {"plan_id": plan_id})
            record = record_of(result)
            if record.get("state") not in {"submitting", "writing"}:
                return record
            await asyncio.sleep(0.25)
        self.fail("the write never finished")

    async def test_a_lost_answer_to_the_write_is_reconciled_by_reading(self):
        real = server.editor.request

        def lossy(method, path, body=None, **options):
            answer = real(method, path, body, **options)
            if method == "POST" and path == "/api/apply":
                raise EditorError("app_not_running", "Rejig's editor is not answering.")
            return answer

        async with self.client() as client:
            plan = await self.plan(client)
            with mock.patch.object(server.editor, "request", lossy):
                result = await client.call_tool("apply_plan", {"plan_id": plan["plan_id"]})
            self.assertEqual(data(result)["error"]["code"], "outcome_unknown")
            self.assertEqual(plans.read_journal(self.support / "mcp", plan["plan_id"])["state"], "submitting")
            again = await client.call_tool("apply_plan", {"plan_id": plan["plan_id"]})
            self.assertIn(record_of(again).get("state"), {"submitting"}, again.content)
            final = await self.settle(client, plan["plan_id"])
        self.assertEqual(final["state"], "applied_reconciled")
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(len(self.backups()), 1)

    async def test_a_cancel_after_the_write_was_sent_leaves_it_running(self):
        journal = self.support / "mcp" / "operations"
        async with self.client() as client:
            plan = await self.plan(client)
            task = asyncio.create_task(client.call_tool("apply_plan", {"plan_id": plan["plan_id"]}))
            for _ in range(200):
                found = journal / f"{plan['plan_id']}.json"
                if found.exists() and json.loads(found.read_text()).get("state") == "writing":
                    break
                await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(BaseException):
                await task
        async with self.client() as client:
            final = await self.settle(client, plan["plan_id"])
        self.assertEqual(final["state"], "applied")
        self.assertEqual(len(self.backups()), 1)

    async def test_replayed_approval_state_does_not_approve_another_plan(self):
        if self.mode == "legacy":
            self.skipTest("request_state exists only on the 2026-07-28 round trip")
        async with self.client() as client:
            first = await self.plan(client)
            draft = server.drafts.get(next(iter(server.drafts._drafts)))  # the plan's draft, one read
            last = draft.layout["dock"][0]["id"]
            await client.call_tool(
                "edit_draft",
                {
                    "draft_id": draft.id,
                    "base_revision": 1,
                    "operations": [{"op": "move", "item": last, "to": {"section": "page", "page": len(draft.layout["pages"]), "index": 2}}],
                },
            )
            second = data(await client.call_tool("preview_draft", {"draft_id": draft.id, "revision": 2}))
            self.assertNotEqual(second["plan_id"], first["plan_id"])
            asked = await client.session.call_tool(
                "apply_plan", {"plan_id": first["plan_id"]}, allow_input_required=True
            )
            self.assertIsInstance(asked, InputRequiredResult)
            yes = {key: ElicitResult(action="accept", content={"approve": True}) for key in asked.input_requests}
            try:
                replayed = await client.session.call_tool(
                    "apply_plan",
                    {"plan_id": second["plan_id"]},
                    input_responses=yes,
                    request_state=asked.request_state,
                    allow_input_required=True,
                )
            except Exception:  # the SDK refuses the state outright
                replayed = None
        self.assertFalse(getattr(replayed, "structured_content", None) and record_of(replayed).get("state") == "applied")
        self.assertIsNone(plans.read_journal(self.support / "mcp", second["plan_id"]))
        self.assertEqual(self.backups(), [])

    # ------------------------------------------------------------------ the Mac app's window

    async def test_client_without_forms_uses_the_mac_window(self):
        async with self.client(forms=False) as client:
            (self.approver_dir / "answer").write_text("1")
            plan = await self.plan(client)
            declined = await client.call_tool("apply_plan", {"plan_id": plan["plan_id"]})
            self.assertEqual(data(declined)["error"]["code"], "approval_declined")
            self.assertEqual(self.backups(), [])
            asked = json.loads((self.approver_dir / "asked.json").read_text())
            self.assertEqual(asked["lines"], plan["changes"])

            (self.approver_dir / "answer").write_text("wait")
            plan = await self.plan(client)
            with mock.patch.object(server, "APPROVAL_WAIT", 0.5):
                waiting = await client.call_tool("apply_plan", {"plan_id": plan["plan_id"]})
            self.assertEqual(data(waiting)["operation"]["state"], "awaiting_approval")
            status = data(await client.call_tool("get_status", {}))
            self.assertEqual(status["last_operation"]["state"], "awaiting_approval")
            (self.approver_dir / "answer").write_text("0")
            applied = await client.call_tool("apply_plan", {"plan_id": plan["plan_id"]})
        self.assertFalse(applied.is_error, applied.content)
        self.assertEqual(data(applied)["operation"]["state"], "applied")
        self.assertEqual(self.asked, [])
        self.assertEqual(len(self.backups()), 1)

    async def test_codex_goes_to_the_mac_window_despite_declaring_forms(self):
        codex = Client(
            server.server,
            mode=self.mode,
            elicitation_callback=self.elicit,
            client_info=Implementation(name="codex-mcp-client", version="0.166.0"),
        )
        async with codex as client:
            plan = await self.plan(client)
            result = await client.call_tool("apply_plan", {"plan_id": plan["plan_id"]})
        self.assertEqual(data(result)["operation"]["state"], "applied")
        self.assertEqual(self.asked, [])
        self.assertTrue((self.approver_dir / "asked.json").exists())

    async def test_mac_window_can_be_asked_for(self):
        async with self.client() as client:
            plan = await self.plan(client)
            result = await client.call_tool("apply_plan", {"plan_id": plan["plan_id"], "approval": "mac_app"})
        self.assertEqual(data(result)["operation"]["state"], "applied")
        self.assertEqual(self.asked, [])

    # ------------------------------------------------------------------ restore and recovery

    async def test_restore_puts_the_backup_back(self):
        async with self.client() as client:
            plan = await self.plan(client)
            await client.call_tool("apply_plan", {"plan_id": plan["plan_id"]})
            backups = data(await client.call_tool("list_backups", {}))["backups"]
            self.assertEqual(len(backups), 1)
            restore = await client.call_tool("preview_restore", {"backup": backups[0]["name"]})
            self.assertFalse(restore.is_error, restore.content)
            result = await client.call_tool("apply_plan", {"plan_id": data(restore)["plan_id"]})
            self.assertEqual(data(result)["operation"]["state"], "applied", result.content)
            self.assertTrue(data(result)["operation"]["matches_plan"])
            layout = data(await client.call_tool("read_layout", {"use_last": True}))
        self.assertEqual(layout["dock"][0]["id"], plan["moved"])
        missing = await self.missing_backup()
        self.assertEqual(data(missing)["error"]["code"], "no_backup")

    async def missing_backup(self):
        async with self.client() as client:
            return await client.call_tool("preview_restore", {"backup": "../../etc/passwd"})

    async def test_a_fresh_helper_finds_the_outcome(self):
        async with self.client() as client:
            plan = await self.plan(client)
            await client.call_tool("apply_plan", {"plan_id": plan["plan_id"]})
        with mock.patch.object(server, "drafts", Drafts()), mock.patch.object(server, "approvals", {}):
            async with self.client() as client:
                result = await client.call_tool("get_operation", {"plan_id": plan["plan_id"]})
        self.assertEqual(data(result)["operation"]["state"], "applied")

    async def test_a_failed_read_back_is_retried_by_get_operation_without_writing(self):
        real = server.editor.request

        def no_read_back(method, path, body=None, **options):
            answer = real(method, path, body, **options)
            job = answer.get("job") if isinstance(answer, dict) else None
            if job and job.get("kind") == "apply" and job.get("state") == "done":
                job["result"] = {**job["result"], "reread_error": {"code": "read_failed", "title": "No answer", "detail": secret}}
            return answer

        secret = "/var/folders/zz/raw-trace-00008120-SECRET"  # upstream friendly() puts str(exc) in detail
        async with self.client() as client:
            plan = await self.plan(client)
            with mock.patch.object(server.editor, "request", no_read_back):
                result = await client.call_tool("apply_plan", {"plan_id": plan["plan_id"]})
            self.assertFalse(result.is_error, result.content)
            self.assertEqual(record_of(result)["state"], "applied_unverified")
            self.assertEqual(record_of(result)["reread_error"], {"code": "read_failed", "title": "No answer"})
            repeat = await client.call_tool("apply_plan", {"plan_id": plan["plan_id"]})
            self.assertEqual(record_of(repeat)["state"], "applied_unverified")
            status_result = await client.call_tool("get_status", {})
            status = data(status_result)
            self.assertEqual(status["last_operation"]["state"], "applied_unverified")
            for shown in (result, repeat, status_result):
                self.assertNotIn(secret, json.dumps(data(shown)) + str(shown.content))
            journal = self.support / "mcp" / "operations" / f"{plan['plan_id']}.json"
            self.assertIn(secret, journal.read_text())  # the raw detail stays in the local journal
            checked = await client.call_tool("get_operation", {"plan_id": plan["plan_id"]})
        self.assertFalse(checked.is_error, checked.content)
        self.assertEqual(record_of(checked)["state"], "applied")
        self.assertTrue(record_of(checked)["matches_plan"])
        self.assertTrue(record_of(checked)["library_check"]["ok"])
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(len(self.backups()), 1)

    async def test_an_unexpected_editor_error_reaches_the_model_without_its_raw_text(self):
        secret = "/var/folders/zz/raw-trace-00008120-SECRET"
        body = {"code": "unexpected", "message": secret, "error": {"code": "x", "title": "Something went wrong", "detail": secret}}
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError(
            "http://127.0.0.1/api/state", 500, "Internal Server Error", {}, io.BytesIO(json.dumps(body).encode())
        )
        stderr = io.StringIO()
        with mock.patch.object(server.editor, "_login", return_value=opener), contextlib.redirect_stderr(stderr):
            async with self.client() as client:
                result = await client.call_tool("get_status", {})
        self.assertTrue(result.is_error)
        self.assertEqual(data(result)["error"], {"code": "unexpected", "message": "Something went wrong"})
        self.assertNotIn(secret, json.dumps(data(result)) + str(result.content))
        self.assertIn(secret, stderr.getvalue())  # kept in the client's local server log

    async def lost_write(self, client, plan_id, **changes):
        """A plan whose write was journalled, then lost by the editor; get_operation's answer."""
        root = self.support / "mcp"
        record = plans.read_journal(root, plan_id) or {"plan_id": plan_id}
        plans.write_journal(root, plan_id, {**record, "state": "writing", "job_id": "gone", **changes})
        return await client.call_tool("get_operation", {"plan_id": plan_id})

    async def test_recovery_never_credits_another_iphone(self):
        async with self.client() as client:
            plan = await self.plan(client)
            saved = json.loads((self.support / "mcp" / "plans" / f"{plan['plan_id']}.json").read_text())
            shown = server.editor.state()["read"]["view"]["fingerprint"]
            # A plan for phone A whose target is exactly what the connected phone B shows.
            other = plans.save_plan(self.support / "mcp", {**saved, "device_udid": "PHONE-A", "target_fingerprint": shown})
            result = await self.lost_write(client, other, library_before=[])
        self.assertEqual(data(result)["error"]["code"], "other_device")
        self.assertEqual(plans.read_journal(self.support / "mcp", other)["state"], "writing")

    async def test_recovery_checks_the_app_library(self):
        async with self.client() as client:
            plan = await self.plan(client)
            applied = record_of(await client.call_tool("apply_plan", {"plan_id": plan["plan_id"]}))
            self.assertEqual(applied["state"], "applied")
            before = applied["library_before"]
            result = await self.lost_write(client, plan["plan_id"], library_before=[*before, "com.example.gone"])
        self.assertEqual(data(result)["error"]["code"], "library_violation")
        self.assertEqual(record_of(result)["library_check"]["unexpected_on_home"], ["com.example.gone"])

    async def test_optimize_without_matched_usage_leaves_the_draft(self):
        async with self.client() as client:
            layout = data(await client.call_tool("read_layout", {}))
            for usage in ([], [{"app": "No Such App", "minutes": 30}]):
                result = await client.call_tool(
                    "optimize_draft", {"draft_id": layout["draft_id"], "base_revision": 0, "usage": usage}
                )
                self.assertFalse(result.is_error, result.content)
                self.assertFalse(data(result)["improved"])
                self.assertEqual(data(result)["revision"], 0)

    async def test_editor_restart_mid_write_is_reconciled_by_reading(self):
        async with self.client() as client:
            plan = await self.plan(client)
        # The helper journalled "writing", then the editor (and its pretend phone) restarted.
        root = self.support / "mcp"
        plans.write_journal(root, plan["plan_id"], {"plan_id": plan["plan_id"], "state": "writing", "job_id": "gone"})
        self.demo.stop()
        self.demo = DemoEditor(self.support)
        self.addCleanup(self.demo.stop)
        async with self.client() as client:
            result = await client.call_tool("get_operation", {"plan_id": plan["plan_id"]})
        # A phone showing the old layout is no proof: the write may have been rolled back.
        self.assertEqual(data(result)["error"]["code"], "outcome_unknown")
        self.assertTrue(data(result)["error"]["operation"]["phone_matches_base"])
        self.assertEqual(data(result)["error"]["operation"]["new_backups"], [])


class ModernSessionTest(SessionTest):
    """The same matrix over the 2026-07-28 input-required round trip."""

    mode = "2026-07-28"



class FinishTest(unittest.IsolatedAsyncioTestCase):
    """How a finished editor job is journalled, for the outcomes the demo phone cannot fake."""

    PLAN = {
        "plan_id": "c" * 64,
        "kind": "layout",
        "device_udid": "PHONE-A",
        "target_fingerprint": "approved",
        "library_moves": {"to_home": [], "to_library": []},
    }
    ATTEMPT = {"plan_id": "c" * 64, "state": "writing", "backups_before": ["backup-old"], "library_before": ["chess"]}

    async def asyncSetUp(self):
        support = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = support / "mcp"
        stub = mock.Mock(support=support)
        stub.request.return_value = {"backups": [{"name": "backup-new"}, {"name": "backup-old"}]}
        self.enterContext(mock.patch.object(server, "editor", stub))
        self.read_back()

    def read_back(self, fingerprint="approved", library=("chess",)):
        """The read the editor's session keeps after the write."""
        view = {"device": {"udid": "PHONE-A"}, "fingerprint": fingerprint, "library": [{"id": i} for i in library]}
        server.editor.state.return_value = {"read": {"file": "pull-after", "view": view}}

    async def finish(self, job):
        plans.write_journal(self.root, "c" * 64, self.ATTEMPT)  # what write() journalled before
        record = await server.finish(self.PLAN, {"id": "j1", **job}, self.ATTEMPT)
        self.assertEqual(plans.read_journal(self.root, "c" * 64), record)
        # Every outcome spends the plan: a repeated apply_plan finds it instead of writing.
        self.assertEqual(plans.claim(self.root, "c" * 64, {"state": "submitting"}), record)
        return record, server.operation(record)

    async def test_applied_and_read_back_as_approved(self):
        record, answer = await self.finish({"state": "done", "result": {"status": "applied", "backup_name": "b1"}})
        self.assertEqual(record["state"], "applied")
        self.assertTrue(record["matches_plan"])
        self.assertTrue(record["library_check"]["ok"])
        self.assertFalse(answer.is_error)

    async def test_a_read_back_other_than_the_approved_target_is_a_mismatch(self):
        self.read_back(fingerprint="something-else")
        record, answer = await self.finish({"state": "done", "result": {"status": "applied", "backup_name": "b1"}})
        self.assertEqual(record["state"], "readback_mismatch")
        self.assertFalse(record["matches_plan"])
        self.assertEqual(record["backup"], "b1")
        self.assertTrue(record["library_check"]["ok"])
        self.assertTrue(answer.is_error)
        self.assertIn("restore the backup", record["message"])

    async def test_an_app_library_app_on_the_home_screen_is_a_violation(self):
        self.read_back(library=())
        record, answer = await self.finish({"state": "done", "result": {"status": "applied", "backup_name": "b1"}})
        self.assertEqual(record["state"], "library_violation")
        self.assertEqual(record["library_check"]["unexpected_on_home"], ["chess"])
        self.assertTrue(answer.is_error)

    async def test_rolled_back_and_rollback_failed_are_errors_with_the_backup(self):
        for status in ("rolled-back", "rollback-failed"):
            with self.subTest(status):
                record, answer = await self.finish({"state": "done", "result": {"status": status, "backup_name": "b1"}})
                self.assertEqual(record["state"], status.replace("-", "_"))
                self.assertEqual(record["backup"], "b1")
                self.assertTrue(answer.is_error)

    async def test_a_refusal_before_writing_is_not_written(self):
        record, answer = await self.finish({"state": "failed", "error": {"code": "other_device", "title": "Other"}})
        self.assertEqual(record["state"], "not_written")
        self.assertTrue(answer.is_error)

    async def test_an_unknown_failure_names_only_new_backups(self):
        record, answer = await self.finish({"state": "failed", "error": {"code": "write_failed", "title": "Lost"}})
        self.assertEqual(record["state"], "outcome_unknown")
        self.assertEqual(record["new_backups"], ["backup-new"])
        self.assertIsNone(record["backup"])
        self.assertTrue(answer.is_error)

    async def test_applied_without_a_read_back_says_the_check_is_incomplete(self):
        result = {"status": "applied", "backup_name": "b1", "reread_error": {"code": "x", "title": "No answer"}}
        record, answer = await self.finish({"state": "done", "result": result})
        self.assertEqual(record["state"], "applied_unverified")
        self.assertEqual(record["reread_error"], {"code": "x", "title": "No answer"})
        self.assertEqual(record["backup"], "b1")
        self.assertIsNone(record["matches_plan"])
        self.assertFalse(answer.is_error)
        self.assertIn("Call get_operation", answer.content[0].text)


def helper(support: Path) -> StdioServerParameters:
    """``python -m rejig_mcp`` as an AI client starts it, from this source tree; the app path
    does not exist, so it can never launch a real Rejig.app."""
    env = {**os.environ, "REJIG_SUPPORT_DIR": str(support), "REJIG_APP": str(support / "none.app")}
    return StdioServerParameters(command=sys.executable, args=["-m", "rejig_mcp"], env=env, cwd=Path(__file__).parents[1])


class HelperProcessTest(unittest.IsolatedAsyncioTestCase):
    async def test_two_helper_processes_applying_one_plan_write_once(self):
        support = Path(self.enterContext(tempfile.TemporaryDirectory()))
        demo = DemoEditor(support)
        self.addCleanup(demo.stop)

        async def approve(context, params):
            return ElicitResult(action="accept", content={"approve": True})

        async with Client(helper(support), elicitation_callback=approve) as one, Client(
            helper(support), elicitation_callback=approve
        ) as two:
            plan = await SessionTest.plan(self, one)
            results = await asyncio.gather(
                one.call_tool("apply_plan", {"plan_id": plan["plan_id"]}),
                two.call_tool("apply_plan", {"plan_id": plan["plan_id"]}),
            )
            final = await SessionTest.settle(self, two, plan["plan_id"])
        self.assertEqual(final["state"], "applied")
        backups = list(Path(Editor(support=support).state()["backup_dir"]).glob("*"))
        self.assertEqual(len(backups), 1)
        states = sorted(record_of(r).get("state") or data(r)["error"]["code"] for r in results)
        self.assertIn("applied", states)
        self.assertTrue(set(states) <= {"applied", "busy", "submitting", "writing"}, states)


class ApproverTest(unittest.TestCase):
    def test_no_setting_changes_the_approval_program(self):
        env = {**os.environ, "REJIG_APP": "/tmp/fake.app"}
        shown = subprocess.run(
            [sys.executable, "-c", "from rejig_mcp import server; print(server.editor.app, server.approver_command()[0])"],
            env=env, cwd=Path(__file__).parents[1], capture_output=True, text=True, check=True,
        ).stdout.split()
        self.assertEqual(shown, ["/tmp/fake.app", str(Path.home() / "Applications/Rejig.app/Contents/MacOS/Rejig")])


class StdioTest(unittest.IsolatedAsyncioTestCase):
    async def test_python_m_rejig_mcp_starts_and_lists_tools(self):
        support = Path(self.enterContext(tempfile.TemporaryDirectory()))
        params = helper(support)
        async with Client(params) as client:
            tools = (await client.list_tools()).tools
            status = await client.call_tool("get_status", {})
        self.assertEqual(len(tools), 12)
        self.assertEqual(data(status), {"app": {"running": False}})
        self.assertTrue({t.name: t for t in tools}["read_layout"].annotations.read_only_hint)


if __name__ == "__main__":
    unittest.main()
