# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Every workspace intro cut to 2-3 lines and section prose removed; force-reimported because a bumped `modified` ships nothing on an opened desk."""
from tatva_connect.patches import _desk


def execute():
	_desk.reimport_all([
		("observability", "workspace", "assets", "assets.json"),
		("observability", "workspace", "jobs", "jobs.json"),
		("observability", "workspace", "observability", "observability.json"),
		("tatva_connect", "workspace", "automations", "automations.json"),
		("tatva_connect", "workspace", "communications", "communications.json"),
		("tatva_connect", "workspace", "crm_configuration", "crm_configuration.json"),
		("tatva_connect", "workspace", "external_leads", "external_leads.json"),
		("tatva_connect", "workspace", "infrastructure", "infrastructure.json"),
	])
