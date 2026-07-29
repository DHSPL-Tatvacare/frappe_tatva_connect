"""The activity engine's server brain.

An "activity" is a CRM Task of an activity type (a CRM Task Type whose composite key carries
a grain), logged complete on save. One writer (`save_activity`) routes every submitted answer
through `field_target` — the retained common columns, or a section child row. One projection (`lead_timeline`) feeds BOTH
the SPA Activity timeline and the Desk Lead timeline. Availability is grain-scoped through
the single brain `taxonomy.grain.resolve_scoped` — nothing here hardcodes a type or grain.
Ships dormant: a CRM Task Type with an all-blank grain never surfaces as an activity.
"""
import json
from collections import Counter
from urllib.parse import parse_qs, urlparse

import frappe
from frappe import _
from frappe.model import NO_VALUE_FIELDS
from frappe.utils import cint, cstr, flt, format_datetime, formatdate, get_datetime

from tatva_connect.taxonomy import labels
from tatva_connect.taxonomy.grain import resolve_scoped
from tatva_connect.taxonomy.labels import TASK_TYPE

# The common CRM Task columns the plan RETAINS, so a field naming one keeps the home it already has
# (§8 rule 2). Nothing else is enumerated: a slot is simply a target neither a section nor this claims.
# The CRM Task columns a declared field may keep living on — RETAINED on the row, not merely rep-facing: custom_asm is operational but is still validated (_validate_asm) and must never fall to a section row.
COMMON_COLUMNS = ("custom_outcome", "custom_followup_at", "custom_scheduled_at", "custom_asm")


# The rule grammar, named here the way a fieldtype is named: these ARE the language (D27/D28), declared as
# the Select options of CRM Task Type Rule and read back by the compile below. The doctype JSON is the one
# home; tests/activity/test_rule_compilation.py fails if the two ever disagree.
RULE_SHOW, RULE_HIDE, RULE_MANDATORY = "Show", "Hide", "Make Mandatory"
RULE_ACTIONS = (RULE_SHOW, RULE_HIDE, RULE_MANDATORY)
# The operators that compare against a value, so the value is validated against the field's options; the
# other two ask only whether an answer exists (D27).
RULE_VALUE_OPERATORS = ("is", "is not")
RULE_OPERATORS = (*RULE_VALUE_OPERATORS, "is set", "is not set")

# A declared field's `source`: whose record the answer belongs to. Lead answers live on the LEAD through the
# lead's own brain and are never copied onto the task (D11/D31).
LEAD_SOURCE = "Lead"


def field_target(f):
	"""THE home a declared activity field lands in — `(section_key, address)`, section None meaning the task row itself. Since Phase 5 it is the ONLY write seam, and since Phase 7 the only read seam too: there is no second home left to ask about."""
	target = f.get("target") or ""
	section = f.get("section") or ""
	if section and target and frappe.get_meta(
		frappe.get_cached_value("CRM Task Section", section, "target_doctype")
	).get_field(target):
		return section, target
	if target in COMMON_COLUMNS:
		return None, target
	key_value = _key_value_section()
	if key_value is None:
		# Never (None, fieldname) — that reads as "the task row itself", and the caller would write the
		# answer to a column of that name which does not exist. Say what is actually wrong instead.
		frappe.throw(_("No CRM Task Section is declared key-value, so `{0}` has nowhere to land. The "
		               "declaration is seeded by section_seed.ensure_rows on after_migrate.").format(
			f.get("fieldname")))
	return key_value, f.get("fieldname")


def _key_value_section():
	"""The section a field carrying no shape of its own answers in — the one the operator declared
	key-value, so the default home is read off the declaration and never named in code.

	None when the declaration has not been seeded yet. `CRM Task Section` rows are written by
	`section_seed.ensure_rows` on **after_migrate**, which runs AFTER post-model-sync patches — so a patch
	asking this question on a site mid-upgrade has to be able to hear "not yet" instead of an IndexError
	that aborts the whole migrate. It did abort one, on 2026-07-27."""
	rows = frappe.get_all(
		"CRM Task Section", filters={"is_key_value": 1}, order_by="display_order", limit=1, pluck="name"
	)
	return rows[0] if rows else None


def sections_ready():
	"""Has the operator's CRM Task Section declaration landed? THE readiness question, asked by everything
	that can run before `after_migrate` has seeded it — so a caller can skip-until-ready rather than crash,
	and so no caller has to know which row proves it."""
	return _key_value_section() is not None


# The column a declared fieldtype's answer can be COMPARED in beside the one it is read from, and the
# cast that fills it (D17). ONE map: the writer fills the column and Smart Views filter and sort on it,
# so the same answer can never be a date on the write side and a string on the read side.
_TYPED_COLUMNS = {
	"Date": ("value_datetime", get_datetime),
	"Datetime": ("value_datetime", get_datetime),
	"Int": ("value_number", flt),
	"Float": ("value_number", flt),
	"Currency": ("value_number", flt),
	"Check": ("value_number", flt),
}


def typed_column(fieldtype):
	"""The column an answer of this declared fieldtype compares in, or None when the column it is read from is the only one it has."""
	spec = _TYPED_COLUMNS.get(fieldtype or "")
	return spec[0] if spec else None


def _row_values(section, address, fieldtype, value):
	"""The columns ONE section row carries for one field: a key-value row is addressed by the field's own
	fieldname and answers in the column the section DECLARES as its value field — always, because that is
	the column every consumer reads through — with the declared fieldtype's typed mirror of the same answer
	beside it, so a date compares as a date and a number as a number. A column section names its column
	outright. The section is the only thing that decides which shape applies."""
	if not section.is_key_value:
		return {address: value}
	row = {section.row_key_field: address, section.value_field: cstr(value)}
	spec = _TYPED_COLUMNS.get(fieldtype or "")
	if spec:
		column, cast = spec
		row[column] = None if value in (None, "") else cast(value)
	return row


def _put_section_value(doc, f, value):
	"""Dual-write leg for a live CRM Task: put ONE field's value in the new home field_target names,
	upserting the row it addresses so a re-write never grows a second one. Returns True iff a row changed.
	Rule 2 writes nothing — the retained common column the caller already set IS the new home."""
	section_key, address = field_target(f)
	if section_key is None:
		return False
	section = frappe.get_cached_doc("CRM Task Section", section_key)
	values = _row_values(section, address, f.fieldtype, value)
	rows = doc.get(section.child_table_field) or []
	row = (next((r for r in rows if r.get(section.row_key_field) == address), None)
		   if section.is_key_value else (rows[0] if rows else None))
	# A blank earns no row: key-value drops the row (clearing it), a column row keeps its siblings and just blanks its own.
	if value in (None, ""):
		if row is None:
			return False
		if section.is_key_value:
			doc.remove(row)
			return True
	if row is None:
		doc.append(section.child_table_field, values)
		return True
	if all(row.get(k) == v for k, v in values.items()):
		return False
	row.update(values)
	return True


