# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Workflow Health opens for the Automation Manager, whose role already reads every log it shows. Forced, because a bumped
`modified` alone ships nothing on a desk that was ever opened (import_file.py:141). No schema_setup twin."""
from tatva_connect.patches import _desk


def execute():
	_desk.reimport_all([
		("observability", "workspace", "automation_health", "automation_health.json"),
	])
