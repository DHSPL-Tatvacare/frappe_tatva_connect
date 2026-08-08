"""The automation builder's describe contract — the single, derived source the Rule form renders
from and the controller validates against.

No field/operator/value list is hardcoded in the UI: everything flows from one place — the task
type's activity schema (`CRM Task Type Field`, which carries fieldtype + options) scoped to what the
workflow's grain entitles. Add a field to a task type's form and it appears in the builder, with
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


# Layout, not data. `Tab Break` was missing, so a form's tab was offered as a field a rule could test.
_STRUCTURAL_FIELDTYPES = ("Column Break", "Section Break", "Tab Break", "HTML", "Button", "Fold")


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
	`activity.api.compute_activity` homes through `field_target` — a retained common column on the task
	row, or the section row that addresses it - it is NEVER a CRM Task doctype field itself. So the
	vocabulary here is unioned with every distinct activity-schema fieldname (meta wins on a name
	clash) - the SAME union `router._activity_values` resolves at fire time (one brain, no drift).

	CRM Lead is under-described by its meta for the mirror-image reason: a lead's counters and profile
	values live in SINGLETON CHILD tables, so the meta carries the Table field and never its columns. They
	are unioned in under `<child_table>.<column>` — the dotted path `field_catalog` already offers and
	`context.section_values` writes at fire time."""
	if not doctype:
		return []
	descriptors = [_descriptor(df.fieldname, df.label, df.fieldtype, df.options) for df in _meta_fields(doctype)]
	if doctype == "CRM Task":
		present = {d["key"] for d in descriptors}
		for fieldname, r in activity_schema_fields().items():
			if fieldname in present:
				continue
			descriptors.append(_descriptor(r.fieldname, r.label, r.fieldtype, r.options))
	if doctype == "CRM Lead":
		from tatva_connect.partner_api.doctype.crm_lead_section import crm_lead_section

		tables = {s.child_table_field for s in crm_lead_section.child_sections()}
		descriptors += [d for d in field_catalog(doctype) if d["key"].split(".", 1)[0] in tables]
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


def _settable_fields(doctype, vertical, group, program):
	"""Fields a write node may target ON `doctype` at this workflow's grain, typed for the control.

	The grain here is the workflow's DECLARED one — a RULE grain whose blank axis means ANY — so the rows
	come from `fields.settable_rows_in_rule_grain` and never from the data-grain resolver. Handing a rule
	grain to that one compared the wildcard as the literal empty string, and a workflow declaring
	`vertical=X, group=""` was offered only fields whose contract was equally blank: a field ticked by
	`(X, G1, "")` was hidden, though execution would have allowed the write.

	Typed off the doctype's own meta, falling back for CRM Task to the activity schema — a task's real
	business fields are `CRM Task Type Field` rows and never meta fields, so dropping them would answer
	empty on exactly the subject the doctype fix was for. Same union `fields_for_doctype` already reads.

	A FIELD IS LISTED ONCE, not once per contract row that ticks it. A rule grain's blank axis means ANY,
	so a field ticked by two contracts at different grains matches twice and the picker offered it twice —
	`custom_substage` on the wire, 4 rows with one duplicate. Deduped HERE, at the source: the consumer's
	own dedupe stays as a belt (a picker showing one field twice is a defect whatever caused it) but is no
	longer the only thing preventing it. First row wins; they describe the same field.
	"""
	meta = frappe.get_meta(doctype)
	schema = activity_schema_fields() if doctype == fields.TASK_DT else {}
	out, seen = [], set()
	for r in fields.settable_rows_in_rule_grain(doctype, (vertical, group, program)):
		if r.fieldname in seen:
			continue
		df = meta.get_field(r.fieldname)
		if df:
			out.append({**_descriptor(r.fieldname, df.label, df.fieldtype, df.options), "doctype": doctype})
		elif r.fieldname in schema:
			s = schema[r.fieldname]
			out.append({**_descriptor(s.fieldname, s.label, s.fieldtype, s.options), "doctype": doctype})
		else:
			continue  # neither a meta field nor a schema field — nothing was appended, so nothing is seen
		seen.add(r.fieldname)
	return out


