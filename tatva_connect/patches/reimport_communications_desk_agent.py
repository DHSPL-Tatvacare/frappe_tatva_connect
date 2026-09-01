# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Force the Communications desk to show Telephony Agents and its sidebar entry.

`CRM Telephony Agent` shipped reachable by nobody: no workspace shortcut, no sidebar link. It is the
seat table, and it answers BOTH halves of a rep's telephony identity — which phone the CRM rings when
they click to call, and the name a call they answer is recorded under. An operator who cannot find it
leaves every outbound call refused for want of a seat and every inbound call unattributed, however
correct the code is. Same defect and same remedy as `reimport_communications_desk_transcription`.

Placed in config order, before the agent map: a seat is filled for every rep, while the map is the
exception for an agent whose provider login is not their CRM email.

Forced rather than bumped: `import_file.py:141` skips a standard desk JSON whose DB row looks newer than
the file, so on any site whose desk has ever been opened the bumped `modified` alone migrates silently
green and changes nothing. The sidebar goes with the workspace because a Desk tile is a Link permitted
only through a Workspace Sidebar of the same name.
"""
from tatva_connect.patches import _desk


def execute():
	_desk.reimport_all([
		("tatva_connect", "workspace", "communications", "communications.json"),
		("workspace_sidebar", "communications.json"),
	])
