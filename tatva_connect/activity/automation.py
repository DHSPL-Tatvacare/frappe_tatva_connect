"""Activity value reconstruction — the one brain for reading a submitted activity form.

`reconstruct_values` rebuilds the submitted form from how compute_activity stored it (a JSON payload +
the 9 promoted columns, each reverse-mapped to its schema fieldname via the field's `target`). The
automation engine (`tatva_connect.automation.dispatcher`) reuses this so a rule's criteria match the
schema fieldname regardless of where the writer parked the value.

The legacy task-type transition engine (apply_transitions / _apply_one) that also lived here was
folded into the unified automation engine — see db-seeds migration + patches.txt trace.
"""
import json

import frappe


def reconstruct_values(doc):
	"""Rebuild the submitted activity form as a {fieldname: value} dict from how compute_activity
	stored it: the JSON payload (already keyed by fieldname for payload fields) merged with the 9
	promoted columns, each mapped back to the schema fieldname whose `target` is that column.
	Criteria match on the SCHEMA fieldname, so both halves land under it."""
	values = {}
	payload = (doc.get("custom_activity_payload") or "").strip()
	if payload:
		try:
			values.update(json.loads(payload) or {})
		except (ValueError, TypeError):
			frappe.log_error("automation: bad activity payload JSON")
	for f in frappe.get_all(
		"CRM Task Type Field",
		filters={"parent": doc.custom_task_type, "parenttype": "CRM Task Type", "target": ["is", "set"]},
		fields=["fieldname", "target"],
	):
		val = doc.get(f.target)
		if val is not None:
			values[f.fieldname] = val
	return values