def _stage_section_value(staged, f, value):
	"""Dual-write leg for a task that is only a dict so far: stage ONE field's value into
	{child_table_field: [rows]} — the shape Document.update applies to a Table field natively, so the same
	answer reaches the same row whether the task is being created or completed."""
	section_key, address = field_target(f)
	if section_key is None:
		return
	section = frappe.get_cached_doc("CRM Task Section", section_key)
	# Same rule as `_put_section_value`, and simpler here: this task is being CREATED, so there is no earlier
	# answer to clear — a blank of either shape just earns no row and no column.
	if value in (None, ""):
		return
	rows = staged.setdefault(section.child_table_field, [])
	values = _row_values(section, address, f.fieldtype, value)
	if section.is_key_value or not rows:
		rows.append(values)
	else:
		rows[0].update(values)


def set_schema_field(task, task_type, fieldname, value):
	"""Write ONE declared activity-schema field onto an existing CRM Task, routed by the SAME rule
	compute_activity uses (field_target): a field the task row keeps lands on its retained common column,
	every other answer lands in the section row that addresses it. Raises if the field is not declared on
	the type — so no out-of-declaration key can be poked into a home nothing declared (the second-writer
	bug this replaces). Returns True iff it changed.

	Phase 7: the slot columns and the JSON payload are gone, so a field the task row does not keep has
	exactly ONE home and `_put_section_value` is the whole of the write."""
	f = next((x for x in frappe.get_doc("CRM Task Type", task_type).schema if x.fieldname == fieldname), None)
	if not f:
		frappe.throw(_("{0} is not a declared field of activity type {1}.").format(fieldname, task_type))
	changed = _put_section_value(task, f, value)
	section_key, column = field_target(f)
	if section_key is None:
		if task.get(column) == value:
			return changed
		task.set(column, value)
		return True
	return changed


def _lead_axes(lead):
	v = frappe.db.get_value(
		"CRM Lead", lead, ["custom_vertical", "custom_group", "custom_current_program"], as_dict=True
	)
	if not v:
		frappe.throw(_("Lead {0} not found").format(lead))
	return (v.custom_vertical or ""), (v.custom_group or ""), (v.custom_current_program or "")


def _grain_of(task_type):
	"""The activity type's grain — the PARENT record's composite key (vertical::group::program::
	type_name), which is the ONE grain source. Returns a {vertical, group, program} dict (the
	resolve_scoped candidate shape) or None for a dormant/unscoped type."""
	g = frappe.db.get_value(
		"CRM Task Type", task_type, ["vertical", "`group` as grp", "program"], as_dict=True
	)
	if g and (g.vertical or g.grp or g.program):
		return {"vertical": g.vertical or "", "group": g.grp or "", "program": g.program or ""}
	return None


def _grain_matches(grain, vertical, group, program):
	"""THE one availability predicate, shared by the picker (list_types_for_lead) and the gate
	(_scope_applies): a SET axis must equal the lead's; a BLANK axis is a wildcard; an ALL-BLANK grain
	is dormant (never available). Built on the shared resolve_scoped brain — no third definition."""
	if not grain or not (grain.get("vertical") or grain.get("group") or grain.get("program")):
		return False
	return resolve_scoped([grain], vertical, group, program) is not None


def _scope_applies(task_type, vertical, group, program):
	"""True if this activity type is available to the lead's grain — via the ONE shared predicate."""
	return _grain_matches(_grain_of(task_type), vertical, group, program)


def scope_applies_to_lead(task_type, lead):
	"""Same gate, keyed on the LEAD — the seam every task writer calls, so no caller assembles axes."""
	return _scope_applies(task_type, *_lead_axes(lead))


def _activity_type_names():
	"""Every CRM Task Type configured as an activity (grain-assigned) — the grain is the parent vertical."""
	return {r.name for r in frappe.get_all("CRM Task Type", filters={"vertical": ["!=", ""]}, fields=["name"])}


def resolve_type_for_lead(lead, type_name):
	"""Resolve a bare `type_name` to its grain-specific composite-key CRM Task Type for THIS lead's
	grain — most-specific-wins via the shared `resolve_scoped` brain. The composite key carries the
	grain, so the same type_name (e.g. "Welcome Call") resolves to the right record per grain. Returns
	the composite PK, or None if no grain-matching record exists. Any caller that holds a type_name
	(not a PK) — the LSQ migration, future imports — goes through here (no own codepath)."""
	if not type_name:
		return None
	vertical, group, program = _lead_axes(lead)
	candidates = [
		{"name": r.name, "vertical": r.vertical, "group": r.grp, "program": r.program}
		for r in frappe.get_all(
			"CRM Task Type",
			filters={"type_name": type_name},
			fields=["name", "vertical", "`group` as grp", "program"],
		)
	]
	if not candidates:
		# Not yet re-keyed (a name-keyed record still named exactly type_name) — use it as-is.
		return type_name if frappe.db.exists("CRM Task Type", type_name) else None
	winner = resolve_scoped(candidates, vertical, group, program)
	return winner["name"] if winner else None


def _type_has_schema(task_type):
	"""True if the type carries a form schema (≥1 field) — i.e. completing it must log details."""
	return bool(task_type and frappe.db.exists(
		"CRM Task Type Field", {"parent": task_type, "parenttype": "CRM Task Type"}
	))


def activity_is_unlogged(doc):
	"""True if this is a form-activity task being marked Done with NO details captured. The single
	definition of 'an activity completed empty' — used by the validate backstop (one brain) so the
	rule holds on every save path, not just the Form-view controller."""
	if (doc.status or "") != "Done":
		return False
	if not _type_has_schema(doc.custom_task_type):
		return False
	# Phase 7 dropped the JSON payload this used to read, so "was the form submitted?" is asked of the
	# storage the writer actually fills: a retained common column, or a row in one of the declared sections.
	# A raw set_value(status=Done) fills neither, which is the whole of what this backstop is for.
	if any(doc.get(c) for c in COMMON_COLUMNS):
		return False
	return not any(doc.get(s.child_table_field) for s in _sections() if s.child_table_field)


@frappe.whitelist()
def open_activity_tasks(lead):
	"""Open (not Done/Canceled) activity tasks on a lead — lets the client map a Tasks-tab row to
	the activity type/name so completing it opens the activity form instead of an empty status flip."""
	frappe.has_permission("CRM Lead", "read", doc=lead, throw=True)
	types = _activity_type_names()
	if not types:
		return []
	rows = frappe.get_all(
		"CRM Task",
		filters={
			"reference_docname": lead,
			"status": ["not in", ["Done", "Canceled"]],
			"custom_task_type": ["in", list(types)],
		},
		fields=["name", "title", "custom_task_type"],
		order_by="creation desc",
	)
	type_names = labels.labels([r.custom_task_type for r in rows], TASK_TYPE)
	for r in rows:
		r["custom_task_type_label"] = type_names.get(r.custom_task_type, "")
	return rows


