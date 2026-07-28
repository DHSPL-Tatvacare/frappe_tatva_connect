# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Force the Automations workspace + sidebar back to what the app ships.

The workflow-engine config surfaces (Action Groups, Workflows) and its queue/log views (Running
Instances, Signal Inbox, Step Log) were added to the workspace content, shortcuts, and sidebar. The
standard import skips a desk doc whose DB copy looks newer than its file, so a site whose Automations
desk was ever touched would keep the old layout and never show them &mdash; force both (same reason as
reimport_communications_desk).
"""
from tatva_connect.patches import _desk


def execute():
	_desk.reimport_all([
		("tatva_connect", "workspace", "automations", "automations.json"),
		("workspace_sidebar", "automations.json"),
	])
