"""The automation builder's describe contract — the single, derived source the Rule form renders
from and the controller validates against.

No field/operator/value list is hardcoded in the UI: everything flows from one place — the task
type's activity schema (`CRM Task Type Field`, which carries fieldtype + options) and the
Automatable-Field allowlist. Add a field to a task type's form and it appears in the builder, with
the right operators and value choices, automatically. The engine's *evaluation* of these operators
lives in `automation/rules._one_match`; this module owns only the *vocabulary* (what's offered and
accepted), so the UI and the validator can never drift from each other.
"""
import frappe

from tatva_connect.automation import fields

# Which operators are valid for a field of each schema type — the one catalog, consumed by describe()
# (to offer), the rule controller (to reject), and the builder JS (to render).
_TEXT = ["=", "!=", "like", "not like", "in", "not in", "is set", "is unset"]
_CHOICE = ["=", "!=", "in", "not in", "is set", "is unset"]
_TEMPORAL = ["=", "!=", "<", ">", "<=", ">=", "between", "is set", "is unset"]
_PRESENCE = ["is set", "is unset"]

OPERATORS_BY_TYPE = {
	"Data": _TEXT,
	"Small Text": _TEXT,
	"Select": _CHOICE,
	"Link": _CHOICE,
	"Check": ["="],
	"Datetime": _TEMPORAL,
	"Attach": _PRESENCE,
	"Attach Image": _PRESENCE,
}


def operators_for(fieldtype):
	return OPERATORS_BY_TYPE.get(fieldtype or "Data", _TEXT)


def _value_options(fieldtype, raw_options):
	"""Pickable values for a field: Select -> its option lines; Link -> its target doctype; else None."""
	if fieldtype == "Select":
		return [o.strip() for o in (raw_options or "").split("\n") if o.strip()]
	if fieldtype == "Link":
		return (raw_options or "").strip() or None
	return None


def _descriptor(key, label, fieldtype, raw_options):
	ftype = fieldtype or "Data"
	return {
		"key": key,
		"label": label or key,
		"type": ftype,
		"operators": operators_for(ftype),
		"options": _value_options(ftype, raw_options),
	}


_STRUCTURAL_FIELDTYPES = ("Column Break", "Section Break", "HTML", "Button", "Fold")


def _meta_fields(doctype):
	"""THE meta-walking brain: real, non-structural fields of a doctype's meta, in form order. Every
	describe resolver that reads a DocType meta (Field-Changed vocabulary, the v2 typed catalog) walks
	through here - one reader, no parallel `frappe.get_meta` loop (A.8)."""
	return [df for df in frappe.get_meta(doctype).fields if df.fieldtype not in _STRUCTURAL_FIELDTYPES]


def fields_for_task_type(task_type):
	"""THE resolver for 'what fields does this task type's activity form have'. Used by both the
	describe endpoint and the rule controller's validation - one brain, no parallel query."""
	if not task_type:
		return []
	return [
		_descriptor(r.fieldname, r.label, r.fieldtype, r.options)
		for r in frappe.get_all(
			"CRM Task Type Field",
			filters={"parent": task_type, "parenttype": "CRM Task Type"},
			fields=["fieldname", "label", "fieldtype", "options"],
			order_by="idx",
		)
	]


def fields_for_doctype(doctype):
	"""THE resolver for 'what fields does a watched doctype expose to a Field-Changed rule'. Reads
	the doctype META (not an activity schema) - a Field-Changed criterion tests the watched
	doctype's own fields, so the vocabulary is the meta. Sibling to fields_for_task_type (which
	reads the CRM Task Type Field activity schema for the Task-Completed path): two functions, two
	genuine sources, one describe contract so the builder + validator + dispatcher never drift.

	TATVA v2 (Task 13): for CRM Task specifically, the meta alone UNDER-describes what a rule can
	actually reference. A completed activity's real business signal (outcome/training_status/
	call_completed_next_steps/...) is a per-task-type SCHEMA field (`CRM Task Type Field`) that
	`activity.api.compute_activity` either promotes onto one of the 9 shared columns or folds into
	the `custom_activity_payload` JSON blob - it is NEVER a CRM Task doctype field itself. So the
	vocabulary here is unioned with every distinct activity-schema fieldname (meta wins on a name
	clash) - the SAME union `crm_automation_field._require_real_field` accepts for a can_watch/
	can_set row and `router._activity_values` resolves at fire time (one brain, no drift)."""
	if not doctype:
		return []
	descriptors = [_descriptor(df.fieldname, df.label, df.fieldtype, df.options) for df in _meta_fields(doctype)]
	if doctype == "CRM Task":
		present = {d["key"] for d in descriptors}
		for fieldname, r in activity_schema_fields().items():
			if fieldname in present:
				continue
			descriptors.append(_descriptor(r.fieldname, r.label, r.fieldtype, r.options))
	return descriptors


