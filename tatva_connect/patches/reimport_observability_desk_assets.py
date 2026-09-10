# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Observability gains a second page, Assets, and the sidebar entry that reaches it.

Declared end state: the Observability sidebar carries `Assets` directly under `Overview`, opening a
workspace of the same name that counts every artifact this app owns — the ones a provider still owes us
and the sweep is chasing, the ones that landed as files, and the bytes still sitting on the app server —
with `Asset Inventory` holding the per-channel breakdown.

WHY A SECOND PAGE AND NOT A SECTION. Overview answers whether traffic ARRIVED: hits, errors, latency,
silence, all of it counted at the edge. Assets answers whether the work COMPLETED, which is a different
question over different tables, and appending it would have made one page answer both badly.

WHY A PATCH AT ALL. Only the sidebar needs one. `import_file.py:141` skips a standard JSON whose DB row
is not older than the file, so on any site whose desk was ever opened the added item migrates silently
green and changes nothing; the bumped `modified` is necessary and not sufficient. Goes through
`_desk.reimport_all`, never a hand-rolled import_file_by_path.

The workspace itself is NOT reimported: it is new, so no DB row can shadow it and the standard import
lands it on every site. No desktop-icon twin either — `create_desktop_icons_from_workspace` runs at
install only, and a Link tile is permitted through a Workspace Sidebar of its own name, which Assets
deliberately does not have: it is reached from Observability's sidebar, not from the desk.

No schema_setup twin: no doctype, no field, no index. A fresh site imports both JSONs as they stand.
"""
from tatva_connect.patches import _desk


def execute():
	_desk.reimport_all([
		("workspace_sidebar", "observability.json"),
	])
