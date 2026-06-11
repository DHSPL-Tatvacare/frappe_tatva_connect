"""C1: the CRM Task Quick Entry layout must carry custom_task_type + custom_checklist
or the checklist feature has no UI. Idempotent merge into the existing layout."""
import json

import frappe


def execute():
	name = "CRM Task-Quick Entry"
	if not frappe.db.exists("CRM Fields Layout", name):
		return
	doc = frappe.get_doc("CRM Fields Layout", name)
	layout = json.loads(doc.layout)
	sections = layout[0]["sections"]
	for sec in sections:
		for col in sec.get("columns", []):
			if "status" in col.get("fields", []) and "custom_task_type" not in col["fields"]:
				col["fields"].append("custom_task_type")
	if not any("custom_checklist" in c.get("fields", []) for s in sections for c in s.get("columns", [])):
		sections.append({"name": "checklist_section",
		                 "columns": [{"name": "column_chk1", "fields": ["custom_checklist"]}]})
	doc.layout = json.dumps(layout)
	doc.save(ignore_permissions=True)
	frappe.db.commit()
