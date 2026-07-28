# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Force the Automations + Observability desks back to what the app ships after the fold.

The automation-rule engine was deleted and the Automations workspace/sidebar were rebuilt around the one
Flow engine: Setup (Field Allowlist / Action Groups / Flows), Reference (Webhook Endpoints + Webhook Log),
Monitor (Running Instances / Signal Inbox / Execution Log), with the rule surfaces (Automation Rules,
Queue, Run Log) removed and progressive third-person guidance in the content. The Observability desk lost
its Run Log links. The standard import skips a desk doc whose DB copy looks newer than its file, so a site
whose desk was ever touched would keep the old rule layout and never show the new one — force all four
(same reason as reimport_communications_desk)."""
from tatva_connect.patches import _desk


def execute():
	_desk.reimport_all([
		("tatva_connect", "workspace", "automations", "automations.json"),
		("workspace_sidebar", "automations.json"),
		("observability", "workspace", "observability", "observability.json"),
		("workspace_sidebar", "observability.json"),
	])