def activity_schema_fields():
	"""Every distinct activity-schema fieldname across ALL `CRM Task Type Field` rows (any task type,
	any grain), first-definition-wins (ordered by parent, idx - deterministic, not grain-scoped: a
	rule's criterion vocabulary is doctype-wide, exactly like a real meta field would be). This is the
	ADDITIONAL vocabulary CRM Task exposes beyond its own doctype meta - see `fields_for_doctype`."""
	out = {}
	for r in frappe.get_all(
		"CRM Task Type Field",
		filters={"parenttype": "CRM Task Type"},
		fields=["fieldname", "label", "fieldtype", "options"],
		order_by="parent, idx",
	):
		out.setdefault(r.fieldname, r)
	return out


def activity_schema_fieldnames():
	"""Just the names - what `crm_automation_field._require_real_field` checks a CRM Task can_watch/
	can_set row's fieldname against when the doctype meta itself doesn't carry it."""
	return set(activity_schema_fields())


def _pick_for(fieldtype, raw_options):
	"""The typed pick-source for a v2 catalog entry: Link -> its target doctype; Select -> its option
	lines; else None (plain typed field, no picker)."""
	if fieldtype == "Link":
		target = (raw_options or "").strip()
		return {"kind": "link", "target": target} if target else None
	if fieldtype == "Select":
		options = [o.strip() for o in (raw_options or "").split("\n") if o.strip()]
		return {"kind": "select", "options": options} if options else None
	return None


def field_catalog(doctype):
	"""The v2 predicate builder's typed, pick-aware field catalog: every real field of `doctype`,
	plus one level of child-table fields under a dotted `table.field` key - so the builder renders a
	real control (Link search / Select options) instead of free text, and the server can re-validate
	off the same catalog. Shares `_meta_fields` (the one meta-walking brain) with fields_for_doctype;
	recursion is capped at one level - a nested Table field is listed but not walked into, so a
	Table-in-Table can never loop."""
	if not doctype:
		return []
	out = []
	for df in _meta_fields(doctype):
		if df.fieldtype == "Table":
			for cf in _meta_fields(df.options) if df.options else []:
				if cf.fieldtype == "Table":
					continue
				path = f"{df.fieldname}.{cf.fieldname}"
				out.append({
					"key": path,
					"label": cf.label or cf.fieldname,
					"type": cf.fieldtype,
					"pick": {"kind": "child", "path": path},
				})
			continue
		out.append({
			"key": df.fieldname,
			"label": df.label or df.fieldname,
			"type": df.fieldtype,
			"pick": _pick_for(df.fieldtype, df.options),
		})
	return out


def _settable_fields(vertical, group, program):
	"""CRM Lead parent fields a Set Field action may target at this grain — enabled can_set rows,
	enriched with type/options from the lead meta. (Set Field can also target the triggering doc; the
	validator gates any target via fields.is_settable — this dropdown hints the dominant Lead case.)"""
	meta = frappe.get_meta("CRM Lead")
	out = []
	for r in fields.settable_rows("CRM Lead", (vertical, group, program)):
		df = meta.get_field(r.fieldname)
		if df:
			out.append(_descriptor(r.fieldname, df.label, df.fieldtype, df.options))
	return out


def _watchable_fields_for(doctype):
	"""The enabled can_watch fieldnames for a doctype, enriched with type/options from the doctype
	meta - so the Rule form's watch_field dropdown and the validator read the same derived list (one
	brain, no parallel query)."""
	if not doctype:
		return []
	meta = frappe.get_meta(doctype)
	out = []
	for fieldname in fields.watchable_fields(doctype):
		df = meta.get_field(fieldname)
		if df:
			out.append(_descriptor(df.fieldname, df.label, df.fieldtype, df.options))
	return out


@frappe.whitelist()
def describe(task_type=None, vertical=None, group=None, program=None, watch_doctype=None):
	"""Everything the Automation Rule builder needs, derived from the task type schema (Task-Completed
	trigger) + the Watchable allowlist + the Automatable allowlist. Read-only metadata, restricted to
	the rule-authoring roles (read on CRM Automation Rule). `watch_doctype` drives the Field-Changed
	path: the watch_field dropdown + the criteria vocabulary come from the watched doctype's meta."""
	if not frappe.has_permission("CRM Automation Rule", "read"):
		frappe.throw(frappe._("Not permitted"), frappe.PermissionError)
	return {
		"activity_fields": fields_for_task_type(task_type),
		"watch_fields": _watchable_fields_for(watch_doctype),
		"set_field_targets": _settable_fields(vertical, group, program),
	}
