# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Simulate — a read-only preview of what a rule's criteria + actions WOULD do against a chosen
sample record (TATVA v2, Task 15, the builder's "test before you enable" feedback).

A read-only SIBLING of `dispatcher.run_effects` (A.8), never a second engine: `dry_run` reuses the
SAME criteria evaluator (`rules.criteria_match`), the SAME context builder the router uses
(`router._context_for`/`_field_types_for`), and the SAME pure resolver helpers `actions.py`'s verb
handlers already call (`_due_at`, `_resolve_set_field_value`, `_resolve_map`, `_action_label`,
`expr.resolve_expression`) — it only stops short of the write. No handler in `actions._ACTION_LANES`
is ever invoked here; nothing is `.save()`d, `.insert()`d, enqueued, or sent.

Updated-event honesty (Part H): a dry_run has no real before/after pair to diff — only the ONE sample
record the author picked — so the context is built with an empty `changed` dict exactly like a
Created/Deleted fire. A `changed to`/`changed from…to` criterion therefore evaluates as a clean
non-match (the same fail-soft semantics `rules._changed_match` already gives a missing `__before`
key), and the preview says so via the ordinary "criteria did not match" note — no special-cased text.
"""
import frappe
from frappe import _

from tatva_connect.automation import actions, expr, rules, subjects
from tatva_connect.automation.router import _context_for, _field_types_for
from tatva_connect.taxonomy import labels


@frappe.whitelist()
def dry_run(rule_name, sample_doctype, sample_name):
	"""Preview one rule's fire against one sample record. Fail-closed (S.1): both permission checks
	run FIRST, before either doc is even loaded — you cannot simulate a rule you can't read, against a
	record you can't read. Returns `{matched, guards: [{verb, would}], effects: [{verb, would}]}`."""
	frappe.has_permission("CRM Automation Rule", "read", throw=True)
	frappe.has_permission(sample_doctype, "read", doc=sample_name, throw=True)

	rule = frappe.get_doc("CRM Automation Rule", rule_name)
	sample = frappe.get_doc(sample_doctype, sample_name)

	subject_name = subjects.resolve_lead_name(sample)
	if not subject_name:
		frappe.throw(
			_("{0} {1} has no resolvable subject lead — nothing to simulate against.").format(sample_doctype, sample_name),
			title=_("No subject"),
		)
	axes = rules.lead_axes(subject_name)

	# Same context builder the router uses for every event — an empty `changed` dict (no real
	# before-state available from a single sample) so `changed…` criteria evaluate as a non-match,
	# never a raise, exactly like a Created/Deleted fire (see module docstring).
	context = _context_for(sample, {})
	field_types = _field_types_for(sample_doctype)
	matched = rules.criteria_match(rule.criteria, context, field_types)

	guards, effects = [], []
	for action in rule.actions:
		lane, _handler = actions._ACTION_LANES.get(action.action_type, (None, None))
		would = _preview_action(action, context, subject_name, axes)
		if not matched:
			would = f"{would} — criteria did not match; would NOT run"
		entry = {"verb": action.action_type, "would": would}
		(guards if lane == "guard" else effects).append(entry)

	return {"matched": matched, "guards": guards, "effects": effects}


# -- per-verb preview builders -------------------------------------------------
#
# Each builder resolves the SAME value a live fire would, via the SAME pure resolver the verb's
# handler in actions.py calls — never a second resolution path (A.8). None of these touch a write
# path (no `.save`/`.insert`/`frappe.enqueue`/`sends.*`/adapter call) — Send WhatsApp/Send Email
# preview the config only (the live handlers themselves stay behind the dormant sends gate).


def _preview_action(action, context, subject_name, axes):
	"""The 'would' string for one action. A resolution failure (e.g. a Set Field Expression
	referencing a context key this particular sample doesn't carry) degrades to an error note rather
	than raising — a sample that doesn't fit the rule must never crash the preview."""
	builder = _WOULD_BUILDERS.get(action.action_type, _would_generic)
	try:
		return builder(action, context, subject_name, axes)
	except Exception as e:
		return f"{actions._action_label(action)} — could not resolve against this sample ({e})"


def _would_require_fields(action, context, subject_name, axes):
	fieldnames = [f.strip() for f in (action.require_fields or "").split(",") if f.strip()]
	blank = [f for f in fieldnames if context.get(f) in (None, "")]
	if blank:
		return f"Require {', '.join(fieldnames)} to be set — blank: {', '.join(blank)} (would block save)"
	return f"Require {', '.join(fieldnames)} to be set — all present (would pass)"


def _would_require_location(action, context, subject_name, axes):
	from tatva_connect.location import api as location_api

	radius = location_api.location_required(context.get("custom_task_type"), subject_name, context)
	if radius is None:
		return "Require a captured location — not required for this task type/values (would pass)"
	lat, lng = context.get("custom_location_latitude"), context.get("custom_location_longitude")
	if not (lat and lng):
		return f"Require a captured location within {action.geofence_meters or radius}m — none captured (would block save)"
	return f"Require a captured location within {action.geofence_meters or radius}m — location present (would pass)"


def _would_create_task(action, context, subject_name, axes):
	due = actions._due_at(action, context)
	when = f"due {due}" if due else "due (default lead time)"
	note = ""
	if frappe.db.exists("CRM Task Type Scope", {"parent": action.task_type, "parenttype": "CRM Task Type"}):
		from tatva_connect.activity.api import _scope_applies

		if not _scope_applies(action.task_type, axes[0], axes[1], axes[2]):
			note = " — NOTE: this task type is not in the sample's grain, would be blocked"
	return f"Create Task {labels.label(action.task_type, labels.TASK_TYPE) or '?'} {when}{note}"


def _would_update_field(action, context, subject_name, axes):
	value = actions._resolve_set_field_value(action, context)
	value = labels.shown(action.target_doctype, action.fieldname, value)
	return f"Set {action.fieldname or '?'} on {action.target_doctype or '?'} = {value!r}"


def _would_append_child(action, context, subject_name, axes):
	values = actions._resolve_map(action.set_json, context)
	return f"Append a row to {action.child_table or '?'} with {values}"


def _would_upsert_child(action, context, subject_name, axes):
	match = actions._resolve_map(action.match_json, context)
	values = actions._resolve_map(action.set_json, context)
	return f"Upsert a row in {action.child_table or '?'} matching {match}, set {values}"


def _would_call_webhook(action, context, subject_name, axes):
	return f"Call Webhook {action.webhook_endpoint or '?'}"


def _would_create_note(action, context, subject_name, axes):
	if action.comment_mode == "Expression":
		text = expr.resolve_expression(action.comment_expression, context)
	else:
		text = action.comment_text or ""
	return f"Add a note on the subject: {text!r}"


def _would_send_whatsapp(action, context, subject_name, axes):
	mobile = frappe.db.get_value("CRM Lead", subject_name, "mobile_no")
	return "Send WhatsApp {} to {} (suppressed unless sends enabled)".format(
		action.whatsapp_template or "?", mobile or "(no mobile_no on the lead)"
	)


def _would_send_email(action, context, subject_name, axes):
	return "Send Email to {}: {!r} (suppressed unless sends enabled)".format(
		action.email_recipient or "?", action.email_subject or ""
	)


def _would_wait(action, context, subject_name, axes):
	delay = expr.resolve_expression(action.wait_expression, context)
	return f"Wait {delay!r} then continue"


def _would_generic(action, context, subject_name, axes):
	return actions._action_label(action)


_WOULD_BUILDERS = {
	"Require Fields": _would_require_fields,
	"Require Location": _would_require_location,
	"Create Task": _would_create_task,
	"Update Field": _would_update_field,
	"Append Child Row": _would_append_child,
	"Upsert Child Row": _would_upsert_child,
	"Call Webhook": _would_call_webhook,
	"Create Note": _would_create_note,
	"Send WhatsApp": _would_send_whatsapp,
	"Send Email": _would_send_email,
	"Wait": _would_wait,
}
