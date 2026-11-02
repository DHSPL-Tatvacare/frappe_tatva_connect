# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Smart Setup moves from the CRM Setup desk to Tatva Setup, below Feature Flags, which the Automation Manager now opens; force-reimported because a bumped `modified` ships nothing on an opened desk."""
from tatva_connect.patches import _desk


def execute():
	_desk.reimport_all([
		("crm_setup", "sidebar", "crm_setup", "crm_setup.json"),
		("crm_setup", "workspace", "crm_setup", "crm_setup.json"),
		("tatva_setup", "sidebar", "tatva_setup", "tatva_setup.json"),
		("tatva_setup", "workspace", "tatva_setup", "tatva_setup.json"),
	])
