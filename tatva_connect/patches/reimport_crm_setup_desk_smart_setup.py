# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Smart Setup joins the CRM Setup desk (sidebar Transfer section and a shortcut); force-reimported because a bumped `modified` ships nothing on an opened desk."""
from tatva_connect.patches import _desk


def execute():
	_desk.reimport_all([
		("workspace_sidebar", "crm_setup.json"),
		("tatva_connect", "workspace", "crm_setup", "crm_setup.json"),
		("workspace_sidebar", "automations.json"),
		("tatva_connect", "workspace", "automations", "automations.json"),
	])
