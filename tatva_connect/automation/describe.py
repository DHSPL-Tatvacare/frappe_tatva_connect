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


def fields_for_task_type(task_type):
	"""THE resolver for 'what fields does this task type's activity form have'. Used by both the
	describe endpoint and the rule controller's validation — one brain, no parallel query."""
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


def _settable_fields(vertical, group, program):
	"""CRM Lead parent fields a Set Field action may target at this grain: the enabled allowlist rows
	(parent, not child), each enriched with the field's type/options from the lead meta."""
	from tatva_connect.automation import rules

	meta = frappe.get_meta("CRM Lead")
	out = []
	for r in frappe.get_all(
		"CRM Automatable Field",
		filters={"target_doctype": "CRM Lead", "enabled": 1},
		fields=["fieldname", "child_table_field", "vertical", "group", "program"],
	):
		if r.child_table_field or not rules.grain_matches(r, vertical, group, program):
			continue
		df = meta.get_field(r.fieldname)
		if df:
			out.append(_descriptor(r.fieldname, df.label, df.fieldtype, df.options))
	return out


@frappe.whitelist()
def describe(task_type=None, vertical=None, group=None, program=None):
	"""Everything the Automation Rule builder needs, derived from the task type schema + allowlist.
	Read-only metadata, restricted to the rule-authoring roles (read on CRM Automation Rule)."""
	if not frappe.has_permission("CRM Automation Rule", "read"):
		frappe.throw(frappe._("Not permitted"), frappe.PermissionError)
	return {
		"activity_fields": fields_for_task_type(task_type),
		"set_field_targets": _settable_fields(vertical, group, program),
	}
