# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Force the Communications desk to show the Transcription Setup section and its sidebar entry.

`CRM Transcription Account` shipped reachable by nobody: no workspace section, no sidebar link. An
operator cannot configure a service they cannot find, so the doctype was effectively inert however
correct its code was.

Forced rather than bumped: `import_file.py:141` skips a standard desk JSON whose DB row looks newer than
the file, so on any site whose desk has ever been opened the bumped `modified` alone migrates silently
green and changes nothing. The sidebar goes with the workspace because a Desk tile is a Link permitted
only through a Workspace Sidebar of the same name — same pairing as
`reimport_communications_desk_voice`, which added the AI Voice section this one mirrors.
"""
from tatva_connect.patches import _desk


def execute():
	_desk.reimport_all([
		("tatva_connect", "workspace", "communications", "communications.json"),
		("workspace_sidebar", "communications.json"),
	])
