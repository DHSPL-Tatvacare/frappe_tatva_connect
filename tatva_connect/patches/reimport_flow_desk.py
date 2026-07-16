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
import frappe
from frappe.modules.import_file import import_file_by_path


def execute():
	for parts in (
		("tatva_connect", "workspace", "automations", "automations.json"),
		("workspace_sidebar", "automations.json"),
		("observability", "workspace", "observability", "observability.json"),
		("workspace_sidebar", "observability.json"),
	):
		path = frappe.get_app_path("tatva_connect", *parts)
		try:
			import_file_by_path(path, force=True)
		except Exception:
			frappe.log_error(title=f"reimport {'/'.join(parts)} failed", message=frappe.get_traceback())

	frappe.clear_cache()
