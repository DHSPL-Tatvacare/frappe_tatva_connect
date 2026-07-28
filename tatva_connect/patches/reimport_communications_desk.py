# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Force the Communications workspace + sidebar back to what the app ships.

`merge_did_into_routing` dropped `CRM Telephony DID`, but the standard import skips a workspace whose
DB copy looks newer than its file, so such a site kept a shortcut and a sidebar item pointing at the
dead doctype — and the desk answers "DocType CRM Telephony DID not found". Re-import both, force.
"""
from tatva_connect.patches import _desk


def execute():
	_desk.reimport_all([
		("tatva_connect", "workspace", "communications", "communications.json"),
		("workspace_sidebar", "communications.json"),
	])
