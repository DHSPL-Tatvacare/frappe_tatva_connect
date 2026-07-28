# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Force the Communications workspace back to what the app ships, for the Facebook Routing section.

A bumped `modified` alone does not ship a workspace on a site whose desk was ever touched: import_file
skips a standard file whose timestamp is not newer than the DB row, so the edit migrates silently green
and changes nothing. Re-import, force — the same reason reimport_communications_desk exists.
"""
from tatva_connect.patches import _desk


def execute():
	_desk.reimport_all([("tatva_connect", "workspace", "communications", "communications.json")])
