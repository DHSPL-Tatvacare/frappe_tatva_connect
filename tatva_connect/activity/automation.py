"""The activity journey engine — generic, grain-agnostic.

When a logged activity becomes Done, each CRM Task Type Transition row (config) is matched against
the submitted values; a match may raise the next activity task and/or move the lead's stage. The
engine never names an activity, option, or stage — it only reads config rows and acts. One brain:
reconstruct_values rebuilds the submitted form from how the writer stored it (a JSON payload + the
first-class columns), reverse-mapping each first-class column back to its schema fieldname so a
transition's on_field matches regardless of where the writer parked the value.
"""
import json

import frappe

from tatva_connect import automation


def reconstruct_values(doc):
	"""Rebuild the submitted activity form as a {fieldname: value} dict from how compute_activity
	stored it: the JSON payload (already keyed by fieldname for non-first-class fields) merged with
	the first-class columns, each mapped back to the schema fieldname whose first_class_target is
	that column. Transitions match on the SCHEMA fieldname, so both halves land under it."""
	values = {}
	payload = (doc.get("custom_activity_payload") or "").strip()
	if payload:
		try:
			values.update(json.loads(payload) or {})
		except (ValueError, TypeError):
			frappe.log_error("automation: bad activity payload JSON")
	for f in frappe.get_all(
		"CRM Task Type Field",
		filters={"parent": doc.custom_task_type, "parenttype": "CRM Task Type", "first_class_target": ["is", "set"]},
		fields=["fieldname", "first_class_target"],
	):
		val = doc.get(f.first_class_target)
		if val is not None:
			values[f.fieldname] = val
	return values


def apply_transitions(doc, method=None):
	"""CRM Task on_update: when a logged activity FIRST becomes Done, run its type's transitions.
	Fires once (idempotent) — skips unless this save is the Done flip. Each rule is isolated in a
	try/except so one bad config row never breaks the save."""
	if not automation.is_enabled("activity_transitions"):
		return
	from tatva_connect.activity.api import _type_has_schema

	if doc.status != "Done" or not _type_has_schema(doc.custom_task_type):
		return
	before = doc.get_doc_before_save()
	if before and before.status == "Done":
		return  # already Done before this save — don't re-fire
	if doc.reference_doctype != "CRM Lead" or not doc.reference_docname:
		return

	transitions = frappe.get_all(
		"CRM Task Type Transition",
		filters={"parent": doc.custom_task_type, "parenttype": "CRM Task Type"},
		fields=["on_field", "on_value", "then_create", "due_from", "set_stage"],
	)
	if not transitions:
		return
	values = reconstruct_values(doc)
	lead = doc.reference_docname
	for tr in transitions:
		if str(values.get(tr.on_field) or "") != str(tr.on_value or ""):
			continue
		try:
			_apply_one(tr, lead, values, doc)
		except Exception:
			frappe.log_error("automation: transition failed")


def _apply_one(tr, lead, values, doc):
	"""Run a single matched transition: set the lead stage and/or raise the next activity task."""
	from tatva_connect.tasks.tasks import create_followup_task

	if tr.set_stage:
		# Multi-grain stage lives in custom_stage (Link -> CRM Lead Stage, a program-scoped composite
		# master), NOT the shared frappe-default `status`. The transition row carries the full
		# '<program>::<stage>' value (the type is grain-scoped, so it's unambiguous).
		frappe.db.set_value("CRM Lead", lead, "custom_stage", tr.set_stage)
	if tr.then_create:
		create_followup_task(
			lead=lead,
			task_type=tr.then_create,
			due_at=values.get(tr.due_from) if tr.due_from else None,
			assigned_to=doc.get("assigned_to"),
		)
