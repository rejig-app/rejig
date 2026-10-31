"""The Rejig MCP companion: AI clients read, edit and (with a person's approval) apply a
Home Screen layout through the running Rejig app.

Writes need an Approval the model cannot give: an in-chat confirmation form (MCP
elicitation with a required ``approve`` field) or, for clients without forms, the Rejig
app's own Approve window. Every write goes through the app's editor, which saves a Backup
first, refuses a phone other than the one the plan was read from, and reads the layout back.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import json
import subprocess
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

import anyio
from mcp.server.mcpserver import (
    AcceptedElicitation,
    CancelledElicitation,
    Context,
    Elicit,
    ElicitationResult,
    MCPServer,
    Resolve,
)
from mcp_types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from ios_homescreen.model import describe, describe_changes, match_folders, to_document
from ios_homescreen.planner import plan_layout
from ios_homescreen.report import app_places, change_summary
from ios_homescreen.snapshots import device_of, load_pull, state_from_file
from ios_homescreen.state import fingerprint, to_payload
from ios_homescreen.storage import PULLS, find_backup

from . import draft as drafting
from .draft import DraftError, Drafts
from .editor import INSTALLED_APP, EditorError, Editor
from .optimize import optimize
from .plans import (
    ATTEMPTED,
    PlanError,
    backup_digest,
    change_lines,
    claim,
    library_check,
    library_moves,
    load_plan,
    read_journal,
    save_plan,
    write_journal,
)

INSTRUCTIONS = """\
Rejig arranges the Home Screen of an iPhone through the Rejig Mac app.
Rules:
- Work only through these tools. Never use the ios-homescreen CLI, pymobiledevice3 or any
  other route to the phone.
- Flow: read_layout, then edit_draft (or optimize_draft), then preview_draft, then
  apply_plan. apply_plan asks the person to approve the exact change list; you cannot
  approve it for them. Never say a change is on the phone unless apply_plan or
  get_operation reports state "applied".
- If an apply_plan call fails or its answer is lost, call get_operation with the same
  plan_id. Never call apply_plan again for a plan that has started writing.
- Apps only in the App Library stay there unless the person asks to bring one to the Home
  Screen. Nothing is ever deleted or uninstalled; widgets, web clips and web apps are not
  removed.
- Usage for optimize_draft comes from the person (for example Settings > Screen Time >
  See All App & Website Activity). Never invent usage numbers.
"""

READ_WAIT = 180.0
WRITE_WAIT = 600.0
APPROVAL_WAIT = 45.0
PROGRESS_EVERY = 10.0
MAX_FORM_LINES = 60
MAX_ICONS = 24
# Failures the editor reports before it writes anything.
NOT_WRITTEN_CODES = frozenset(
    {
        "other_device",
        "layout_changed",
        "no_usb_device",
        "not_advertising",
        "no_pair_record",
        "stale_pair_record",
        "several_pair_records",
        "connection_failed",
    }
)
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False)
LOCAL_EDIT = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False)

server = MCPServer("rejig", instructions=INSTRUCTIONS)
editor = Editor()
drafts = Drafts()
approvals: dict[str, subprocess.Popen[bytes]] = {}


class Approve(BaseModel):
    approve: bool = Field(title="Approve", description="Yes writes exactly these changes to the iPhone.")


# Output schemas: the keys a client can rely on in a successful answer; answers may carry more.
# Errors are {"error": {code, message, ...}} with isError set.
class Output(BaseModel):
    model_config = ConfigDict(extra="allow")


class Status(Output):
    app: dict[str, Any]
    connection: str | None = None
    device: dict[str, Any] | None = None
    read: dict[str, Any] | None = None
    ui: dict[str, Any] | None = None
    last_operation: dict[str, Any] | None = None


class Layout(Output):
    pull: str
    draft_id: str
    revision: int
    dock: list[dict[str, Any]]
    pages: list[list[dict[str, Any]]]
    library: list[dict[str, Any]]
    counts: dict[str, int]


class Apps(Output):
    pull: str
    apps: list[dict[str, Any]]
    library_only_count: int


class Icons(Output):
    returned: list[str]
    missing: list[str]
    pending: bool


class DraftRevision(Output):
    draft_id: str
    revision: int
    valid: bool | None = None
    issues: list[dict[str, Any]] | None = None
    changes: list[str] | None = None
    summary: str | None = None
    library_moves: dict[str, list[str]] | None = None
    improved: bool | None = None
    coverage: dict[str, Any] | None = None
    swipe_estimate: dict[str, float | None] | None = None


class PlanPreview(Output):
    plan_id: str
    kind: Literal["layout", "restore"]
    changes: list[str]
    summary: str


class Operation(Output):
    operation: dict[str, Any]


class Backups(Output):
    backups: list[dict[str, Any]]


@dataclass
class Gate:
    """What apply_plan's approval step decided before any question is asked."""

    route: str  # "mac_app" | "journal" | "error"
    lines: list[str] = field(default_factory=list)
    title: str = ""
    record: dict[str, Any] | None = None
    code: str = ""
    message: str = ""


class Failure(Exception):
    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.extra = extra


