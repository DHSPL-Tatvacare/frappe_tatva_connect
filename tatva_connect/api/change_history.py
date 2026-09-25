# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A configuration record's edit history — Task Forms and Workflows read it through this one call.

The rows are the Lead Activity rail's own: Frappe's `Version` records read by `lead_events.recent_versions` and
shaped by `api.activities._version_row`, closed by `lead_events.creation_event`. Nothing is worded twice.
A workflow tracks no field edits (its fields are derived from its graph); what went live is its publish history.
"""
import frappe
from frappe import _

from tatva_connect.activity.lead_events import VERSION_WINDOW, creation_event, recent_versions
from tatva_connect.api.activities import _version_row


@frappe.whitelist()
def change_history(doctype, name):
	"""Who changed what on this record, newest first, as rail rows; gated on reading the record, as Desk's own history is."""
	frappe.has_permission(doctype, "read", name, throw=True)
	rows = [row for row in (_version_row(v, doctype) for v in recent_versions(doctype, name)) if row]
	if doctype == "CRM Workflow":
		rows += _publishes(name)
	rows = sorted(rows, key=lambda row: row["creation"], reverse=True)[:VERSION_WINDOW]
	return [*rows, {"name": f"creation:{name}", **creation_event(doctype, name)}]


def _publishes(workflow):
	"""The latest frozen versions of a workflow as rail rows, one line per step added, changed or removed since the
	version before — named by their readable `node_id`. Read after the workflow's own read check above."""
	versions = frappe.get_all(
		"CRM Workflow Version", filters={"workflow": workflow},
		fields=["name", "owner", "creation", "version_no", "payload_json"],
		order_by="version_no desc", limit=VERSION_WINDOW + 1,
	)[::-1]
	steps = [{n["node_id"]: n for n in frappe.parse_json(v.payload_json or "{}").get("nodes") or []} for v in versions]
	# The oldest read is only the baseline, unless it is the first version ever.
	first = 0 if len(versions) <= VERSION_WINDOW else 1
	rows = []
	for i in range(first, len(versions)):
		before, after = steps[i - 1] if i else {}, steps[i]
		lines = [{"label": _("Added step"), "from": "", "to": k} for k in after if k not in before]
		lines += [{"label": _("Changed step"), "from": "", "to": k} for k in after if k in before and after[k] != before[k]]
		lines += [{"label": _("Removed step"), "from": k, "to": ""} for k in before if k not in after]
		v = versions[i]
		rows.append({
			"name": v.name, "activity_type": "published", "creation": v.creation, "owner": v.owner,
			"version_no": v.version_no, "changes": lines,
		})
	return rows
