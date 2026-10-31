# Rejig

Rejig is a Mac app for arranging the real Home Screen on your iPhone: pages, the dock,
folders, existing widgets, and which apps live only in the App Library. Nothing is
installed on the phone. Rejig lists every change before you apply it, saves a backup
first, and reads the phone back afterwards to check the result.

It works over **Wi-Fi** after a one-time USB pairing, so day to day you don't need a cable.
USB works too, and **Demo** mode lets you try everything on a pretend phone.

![Rejig in Demo mode: App Library list, pretend iPhone and an empty change list](docs/screenshots/demo-library.png)

![Rejig in Demo mode with three pending changes listed before Apply](docs/screenshots/demo-changes.png)

*Screenshots use Demo mode: invented apps on a pretend phone, no real device.*

## What it does

- Reads the Home Screen layout from your iPhone over Wi-Fi or USB.
- Lets you drag apps, folders and existing widgets; add and remove pages; create folders;
  move apps to and from the App Library.
- Lists every change (moved, added, sent to the App Library) before you press **Apply**.
- Saves a layout backup before each write, then reads the phone again to confirm it holds
  what was written. If that check fails, it writes the backup back and checks again.
- Restores an earlier backup (**Backups**) after a confirmation that shows its date and
  item counts, then reads the phone back to check it. A restore does not list every item.
- Writes a backup or an edited layout only to the iPhone it came from. A different iPhone,
  or one whose identity is unknown, gets nothing.
- Imports and exports layouts as files.

## Requirements