@frappe.whitelist()
def list_types_for_lead(lead):
	"""Activity types available to this lead's grain — the searchable picker source. Grain lives on the
	PARENT (composite key); availability is the ONE shared `_grain_matches` predicate (same brain the
	gate uses) — a set axis equals the lead's, a blank axis is a wildcard, an all-blank grain is dormant.
	Native `frappe.get_all` pre-filters to candidate grains (no raw SQL), then the predicate decides.
	Value = the composite PK (`name`); label = the clean `type_name`."""
	if not frappe.flags.ignore_permissions:  # partner API runs trusted (gated by mapping + grain)
		frappe.has_permission("CRM Lead", "read", doc=lead, throw=True)
	vertical, group, program = _lead_axes(lead)
	rows = frappe.get_all(
		"CRM Task Type",
		filters={
			"vertical": ["in", ["", vertical]],
			"group": ["in", ["", group]],
			"program": ["in", ["", program]],
		},
		fields=["name", "type_name", "vertical", "`group` as grp", "program", "is_logged_complete", "visit_mode"],
		order_by="type_name",
	)
	out = []
	for r in rows:
		if not _grain_matches({"vertical": r.vertical, "group": r.grp, "program": r.program}, vertical, group, program):
			continue
		out.append({
			"name": r.name, "label": r.type_name or r.name,
			"is_logged_complete": int(r.is_logged_complete or 0), "visit_mode": r.visit_mode or "",
		})
	return out


def _field_descriptor(f):
	"""One shape for an activity field descriptor from a CRM Task Type schema row — the client form.

	A `_dict` rather than a plain dict so the SAME object serves the renderer (`d["fieldname"]`) and the
	writers, which read a schema row's attributes (`f.fieldtype`). One descriptor list, two access styles,
	so the compile below can hand `compute_activity` exactly what `_type_config` hands the form."""
	return frappe._dict({
		"label": f.label,
		"fieldname": f.fieldname,
		"fieldtype": f.fieldtype,
		"options": f.options or "",
		"reqd": int(f.reqd or 0),
		"target": f.target or "",
		"section": (f.get("section") or ""),
		"source": (f.get("source") or ""),
		"read_only": 0,  # fill-once closes a lead field for THIS lead; stamped by _mark_lead_read_only
		"depends_on": (f.get("depends_on") or ""),
		"mandatory_depends_on": (f.get("mandatory_depends_on") or ""),
		"container_depends_on": [],  # the conditions of the tab/section/column holding it; stamped by _layout
	})


def _rule_atom(row):
	"""ONE rule row's When columns as an expression, in the syntax BOTH shipped evaluators read alike.

	A blank When is "always" (D25/§17.2), so it is the constant 1. Every operator (D27) compiles to a
	COMPARISON because a comparison is the largest syntax the two evaluators share: the server's is Python
	`safe_eval` (`_field_visible`) and the client's is a JS `new Function` (`utils/expressions.js`), so
	`and`/`or`/`not` parse only in one and `&&`/`||`/`!` only in the other. `_rule_or` / `_rule_not` below
	therefore combine with arithmetic, which reads identically in both. A value is JSON-quoted, which is
	also a literal both languages accept."""
	field = (row.condition_field or "").strip()
	if not field:
		return "1"
	ref = "doc." + field
	value = json.dumps(cstr(row.condition_value or ""))
	operator = (row.operator or RULE_OPERATORS[0]).strip()
	if operator == "is not":
		return f"{ref}!={value}"
	if operator == "is set":
		return f'{ref}!=""'
	if operator == "is not set":
		return f'{ref}==""'
	return f"{ref}=={value}"


def _rule_or(atoms):
	"""OR over rule atoms. `+` because it is truthy-summing in Python and in JS alike — see `_rule_atom`."""
	return "+".join(f"({a})" for a in atoms)


def _rule_not(expr):
	"""NOT of a rule expression, as a comparison — the one negation both evaluators agree on. The outer
	parentheses are load-bearing: `*` binds tighter than `==` in Python AND JS, so without them a spliced
	`(a)*(b)==0` parses as `((a)*(b))==0` and every conditional Hide stops overriding its Show."""
	return f"(({expr})==0)"


def _rules_by_target(tt):
	"""The type's rule rows keyed by the field they act on: {fieldname: {action: [(atom, is_conditional)]}}.

	One walk of the child table; a rule naming five targets is five entries of the same atom, which is the
	whole of what "flat rows" (D9) means. `is_conditional` distinguishes a row with a When from a blank one,
	because a blank-When row is the form's OPENING state (D25) and not a veto on every later reveal."""
	out = {}
	for row in tt.get("rules") or []:
		atom = _rule_atom(row)
		conditional = bool((row.condition_field or "").strip())
		for target in _rule_targets(row):
			out.setdefault(target, {}).setdefault(row.action or "", []).append((atom, conditional))
	return out


def _rule_targets(row):
	"""The fieldnames one rule row acts on — comma-separated text (D26), because a child table cannot hold
	a multiselect (`model/__init__.py:99`); the Desk script paints the picker that appends to it."""
	return [t.strip() for t in (row.targets or "").split(",") if t.strip()]


def _compiled_visibility(entry, own):
	"""ONE field's compiled `depends_on` (§17.3): visible = OR(its Show rows) AND NOT OR(its CONDITIONAL
	Hide rows).

	A field no rule names keeps the condition its own row declares — a type with no rules is byte-identical
	to what shipped. A field with no Show row is visible by default, unless a blank-When Hide row names it:
	that row is the opening state, so the field starts closed and only a Show row reveals it (D25 — "a field
	named in an onload Hide with Show rules elsewhere compiles to OR(those conditions)"). A blank-When Hide
	is therefore the baseline and never appears inside the negation, which is what stops it cancelling every
	reveal declared against it."""
	shows = entry.get(RULE_SHOW) or []
	hides = entry.get(RULE_HIDE) or []
	if not shows and not hides:
		return own
	conditional_hides = [atom for atom, conditional in hides if conditional]
	if shows:
		base = _rule_or([atom for atom, _ in shows])
	else:
		base = "0" if len(conditional_hides) < len(hides) else "1"
	expr = base if not conditional_hides else f"({base})*{_rule_not(_rule_or(conditional_hides))}"
	return "eval:" + expr


def _compiled_mandatory(entry, own):
	"""ONE field's compiled `mandatory_depends_on` (§17.3): OR of the conditions its Make Mandatory rows
	name. A field no such row names keeps its own declared condition. Static `reqd` is NOT folded in here —
	it stays its own flag, and `_required_here` asks for either."""
	rows = entry.get(RULE_MANDATORY) or []
	if not rows:
		return own
	return "eval:" + _rule_or([atom for atom, _ in rows])


