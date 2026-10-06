# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Observability gains an Investigate page, and a new CRM Reports tile lands page, sidebar, then icon, so the icon never links a sidebar that is not there yet."""
from tatva_connect.patches import _desk


def execute():
	_desk.reimport_all([
		("workspace_sidebar", "observability.json"),
		("tatva_connect", "workspace", "crm_reports", "crm_reports.json"),
		("workspace_sidebar", "crm_reports.json"),
		("desktop_icon", "crm_reports.json"),
	])
