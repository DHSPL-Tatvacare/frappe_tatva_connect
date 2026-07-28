# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Force the Infrastructure workspace + sidebar back to what the app ships, for the new Indexing group.

Search Aliases is operator config with no route from any workspace: the vocabulary is what turns a typed word
into a filter, and the words a team really uses are the one thing the app cannot declare for itself. The group
sits under Infrastructure beside Storage and Location, with the guided section stating which switch reads it.

The standard import skips a workspace whose DB copy looks newer than its file (import_file.py:141), so a site
whose desk was ever opened keeps the old two-group sidebar and never sees the new link. The bumped `modified`
in the JSON covers a site that never diverged; this covers the ones that did. Same shape as
`reimport_infrastructure_desk_storage` and `reimport_infrastructure_desk_sniffing`, both applied and therefore dead.
"""
from tatva_connect.patches import _desk


def execute():
	_desk.reimport_all([
		("tatva_connect", "workspace", "infrastructure", "infrastructure.json"),
		("workspace_sidebar", "infrastructure.json"),
	])
