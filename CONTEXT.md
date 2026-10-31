# Rejig

A Mac app and its MCP companion for arranging the real Home Screen of a connected iPhone, by hand or with an AI client, without writing anything the user has not approved.

## Language

**Rejig**:
The public product: the Mac app plus the MCP companion.

**MCP companion**:
The local MCP server that gives AI clients Rejig's device workflows.
_Avoid_: plugin, bridge, connector

**Layout**:
The full arrangement of one phone's Home Screen: pages, dock, folders, widgets and which apps sit only in the App Library.
_Avoid_: icon state, home screen (when meaning the data)

**Change list**:
The exact differences between a phone's current Layout and a proposed one, shown before anything is written.
_Avoid_: diff, preview (when meaning the data)

**Approval**:
A person's confirmation of one exact Change list for one phone, given through a channel the model cannot answer: the MCP companion's in-chat confirmation or the Mac app's Approve button. An AI client's tool-permission prompt is not an Approval.
_Avoid_: tool approval, consent, auto-approve

**Apply**:
Writing an approved Layout to the phone, preceded by a Backup and followed by a Readback.
_Avoid_: push, sync, save

**Readback**:
Reading the Layout again after a write to confirm the phone holds what was written.
_Avoid_: verify (alone), refresh

**Backup**:
A saved copy of a phone's Layout taken before every write, restorable through the same Approval and Apply workflow.
_Avoid_: snapshot, undo