- A Mac with Apple silicon (M1 or later) running macOS 15 or later.
- Xcode Command Line Tools: `xcode-select --install`
- [Homebrew](https://brew.sh) and Python 3.13: `brew install python@3.13`
- An internet connection during installation (it downloads about 120 Python packages).
- An iPhone on iOS 17 or later for Wi-Fi mode.

## Install

1. Get the source:

   ```bash
   git clone https://github.com/rejig-app/rejig.git
   cd rejig
   ```

2. Build and install the app and its Python runtime (a few minutes, mostly downloads):

   ```bash
   bash scripts/install.sh
   ```

   This builds `build/Rejig.app`, puts the app in `~/Applications/Rejig.app` and its
   runtime in `~/Library/Application Support/Rejig/.venv`. Dependencies are installed at
   the exact versions, with hash checks, listed in `requirements.lock` and
   `requirements-mcp.lock` (the MCP SDK for [AI clients](#connect-an-ai-client)).

3. Open it:

   ```bash
   open ~/Applications/Rejig.app
   ```

4. Choose **Demo** in the top bar to try it without a phone.

The app is built and signed ad hoc on your own Mac; it is not notarized. It needs both
the app and the runtime folder. If a Homebrew upgrade removes Python 3.13, run
`bash scripts/install.sh` again.

## Update

Quit Rejig, then from the `rejig` folder:

```bash
git pull
bash scripts/install.sh
```

The installer rebuilds the app and reinstalls the engine with the current patches. Your
layouts, backups and Wi-Fi pairing stay where they are.

## Uninstall

Quit Rejig. Removing it does not change anything on your iPhone.

1. Remove the app and its runtime. This keeps your layouts and backups:

   ```bash
   rm -rf ~/Applications/Rejig.app "$HOME/Library/Application Support/Rejig/.venv"
   ```

2. To also delete your saved layouts, backups, icon cache and logs, remove the rest of
   `~/Library/Application Support/Rejig`. The backups there are the only way to go back
   to an earlier layout: copy the `.plist` files in `data/backups/` somewhere else first
   if you want to keep them.
3. The Wi-Fi pairing is the phone's `remote_<UDID>.plist` file in pymobiledevice3's
   folder (see [Your data](#your-data)). Other pymobiledevice3 tools use that folder too;
   delete only that file if you no longer want this Mac paired.
4. Optional: the iPhone keeps trusting this Mac until you reset that on the phone:
   **Settings > General > Transfer or Reset iPhone > Reset > Reset Location & Privacy**.
   This resets trust for every computer and also resets location and privacy settings
   for your apps. Removing Rejig does not require it.

## Connect your iPhone

**USB:** plug the iPhone in, unlock it, tap **Trust This Computer**, choose **USB**, then
**Read from phone**.

**Wi-Fi (one-time setup):**

1. Connect the iPhone to this Mac with USB. Unlock it and accept **Trust This Computer**.
2. Click **Set Up Wi-Fi…**, then **Pair Connected iPhone**. This turns on wireless device
   connections and saves a pairing on the Mac.
3. Unplug the cable. Keep the phone unlocked and on the same network as the Mac.
4. Choose **Wi-Fi**, then **Read from phone**. Allow local network access if macOS asks.

Wi-Fi mode uses the engine's userspace RemotePairing tunnel. It needs neither root nor
Developer Mode. Use one iPhone at a time: the engine refuses to guess when several phones
are connected or paired.

## Connect an AI client

Rejig includes an MCP server, so Claude Code, Codex or Claude Desktop can read your Home
Screen, plan a new arrangement with you, and apply it once you approve. It talks to the
Rejig app on this Mac (the app opens if it is not running) and never to the phone directly.
Choose **Demo** in the app first to try it on the pretend phone.

The server command is the runtime's Python with `-m rejig_mcp`. Add it to your client:

**Claude Code:**

```bash
claude mcp add --scope user rejig -- "$HOME/Library/Application Support/Rejig/.venv/bin/python3" -m rejig_mcp
```

Check it with `claude mcp list` (rejig shows as connected) or `/mcp` in a session.

**Codex:**

```bash
codex mcp add rejig -- "$HOME/Library/Application Support/Rejig/.venv/bin/python3" -m rejig_mcp
```

Then give it time to wait for your approval and for the phone: in `~/.codex/config.toml`,
under the `[mcp_servers.rejig]` table this command created, add `tool_timeout_sec = 600`.
Check it with `codex mcp list` or `/mcp` in the Codex TUI.

**Claude Desktop:** open `~/Library/Application Support/Claude/claude_desktop_config.json`
(create it if needed) and add this; `sh` finds the runtime in your home folder, so it
pastes as is:

```json
{
  "mcpServers": {
    "rejig": {
      "command": "/bin/sh",
      "args": ["-c", "exec \"$HOME/Library/Application Support/Rejig/.venv/bin/python3\" -m rejig_mcp"]
    }
  }
}
```

If the file already has an `mcpServers` block, add the `"rejig"` entry inside it.

Quit and reopen Claude Desktop; rejig then appears in the chat's list of tools.

**Try it:**

- "Use Rejig to read my iPhone Home Screen. What is on page 1?"
- "Put my finance apps in a folder called Money on page 2 and show me the plan."
- "Apply that plan."
- "Here are my Screen Time minutes for last week: … Put the apps I use most on page 1."
- "Restore the backup from before that change."

**What needs your approval:** reading and drafting never touch the phone. Only applying
writes, and only after you approve the exact numbered change list: in the chat, when the
client can show a form (Claude Code can), or otherwise in Rejig's own Approve window on
the Mac running Rejig, where you click Approve. Rejig always uses the window for Codex:
in testing with Codex CLI 0.166.0, its TUI cancelled form requests without showing them.
So an unattended `codex exec` run cannot write until someone clicks the window. You can
always ask for the window ("approve it in the Rejig window").
Each write saves a backup first and reads the phone back. Nothing is ever deleted or
uninstalled, and apps that live only in the App Library stay there unless you ask.

Know where that approval stops. Any program running as you on this Mac can do what you can,
including answering a form or clicking a window, so the approval keeps a model from acting
on its own; it is not a lock against other software. Don't add Claude Code hooks that
answer MCP forms automatically.

While an AI client is reading or writing, the Rejig window waits for it to finish before
you can switch connection or quit; if its editor stops answering, Rejig asks before letting
you leave, since a write may still be running. If the window has unsaved edits, the client is told so
and does nothing until you apply, export or discard them.

## Your data

- Layouts and backups: `~/Library/Application Support/Rejig/data/`
- Cached app icons and wallpaper: `~/Library/Application Support/Rejig/cache/`
- AI client plans and their outcomes: `~/Library/Application Support/Rejig/mcp/`
- Connection errors: `~/Library/Application Support/Rejig/backend.log`
- Wi-Fi pairing output: `~/Library/Application Support/Rejig/pairing.log`
- Wi-Fi pairing credentials: pymobiledevice3's folder, `~/.local/share/pymobiledevice3/`
  (or `~/.pymobiledevice3/` if an older install created it). Treat these like passwords
  and don't share them.

Everything stays on your Mac. The editor inside the window listens only on `127.0.0.1`,
with a fresh session token and request checks; it is not reachable from your network.

Don't disconnect or force-quit while Rejig is applying or restoring. If the check and the
rollback both fail, Rejig says so and keeps the backup: reconnect and use **Backups**.
A layout backup is not a full iPhone backup.

## Checks

```bash
build/Rejig.app/Contents/MacOS/Rejig --self-test
build/Rejig.app/Contents/MacOS/Rejig --smoke-test
"$HOME/Library/Application Support/Rejig/.venv/bin/python3" -m unittest discover -s tests -v
```

The smoke test opens the real window on a pretend phone, adds and undoes a page, reorders
icons, opens the Apply preview, applies, checks the backup and readback, and restores the
backup. It never contacts a real phone. It saves `demo-preview.png` in the app's data folder.

The unit tests cover the Wi-Fi pairing proof format, the Wi-Fi reconnect patch
(recovery, same-phone check, USB failure, cancellation), the device identity patch
(Restore and Apply refuse another iPhone and still write to the right one) and the MCP
server. The MCP tests start a real Demo editor and drive the server with the MCP SDK's own
client, over both the older approval protocol and the 2026-07-28 one: approve, decline,
cancel, stale and changed plans, unsaved edits, two clients applying one plan, a lost
answer to a write, restores and recovery after a restart.

## Tested on

- Macs: Apple silicon on macOS 26.
- iPhone: an iPhone 16 Pro on iOS 27.0.1, over USB and Wi-Fi: layout reads, writes with
  readback, and a backup restore.
- AI clients: Claude Code 2.1.316 and Codex CLI 0.166.0 (TUI and `--yolo`) on Demo, and
  an MCP write and restore on that iPhone over Wi-Fi, approved in Rejig's window. Claude
  Desktop: the setup above is written but not tested yet.

That is one phone. Apple can change the private Home Screen service in any iOS release,
so try Demo first and keep the backup Rejig makes. Intel Macs, Windows and Linux are not
supported.

## Limits

- The installer applies four patches to the engine. `patches/wifi-reconnect.patch`: a
  layout write can close the Wi-Fi tunnel, and the patch reopens it for the same phone
  only. `patches/device-identity.patch`: Restore and Apply check that the connected phone
  is the one the backup or layout came from before writing.
  `patches/offloaded-apps.patch`: the app list includes offloaded apps, so a write keeps
  the ones that are only in the App Library there instead of putting them back on a page.
  `patches/mcp-companion.patch`: a write planned against one read is refused once the
  phone has been read again or changed, and a restore is refused if its backup changed
  after you approved it.
- The MCP server arranges the layout only. It does not uninstall apps, create widgets or
  read Screen Time; give it usage numbers yourself if you want it to sort by them.
- Rejig moves existing widgets and stacks; it cannot create new widgets.
- `srptools` stays at 1.0.1: version 2 reads pymobiledevice3's hex-encoded pairing proof
  as raw bytes and Wi-Fi pairing fails. Proof checking stays on.

## Credits

Rejig is by keyclipse. The Mac window, connection controls, installer, MCP server and the
engine patches are Rejig's own work, under the MIT license. The MCP server uses the
[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (MIT).

Reading and writing the layout, the editor you see in the window and Demo mode come from
[ios-homescreen](https://github.com/YakupEmreYerli/ios-homescreen) by Yakup Emre Yerli
(MIT), pinned at `97649547dbc4`. ios-homescreen talks to the iPhone through
[pymobiledevice3](https://github.com/doronz88/pymobiledevice3) by doronz88 and
contributors (GPL-3.0-or-later), which runs inside the engine's Python process. Rejig
starts that process and talks to it over a loopback connection. Wi-Fi pairing uses
[srptools](https://github.com/idlesign/srptools) (BSD-3-Clause). See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Rejig does not implement Apple's device protocols itself. It is not affiliated with or
endorsed by Apple, ios-homescreen or pymobiledevice3. iPhone is a trademark of Apple Inc.

## License

MIT. See [LICENSE](LICENSE).
