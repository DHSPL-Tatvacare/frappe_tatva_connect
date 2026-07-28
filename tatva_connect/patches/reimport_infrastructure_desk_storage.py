# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Force the Infrastructure workspace + sidebar back to what the app ships, for the storage journey.

The Storage group is now the file's real journey, in the order the system applies it: the platform's own
limits, then screening, then the privacy floor and the offload, then retention — plus the two surfaces
that record what happened (File, CRM File Scan Log) and a guided section that says what each step is and
what it costs to leave it blank. `System Settings > Files` is reachable only from the sidebar, because a
Workspace Shortcut cannot open a form's tab and a sidebar item can (`navigate_to_tab`).

The standard import skips a workspace whose DB copy looks newer than its file (import_file.py:141), so a
site whose desk was ever touched keeps the old flat tile row and never sees any of it. The bumped
`modified` in both JSONs covers a site that never diverged; this covers the ones that did.

`reimport_infrastructure_desk` has already run on those sites and an applied patch is dead (rule 3), so
this ships as its own line and re-asserts the same declared end state.
"""
from tatva_connect.patches import _desk


def execute():
	_desk.reimport_all([
		("tatva_connect", "workspace", "infrastructure", "infrastructure.json"),
		("workspace_sidebar", "infrastructure.json"),
	])
