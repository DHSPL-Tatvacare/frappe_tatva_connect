# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""External Leads gains Meta conversions: Meta Datasets and Meta Sends in the sidebar group, renamed Facebook to Meta, and a
setup block with two tiles on the workspace. Forced, because a bumped `modified` alone ships nothing on a
desk that was ever opened (import_file.py:141). No schema_setup twin: no doctype, no field, no index."""
from tatva_connect.patches import _desk


def execute():
	_desk.reimport_all([
		("workspace_sidebar", "external_leads.json"),
		("tatva_connect", "workspace", "external_leads", "external_leads.json"),
	])
