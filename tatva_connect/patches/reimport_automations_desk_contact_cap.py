# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""The contact cap becomes reachable from the Automations desk.

Declared end state: the Automations workspace's Setup card and its sidebar both offer <b>Contact Limit</b>
beside Engine Switches, and the Setup prose has a fourth numbered step saying what the ceiling is and that
it ships off.

WHAT WAS WRONG. `CRM Contact Cap Settings` shipped reachable by nobody — no workspace section, no sidebar
entry — so an operator could not find the one page that has to be enabled for the ceiling to exist. The
doctype was inert however correct its code, which is the same defect `reimport_communications_desk_transcription`
was written for.

WHY A PATCH AT ALL. `import_file.py:141` skips a standard JSON whose DB row is not older than the file, so
on any site whose desk was ever opened the edit migrates silently green and changes nothing. The bumped
`modified` on both files is necessary and not sufficient.

Both files, because a Desk tile is a Link permitted only through a Workspace Sidebar of the same name —
the workspace card alone would leave the page listed and still unreachable from the rail.

Through `patches/_desk.reimport`, never a hand-rolled `import_file_by_path(force=True)`: force DELETES and
re-inserts, and on a developer_mode site that rmtree's the exported folder while the re-insert's export
returns early, so the patch eats its own JSON.

A fresh site baselines this line without running it: it is born with both files already imported.
"""
from tatva_connect.patches import _desk

DESKS = (
	("tatva_connect", "workspace", "automations", "automations.json"),
	("workspace_sidebar", "automations.json"),
)


def execute():
	_desk.reimport_all(DESKS)