# ---------------------------------------------------------------------- helpers


def ok(text: str, data: dict[str, Any], extra: list[Any] | None = None) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=text), *(extra or [])], structured_content=data
    )


def fail(code: str, message: str, **extra: Any) -> CallToolResult:
    error = {"code": code, "message": message, **extra}
    return CallToolResult(
        content=[TextContent(type="text", text=f"{code}: {message}")],
        structured_content={"error": error},
        is_error=True,
    )


def guarded(fn: Callable[..., Awaitable[CallToolResult]]) -> Callable[..., Awaitable[CallToolResult]]:
    @functools.wraps(fn)
    async def run(*args: Any, **kwargs: Any) -> CallToolResult:
        try:
            return await fn(*args, **kwargs)
        except (EditorError, Failure) as error:
            return fail(error.code, str(error), **error.extra)
        except (DraftError, PlanError) as error:
            extra = {"index": error.index} if getattr(error, "index", None) is not None else {}
            return fail(error.code, str(error), **extra)

    return run


async def call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))


def device_id(udid: str | None) -> str | None:
    """A stand-in for the UDID: the model never sees the phone's real identifier."""
    return hashlib.sha256(udid.encode()).hexdigest()[:12] if udid else None


def mcp_root() -> Path:
    """Plans and journals: beside the bridge file, so they outlive editor restarts."""
    return editor.support / "mcp"


def current_read(state: dict[str, Any]) -> dict[str, Any]:
    read = state.get("read")
    if not read or not read.get("file"):
        raise Failure("no_read", "Nothing has been read from the phone yet; call read_layout.")
    return read


def udid_of(read: dict[str, Any]) -> str:
    return str((read["view"].get("device") or {}).get("udid") or "")


def compact(item: dict[str, Any]) -> dict[str, Any]:
    """An item as the model needs it: id, kind, name, and what is inside a folder."""
    kind = item.get("type", "app")
    out: dict[str, Any] = {"id": item["id"], "type": kind, "name": item.get("name", "")}
    if kind == "folder":
        out["pages"] = [[compact(x) for x in page] for page in item.get("pages", [])]
    elif kind == "widget":
        out["size"] = item.get("size")
    elif kind == "app" and item.get("offloaded"):
        out["offloaded"] = True
    return out


def layout_view(layout: dict[str, Any]) -> dict[str, Any]:
    return {
        "dock": [compact(x) for x in layout["dock"]],
        "pages": [[compact(x) for x in page] for page in layout["pages"]],
    }


async def wait_job(job_id: str, seconds: float, ctx: Context | None) -> dict[str, Any]:
    deadline = time.monotonic() + seconds
    seen, last_progress = 0, 0.0
    while True:
        job = (await call(editor.request, "GET", f"/api/job?since={seen}")).get("job")
        if not job or job.get("id") != job_id:
            raise Failure("job_lost", "The Rejig editor no longer knows this job; call get_operation.")
        seen = job.get("event_count", seen)
        if job.get("state") != "running":
            return job
        now = time.monotonic()
        if ctx is not None and now - last_progress >= PROGRESS_EVERY:
            last_progress = now
            events = job.get("events") or [{}]
            await ctx.report_progress(now - deadline + seconds, None, events[-1].get("message"))
        if now > deadline:
            raise Failure("timeout", "The phone is taking too long; call get_operation later.")
        await anyio.sleep(0.25)


def job_failure(job: dict[str, Any]) -> Failure:
    error = job.get("error") or {}
    steps = error.get("steps") or []
    return Failure(str(error.get("code") or "failed"), str(error.get("title") or "Failed"), steps=steps)


async def read_now(ctx: Context | None) -> dict[str, Any]:
    """Read the phone through the app and return the editor state."""
    await call(editor.require_clean)
    running = await call(editor.current_job)
    if running and running.get("state") == "running":  # e.g. the read the app starts at launch
        await wait_job(running["id"], READ_WAIT, ctx)
    job = (await call(editor.request, "POST", "/api/read", {}))["job"]
    final = await wait_job(job["id"], READ_WAIT, ctx)
    if final.get("state") != "done":
        raise job_failure(final)
    return await call(editor.state)


async def check(layout: dict[str, Any]) -> dict[str, Any]:
    """The editor's own validation of a layout: issues, changes, summary, dirty."""
    return await call(
        editor.request, "POST", "/api/plan", {"layout": drafting.to_layout(layout), "remove": []}
    )