def _compiled_rows(tt):
	"""Every declared ROW of a task type — layout markers included, in declaration order — with the type's
	RULES compiled into each one's `depends_on` and `mandatory_depends_on` (§17.3).

	A marker is compiled like any other row on purpose: that is what lets a rule Show or Hide a whole
	section, which is how the source forms behave, without a second kind of rule."""
	by_target = _rules_by_target(tt)
	out = []
	for f in tt.schema:
		d = _field_descriptor(f)
		entry = by_target.get(f.fieldname) or {}
		d.depends_on = _compiled_visibility(entry, d.depends_on)
		d.mandatory_depends_on = _compiled_mandatory(entry, d.mandatory_depends_on)
		out.append(d)
	return out


def _layout(rows):
	"""The form's LAYOUT: the declared rows walked ONCE into tabs -> sections -> columns -> fields, exactly
	the way `frappe/public/js/frappe/form/layout.js` walks a DocType's docfields.

	The point of copying Frappe here is the property that walk has and a filter-and-flow grid does not: a
	field belongs to whichever column was open when it was DECLARED, so its column can never change with what
	happens to be visible. A revealed neighbour pushes it down its own column and never sideways.
	A type declaring no markers yields one tab, one section, one column — the flat form, unchanged.

	A container carries only what it is CALLED. It carries no condition: the conditions of the containers
	holding a field are stamped on that field instead, as `container_depends_on`, and a container is on
	screen exactly when it still holds a field that is — which is what Frappe decides in `refresh_sections`
	rather than re-testing the section's own condition. Stating a condition on both would be one fact in two
	places, and the two could disagree.

	A column holds fieldNAMES, not descriptors: the descriptors are the flat `fields` list this is returned
	beside, and sending them twice would put a second copy of every declaration on the wire for a client that
	addresses them by name anyway."""
	tabs, index, gate = [], 0, {}

	def opened(kind, d):
		nonlocal index
		index += 1
		gate[kind] = (d.depends_on or "") if d else ""
		return {"key": (d.fieldname if d else "") or f"{kind}-{index}",
				"label": (d.label or "") if d else ""}

	def start_tab(d=None):
		tabs.append({**opened("tab", d), "sections": []})
		start_section()

	def start_section(d=None):
		tabs[-1]["sections"].append({**opened("section", d), "columns": []})
		start_column()

	def start_column(d=None):
		tabs[-1]["sections"][-1]["columns"].append({**opened("column", d), "fields": []})

	start_tab()
	for d in rows:
		if d.fieldtype == "Tab Break":
			start_tab(d)
		elif d.fieldtype == "Section Break":
			start_section(d)
		elif d.fieldtype == "Column Break":
			start_column(d)
		elif d.fieldtype in NO_VALUE_FIELDS:
			continue  # a marker this form has no layout meaning for stores nothing and renders nothing
		else:
			d.container_depends_on = [c for c in (gate["tab"], gate["section"], gate["column"]) if c]
			tabs[-1]["sections"][-1]["columns"][-1]["fields"].append(d.fieldname)
	return _prune(tabs)


def _prune(tabs):
	"""Drop every container holding no field. A declaration opening with a Section Break, or carrying two
	markers back to back, otherwise leaves a container with nothing in it — structure that draws nothing and
	that the client would have to know to ignore."""
	for tab in tabs:
		for section in tab["sections"]:
			section["columns"] = [c for c in section["columns"] if c["fields"]]
		tab["sections"] = [s for s in tab["sections"] if s["columns"]]
	return [t for t in tabs if t["sections"]]


def compiled_layout(tt):
	"""The one reading of a task type's declaration, projected twice: `fields` is the flat list every READER
	of an answer walks, `tabs` is the tree the FORM renders and it NAMES those same fields rather than
	restating them — so what the rep is shown and what the save enforces can never be two answers.

	Layout markers are filtered out of `fields` by `NO_VALUE_FIELDS` — Frappe's own list, the same one that
	keeps a Section Break out of a table's columns — so nothing that stores or reads an answer ever meets
	one."""
	rows = _compiled_rows(tt)
	tabs = _layout(rows)
	return [d for d in rows if d.fieldtype not in NO_VALUE_FIELDS], tabs


def compiled_fields(tt):
	"""Every declared FIELD of a task type with its rules compiled in — `get_schema` publishes this and
	`compute_activity` enforces it. Layout is the other half of the same walk; see `compiled_layout`."""
	return compiled_layout(tt)[0]


@frappe.whitelist()
def get_schema(task_type):
	"""The activity type's per-field schema, in order, with its rules compiled in — for the client form."""
	if not frappe.flags.ignore_permissions:  # partner API runs trusted (gated by mapping + grain)
		frappe.has_permission("CRM Task Type", "read", doc=task_type, throw=True)
	return compiled_fields(frappe.get_doc("CRM Task Type", task_type))


def _validate_asm(asm):
	"""An ASM stamped onto an activity must hold the 'Sales Manager' role — keeps the audited ASM data
	clean to actual Sales Managers. No-op when no ASM is set."""
	if not asm:
		return
	if "Sales Manager" not in frappe.get_roles(asm):
		frappe.throw(
			_("{0} is not a Sales Manager and cannot be set as ASM.").format(
				frappe.db.get_value("User", asm, "full_name") or asm),
			title=_("Invalid ASM"),
		)


def _field_visible(depends_on, values):
	"""Mirror the client's evaluateDependsOnValue: a field whose depends_on doesn't pass is hidden, so
	it is never submitted and must NOT be enforced as required. Supports 'eval:<expr>' (doc.<field>
	against the submitted values) and a bare fieldname (truthy). Blank/unparseable => visible (same as
	the client, which treats an eval error as shown). One brain for the required-when rule."""
	cond = (depends_on or "").strip()
	if not cond:
		return True
	if cond.startswith("eval:"):
		try:
			return bool(frappe.utils.safe_eval(cond[5:], None, {"doc": frappe._dict(values or {})}))
		except Exception:
			return True
	return bool((values or {}).get(cond))


def _shown_here(f, values):
	"""True when the form shows this field for these answers: its own condition passes AND every container
	holding it — tab, section, column — is open. The container conditions were stamped on the descriptor by
	the one layout walk, so this asks `_field_visible` and never a second rule, and never a lookup."""
	return all(_field_visible(c, values) for c in f.container_depends_on) \
		and _field_visible(f.depends_on, values)


