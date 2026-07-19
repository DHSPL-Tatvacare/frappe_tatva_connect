"""The ONE trigger-context toolbox — subject resolution + the field-diff + the context/field-type
builders, shared by everything that reacts to a doc write.

These brains are not engine-specific — they resolve the parent lead, diff the watched fields, and assemble
the context a criteria predicate reads. The Flow front-door (`workflow_engine.triggers`) and the location
backstop (`tasks.enforce_location`) both build on them, so they live in one neutral module with one
implementation (A.8, no second copy).
"""
import frappe

from tatva_connect.automation import subjects


def subject(doc):
	"""The subject a Flow/guard acts ON — the parent Lead. Name resolution is delegated to the ONE brain
	(`subjects.resolve_lead_name`): Lead → itself, Task → its parent Lead. Returns the Lead DOC so callers
	read `.name` + fields; `None` for an unresolvable subject (fail-closed). A Lead trigger returns the
	already-fetched doc (no reload)."""
	lead_name = subjects.resolve_lead_name(doc)
	if not lead_name:
		return None
	return doc if doc.doctype == "CRM Lead" else frappe.get_doc("CRM Lead", lead_name)


def subject_axes(subject_doc):
	"""(vertical, group, program) read straight off the in-memory subject Lead DOCUMENT — never a DB round
	trip. `subject()` always returns the real CRM Lead doc (itself, or the loaded parent Lead for a Task),
	already grain-stamped by `before_validate`, so its own fields are the answer."""
	return (
		subject_doc.get("custom_vertical") or "",
		subject_doc.get("custom_group") or "",
		subject_doc.get("custom_current_program") or "",
	)


def diff_watched_fields(doc):
	"""Return {fieldname: (old, new)} for every watched field whose value changed on this save. Mirrors
	frappe Notification's Value Change event: skip new docs, cast both sides through the field's own
	fieldtype, compare. The Watchable registry is read per-doctype and cached for the request."""
	watched = watchable_fields_for(doc.doctype)
	if not watched:
		return {}
	before = doc.get_doc_before_save()
	if not before:
		return {}  # no before-state (e.g. a migration re-save) - nothing to diff
	out = {}
	for fieldname in watched:
		df = doc.meta.get_field(fieldname)
		if df is None:
			continue  # a stale registry row pointing at a removed field - skip, don't crash
		old, new = before.get(fieldname), doc.get(fieldname)
		try:
			if frappe.utils.cast(df.fieldtype, old) != frappe.utils.cast(df.fieldtype, new):
				out[fieldname] = (old, new)
		except Exception:  # nosec B110 - an uncastable value falls back to raw equality
			if old != new:
				out[fieldname] = (old, new)
	return out


def context_for(doc, changed):
	"""The trigger context a criteria predicate reads: the doc's own persistable fields (get_valid_dict)
	PLUS, for each changed watched field, a `{field}__before` key carrying the old value (the pair the
	`changed to`/`changed from…to` operators read) PLUS CRM Task's activity-schema values, keyed by their
	own schema fieldname, `setdefault`-merged so a genuine doc column always wins a name clash. ONE builder
	for every subject/event."""
	context = doc.get_valid_dict()
	for fieldname, (old, _new) in changed.items():
		context[f"{fieldname}__before"] = old
	for fieldname, value in activity_values(doc).items():
		context.setdefault(fieldname, value)
	return context


def activity_values(doc):
	"""CRM Task's activity-schema submitted values, keyed by their SCHEMA fieldname — NOT the promoted
	column / payload key `activity.api.compute_activity` routed them to. A criterion is authored against
	the schema fieldname (outcome/training_status/…), so the context must expose the SAME name at fire
	time. Reuses the ONE existing brain (`activity.api._task_values` + `_type_config`). Empty for a
	non-CRM-Task subject or a plain task with no activity type.

	NOTE (audit GAP 3, deferred): read is gated by can_read only at AUTHOR time (describe), not here at
	fire time. Enforcing readable_fields here is the right shape but needs the seed to first tick can_read
	on EVERY criterion-referenced schema field (many live TP-tuple rules key on can_read=0 fields today) —
	a coordinated seed change owned by the automation engine, not a code-only fix. Tracked for that owner."""
	if doc.doctype != "CRM Task" or not doc.get("custom_task_type"):
		return {}
	from tatva_connect.activity.api import _task_values, _type_config

	cfg = _type_config(doc.custom_task_type)
	if not cfg:
		return {}
	return _task_values(doc, cfg)


def field_types_for(doctype):
	"""{fieldname: schema type} for the trigger doctype's meta fields, so criteria evaluate type-aware.
	Reuses `describe.fields_for_doctype` — the same vocabulary the builder + validator read."""
	from tatva_connect.automation.describe import fields_for_doctype

	return {f["key"]: f["type"] for f in fields_for_doctype(doctype)}


def watchable_fields_for(doctype):
	"""The enabled can_watch fieldnames for a doctype, cached per-request (the query brain is
	`fields.watchable_fields` — this is just the cache)."""
	from tatva_connect.automation import fields

	cache = frappe.flags.setdefault("_watchable_fields_cache", {})
	if doctype not in cache:
		cache[doctype] = fields.watchable_fields(doctype)
	return cache[doctype]
