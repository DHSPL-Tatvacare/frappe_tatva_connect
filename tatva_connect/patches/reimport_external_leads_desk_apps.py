# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Point the External Leads desk at Facebook Apps, since Facebook Settings no longer exists.

The workspace shortcut and the sidebar link both named the retired Single, so both would 404. The
Facebook Routing steps also gain the app as step one — it is now the first thing an operator sets, and
the token pasted on a source has to come from the app named there.

Forced rather than bumped: `import_file.py:141` skips a standard desk JSON whose DB row looks newer than
the file, so on any site whose desk has ever been opened the bumped `modified` alone migrates silently
green and changes nothing. Through `patches/_desk.reimport` and never a hand-rolled
`import_file_by_path` — force=True DELETES and re-inserts, and on a developer_mode site that rmtree's the
exported folder while the re-insert's export returns early, so the patch would eat its own JSON. The
sidebar goes with the workspace because a Desk tile is a Link permitted only through a Workspace Sidebar
of the same name.
"""
from tatva_connect.patches import _desk


def execute():
	_desk.reimport_all([
		("tatva_connect", "workspace", "external_leads", "external_leads.json"),
		("workspace_sidebar", "external_leads.json"),
	])
