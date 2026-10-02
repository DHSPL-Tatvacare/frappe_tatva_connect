# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""HD Setup, CRM Setup, External Leads and Communications trade their count tiles for link cards; force-reimported because a bumped `modified` ships nothing on an opened desk."""
from tatva_connect.patches import _desk


def execute():
	_desk.reimport_all([
		("tatva_connect", "workspace", "hd_setup", "hd_setup.json"),
		("tatva_connect", "workspace", "crm_setup", "crm_setup.json"),
		("tatva_connect", "workspace", "external_leads", "external_leads.json"),
		("tatva_connect", "workspace", "communications", "communications.json"),
	])
