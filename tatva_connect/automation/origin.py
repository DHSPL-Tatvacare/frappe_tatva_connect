"""Read-only attribution for automation-authored rows: which workflow raised a task / note / message.

The automation engine already fingerprints its own writes — a `custom_workflow_token` on a CRM Task
(`actions._stamp_workflow_token`) and a `custom_workflow_correlation` on a WhatsApp Message (`sends`), both
carrying the SAME shape the interpreter mints: `{journey_name}::{node_id}` (`interpreter._run_verb`). This
module is the ONE place that reads that stamp back into a human label + the journey to deep-link to. It never
writes, never touches the workflow engine, and adding a new automation-authored doctype later is one row in
`AUTOMATION_STAMP` — the operator's/author's later scope, never new code.
"""
import frappe

# doctype -> the column the engine stamps its `{journey_name}::{node_id}` token on. Seeded with the two that
# exist today; a new automation-authored doctype is one line here, never a second reader of the stamp.
AUTOMATION_STAMP = {
	"CRM Task": "custom_workflow_token",
	"WhatsApp Message": "custom_workflow_correlation",
	# A placed AI call is the third automation-authored row, and its stamp does a second job: it is how the
	# catch-up reconciler finds the execution a parked journey is waiting on, by the token the journey holds.
	"CRM Call Log": "custom_workflow_correlation",
}


def automation_origins(doctype, names):
	"""`{name: origin}` for many rows in TWO queries, not two per row — what a rail page needs.

	Same answer `automation_origin` gives, from the same two reads; a page of twenty tasks used to cost
	forty round trips through the single-row call.
	"""
	field = AUTOMATION_STAMP.get(doctype)
	names = [n for n in (names or []) if n]
	if not field or not names:
		return {}
	stamped = {
		r["name"]: r[field]
		for r in frappe.get_all(doctype, filters={"name": ["in", names]}, fields=["name", field])
		if r.get(field) and "::" in r[field]
	}
	journeys = {s.split("::")[0] for s in stamped.values()}
	labels = {
		r["name"]: r["workflow"]
		for r in frappe.get_all(
			"CRM Workflow Journey", filters={"name": ["in", list(journeys)]}, fields=["name", "workflow"]
		)
	}
	out = {}
	for name, stamp in stamped.items():
		journey = stamp.split("::")[0]
		if labels.get(journey):
			out[name] = {"label": labels[journey], "journey": journey}
	return out


def automation_origin(doctype, name):
	"""`{"label", "journey"}` when this row carries its declared workflow stamp, else `None`.

	The stamp is `{journey_name}::{node_id}`; the journey_name is everything left of the FIRST `::`, resolved to its
	workflow's label off the `CRM Workflow Journey` row. A stamp without `::` (malformed / not ours) or a journey
	that no longer exists yields `None` — the caller attributes to the human owner, never to a wrong name.
	"""
	field = AUTOMATION_STAMP.get(doctype)
	if not field or not name:
		return None
	stamp = frappe.db.get_value(doctype, name, field)
	if not stamp or "::" not in stamp:
		return None
	journey_name = stamp.split("::")[0]
	label = frappe.db.get_value("CRM Workflow Journey", journey_name, "workflow")
	if not label:
		return None
	return {"label": label, "journey": journey_name}