def _evaluable(fields, values):
	"""The answers a condition is evaluated against: every DECLARED field present, blank until answered.

	`is not set` compiles to `doc.<f>==""` (`_rule_atom`), and an absent key reads as None on the server and
	as undefined on the client — neither of which equals "". Seeding the blanks is what makes the two
	evaluators agree on an unanswered field; the client seeds the same bag before it paints."""
	bag = {f.fieldname: "" for f in fields}
	bag.update({k: ("" if v is None else v) for k, v in (values or {}).items()})
	return bag


def _shown_fieldnames(fields, values):
	"""The declared fields the form actually shows for these answers, settled to the fixpoint (D29).

	A hidden field's value is INERT (D22): a condition naming it reads it blank, so hiding a parent collapses
	whatever hung off it and no imperative rule order has to be invented. The pass repeats until the shown
	set stops moving, bounded by the field count — the plan's own bound, because a Hide row can in principle
	flip a field back on and a bounded loop settles that deterministically instead of spinning."""
	declared = {f.fieldname for f in fields}
	shown = set(declared)
	for _pass in range(len(fields) + 1):
		live = _inert(fields, values, shown)
		settled = {f.fieldname for f in fields if _shown_here(f, live)}
		if settled == shown:
			break
		shown = settled
	return shown


def _inert(fields, values, shown):
	"""The evaluable answers with every HIDDEN declared field read back as blank — D22, expressed once."""
	live = _evaluable(fields, values)
	for f in fields:
		if f.fieldname not in shown:
			live[f.fieldname] = ""
	return live


def _required_here(f, shown, live):
	"""True when the submitted form must carry this field: it is actually SHOWN, and it is mandatory —
	declared `reqd`, or made so by a Make Mandatory rule whose condition passes (§17.3).

	A hidden field is never required, whether its own condition, its section's or a rule hid it: the rep was
	never shown it, so requiring it would brick the save. Judged on the server by the SAME evaluator the
	client mirrors — `_field_visible` — never a second rule."""
	if f.fieldname not in shown:
		return False
	return bool(f.reqd) or (bool(f.mandatory_depends_on) and _field_visible(f.mandatory_depends_on, live))


def compute_activity(lead, task_type, values, task=None):
	"""The ONE brain that turns a submitted activity form into CRM Task field values: validates
	grain + required, routes every answer by `field_target`, and runs the location guard (set/check the
	clinic anchor, resolve the address). Returns a dict of CRM Task fieldname -> value (status,
	the retained common cols, location cols, and the section child rows keyed by their Table field, which
	Document.update applies natively). Since Phase 7 there is nowhere else to write: a field the task row
	does not keep answers in its section row, and the slot columns and JSON payload are gone. Raises on
	out-of-scope / missing /
	out of range. Used by BOTH save paths: save_activity (complete/update existing) and the native
	new-task create (the form script stamps these onto the doc before insert). No second writer.

	`task` is the CRM Task name being completed (or the freshly-inserted shell for a new punch) — it
	is stamped onto the location audit so every Accepted/Not Required row carries its exact task id."""
	if isinstance(values, str):
		values = frappe.parse_json(values) or {}
	vertical, group, program = _lead_axes(lead)
	if not _scope_applies(task_type, vertical, group, program):
		frappe.throw(_("This activity is not available for this lead."), title=_("Out of scope"))

	tt = frappe.get_doc("CRM Task Type", task_type)
	# The rules compiled in — the SAME projection the form rendered from, so the save cannot demand or accept
	# anything the rep was not shown (§17.3).
	schema = compiled_fields(tt)
	shown = _shown_fieldnames(schema, values)
	live = _inert(schema, values, shown)
	promoted, staged = {}, {}
	for f in schema:
		val = values.get(f.fieldname)
		if f.fieldname not in shown:
			# D22: a hidden field's value is inert. A form that never showed it cannot have collected it, so a
			# value arriving for it is refused rather than quietly stored under a question nobody was asked.
			if val not in (None, ""):
				frappe.throw(_("{0} was not shown on this form and its value cannot be saved.").format(f.label),
							 title=_("Hidden field"))
			continue
		if _required_here(f, shown, live) and (val is None or val == ""):
			frappe.throw(_("{0} is required.").format(f.label), title=_("Missing field"))
		if (f.source or "") == LEAD_SOURCE:
			continue  # D11: a lead-sourced answer lives on the LEAD; the task never carries a copy of it
		# Route by the ONE seam: a retained common column stays on the task row, every other answer is its section row's.
		section_key, column = field_target(f)
		if section_key is None:
			promoted[column] = val
		_stage_section_value(staged, f, val)

	# The lead-sourced answers, written to the LEAD in this same transaction (D31) — before the task fields are
	# assembled, so a refused lead write refuses the whole save rather than leaving half of one behind.
	write_lead_fields(lead, schema, values, shown)

	# Keep the audited ASM data clean: an ASM must actually be a Sales Manager.
	_validate_asm(promoted.get("custom_asm"))

	fields = {
		"status": "Done" if int(tt.is_logged_complete or 0) else "Todo",
		**promoted,
		**staged,
	}
	notes = values.get("notes")
	if notes and frappe.get_meta("CRM Task").has_field("description"):
		fields["description"] = notes

	# Location guard: in-person activity on a tracked grain must carry an in-range fix. The gate +
	# anchor/radius rule live once in location.api (one brain); this just feeds them.
	from tatva_connect.location.api import (
		_reverse_geocode,
		is_location_tracked,
		location_fields,
		location_required,
		log_visit_audit,
		set_or_check_anchor,
	)

	# A dormant feature writes NOTHING. Location tracking off (kill switch, or this grain is not a
	# tracked one) means there is no visit trail to keep, so no audit row is written — and with no
	# audit row to point at a task, save_activity does not need to insert a shell first either. That
	# is two of the three writes every activity used to pay for a switch that was never on.
	if is_location_tracked(lead) is None:
		return fields

	# Tracking IS live on this grain. A phone or office activity still records that it legitimately
	# needed no fix: with the feature on, "Not Required" is a real entry in the trail, not noise.
	radius = location_required(task_type, lead, values)
	if radius is None:
		log_visit_audit(lead, task_type, "Not Required", task=task)
		return fields

	lat, lng, accuracy = values.get("lat"), values.get("lng"), values.get("accuracy")
	if not (lat and lng):
		frappe.throw(_("A captured location is required for this in-person activity."),
					 title=_("Location required"))
	guard = set_or_check_anchor(lead, lat, lng, accuracy, radius)  # throws if out of range
	fields.update(location_fields(lat, lng, address=_reverse_geocode(lat, lng), accuracy=accuracy))
	# Accepted (in range, or the first capture that establishes the anchor). Distance is logged from
	# the guard's single haversine — no recompute. Commits with the task save (same txn — task succeeds).
	log_visit_audit(
		lead, task_type, "Accepted", lat=lat, lng=lng,
		distance_m=guard.get("distance_m"), allowed_m=guard.get("allowed_m"),
		anchor_lat=guard.get("anchor_lat"), anchor_lng=guard.get("anchor_lng"), task=task,
	)
	return fields