def _settable_targets(subject, vertical, group, program):
	"""Every field a write node in this workflow may target, across every record the journey can reach.

	It used to ask for `CRM Lead` whatever the workflow watched, so a Task-triggered workflow offered the
	Target `CRM Task` and then listed LEAD fields underneath it — two controls describing different
	records. The reachable set is read from the ONE declaration (`actions.reachable_targets`), the same
	one `_resolve_write_target` enforces and the publish gate checks, so the three cannot drift.

	Each descriptor carries the `doctype` it belongs to: a flat list spanning two records cannot be
	rendered under a chosen Target without it.

	The lead's CHILD sections are reachable too — a child-row node names one and then sets its columns,
	which is the same Field Map asking the same question about a different record. Listed from
	`crm_lead_section.child_sections()`, so a section added tomorrow is offered with nothing to regenerate.
	"""
	from tatva_connect.automation import actions
	from tatva_connect.partner_api.doctype.crm_lead_section import crm_lead_section

	targets = actions.reachable_targets(subject) + [s.target_doctype for s in crm_lead_section.child_sections()]
	return [
		descriptor
		for dt in dict.fromkeys(targets)
		for descriptor in _settable_fields(dt, vertical, group, program)
	]


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
# verbs <- actions.VERBS, set_targets <- fields.settable_rows (via _settable_fields above).

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


def _criterion_fields(doctype, vertical, group, program):
	"""`fields` for the builder contract: the typed catalog scoped to what this workflow's GRAIN entitles.

	The scope is `fields.settable_rows_in_rule_grain` — the same rows `_settable_fields` reads, so the
	criterion picker and the write picker cannot offer different sets, and nothing is offered that
	execution would refuse. It replaced a per-field read allowlist that answered a question the grain
	contract already answered, differently."""
	if not doctype:
		return []
	allowed = {r.fieldname for r in fields.readable_rows_in_rule_grain(doctype, (vertical, group, program))}
	return [d for d in _typed_catalog(doctype) if d["key"] in allowed]


# Which of the CRM Automation Action doctype's OWN fields are a given verb's params - the builder
# reads their type/options straight off that doctype's live meta (_verb_params), so a schema change
# there (a relabel, a new picker) reaches the builder with zero edits here.
# Each verb's parameters, DECLARED here — name, label, type and options. They used to be read off the
# CRM Action Group Item doctype's meta, which meant the builder's contract was a side effect of a
# 26-column table holding the union of every verb's fields. A verb owns its own parameters; W1 turns
# this into the node-type registry, and this is that declaration's first form.

def builder_verbs():
	"""`verbs` for the builder contract, read from the ONE verb declaration (`actions.VERBS`).

	A verb's lane and its parameters are declared beside the handler that runs them, so the builder can
	never offer a verb the engine cannot run, nor a parameter the handler does not read.
	"""
	from tatva_connect.automation import actions

	return [
		# `params_of`, never `declared["params"]`: a field a provider does not support must not be
		# advertised here while the inspector hides it — one question, one answer.
		{"verb": verb, "lane": declared["lane"], "params": actions.params_of(verb)}
		for verb, declared in actions.VERBS.items()
	]


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
	this. Grain-scoped, whitelisted, read-only, permission-gated (read on CRM Workflow)."""
	if not frappe.has_permission("CRM Workflow", "read"):
		frappe.throw(frappe._("Not permitted"), frappe.PermissionError)
	return {
		"fields": _criterion_fields(on_doctype, vertical, group, program),
		"operators_by_type": operators_by_type(),
		"operator_shapes": operator_shapes(),
		"verbs": builder_verbs(),
		"set_targets": _settable_targets(on_doctype, vertical, group, program),
	}


def operator_shapes():
	"""Which operators take no value, a range, or a list — read from the operator families themselves.

	The builder has to know this to render the right widget, and it used to know it by keeping its own
	copy of the operator names in JavaScript. That copy had no lock: renaming an operator in Python
	would leave the control silently rendering a text box for `is set`, which then saves a value the
	evaluator ignores.
	"""
	return {
		"none": sorted(rules._PRESENCE_OPS),
		"range": sorted(rules._RANGE_OPS),
		"list": sorted(rules._MEMBERSHIP_OPS),
	}
