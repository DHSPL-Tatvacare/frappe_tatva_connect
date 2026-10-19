# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""External Leads gains Meta conversions: Conversion Datasets and Conversion Sends in the Meta sidebar group and
the Meta card. Forced, because a bumped `modified` alone ships nothing on a
desk that was ever opened (import_file.py:141). No schema_setup twin: no doctype, no field, no index."""
from tatva_connect.patches import _desk


def execute():
	_desk.reimport_all([
		("external_leads", "sidebar", "external_leads", "external_leads.json"),
		("external_leads", "workspace", "external_leads", "external_leads.json"),
	])
