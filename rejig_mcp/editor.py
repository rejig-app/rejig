"""The running Rejig app's editor, reached through its loopback API.

The app writes ``mcp-bridge.json`` (mode 0600) next to its data while its editor runs:
``{port, token, pid, connection, dirty}``. Every request goes to that editor
with the same token, cookie and same-origin headers the editor page uses, so the companion
gets the page's rules, its one-job-at-a-time lock and its phone connection. The companion
never talks to the phone itself.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

SUPPORT = Path(os.environ.get("REJIG_SUPPORT_DIR") or Path.home() / "Library/Application Support/Rejig")
INSTALLED_APP = Path.home() / "Applications/Rejig.app"
# Which app to open when Rejig is not running; never the approval window (server.approver_command).
APP = Path(os.environ.get("REJIG_APP") or INSTALLED_APP)
LAUNCH_WAIT = 20.0
POLL = 0.25


class EditorError(Exception):
    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.extra = extra


class Editor:
    def __init__(self, support: Path = SUPPORT, app: Path = APP) -> None:
        self.support = support
        self.app = app
        self._session: tuple[int, str] | None = None
        self._opener: urllib.request.OpenerDirector | None = None

    # ------------------------------------------------------------------ bridge

    def bridge(self) -> dict[str, Any]:
        """The bridge file of the running app, or ``app_not_running``."""
        try:
            data = json.loads((self.support / "mcp-bridge.json").read_text())
            port, token, pid = int(data["port"]), str(data["token"]), int(data["pid"])
            os.kill(pid, 0)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise EditorError("app_not_running", "Rejig is not running.") from exc
        if not 0 < port < 65536 or not token:
            raise EditorError("app_not_running", "Rejig is not running.")
        return {**data, "port": port, "token": token, "pid": pid}

    def ensure_running(self) -> dict[str, Any]:
        """The bridge, opening the app first when it is not running."""
        try:
            return self.bridge()
        except EditorError:
            pass
        if not (self.app / "Contents/MacOS/Rejig").is_file():
            raise EditorError(
                "app_not_installed", f"Rejig is not installed at {self.app}; run scripts/install.sh."
            )
        subprocess.run(["/usr/bin/open", "-a", str(self.app)], check=False, capture_output=True)
        return self.wait_for(lambda bridge: True, LAUNCH_WAIT, "app_not_running", "Rejig did not start.")

    def wait_for(
        self, ready: Callable[[dict[str, Any]], bool], seconds: float, code: str, message: str
    ) -> dict[str, Any]:
        deadline = time.monotonic() + seconds
        while True:
            try:
                bridge = self.bridge()
                if ready(bridge):
                    self.request("GET", "/api/state")
                    return bridge
            except EditorError:
                pass
            if time.monotonic() > deadline:
                raise EditorError(code, message)
            time.sleep(POLL)

    def require_clean(self) -> dict[str, Any]:
        """The bridge, refusing while the app's page has unsaved edits."""
        bridge = self.ensure_running()
        if bridge.get("dirty"):
            raise EditorError(
                "app_has_unsaved_edits",
                "The Rejig window has unsaved layout edits. Ask the person to apply, export or "
                "discard them first; nothing was done.",
            )
        return bridge

    # ------------------------------------------------------------------ HTTP

    def _login(self, bridge: Mapping[str, Any]) -> urllib.request.OpenerDirector:
        key = (bridge["port"], bridge["token"])
        if self._session != key or self._opener is None:
            opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
                urllib.request.ProxyHandler({}),
            )
            base = f"http://127.0.0.1:{bridge['port']}"
            opener.open(f"{base}/?token={bridge['token']}", timeout=10).read()
            self._session, self._opener = key, opener
        return self._opener

    def request(self, method: str, path: str, body: Any = None, *, raw: bool = False) -> Any:
        bridge = self.bridge()
        base = f"http://127.0.0.1:{bridge['port']}"
        headers = {"X-IHS-Token": bridge["token"], "Origin": base}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
        try:
            with self._login(bridge).open(request, timeout=30) as response:
                payload = response.read()
                if raw:
                    return payload, response.headers.get("content-type", "")
                return json.loads(payload or b"null")
        except urllib.error.HTTPError as error:
            if raw and error.code == 404:
                return None
            try:
                found = json.loads(error.read() or b"{}")
            except ValueError:
                found = {}
            code = found.pop("code", None) or f"http_{error.code}"
            raise EditorError(code, str(found.pop("message", error.reason)), **found) from error
        except (urllib.error.URLError, OSError) as error:
            self._session = None
            raise EditorError("app_not_running", "Rejig's editor is not answering.") from error

    # ------------------------------------------------------------------ jobs

    def state(self) -> dict[str, Any]:
        return self.request("GET", "/api/state")

    def current_job(self) -> dict[str, Any] | None:
        return self.request("GET", "/api/job").get("job")