def draft_answer(current: drafting.Draft, checked: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    changes = checked.get("changes") or []
    read = state.get("read") or {}
    return {
        "draft_id": current.id,
        "revision": current.revision,
        "valid": bool(checked.get("ok")),
        "issues": checked.get("issues") or [],
        "changes": change_lines(changes),
        "summary": change_summary(changes),
        "library_moves": library_moves(changes),
        "read_changed": read.get("file") != current.base.get("pull"),
    }


def backup_view(state: dict[str, Any], name: str) -> tuple[Path, dict[str, Any], str]:
    """A backup as an editor view (folders matched to the current read's; its fingerprint is
    what the phone reads back once restored) and the digest of what restoring it writes,
    both from one reading of the file."""
    read = current_read(state)
    path = find_backup(Path(state["backup_dir"]), name, allow_paths=False)
    saved = state_from_file(path)
    pull = load_pull(Path(state["data_dir"]) / PULLS / read["file"])
    view = describe({"state": saved, "apps": pull.get("apps", {})})
    match_folders(read["view"], view, prefix="backup")
    return path, view, backup_digest(to_payload(saved))


def target_fingerprint(state: dict[str, Any], layout: dict[str, Any]) -> str:
    """What the phone reads back once ``layout`` is written: the fingerprint of the engine's
    plan of it against the current read, made as the editor makes it (empty pages dropped)."""
    pull = load_pull(Path(state["data_dir"]) / PULLS / current_read(state)["file"])
    kept = {"dock": layout["dock"], "pages": [page for page in layout["pages"] if page]}
    return fingerprint(plan_layout(to_document(kept), pull, allow_removal=True).payload)


def verify(record: dict[str, Any], plan: dict[str, Any], state: dict[str, Any], success: str) -> dict[str, Any]:
    """``record`` with the companion's own checks of the phone as read in ``state``: the
    iPhone the plan was made for, the approved target (the engine's fingerprint, frozen in the
    plan) and the App Library-only apps. Its state is ``success`` only when every check passes."""
    read = current_read(state)
    if udid_of(read) != plan["device_udid"]:
        raise Failure(
            "other_device",
            "A different iPhone is connected now; connect the iPhone this plan was for and call get_operation again.",
        )
    moves = plan.get("library_moves") or {"to_home": [], "to_library": []}
    library = [a["id"] for a in read["view"]["library"]]
    checked = {
        **record,
        "state": success,
        "message": None,
        "new_pull": read["file"],
        "matches_plan": read["view"].get("fingerprint") == plan["target_fingerprint"],
        "library_check": library_check(record.get("library_before", []), library, **moves),
    }
    if not checked["matches_plan"]:
        checked["state"] = "readback_mismatch"
        checked["message"] = "The phone was written, but it reads back different from the approved plan; check it, and restore the backup if needed."
    elif not checked["library_check"]["ok"]:
        checked["state"] = "library_violation"
        checked["message"] = "App Library apps did not stay as planned; restore the backup."
    return checked


async def plan_check(bridge: dict[str, Any], plan: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Preconditions for writing ``plan`` now, and its change list. Raises Failure."""
    if bridge.get("dirty"):
        raise Failure("app_has_unsaved_edits", "The Rejig window has unsaved edits; nothing was done.")
    state = await call(editor.state)
    if (state.get("job") or {}).get("state") == "running":
        raise Failure("busy", "Rejig is busy with another operation; try again shortly.")
    read = current_read(state)
    if udid_of(read) != plan["device_udid"]:
        raise Failure("other_device", "This plan was made for a different iPhone; nothing was done.")
    if read["file"] != plan["base_pull"]:
        raise Failure("read_changed", "The phone was read again after this plan; preview again.")
    if plan["kind"] == "restore":
        _, view, digest = backup_view(state, plan["backup"])
        if digest != plan["backup_digest"]:
            raise Failure("backup_changed", "The backup changed after this plan was made; preview again.")
        return state, describe_changes(read["view"], view)
    checked = await check(drafting.from_view(plan["layout"]))
    if not checked.get("ok"):
        raise Failure("plan_invalid", "The editor no longer accepts this layout.", issues=checked.get("issues"))
    return state, checked.get("changes") or []


FORMLESS_CLIENTS = {"codex-mcp-client"}


def form_supported(ctx: Context) -> bool:
    params = ctx.session.client_params
    # shortcut: Codex 0.166.0 advertises form elicitation but its TUI cancels every form unseen; drop this when a Codex release shows them.
    if params is not None and params.client_info.name in FORMLESS_CLIENTS:
        return False
    capabilities = ctx.client_capabilities
    elicitation = capabilities.elicitation if capabilities is not None else None
    # An empty elicitation capability means form mode (spec 2025-11-25).
    return elicitation is not None and (elicitation.form is not None or elicitation.url is None)


def approval_text(state: dict[str, Any], lines: list[str], plan_id: str) -> str:
    name = (state.get("device") or {}).get("name") or "the iPhone"
    return f"Write these {len(lines)} changes to {name}? A backup is saved first. (Plan {plan_id[:12]})"


def operation(record: dict[str, Any]) -> CallToolResult:
    data = {"operation": record}
    state = record.get("state")
    if state in {"applied", "applied_reconciled"}:
        return ok(f"Applied; the phone was read back. Backup: {record.get('backup')}.", data)
    if state == "applied_unverified":
        return ok(
            "Written: Rejig's editor verified the new layout on the phone, but the companion could not "
            "read the phone back for its own checks (the approved layout, App Library apps). Call "
            f"get_operation with this plan_id to retry those checks; nothing is written again. Backup: {record.get('backup')}.",
            data,
        )
    if state in {"submitting", "writing", "awaiting_approval"}:
        return ok(f"Still {state.replace('_', ' ')}; call get_operation again shortly.", data)
    message = record.get("message") or f"The write ended {state}."
    return fail(str(state), str(message), operation=record)


# ---------------------------------------------------------------------- approval


def approver_command() -> list[str]:
    """The installed app's Approve window, at its fixed path: no setting can swap in another
    program that would answer for the person."""
    return [str(INSTALLED_APP / "Contents/MacOS/Rejig"), "--approve"]


async def mac_approval(plan_id: str, title: str, lines: list[str], ctx: Context, root: Path) -> str:
    """``approved``, ``declined``, ``closed`` or ``pending`` (the window is still open)."""
    child = approvals.get(plan_id)
    if child is None:
        if not lines:
            raise Failure("approval_lost", "The approval window was lost; call apply_plan again.")
        try:
            child = subprocess.Popen(approver_command(), stdin=subprocess.PIPE)
        except OSError as error:
            raise Failure("approval_unavailable", f"Cannot open Rejig's approval window: {error}") from error
        assert child.stdin is not None
        child.stdin.write(json.dumps({"title": title, "lines": lines}).encode())
        child.stdin.close()
        approvals[plan_id] = child
        write_journal(root, plan_id, {"plan_id": plan_id, "state": "awaiting_approval", "since": time.time()})
    deadline = time.monotonic() + APPROVAL_WAIT
    last_progress = time.monotonic()
    finished = False
    try:
        while child.poll() is None:
            if time.monotonic() > deadline:
                finished = True
                return "pending"
            if time.monotonic() - last_progress >= PROGRESS_EVERY:
                last_progress = time.monotonic()
                await ctx.report_progress(0, None, "Waiting for the person to approve in the Rejig window")
            await anyio.sleep(0.25)
        finished = True
    finally:
        if not finished:  # cancelled by the client before anything was sent: close the window
            child.kill()
            approvals.pop(plan_id, None)
            write_journal(root, plan_id, {"plan_id": plan_id, "state": "cancelled"})
    approvals.pop(plan_id, None)
    return {0: "approved", 1: "declined"}.get(child.returncode, "closed")


async def decide(plan_id: str, approval: str, ctx: Context) -> Any:
    """Resolver for apply_plan: the checks before asking, then the question (or a route)."""
    try:
        bridge = await call(editor.ensure_running)
        root = mcp_root()
        plan = load_plan(root, plan_id)
        record = read_journal(root, plan_id) or {}
        if record.get("state") in ATTEMPTED:
            return Gate("journal", record=record)
        if record.get("state") == "awaiting_approval" and plan_id in approvals:
            return Gate("mac_app")
        state, changes = await plan_check(bridge, plan)
    except (EditorError, Failure, PlanError) as error:
        return Gate("error", code=error.code, message=str(error))
    lines = change_lines(changes)
    if not lines:
        return Gate("error", code="unchanged", message="The phone already has this layout.")
    title = approval_text(state, lines, plan_id)
    if approval == "mac_app" or not form_supported(ctx) or len(lines) > MAX_FORM_LINES:
        return Gate("mac_app", lines=lines, title=title)
    return Elicit(f"{title}\n\n" + "\n".join(lines), Approve)


# ---------------------------------------------------------------------- writing


def written_state(job: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if job.get("state") == "done":
        result = job.get("result") or {}
        status = str(result.get("status", "outcome_unknown")).replace("-", "_")
        return status, result
    error = job.get("error") or {}
    code = str(error.get("code") or "failed")
    return ("not_written" if code in NOT_WRITTEN_CODES else "outcome_unknown"), {"error": error}


async def backup_names() -> list[str]:
    return [b.get("name") for b in (await call(editor.request, "GET", "/api/backups")).get("backups") or []]


async def finish(plan: dict[str, Any], job: dict[str, Any], attempt: dict[str, Any]) -> dict[str, Any]:
    """Journal what a finished editor job did to this plan's write, checked by reading back."""
    state, result = written_state(job)
    record: dict[str, Any] = {
        **attempt,
        "job_id": job.get("id"),
        "state": state,
        "backup": result.get("backup_name"),
        "write_unconfirmed": result.get("write_unconfirmed"),
        "finished": time.time(),
    }
    if "error" in result:
        record["message"] = (result["error"] or {}).get("title")
        record["error_code"] = (result["error"] or {}).get("code")
    if state == "applied" and result.get("reread_error"):
        # The engine verified the write itself; only the companion's read back failed.
        record |= {"state": "applied_unverified", "reread_error": result["reread_error"], "matches_plan": None}
    elif state == "applied":
        record = verify(record, plan, await call(editor.state), "applied")
    if record["state"] == "outcome_unknown":
        known = set(attempt.get("backups_before") or [])
        record["new_backups"] = [name for name in await backup_names() if name not in known]
    return write_journal(mcp_root(), plan["plan_id"], record)


async def write(plan: dict[str, Any], ctx: Context) -> CallToolResult:
    """Claim the plan's one write attempt, send it to the editor and journal the outcome."""
    root = mcp_root()
    bridge = await call(editor.bridge)
    state, _ = await plan_check(bridge, plan)  # again: time has passed while the person decided
    attempt = {
        "plan_id": plan["plan_id"],
        "kind": plan["kind"],
        "state": "submitting",
        "job_id": None,
        "library_before": [a["id"] for a in current_read(state)["view"]["library"]],
        "backups_before": await backup_names(),
        "submitted": time.time(),
    }
    existing = await call(claim, root, plan["plan_id"], attempt)
    if existing is not None:  # another call or helper is already writing this plan
        return operation(existing)
    body: dict[str, Any] = {"expect_read": plan["base_pull"]}
    try:
        if plan["kind"] == "restore":
            body |= {"name": plan["backup"], "expect_digest": plan["backup_digest"]}
            job = (await call(editor.request, "POST", "/api/backups/restore", body))["job"]
        else:
            body |= {"layout": plan["layout"], "remove": []}
            job = (await call(editor.request, "POST", "/api/apply", body))["job"]
    except EditorError as error:
        if error.code == "app_not_running":  # no answer: the request may still have reached it
            raise Failure(
                "outcome_unknown", "Rejig did not answer the write request; call get_operation to find out what happened."
            ) from error
        write_journal(root, plan["plan_id"], {**attempt, "state": "refused", "error_code": error.code, "message": str(error)})
        raise
    attempt = write_journal(root, plan["plan_id"], {**attempt, "state": "writing", "job_id": job["id"]})
    # A client cancel from here on leaves the editor's job running; get_operation finds it.
    final = await wait_job(job["id"], WRITE_WAIT, ctx)
    return operation(await finish(plan, final, attempt))


# ---------------------------------------------------------------------- tools


@server.tool(annotations=READ_ONLY)
@guarded
async def get_status() -> Annotated[CallToolResult, Status]:
    """Whether the Rejig app is running, its connection (Wi-Fi, USB or Demo), the phone, the
    last read, whether the app is busy or has unsaved edits, and any unfinished operation."""
    try:
        bridge = await call(editor.bridge)
    except EditorError:
        return ok("Rejig is not running; read_layout opens it.", {"app": {"running": False}})
    state = await call(editor.state)
    read = state.get("read")
    data = {
        "app": {"running": True, "version": state.get("version")},
        "connection": bridge.get("connection"),
        "device": {**(state.get("device") or {}), "id": device_id(udid_of(read)) if read else None},
        "read": {"pull": read.get("file"), "at": read.get("at"), "fingerprint": read["view"].get("fingerprint")}
        if read
        else None,
        "ui": {"busy": (state.get("job") or {}).get("state") == "running", "dirty": bool(bridge.get("dirty"))},
        "last_operation": last_operation(mcp_root()),
    }
    return ok(f"Rejig is running over {bridge.get('connection')}.", data)


def last_operation(root: Path) -> dict[str, Any] | None:
    journals = sorted((root / "operations").glob("*.json"), key=lambda p: p.stat().st_mtime)
    for path in reversed(journals):
        record = json.loads(path.read_text())
        if record.get("state") in {"submitting", "writing", "awaiting_approval", "outcome_unknown", "applied_unverified"}:
            return record
    return None


@server.tool(annotations=LOCAL_EDIT)
@guarded
async def select_connection(transport: Literal["wifi", "usb", "demo"]) -> Annotated[CallToolResult, Status]:
    """Switch the Rejig app to Wi-Fi, USB, or Demo (a pretend iPhone: nothing real is touched)."""
    bridge = await call(editor.require_clean)
    if bridge.get("connection") == transport:
        return await get_status()
    state = await call(editor.state)
    if (state.get("job") or {}).get("state") == "running":
        raise Failure("app_busy", "Rejig is busy; try again when the current operation ends.")
    old_port = bridge["port"]
    subprocess.run(["/usr/bin/open", f"rejig://connect?transport={transport}"], check=False, capture_output=True)
    await call(
        editor.wait_for,
        lambda b: b.get("connection") == transport and b["port"] != old_port,
        20.0,
        "switch_timeout",
        "Rejig did not switch connection.",
    )
    return await get_status()


@server.tool(annotations=READ_ONLY)
@guarded
async def read_layout(ctx: Context, use_last: bool = False) -> Annotated[CallToolResult, Layout]:
    """Read the iPhone's Home Screen (read-only) and start a draft from it. use_last reuses the
    app's most recent read instead of reading again. Returns the dock, pages, folders, widgets,
    App Library-only apps and a draft_id at revision 0."""
    if use_last:
        await call(editor.ensure_running)
        state = await call(editor.state)
    else:
        state = await read_now(ctx)
    read = current_read(state)
    view = read["view"]
    apps = {a["id"]: a["name"] for a in read.get("apps", [])}
    library = [a["id"] for a in view.get("library", [])]
    bridge = await call(editor.bridge)
    current = drafts.create(
        view,
        apps,
        base={"pull": read["file"], "udid": udid_of(read), "library": library, "connection": bridge.get("connection")},
    )
    data = {
        "pull": read["file"],
        "device": {**(state.get("device") or {}), "id": device_id(udid_of(read))},
        "connection": bridge.get("connection"),
        **layout_view(current.layout),
        "library": [{"id": a["id"], "name": a["name"]} for a in view.get("library", [])],
        "counts": {"dock": len(view["dock"]), "pages": len(view["pages"]), "library_only": len(library)},
        "draft_id": current.id,
        "revision": 0,
    }
    return ok(
        f"Read {data['counts']['pages']} pages, {data['counts']['dock']} dock items and "
        f"{len(library)} App Library-only apps. Draft {current.id} is at revision 0.",
        data,
    )


@server.tool(annotations=READ_ONLY)
@guarded
async def list_apps(query: str | None = None, section: Literal["home", "library", "all"] = "all") -> Annotated[CallToolResult, Apps]:
    """Every app the person can see and where it is (dock, page, folder or App Library only),
    from the last read. query filters by name or id."""
    await call(editor.ensure_running)
    state = await call(editor.state)
    read = current_read(state)
    places = app_places(load_pull(Path(state["data_dir"]) / PULLS / read["file"]))
    wanted = []
    for place in places:
        where = place["where"]["section"]
        if section == "home" and where == "library" or section == "library" and where != "library":
            continue
        if query and query.casefold() not in f"{place['name']} {place['id']}".casefold():
            continue
        wanted.append(place)
    library_only = sum(1 for p in places if p["where"]["section"] == "library")
    data = {"pull": read["file"], "apps": wanted, "library_only_count": library_only}
    return ok(f"{len(wanted)} apps ({library_only} App Library-only on the phone).", data)


@server.tool(annotations=READ_ONLY)
@guarded
async def get_app_icons(ids: list[str]) -> Annotated[CallToolResult, Icons]:
    """App icons as images (up to 24 ids per call), so you can see their colors."""
    if not ids or len(ids) > MAX_ICONS:
        raise Failure("bad_request", f"Ask for 1 to {MAX_ICONS} icons.")
    await call(editor.ensure_running)
    state = await call(editor.state)
    names = {a["id"]: a["name"] for a in (current_read(state).get("apps") or [])}
    blocks: list[Any] = []
    returned, missing = [], []
    for ident in dict.fromkeys(ids):
        found = await call(editor.request, "GET", f"/api/icons/{ident}", raw=True) if ident.isprintable() else None
        if not found or not found[1].startswith(("image/png", "image/jpeg")):
            missing.append(ident)
            continue
        data, kind = found
        blocks.append(TextContent(type="text", text=f"{names.get(ident, ident)} ({ident})"))
        blocks.append(ImageContent(type="image", data=base64.b64encode(data).decode(), mime_type=kind.split(";")[0]))
        returned.append(ident)
    pending = bool((state.get("icons") or {}).get("pending"))
    return ok(
        f"{len(returned)} icons." + (" Some icons are still loading; ask again shortly." if pending else ""),
        {"returned": returned, "missing": missing, "pending": pending},
        blocks,
    )


@server.tool(annotations=LOCAL_EDIT)
@guarded
async def edit_draft(draft_id: str, base_revision: int, operations: list[dict[str, Any]]) -> Annotated[CallToolResult, DraftRevision]:
    """Edit a draft (nothing touches the phone). operations run in order, all or nothing; each
    is one object whose "op" names it, e.g. {"op": "move", "item": ID, "to": {...}}:
    move {item, to:{section: dock|page|folder, page?, index?, folder?}} (an App Library app id
    brings it to the Home Screen; 1-based page and index; page N+1 makes a new page);
    send_to_library {items}; folder_create {name, items, to?}; folder_rename {folder, name};
    folder_dissolve {folder}; page_add {at?}; page_remove {page} (empty pages only);
    page_move {from, to}; undo {to_revision} (alone). Items are ids from read_layout; a folder
    may also be named exactly. Returns the new revision, whether Rejig accepts it, and the
    change list."""
    current = drafts.edit(draft_id, base_revision, operations)
    state = await call(editor.state)
    checked = await check(current.layout)
    data = draft_answer(current, checked, state)
    note = "" if data["valid"] else f" Not writable yet: {len(data['issues'])} issues."
    return ok(f"Draft {current.id} revision {current.revision}: {data['summary']}{note}", data)


@server.tool(annotations=LOCAL_EDIT)
@guarded
async def preview_draft(draft_id: str, revision: int) -> Annotated[CallToolResult, PlanPreview]:
    """Freeze a draft revision into a plan for this phone and its last read: the exact change
    list the person will be asked to approve, and a plan_id for apply_plan."""
    current = drafts.get(draft_id)
    if revision != current.revision:
        raise DraftError("revision_conflict", f"The draft is at revision {current.revision}.")
    await call(editor.ensure_running)
    state = await call(editor.state)
    read = current_read(state)
    if read["file"] != current.base["pull"]:
        raise Failure("read_changed", "The phone was read again after this draft; call read_layout.")
    if udid_of(read) != current.base["udid"]:
        raise Failure("other_device", "This draft came from a different iPhone.")
    checked = await check(current.layout)
    if not checked.get("ok"):
        raise Failure("layout_invalid", "Rejig cannot write this layout.", issues=checked.get("issues"))
    changes = checked.get("changes") or []
    if not changes:
        raise Failure("unchanged", "The draft is the same as the phone.")
    plan = {
        "kind": "layout",
        "device_udid": current.base["udid"],
        "base_pull": read["file"],
        "base_fingerprint": read["view"].get("fingerprint"),
        "layout": drafting.to_layout(current.layout),
        "target_fingerprint": target_fingerprint(state, current.layout),
        "backup": None,
        "backup_digest": None,
        "library_moves": library_moves(changes),
    }
    plan_id = save_plan(mcp_root(), plan)
    lines = change_lines(changes)
    data = {
        "plan_id": plan_id,
        "kind": "layout",
        "device": (state.get("device") or {}).get("name"),
        "base_pull": read["file"],
        "changes": lines,
        "summary": change_summary(changes),
        "library_moves": plan["library_moves"],
        "library_only_before": len(read["view"].get("library", [])),
    }
    return ok(f"Plan {plan_id[:12]}: {data['summary']}\n" + "\n".join(lines), data)


@server.tool(annotations=READ_ONLY)
@guarded
async def preview_restore(ctx: Context, backup: str) -> Annotated[CallToolResult, PlanPreview]:
    """Plan restoring a backup (a name from list_backups): reads the phone first, then shows
    what restoring would change. Apply it with apply_plan."""
    await call(editor.require_clean)
    if backup not in await backup_names():
        raise Failure("no_backup", f"There is no backup named {backup!r}; call list_backups.")
    state = await read_now(ctx)
    read = current_read(state)
    path, view, digest = backup_view(state, backup)
    if (device_of(path).get("udid") or "") != udid_of(read):
        raise Failure("other_device", "That backup came from a different iPhone; it is never restored here.")
    changes = describe_changes(read["view"], view)
    if not changes:
        raise Failure("unchanged", "The phone already matches this backup.")
    plan = {
        "kind": "restore",
        "device_udid": udid_of(read),
        "base_pull": read["file"],
        "base_fingerprint": read["view"].get("fingerprint"),
        "layout": None,
        "target_fingerprint": view["fingerprint"],
        "backup": backup,
        "backup_digest": digest,
        "library_moves": library_moves(changes),
    }
    plan_id = save_plan(mcp_root(), plan)
    lines = change_lines(changes)
    data = {"plan_id": plan_id, "kind": "restore", "backup": backup, "changes": lines, "summary": change_summary(changes)}
    return ok(f"Plan {plan_id[:12]} restores {backup}: {data['summary']}\n" + "\n".join(lines), data)


@server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True))
@guarded
async def apply_plan(
    ctx: Context,
    plan_id: str,
    decision: Annotated[ElicitationResult[Approve], Resolve(decide)],
    approval: Literal["auto", "mac_app"] = "auto",
) -> Annotated[CallToolResult, Operation]:
    """Ask the person to approve a plan's exact change list, then write it to the iPhone:
    backup first, then the write, then a read-back. approval "auto" asks in this chat when
    the client supports it, otherwise in the Rejig app's window; "mac_app" always uses the
    window. If it returns awaiting_approval, call it again with the same plan_id to keep
    waiting. A repeated call never writes twice."""
    await call(editor.ensure_running)
    root = mcp_root()
    plan = load_plan(root, plan_id)
    if isinstance(decision, AcceptedElicitation) and isinstance(decision.data, Gate):
        gate = decision.data
        if gate.route == "error":
            raise Failure(gate.code, gate.message)
        if gate.route == "journal":
            return operation(gate.record or {})
        answer = await mac_approval(plan_id, gate.title, gate.lines, ctx, root)
        if answer == "pending":
            write_journal(root, plan_id, {"plan_id": plan_id, "state": "awaiting_approval"})
            return ok(
                "Waiting for the person to click Approve or Decline in the Rejig window. Call "
                "apply_plan again with the same plan_id to keep waiting.",
                {"operation": {"plan_id": plan_id, "state": "awaiting_approval"}},
            )
        if answer != "approved":
            state = "declined" if answer == "declined" else "approval_closed"
            write_journal(root, plan_id, {"plan_id": plan_id, "state": state})
            raise Failure(f"approval_{answer}", "The person did not approve; nothing was written.")
        return await write(plan, ctx)
    if isinstance(decision, AcceptedElicitation) and decision.data.approve is True:
        return await write(plan, ctx)
    if isinstance(decision, CancelledElicitation):
        write_journal(root, plan_id, {"plan_id": plan_id, "state": "cancelled"})
        raise Failure(
            "approval_cancelled",
            "No approval arrived (the form was cancelled or this client cannot show it); nothing "
            'was written. Ask the person before retrying with approval "mac_app", which asks in '
            "the Rejig window.",
        )
    write_journal(root, plan_id, {"plan_id": plan_id, "state": "declined"})
    raise Failure(
        "approval_declined",
        "The approval was declined; nothing was written. If the person says they saw no form "
        '(some clients decline on their own), ask them before retrying with approval "mac_app".',
    )