def write_lead_fields(lead, fields, values, shown):
	"""Every SHOWN `source=Lead` answer, written onto the LEAD — the automation set path reused, never a
	third writer (D31).

	Three gates, all on the SERVER because the modal is paint and cannot be trusted to have applied any of
	them: the caller must hold `write` on this lead; the field must be `can_set` at THIS lead's grain, asked of
	`automation.fields.is_settable` — the same allowlist the Set Field action is gated by, whose section
	routing means a field belonging to a lead CHILD section is refused here just as it is there; and the write
	itself is one load-set-save, so the lead's field permissions, `validate` and every `doc_event` re-fire
	exactly as they do for a Set Field action. A type declaring no lead field does nothing at all.

	The task keeps no copy (D11). A refusal or a failed lead save throws, which rolls the activity back with
	it — one transaction, per D31."""
	# A blank is "not sent", never "erase this" — the rule the partner API already carries, and the reason a
	# form that merely showed a lead field cannot blank the patient record by being saved without it.
	changes = {f.fieldname: values.get(f.fieldname) for f in fields
			   if (f.source or "") == LEAD_SOURCE and f.fieldname in shown
			   and values.get(f.fieldname) not in (None, "")}
	if not changes:
		return
	if not frappe.flags.ignore_permissions:  # partner API runs trusted (gated by mapping + grain)
		frappe.has_permission("CRM Lead", "write", doc=lead, throw=True)
	axes = _lead_axes(lead)
	doc = frappe.get_doc("CRM Lead", lead)
	for fieldname, value in changes.items():
		# The SAME predicate the form painted read-only with, so the rep is never shown a box this refuses.
		if not lead_field_is_open(lead, fieldname, axes, doc.get(fieldname)):
			frappe.throw(_("{0} cannot be written on this lead.").format(fieldname), title=_("Not permitted"))
		doc.set(fieldname, value)
	doc.save(ignore_permissions=frappe.flags.ignore_permissions)  # authz-ok: honors caller flag; UI passes False, partner is pre-gated


def lead_field_is_open(lead, fieldname, axes, current):
	"""THE fill-once rule: may this activity form write `fieldname` onto this lead?

	Two conditions, both about the LEAD and neither about the form. The grain's contract must tick the
	field settable (`automation.fields.is_settable` — the same gate the Set Field action asks, so an
	activity can never write what an automation may not). And the lead must not already hold a value:
	a patient record is filled once from an activity and corrected on the lead's own page, never
	overwritten sideways by a follow-up call.

	ONE predicate, asked twice: `_lead_field_state` paints the form read-only with it, and
	`write_lead_fields` refuses with it. The rep therefore cannot be shown a box the save would reject."""
	from tatva_connect.automation import fields as automation_fields

	if not automation_fields.is_settable(automation_fields.LEAD_DT, fieldname, axes):
		return False
	return current in (None, "")


def _mark_lead_read_only(descriptors, lead, values):
	"""Stamp `read_only` on every lead-sourced descriptor the fill-once rule closes for THIS lead.

	`read_only` rather than a map of its own because that is the key the fork's controls already bind
	their `disabled` to (`SidePanelLayout.vue`) — one descriptor shape, no second vocabulary. Answered
	off the same values the form is prefilled with, so what is greyed out and what the save refuses
	cannot be two answers."""
	if not lead:
		return
	axes = _lead_axes(lead)
	for d in descriptors:
		if (d.get("source") or "") != LEAD_SOURCE:
			continue
		d["read_only"] = 0 if lead_field_is_open(lead, d["fieldname"], axes, values.get(d["fieldname"])) else 1


def lead_field_values(lead, task_type):
	"""The lead's CURRENT answers to this type's `source=Lead` fields — what the form opens prefilled with.

	NOT whitelisted: it rides the `type_config` answer the form already fetches, so loading a form stays ONE
	call however many lead fields it declares. Read through the lead detail brain (`lead.detail.lead_detail`),
	so a field this viewer is not entitled to see is not in the answer at all and nothing here re-decides who
	may read what. Empty for a type that declares no lead field, which is every type until an admin declares
	one."""
	frappe.has_permission("CRM Lead", "read", doc=lead, throw=True)
	wanted = {f.fieldname for f in compiled_fields(frappe.get_doc("CRM Task Type", task_type))
			  if (f.source or "") == LEAD_SOURCE}
	if not wanted:
		return {}
	from tatva_connect.lead.detail import lead_detail

	out = {}
	for section in lead_detail(lead)["sections"]:
		for f in section["fields"]:
			if f["fieldname"] in wanted and f["value"] not in (None, ""):
				out[f["fieldname"]] = f["value"] if isinstance(f["value"], str) else cstr(f["value"])
	return out


@frappe.whitelist()
def compute_activity_fields(lead, task_type, values):
	"""Whitelisted compute for the native new-task path: the CRM Task form script stamps the result
	onto the doc, then lets the native create save it ONCE (no double insert, no lingering popup)."""
	frappe.has_permission("CRM Lead", "write", doc=lead, throw=True)
	return compute_activity(lead, task_type, values)


@frappe.whitelist()
def save_activity(lead, task_type, values, task=None):
	"""THE one writer for completing/updating an activity (board completion + ad-hoc punch). Returns
	the task name.

	A NEW punch on a location-tracked grain inserts the task SHELL first, so the visit audit logged
	inside compute_activity carries the new task's exact id, and then computes and saves onto it. That
	costs two writes to the same row, and it buys exactly one thing: an audit that can name its task.
	Where location is NOT tracked there is no audit, so there is nothing to name, and the task is
	inserted ONCE — already carrying its computed fields. Same writer, same compute, one row.

	The shell insert is in the same request transaction as the guard: an out-of-range throw rolls the
	shell back with everything else, so a blocked visit never leaves an orphan task."""
	from tatva_connect.location.api import is_location_tracked

	if not frappe.flags.ignore_permissions:  # partner API runs trusted (gated by mapping + grain)
		frappe.has_permission("CRM Lead", "write", doc=lead, throw=True)

	if task:
		fields = compute_activity(lead, task_type, values, task=task)
		doc = frappe.get_doc("CRM Task", task)
		doc.update(fields)
		doc.save(ignore_permissions=frappe.flags.ignore_permissions)  # authz-ok: honors caller flag; UI passes False, partner is pre-gated
		return doc.name

	# title = the clean type_name (display), never the composite PK.
	title = labels.label(task_type, TASK_TYPE)
	# Trusted (partner/system) write has no caller-assignee: leave unassigned for the Assignment Rule.
	shell = frappe.get_doc({
		"doctype": "CRM Task",
		"title": title,
		"custom_task_type": task_type,
		"assigned_to": None if frappe.flags.ignore_permissions else frappe.session.user,
		"reference_doctype": "CRM Lead",
		"reference_docname": lead,
	})

	if is_location_tracked(lead) is None:
		# No audit will be written, so nothing needs the task's id before it exists: compute first,
		# then insert once, fully formed.
		shell.update(compute_activity(lead, task_type, values, task=None))
		shell.insert(ignore_permissions=frappe.flags.ignore_permissions)  # authz-ok: honors caller flag; UI passes False, partner is pre-gated
		return shell.name

	shell.insert(ignore_permissions=frappe.flags.ignore_permissions)  # authz-ok: honors caller flag; UI passes False, partner is pre-gated
	fields = compute_activity(lead, task_type, values, task=shell.name)
	doc = frappe.get_doc("CRM Task", shell.name)
	doc.update(fields)
	doc.save(ignore_permissions=frappe.flags.ignore_permissions)  # authz-ok: honors caller flag; UI passes False, partner is pre-gated
	return doc.name




