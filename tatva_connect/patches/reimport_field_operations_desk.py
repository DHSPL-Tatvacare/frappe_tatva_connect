# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Force the Field Operations workspace + sidebar back to what the app ships.

Phase 9/10 gives the space its guided Activity Setup section and the Task Sections master (D20/D30). The
standard import SKIPS a workspace whose DB copy looks newer than its file (`import_file.py:141`), which is
true on every site whose desk was ever opened, so the bumped `modified` alone would migrate silently green
and change nothing. Re-import both, force — the sidebar as well, because a Desk tile is a Link permitted
only through a Workspace Sidebar of the same name.
"""
from tatva_connect.patches import _desk


def execute():
	_desk.reimport_all([
		("tatva_connect", "workspace", "field_operations", "field_operations.json"),
		("workspace_sidebar", "field_operations.json"),
	])
