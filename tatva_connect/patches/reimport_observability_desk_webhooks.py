# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Observability gains Webhook Endpoints (Settings) and Webhook Log (Monitor) in its sidebar, as named on Automations. Forced, because a bumped
`modified` alone ships nothing on a desk that was ever opened (import_file.py:141). No schema_setup twin."""
from tatva_connect.patches import _desk


def execute():
	_desk.reimport_all([
		("observability", "sidebar", "observability", "observability.json"),
	])