@server.tool(annotations=READ_ONLY)
@guarded
async def get_operation(ctx: Context, plan_id: str) -> Annotated[CallToolResult, Operation]:
    """What happened to a plan's write: applied, rolled back, still running, or unknown, with
    the backup's name. Use it after a lost or failed apply_plan instead of applying again."""
    await call(editor.ensure_running)
    root = mcp_root()
    plan = load_plan(root, plan_id)
    record = read_journal(root, plan_id)
    if record is None:
        return ok("This plan was never applied.", {"operation": {"plan_id": plan_id, "state": "none"}})
    if record.get("state") == "awaiting_approval" and plan_id not in approvals:
        record = write_journal(root, plan_id, {"plan_id": plan_id, "state": "approval_lost"})
    if record.get("state") == "applied_unverified":  # retry the companion's read back only
        state = await read_now(ctx)
        return operation(write_journal(root, plan_id, verify(record, plan, state, "applied")))
    if record.get("state") not in {"submitting", "writing"}:
        return operation(record)
    job = await call(editor.current_job)
    if job and record.get("job_id") and job.get("id") == record["job_id"]:
        if job.get("state") == "running":
            return operation(record)
        return operation(await finish(plan, job, record))
    if job and job.get("state") == "running" and not record.get("job_id"):  # maybe this plan's: wait
        return ok("Rejig is busy; call get_operation again shortly.", {"operation": record})
    # The editor no longer knows the write (it restarted, or the request's answer was lost):
    # read the phone. Only a phone showing the plan counts as evidence it was written.
    state = await read_now(ctx)
    known = set(record.get("backups_before") or [])
    unchanged = current_read(state)["view"].get("fingerprint") == plan["base_fingerprint"]
    found = {
        **record,
        "phone_matches_base": unchanged,
        "new_backups": [name for name in await backup_names() if name not in known],
    }
    reconciled = verify(found, plan, state, "applied_reconciled")  # refuses another iPhone
    if reconciled["state"] == "applied_reconciled":
        reconciled["message"] = "The phone shows this plan's layout."
    elif not reconciled["matches_plan"]:
        reconciled |= {
            "state": "outcome_unknown",
            "message": "The phone shows the layout from before this plan: the write did not happen, or it was rolled back."
            if unchanged
            else "The phone shows neither this plan nor the layout it was planned from; read it and check.",
        }
    return operation(write_journal(root, plan_id, reconciled))