def _type_config(task_type):
	"""Render config for a task type: the ordered field schema, the same fields laid out in the tabs,
	sections and columns the declaration draws, whether completing it logs Done, and whether it can capture
	location (visit_mode In-Person, or a conditional location_when). None for a type with no config row.

	`fields` is what every READER of a task's answers walks; `tabs` is what the FORM renders. They are the
	one descriptor list, projected twice — see `compiled_layout`."""
	if not frappe.db.exists("CRM Task Type", task_type):
		return None
	doc = frappe.get_doc("CRM Task Type", task_type)
	fields, tabs = compiled_layout(doc)
	return {
		"fields": fields,
		"tabs": tabs,
		"is_logged_complete": int(doc.is_logged_complete or 0),
		"captures_location": bool((doc.visit_mode or "") == "In-Person" or (doc.location_when or "").strip()),
	}


@frappe.whitelist()
def task_detail(task):
	"""Render-ready detail for ONE task by name — the global Tasks list / sidebar opens TatvaTaskModal
	from this (a task row + its type config). `config` is null for a plain
	(non-activity) task, so the client falls back to the native doctype modal. One brain reused
	(_type_config / _task_values / _task_location)."""
	frappe.has_permission("CRM Task", "read", doc=task, throw=True)
	r = frappe.db.get_value(
		"CRM Task", task,
		["name", "title", "custom_task_type", "status", "priority", "due_date", "start_date",
		 "assigned_to", "owner", "creation", "description",
		 *COMMON_COLUMNS,
		 "custom_location_latitude", "custom_location_longitude",
		 "custom_location_address", "custom_location_captured_at",
		 "reference_doctype", "reference_docname"],
		as_dict=True,
	)
	if not r:
		frappe.throw(_("Task {0} not found").format(task))
	cfg = _type_config(r.custom_task_type) if r.custom_task_type else None
	who = r.assigned_to or r.owner
	return {
		"lead": r.reference_docname if r.reference_doctype == "CRM Lead" else None,
		"config": cfg,
		"task": {
			"name": r.name,
			"title": r.title,
			"description": r.description,
			"task_type": r.custom_task_type or "",  # composite PK (the key)
			"task_type_label": labels.label(r.custom_task_type, TASK_TYPE),
			"status": r.status,
			"priority": r.priority,
			"due_date": str(r.due_date) if r.due_date else None,
			"start_date": str(r.start_date) if r.start_date else None,
			"assigned_to": r.assigned_to,
			"reference_doctype": r.reference_doctype,
			"reference_docname": r.reference_docname,
			"rep": who,
			"rep_name": (who and frappe.db.get_value("User", who, "full_name")) or who,
			"creation": str(r.creation),
			"datetime": format_datetime(r.creation, "d MMM, h:mm a"),
			"values": _task_values(r, cfg),
			"location": _task_location(r),
		},
	}


def capture_flags(task_types):
	"""`{task_type: (label, needs_capture)}` for a PAGE of tasks — two queries, never one per row.

	`needs_capture` answers the only question a card asks its type: does finishing this open the capture
	form, or is it a plain status flip. Same three conditions `_type_config` reports, read from the same
	columns — but counting the schema rows instead of compiling the layout, because a card needs to know
	THAT there are fields, and only the modal needs to know what they are."""
	wanted = sorted({t for t in task_types if t})
	if not wanted:
		return {}
	rows = frappe.get_all(
		"CRM Task Type", filters={"name": ["in", wanted]},
		fields=["name", "type_name", "visit_mode", "location_when", "is_logged_complete"],
	)
	with_fields = {
		r.parent for r in frappe.get_all(
			"CRM Task Type Field", filters={"parent": ["in", wanted]}, fields=["parent"],
		)
	}
	return {
		r.name: (
			r.type_name or r.name,
			bool(
				r.name in with_fields
				or (r.visit_mode or "") == "In-Person"
				or (r.location_when or "").strip()
				or r.is_logged_complete
			),
		)
		for r in rows
	}


@frappe.whitelist()
def type_config(task_type, lead=None):
	"""Render config (fields + is_logged_complete + captures_location) for ONE task type — the
	create-mode modal's source when the chosen type has no existing task seeding it into
	an existing task. Same brain (_type_config) every surface uses, so card/modal/create stay consistent.

	`lead` is optional and is what the form names when it is being logged against a lead: the answer then
	also carries that lead's current values for the type's `source=Lead` fields (D31 prefill). It rides HERE
	rather than on a call of its own so opening a form is ONE round trip whatever the type declares — and the
	client's resource cache keys on the pair, because these values are the LEAD's and not the type's."""
	frappe.has_permission("CRM Task Type", "read", doc=task_type, throw=True)
	cfg = _type_config(task_type)
	if cfg is None:
		frappe.throw(_("Task type {0} not found").format(task_type))
	cfg["lead_values"] = lead_field_values(lead, task_type) if lead else {}
	_mark_lead_read_only(cfg["fields"], lead, cfg["lead_values"])
	return cfg


def _sections():
	"""Every declared activity section — the ONE row naming a section's child table, its row key and the
	column an answer is read from. Storage only; the form's layout is declared on the task type."""
	return frappe.get_all(
		"CRM Task Section",
		fields=["name", "title", "display_order", "target_doctype",
				"child_table_field", "is_key_value", "is_multi_row", "row_key_field", "value_field"],
		order_by="display_order",
	)


def section_rows(task_names):
	"""Every section child row these tasks carry: {task: {child table: [rows]}}. ONE query per section and
	never one per task, so a board or a page of activities costs the same as a single one.

	Keyed by the task name as TEXT: CRM Task is autoincrement-named so its own PK reads back as an int,
	while a child row's `parent` is a varchar — keyed by either as it came, every lookup missed."""
	out = {}
	if not task_names:
		return out
	for s in _sections():
		for row in frappe.get_all(
			s.target_doctype,
			filters={"parent": ["in", [cstr(n) for n in task_names]], "parenttype": "CRM Task"},
			fields=["*"], order_by="idx asc",
		):
			out.setdefault(cstr(row.parent), {}).setdefault(s.child_table_field, []).append(row)
	return out


