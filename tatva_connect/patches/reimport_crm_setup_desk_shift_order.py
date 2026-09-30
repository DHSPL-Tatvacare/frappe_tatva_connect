# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""CRM Setup's shift shortcuts and sidebar links follow setup order; force-reimported because a bumped `modified` ships nothing on an opened desk."""
from tatva_connect.patches import _desk


def execute():
	_desk.reimport_all([
		("workspace_sidebar", "crm_setup.json"),
		("tatva_connect", "workspace", "crm_setup", "crm_setup.json"),
	])