@server.tool(annotations=READ_ONLY)
@guarded
async def list_backups(limit: int = 20) -> Annotated[CallToolResult, Backups]:
    """Backups Rejig saved before each write, newest first (names for preview_restore)."""
    await call(editor.ensure_running)
    backups = (await call(editor.request, "GET", "/api/backups")).get("backups") or []
    shown = []
    for backup in backups[: max(1, min(limit, 50))]:
        device = backup.get("device") or {}
        shown.append(
            {
                "name": backup.get("name"),
                "created": backup.get("created"),
                "device": {"name": device.get("name"), "id": device_id(device.get("udid"))},
                "counts": backup.get("counts"),
            }
        )
    return ok(f"{len(shown)} backups.", {"backups": shown})


@server.tool(annotations=LOCAL_EDIT)
@guarded
async def optimize_draft(
    draft_id: str,
    base_revision: int,
    usage: list[dict[str, Any]],
    period: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
) -> Annotated[CallToolResult, DraftRevision]:
    """Reorder a draft by usage the person gave you (for example Screen Time minutes):
    usage [{app: id or exact name, minutes}], period {from, to, days_with_data}. The most-used
    apps go first on page 1; the dock and its folders stay, App Library-only apps stay,
    nothing is removed. Returns the new revision, coverage (unmatched, ambiguous, apps with no
    data) and a usage-weighted navigation estimate (not measured swipes). Leaves the draft as
    it is when the reorder would not lower that estimate. Never invent usage."""
    current = drafts.get(draft_id)
    if base_revision != current.revision:
        raise DraftError("revision_conflict", f"The draft is at revision {current.revision}.")
    result = optimize(
        current.layout, usage, current.apps, current.base.get("library", []), {**(options or {}), "period": period}
    )
    estimate = result["swipe_estimate"]
    if estimate["before"] is None or estimate["after"] >= estimate["before"]:
        data = {"draft_id": current.id, "revision": current.revision, "improved": False, "coverage": result["coverage"], "swipe_estimate": estimate}
        why = (
            "no usage row matched a Home Screen app with minutes above 0"
            if estimate["before"] is None
            else f"reordering by this usage would not lower the usage-weighted navigation estimate ({estimate['before']})"
        )
        return ok(f"Draft {current.id} stays at revision {current.revision}: {why}.", data)
    current = drafts.add_revision(draft_id, result["layout"])
    state = await call(editor.state)
    checked = await check(current.layout)
    data = {
        **draft_answer(current, checked, state),
        "improved": True,
        "coverage": result["coverage"],
        "swipe_estimate": estimate,
    }
    return ok(
        f"Draft {current.id} revision {current.revision}: {data['summary']} Usage-weighted navigation "
        f"estimate: {estimate['before']} before, {estimate['after']} after.",
        data,
    )


def main() -> None:
    server.run()