def _rows_of(r):
	"""The section rows of ONE task: a live CRM Task Document already carries them; a flat row read by get_all does not, so they are fetched."""
	held = {s.child_table_field: r.get(s.child_table_field) for s in _sections()}
	if all(v is not None for v in held.values()):
		return held
	return section_rows([r.name]).get(cstr(r.name), {})


def _latest(rows, row_key_field):
	"""The ONE row a section shows: newest by its row key, then creation, then name — the same order the
	Smart View join ranks by, so the form and the worklist can never show different rows."""
	if not rows:
		return None
	return sorted(
		rows,
		key=lambda x: (cstr(x.get(row_key_field)) if row_key_field else "", cstr(x.get("creation")), cstr(x.get("name"))),
		reverse=True,
	)[0]


def _section_answer(f, task_row, rows, sections):
	"""ONE declared field's saved value, read at the address `field_target` names and nowhere else: a
	retained common column is the task's own, everything else is the section row that addresses it."""
	section_key, address = field_target(f)
	if section_key is None:
		return task_row.get(address)
	section = sections.get(section_key)
	if not section:
		return None
	held = rows.get(section.child_table_field) or []
	if section.is_key_value:
		row = next((x for x in held if x.get(section.row_key_field) == address), None)
		return row.get(section.value_field) if row else None
	row = _latest(held, section.row_key_field)
	return row.get(address) if row else None


def _task_values(r, cfg, rows=None):
	"""Saved values keyed by SCHEMA fieldname, so the renderer just reads values[fieldname].

	Phase 4: every answer is read at the address `field_target` names — the SAME seam every writer wrote
	by — so the reader can never look at a place the writer never filled. `rows` is the task's section
	rows when the caller already holds them for a whole page; a lone read fetches its own."""
	vals = {}
	if cfg:
		sections = {s.name: s for s in _sections()}
		rows = _rows_of(r) if rows is None else rows
		for f in cfg["fields"]:
			if (f.get("source") or "") == LEAD_SOURCE:
				continue  # D11: it was never written here, so there is nothing here to read — the lead holds it
			value = _section_answer(f, r, rows, sections)
			if value not in (None, ""):
				vals[f["fieldname"]] = value if isinstance(value, str) else cstr(value)
	if r.description:
		vals.setdefault("notes", r.description)
	return vals


def _task_location(r):
	"""Captured-location state for a task card — None if no fix was recorded."""
	if not (r.custom_location_latitude and r.custom_location_longitude):
		return None
	return {
		"lat": flt(r.custom_location_latitude),
		"lng": flt(r.custom_location_longitude),
		"address": r.custom_location_address or "",
		"captured_at": str(r.custom_location_captured_at) if r.custom_location_captured_at else None,
	}


def _blob_key(url):
	"""The host-independent storage key inside a proxy URL (?file_name=<key>) — stable across the
	root-relative and absolute URL forms. Lets us match a payload Attach value to its lead File."""
	if not url:
		return ""
	try:
		return (parse_qs(urlparse(url).query).get("file_name") or [""])[0]
	except Exception:
		return ""


def _lead_files(lead):
	"""blob_key -> {file_url, file_name} for every File on the lead (one query). The File holds the
	real display name; the key links it back to the activity that captured it."""
	files = {}
	for f in frappe.get_all(
		"File", filters={"attached_to_doctype": "CRM Lead", "attached_to_name": lead},
		fields=["file_name", "file_url"],
	):
		key = _blob_key(f.file_url)
		if key:
			files[key] = {"file_url": f.file_url, "file_name": f.file_name}
	return files


def _activity_documents(values, cfg, files_by_key):
	"""Documents an activity captured: each Attach/Attach Image schema field's value resolved to its
	lead File (real name + url) via the blob key. Same schema-driven rule as the board's card media."""
	if not cfg:
		return []
	docs = []
	for f in cfg["fields"]:
		if f.get("fieldtype") not in ("Attach", "Attach Image"):
			continue
		url = values.get(f["fieldname"])
		if not url:
			continue
		key = _blob_key(url)
		docs.append(files_by_key.get(key) or {"file_url": url, "file_name": key.rsplit("/", 1)[-1] or url})
	return docs


@frappe.whitelist()
def lead_timeline(lead):
	"""The single activity projection for a lead — used by BOTH the SPA timeline and the Desk Activity
	Timeline. Newest first; ONE rich entry per activity task: its status, captured location, and the
	documents it attached — so the timeline renders the whole action as one chained line."""
	frappe.has_permission("CRM Lead", "read", doc=lead, throw=True)
	activity_types = _activity_type_names()
	if not activity_types:
		return []
	tasks = frappe.get_all(
		"CRM Task",
		filters={"reference_docname": lead, "custom_task_type": ["in", list(activity_types)]},
		fields=["name", "creation", "modified", "status", "custom_task_type", "description",
				"custom_automated",
				"assigned_to", "owner", *COMMON_COLUMNS,
				"custom_location_latitude", "custom_location_longitude",
				"custom_location_address", "custom_location_captured_at"],
		order_by="creation desc",
	)
	cfgs = {tn: _type_config(tn) for tn in {t.custom_task_type for t in tasks if t.custom_task_type}}
	type_names = labels.labels([t.custom_task_type for t in tasks], TASK_TYPE)
	files_by_key = _lead_files(lead)
	answers_by_task = section_rows([t.name for t in tasks])  # one query per section for the whole timeline
	out = []
	for t in tasks:
		who = t.assigned_to or t.owner
		cfg = cfgs.get(t.custom_task_type)
		loc = _task_location(t)
		out.append({
			"name": t.name,
			"creation": str(t.creation),
			"modified": str(t.modified),
			"date": formatdate(t.creation, "d MMM"),
			"datetime": format_datetime(t.creation, "d MMM, h:mm a"),
			"owner": who,
			"owner_name": (who and frappe.db.get_value("User", who, "full_name")) or who,
			"activity_type": t.custom_task_type,
			"activity_type_label": type_names.get(t.custom_task_type, ""),
			"status": t.custom_outcome or t.status,
			"done": (t.status or "") in ("Done", "Completed"),
			"automated": bool(t.custom_automated),
			"address": t.custom_location_address or "",
			"location": ({"address": loc["address"],
						  "map_url": "https://www.google.com/maps?q={},{}".format(loc["lat"], loc["lng"])}
						 if loc else None),
			"documents": _activity_documents(_task_values(t, cfg, answers_by_task.get(cstr(t.name), {})), cfg, files_by_key),
		})
	return out
