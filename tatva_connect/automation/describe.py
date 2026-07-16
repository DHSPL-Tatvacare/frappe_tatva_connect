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

from tatva_connect.automation import fields, rules

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


def fields_for_doctype(doctype):
	"""THE resolver for 'what fields does a watched doctype expose to a Field-Changed rule'. Reads
	the doctype META (not an activity schema) - a Field-Changed criterion tests the watched
	doctype's own fields, so the vocabulary is the meta.

	TATVA v2 (Task 13): for CRM Task specifically, the meta alone UNDER-describes what a rule can
	actually reference. A completed activity's real business signal (outcome/training_status/
	call_completed_next_steps/...) is a per-task-type SCHEMA field (`CRM Task Type Field`) that
	`activity.api.compute_activity` either promotes onto one of the 9 shared columns or folds into
	the `custom_activity_payload` JSON blob - it is NEVER a CRM Task doctype field itself. So the
	vocabulary here is unioned with every distinct activity-schema fieldname (meta wins on a name
	clash) - the SAME union `crm_automation_field._require_real_field` accepts for a can_read/
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
	"""Just the names - what `crm_automation_field._require_real_field` checks a CRM Task can_read/
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
				# Carry the inner field's own pick source (Link target / Select options) alongside
				# kind="child" - so a child-table criterion renders a real Link/Select control, not a
				# bare text box (Task-2 minor, Task 14 fix).
				pick = {"kind": "child", "path": path}
				inner = _pick_for(cf.fieldtype, cf.options)
				if inner:
					pick.update({k: v for k, v in inner.items() if k != "kind"})
				out.append({
					"key": path,
					"label": cf.label or cf.fieldname,
					"type": cf.fieldtype,
					"pick": pick,
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


# TATVA (A.14): the pre-v2 `describe()` emitter (trigger_type/task_type/watch_doctype vocabulary)
# was retired here — it was a parallel brain to builder_schema below, with zero live callers (the
# Rule form and validate() have read builder_schema exclusively since Task 14). Its only caller was
# test_describe_watch_fields.py, deleted alongside it (behaviour re-proven by test_builder_contract's
# TestBuilderSchemaFields, which already covers read-allowlist gating + live-meta typing).

# -- v2 builder contract (Task 14 / plan Part G) ------------------------------
#
# builder_schema() is the ONE emitter the v2 Rule Form Script renders When->If->Then from, and the
# ONLY vocabulary CRMAutomationRule.validate() re-derives to reject a deviating rule (A.8 - one
# contract, no fourth vocabulary). Every piece is reused from an existing brain, never re-listed:
# fields <- field_catalog/activity_schema_fields, operators <- rules.py's operator-family dicts,
# verbs <- actions._ACTION_LANES, set_targets <- fields.settable_rows (via _settable_fields above).

# Schema-type groupings for the operator table: which operator FAMILIES apply to which field type.
# The operator NAMES themselves are never re-typed here - they come straight off rules.py's own
# family dicts (the exact dispatch table rules._one_match reads), so the builder can never offer an
# operator the evaluator doesn't understand.
_TEXT_TYPES = ("Data", "Small Text", "Text", "Long Text", "Code", "Text Editor")
_CHOICE_TYPES = ("Select", "Link", "Dynamic Link")
_NUMERIC_TYPES = ("Int", "Float", "Currency", "Percent", "Duration")
_TEMPORAL_TYPES = ("Date", "Datetime", "Time")
_BOOL_TYPES = ("Check",)
_PRESENCE_ONLY_TYPES = ("Attach", "Attach Image")
_SCHEMA_TYPES = _TEXT_TYPES + _CHOICE_TYPES + _BOOL_TYPES + _NUMERIC_TYPES + _TEMPORAL_TYPES + _PRESENCE_ONLY_TYPES


def _op_names(*groups):
	"""Flatten rules.py operator-family dicts/sets into one ordered, deduped operator-name list."""
	seen, out = set(), []
	for group in groups:
		for name in group:
			if name not in seen:
				seen.add(name)
				out.append(name)
	return out


def _operators_for_schema_type(ftype):
	"""Which of rules.py's operator families a schema type accepts - the ONE place that decision is
	made (describe emits it, the rule controller re-derives the same call, no second table)."""
	if ftype in _TEXT_TYPES:
		return _op_names(rules._EQUALITY_OPS, rules._TEXT_OPS, rules._MEMBERSHIP_OPS, rules._PRESENCE_OPS, rules._CHANGE_OPS)
	if ftype in _CHOICE_TYPES:
		return _op_names(rules._EQUALITY_OPS, rules._MEMBERSHIP_OPS, rules._PRESENCE_OPS, rules._CHANGE_OPS)
	if ftype in _BOOL_TYPES:
		return _op_names(rules._EQUALITY_OPS, rules._CHANGE_OPS)
	if ftype in _NUMERIC_TYPES or ftype in _TEMPORAL_TYPES:
		return _op_names(rules._EQUALITY_OPS, rules._ORDER_OPS, rules._RANGE_OPS, rules._PRESENCE_OPS, rules._CHANGE_OPS)
	if ftype in _PRESENCE_ONLY_TYPES:
		return _op_names(rules._PRESENCE_OPS, rules._CHANGE_OPS)
	return _op_names(rules._EQUALITY_OPS, rules._PRESENCE_OPS, rules._CHANGE_OPS)  # any other real field type


def operators_by_type():
	"""`operators_by_type` for the builder contract - the frozen v2 word-operator set (rules.py),
	grouped by schema type. ONE source; describe never hardcodes an operator name a second time."""
	return {ftype: _operators_for_schema_type(ftype) for ftype in _SCHEMA_TYPES}


def _typed_catalog(doctype):
	"""field_catalog(doctype) unioned with CRM Task's activity-schema fields, in the SAME typed
	{key,label,type,pick} shape - the vocabulary the v2 builder's field picker renders from. Reuses
	activity_schema_fields() - the SAME union fields_for_doctype and router._field_types_for already
	read at fire time - no parallel activity-schema walk (A.8)."""
	catalog = field_catalog(doctype)
	if doctype == "CRM Task":
		present = {d["key"] for d in catalog}
		for fieldname, r in activity_schema_fields().items():
			if fieldname in present:
				continue
			catalog.append({
				"key": r.fieldname,
				"label": r.label or r.fieldname,
				"type": r.fieldtype,
				"pick": _pick_for(r.fieldtype, r.options),
			})
	return catalog


def _criterion_fields(doctype):
	"""`fields` for the builder contract: the typed catalog INTERSECTED with the enabled READ allowlist
	(fields.readable_fields) - the builder can only offer a field the engine is actually allowed to test
	(Part H: the allowlist is both the security fence and the builder vocabulary). Reading is not
	watching: a field a rule may TEST need not be one whose change may FIRE the rule."""
	if not doctype:
		return []
	allowed = set(fields.readable_fields(doctype))
	return [d for d in _typed_catalog(doctype) if d["key"] in allowed]


# Which of the CRM Automation Action doctype's OWN fields are a given verb's params - the builder
# reads their type/options straight off that doctype's live meta (_verb_params), so a schema change
# there (a relabel, a new picker) reaches the builder with zero edits here.
_VERB_PARAM_FIELDS = {
	"Require Fields": ["require_fields"],
	"Require Location": ["geofence_meters"],
	"Create Task": ["task_type", "due_mode", "due_from", "due_expression"],
	"Update Field": ["target_doctype", "fieldname", "value_mode", "value", "context_field", "expression"],
	"Append Child Row": ["child_table", "set_json"],
	"Upsert Child Row": ["child_table", "match_json", "set_json"],
	"Call Webhook": ["webhook_endpoint", "webhook_payload_source"],
	"Create Note": ["comment_mode", "comment_text", "comment_expression"],
	"Send WhatsApp": ["whatsapp_template"],
	"Send Email": ["email_recipient", "email_subject", "email_body"],
	"Wait": ["wait_expression"],
}


def _verb_params(verb):
	meta = frappe.get_meta("CRM Action Group Item")
	out = []
	for fieldname in _VERB_PARAM_FIELDS.get(verb, []):
		df = meta.get_field(fieldname)
		if not df:
			continue
		out.append({
			"name": df.fieldname,
			"label": df.label or df.fieldname,
			"type": df.fieldtype,
			"pick": _pick_for(df.fieldtype, df.options),
		})
	return out


def builder_verbs():
	"""`verbs` for the builder contract: every registered verb (actions._ACTION_LANES, the ONE verb
	registry) with its lane + typed param fields. A verb sitting in the action_type Select with no
	_ACTION_LANES entry is invisible to the builder - the same guardrail CRMAutomationRule.validate
	already enforces at save time (Task 8), so the builder can never offer a dead verb either."""
	from tatva_connect.automation.actions import _ACTION_LANES

	return [{"verb": verb, "lane": lane, "params": _verb_params(verb)} for verb, (lane, _handler) in _ACTION_LANES.items()]


def coerces(value, ftype):
	"""True if `value` can legitimately be interpreted as schema type `ftype` - the value half of the
	builder contract (CRMAutomationRule.validate calls this per criterion/value). Blank always coerces
	(per-operator requiredness - e.g. `is set` needing no value at all - is the caller's job). A
	Select/Link's value is bounded by its own option list / a Link's existence, not this check; the
	types a bad string can silently mis-cast are Date/Datetime/Int/Float/Currency/Percent/Check, so
	those are the ones actually validated, via frappe.utils' own strict parsers (never a hand-rolled
	regex, A.18)."""
	if value in (None, ""):
		return True
	if ftype == "Datetime":
		try:
			frappe.utils.get_datetime(value)
			return True
		except Exception:
			return False
	if ftype == "Date":
		try:
			frappe.utils.getdate(value)
			return True
		except Exception:
			return False
	if ftype == "Int":
		try:
			int(str(value).strip())
			return True
		except (TypeError, ValueError):
			return False
	if ftype in ("Float", "Currency", "Percent"):
		try:
			float(str(value).strip())
			return True
		except (TypeError, ValueError):
			return False
	if ftype == "Check":
		return str(value).strip() in ("0", "1")
	return True


@frappe.whitelist()
def builder_schema(on_doctype=None, event=None, vertical=None, group=None, program=None):
	"""THE one authoring contract: the Flow form's When/Then builder renders fields/operators/verbs from
	this. Grain-scoped, whitelisted, read-only, permission-gated (read on CRM Workflow Definition)."""
	if not frappe.has_permission("CRM Workflow Definition", "read"):
		frappe.throw(frappe._("Not permitted"), frappe.PermissionError)
	return {
		"fields": _criterion_fields(on_doctype),
		"operators_by_type": operators_by_type(),
		"verbs": builder_verbs(),
		"set_targets": _settable_fields(vertical, group, program),
	}
