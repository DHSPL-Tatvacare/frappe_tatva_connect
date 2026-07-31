# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Force the Infrastructure workspace + sidebar back to what the app ships, for the new Lists group.

`CRM Derived Field` is operator config with no route from any workspace: a derived field is a list column
worked out as the list is drawn and never stored, and which columns a team wants computed is the one thing
the app cannot declare for itself. The group sits under Infrastructure beside Storage, Location and
Indexing, with the guided section stating that a row ships disabled and is proved before it saves.

The standard import skips a workspace whose DB copy looks newer than its file (import_file.py:141), so a
site whose desk was ever opened keeps the old four-group sidebar and never sees the new link. The bumped
`modified` in the JSON covers a site that never diverged; this covers the ones that did. Same shape as
`reimport_infrastructure_desk_indexing`, which is applied and therefore dead.

Through `patches/_desk.reimport`, never a hand-rolled `import_file_by_path`: `force=True` DELETES and
re-inserts the row, and on a developer_mode site that rmtree's the exported folder while the re-insert's
export returns early, so the patch would eat its own JSON.
"""
from tatva_connect.patches import _desk


def execute():
	_desk.reimport_all([
		("tatva_connect", "workspace", "infrastructure", "infrastructure.json"),
		("workspace_sidebar", "infrastructure.json"),
	])
