"""The activity engine's server brain.

An "activity" is a CRM Task of an activity type (a CRM Task Type whose composite key carries
a grain), logged complete on save. One writer (`save_activity`) routes every submitted answer
through `field_target` — the retained common columns, or a section child row. One projection (`lead_timeline`) feeds BOTH
the SPA Activity timeline and the Desk Lead timeline. Availability is grain-scoped through
the single brain `taxonomy.grain.resolve_scoped` — nothing here hardcodes a type or grain.
Ships dormant: a CRM Task Type with an all-blank grain never surfaces as an activity.

Every permission question this module asks goes through `access.posture.require` — the ONE checkpoint
that knows whether the request is an ordinary Desk one (ask the engine) or a server-opened trusted block
(the partner API, already gated by its mapping and grain). A bare `frappe.has_permission` here is a
second answer to that question and is locked out by tests/architecture/test_permission_checks_one_seam.py.
"""
import json

import frappe
from frappe import _
from frappe.model import NO_VALUE_FIELDS
from frappe.utils import cint, cstr, flt, format_datetime, formatdate, get_datetime

from tatva_connect.access import entitlement, posture
from tatva_connect.storage import blob_store, file_events, file_names
from tatva_connect.taxonomy import grain, labels, picklist
from tatva_connect.taxonomy.grain import resolve_scoped
from tatva_connect.taxonomy.labels import TASK_TYPE

_TASK_COLUMNS_CACHE = "tatva_connect:task_settable_columns"
_KEY_VALUE_CACHE = "tatva_connect:key_value_section"


def task_columns():
	"""The CRM Task columns a declared field may live on — read off `CRM Task Field`, the Task resource's own field brain (§8 rule 2), never a list in code. `can_set` is the ONE gate: a write is a write, whether an automation or an activity form makes it. A target this does not name is simply a target no section and no column claims, and falls to the key-value home."""
	from tatva_connect.access import request_cache

	def build():
		return tuple(r.fieldname for r in frappe.get_all(
			"CRM Task Field", filters={"can_set": 1}, fields=["fieldname"], order_by="fieldname"))

	return request_cache(_TASK_COLUMNS_CACHE, "all", build)


# The rule grammar, named here the way a fieldtype is named: these ARE the language (D27/D28), declared as
# the Select options of CRM Task Type Rule and read back by the compile below. The doctype JSON is the one
# home; tests/activity/test_rule_compilation.py fails if the two ever disagree.
RULE_SHOW, RULE_HIDE, RULE_MANDATORY, RULE_SET_VALUE = "Show", "Hide", "Make Mandatory", "Set Value"
RULE_ACTIONS = (RULE_SHOW, RULE_HIDE, RULE_MANDATORY, RULE_SET_VALUE)
# The operators that compare against a value, so the value is validated against the field's options; the
# other two ask only whether an answer exists (D27).
RULE_VALUE_OPERATORS = ("is", "is not")
RULE_OPERATORS = (*RULE_VALUE_OPERATORS, "is set", "is not set")

# A declared field's `source`: whose record the value belongs to. A lead-sourced field opens prefilled with what the lead holds, snapshots onto the task as it stood at the punch, and an answer the rep CHANGES goes back to the lead through the Data tab's own write gate. Left alone it stays context.
LEAD_SOURCE = "Lead"

# The dotted path a `Link -> User` control hands `search_link` as its `query`. Spelled once.
USER_QUERY = "tatva_connect.activity.api.user_query"
# The SAME scoped query `lead.detail._link_query` names, for the same reason: a picklist picker read through the generic list path demands CRM Picklist Value read, which no rep holds.
PICKLIST_QUERY = "tatva_connect.taxonomy.picklist.picklist_query"

# Which role a person field may offer. Read by the picker AND the save, so neither can disagree with the other.
FIELD_ROLE = {"select_asm": "Sales Manager"}


def field_target(f):
	"""THE home a declared activity field lands in — `(section_key, address)`, section None meaning the task row itself. Since Phase 5 it is the ONLY write seam, and since Phase 7 the only read seam too: there is no second home left to ask about."""
	target = f.get("target") or ""
	section = f.get("section") or ""
	if section and target and frappe.get_meta(
		frappe.get_cached_value("CRM Task Section", section, "target_doctype")
	).get_field(target):
		return section, target
	columns = task_columns()
	if target and not columns:
		# Never fall through to key-value here: that would silently put an answer somewhere the reader does not look. Same contract as the key-value throw below.
		frappe.throw(_("No CRM Task Field is declared settable, so `{0}` cannot be routed. The declaration "
		               "is seeded by task_field_seed.ensure_rows on after_migrate.").format(f.get("fieldname")))
	if target in columns:
		return None, target
	# A field NAMING a key-value section means it: addressed by its own fieldname, in the section it declared. Without this the declaration is unreachable — the fallback below always answers with the FIRST key-value section, so a second one could never be written to.
	if section and frappe.get_cached_value("CRM Task Section", section, "is_key_value"):
		return section, f.get("fieldname")
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
	that aborts the whole migrate. It did abort one, on 2026-07-27.

	Memoised per request exactly as `task_columns` above is, and for the same reason: `field_target`
	asks this once per declared FIELD, so an activity with nine fields ran nine identical queries —
	measured at 8.7 per activity and ~5% of the load. A NEGATIVE answer is deliberately not kept: the
	rows appear during `after_migrate`, and pinning "not yet" for the rest of that process is the bug
	the paragraph above is about."""
	from tatva_connect.access import request_cache

	def build():
		rows = frappe.get_all(
			"CRM Task Section", filters={"is_key_value": 1}, order_by="display_order", limit=1, pluck="name"
		)
		return rows[0] if rows else None

	found = request_cache(_KEY_VALUE_CACHE, "all", build)
	if found is None:
		getattr(frappe.local, _KEY_VALUE_CACHE, {}).pop("all", None)
	return found


def sections_ready():
	"""Has the operator's declaration landed? THE readiness question, asked by everything that can run before
	`after_migrate` has seeded it — so a caller can skip-until-ready rather than crash, and so no caller has
	to know which rows prove it. BOTH declarations the router resolves through are asked: the sections an
	answer lands in, and the CRM Task columns a declared field may keep."""
	return _key_value_section() is not None and bool(task_columns())


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


def _blank(value):
	"""No answer. A set-valued field arrives as a LIST, so an empty one is as unanswered as an empty string — the client agrees (`isEmpty`), or required and D22 would judge the two shapes differently."""
	return value is None or value == "" or value == []


def takes_a_set(f):
	"""Does this declared field hold MANY values? Cardinality is the lead catalog's `is_multi_value` and is
	asked HERE by the render, the write and the read-back alike — never re-derived, and never guessed from a
	value's Python type. A set is stored the way the lead stores one: N rows at one address, no separator."""
	from tatva_connect.lead import multi_value

	return (f.get("source") or "") == LEAD_SOURCE and f.get("fieldname") in multi_value.fieldnames()


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


def _at_address(section, held, address):
	"""The rows held at ONE address, in stored order — how a set is read back, and what replacing it drops."""
	return [r for r in held if r.get(section.row_key_field) == address]


def _set_rows(section, address, f, value):
	"""The rows ONE set-valued answer becomes: `_row_values` per selection, blanks dropped. Both write legs
	build their rows here, so create and complete cannot disagree about what a set is stored as."""
	from tatva_connect.lead import multi_value

	if not section.is_key_value:
		frappe.throw(_("{0} takes more than one value and its section keeps one column per field.").format(f.label),
					 title=_("Cannot store a set"))
	return [_row_values(section, address, f.fieldtype, v) for v in multi_value.as_set(value) if v]


def _put_section_set(doc, section, address, f, value):
	"""REPLACE the rows a set-valued field holds at one address — the same words `multi_value.replace` keeps
	on the lead, so both sides of the same answer are N rows and neither encodes a separator. Returns True
	iff the stored set changed."""
	wanted = _set_rows(section, address, f, value)
	current = _at_address(section, doc.get(section.child_table_field) or [], address)
	if [r.get(section.value_field) for r in current] == [r[section.value_field] for r in wanted]:
		return False
	for row in current:
		doc.remove(row)
	for row in wanted:
		doc.append(section.child_table_field, row)
	return True


def _put_section_value(doc, f, value):
	"""Dual-write leg for a live CRM Task: put ONE field's value in the new home field_target names,
	upserting the row it addresses so a re-write never grows a second one. Returns True iff a row changed.
	Rule 2 writes nothing — the retained common column the caller already set IS the new home."""
	section_key, address = field_target(f)
	if section_key is None:
		return False
	section = frappe.get_cached_doc("CRM Task Section", section_key)
	if takes_a_set(f):
		return _put_section_set(doc, section, address, f, value)
	values = _row_values(section, address, f.fieldtype, value)
	rows = doc.get(section.child_table_field) or []
	row = (next((r for r in rows if r.get(section.row_key_field) == address), None)
		   if section.is_key_value else (rows[0] if rows else None))
	# A blank earns no row: key-value drops the row (clearing it), a column row keeps its siblings and just blanks its own.
	if _blank(value):
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
	if _blank(value):
		return
	rows = staged.setdefault(section.child_table_field, [])
	if takes_a_set(f):
		rows.extend(_set_rows(section, address, f, value))
		return
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
	f = next((x for x in frappe.get_cached_doc("CRM Task Type", task_type).schema if x.fieldname == fieldname), None)
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
	if not frappe.db.exists("CRM Lead", lead):
		frappe.throw(_("Lead {0} not found").format(lead))
	return grain.of("CRM Lead", lead)


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


@frappe.whitelist()
def open_activity_tasks(lead):
	"""Open (not Done/Canceled) activity tasks on a lead — lets the client map a Tasks-tab row to
	the activity type/name so completing it opens the activity form instead of an empty status flip."""
	posture.require("CRM Lead", "read", doc=lead)
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

	`enabled = 0` RETIRES a form. LeadSquared leaves a dead designer published for years — `Phone
	Conversation` took 56,796 punches and none since 2026-03-20 — and its history has to stay readable
	while the rep stops being offered it. So this is the ONLY reader of the flag: the picker.
	Native `frappe.get_all` pre-filters to candidate grains (no raw SQL), then the predicate decides.
	Value = the composite PK (`name`); label = the clean `type_name`."""
	posture.require("CRM Lead", "read", doc=lead)
	vertical, group, program = _lead_axes(lead)
	rows = frappe.get_all(
		"CRM Task Type",
		filters={
			# A RETIRED form is not offered — and only here. Everything it already recorded still renders,
			# because `type_config` and `task_detail` read the type by name and never ask this.
			"enabled": 1,
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
		# Declared per field, so one form may take an answer where another only shows it. `_compiled_rows` also forces it on a Set Value target, which the server answers.
		"read_only": int(f.get("read_only") or 0),
		"depends_on": (f.get("depends_on") or ""),
		"mandatory_depends_on": (f.get("mandatory_depends_on") or ""),
		"copy_from": [],  # [{source, when}] — the Set Value rows naming this field; stamped by _compiled_rows
		"container_depends_on": [],  # the conditions of the tab/section/column holding it; stamped by _layout
		"link_query": _link_query(f),  # a Link -> User picker's scoped query; None leaves the native one
	})


def _link_query(f):
	"""The scoped query a Link picker asks, or None for a Link the framework can answer natively.

	Same seam and same reason as `lead.detail._link_query`: `search_link` reads the master through the generic
	list path, so it demands read on that master — which `lockdown.BASELINE_ROLE_TRIMS` strips from every rep.
	Left native, a User picker answers with the one row frappe always allows: you, and a picklist picker 403s.

	A picklist Link also carries `depends_on_field` when its vocabulary cascades. The lead is NOT named here —
	`_stamp_picklist_lead` adds it, because the descriptor is built per TYPE and the grain is per LEAD."""
	if f.fieldtype != "Link":
		return None
	options = f.options or ""
	if options == "User":
		return {"query": USER_QUERY, "filters": {"fieldname": f.fieldname, "task_type": f.get("parent") or ""}}
	if options == "CRM Picklist Value":
		category = picklist.category_of(f.fieldname)
		q = {"query": PICKLIST_QUERY, "filters": {"category": category}}
		parent = _cascade_parent(category)
		if parent:
			# The CLIENT owns the value: it changes as the rep answers, and `Link.vue` re-queries when its filters do.
			q["depends_on_field"] = parent
		return q
	return None


def _cascade_parent(category):
	"""The question this category's options hang off, or None where the vocabulary is flat.

	Declared BY the options themselves (`CRM Picklist Value.depends_on_field`) rather than beside them, so a
	cascading vocabulary says so once and no second map can disagree with it — the rule `picklist_query`
	already filters on. One indexed read per picklist Link per form open."""
	return frappe.db.get_value(  # authz-ok: tier-a — reads one declaration column of a master, no lead or grain in it
		"CRM Picklist Value", {"category": category, "depends_on_field": ("!=", "")}, "depends_on_field"
	)


def _one_condition(field, operator, value):
	"""ONE When triplet as an expression, in the syntax BOTH shipped evaluators read alike.

	A blank field is "always" (D25/§17.2), so it is the constant 1. Every operator (D27) compiles to a
	COMPARISON because a comparison is the largest syntax the two evaluators share: the server's is Python
	`safe_eval` (`_field_visible`) and the client's is a JS `new Function` (`utils/expressions.js`), so
	`and`/`or`/`not` parse only in one and `&&`/`||`/`!` only in the other. `_rule_or` / `_rule_and` /
	`_rule_not` below therefore combine with arithmetic, which reads identically in both. A value is
	JSON-quoted, which is also a literal both languages accept."""
	field = (field or "").strip()
	if not field:
		return "1"
	ref = "doc." + field
	literal = json.dumps(cstr(value or ""))
	operator = (operator or RULE_OPERATORS[0]).strip()
	if operator == "is not":
		return f"{ref}!={literal}"
	if operator == "is set":
		return f'{ref}!=""'
	if operator == "is not set":
		return f'{ref}==""'
	return f"{ref}=={literal}"


def rule_conditions(row):
	"""The When triplets ONE rule row actually declares — THE one reading of a row's condition (D27).

	A row carries two, and either may be left blank. Three readers need that answer: the compile ANDs them,
	`CRMTaskType._validate_rules` checks each, and the dead-field lock enumerates the answers they can tell
	apart. Each used to walk the row itself, and both bugs that cost a morning were one walk stopping at the
	first triplet — a Hide written only in the second compiled to `eval:0` and hid its field forever (T-04),
	and the lock reported six reachable Welcome Call fields dead (T-05). One walk, so a reader cannot see
	less of a row than the compile does.

	Returned verbatim: the compile embeds the value as given and the two checkers strip it, which is what
	each already did."""
	out = []
	for field, operator, value in (
		(row.condition_field, row.operator, row.condition_value),
		(row.get("condition_field_2"), row.get("operator_2"), row.get("condition_value_2")),
	):
		if (field or "").strip():
			out.append(((field or "").strip(), (operator or RULE_OPERATORS[0]).strip(), value))
	return out


def _rule_atom(row):
	"""ONE rule row's whole When as an expression — every triplet it declares, ANDed (§17.2, D27).

	LeadSquared offers `If All` across several conditions and four Anaya rules use it (Welcome Call 3, 5, 9
	and 14 — `ANAYA-SEED-INVENTORY.md §6.2`). Written as two ROWS the compile would OR them (`_rule_or` is
	`+`), which is the inverse: the action would fire when either held. The second triplet is therefore part
	of the SAME row and joins with `_rule_and`.

	Rules 5 and 9 declare THREE conditions in LSQ and two triplets carry two. They still compile correctly,
	because the field their third condition tests is itself hidden until the earlier ones hold and a hidden
	field's answer is inert (D22) — so the fixpoint collapses the third condition rather than dropping it.

	A row declaring no condition at all is the form's opening state and compiles to the constant 1."""
	atoms = [_one_condition(*c) for c in rule_conditions(row)]
	if not atoms:
		return "1"
	return atoms[0] if len(atoms) == 1 else _rule_and(atoms)


def _rule_or(atoms):
	"""OR over rule atoms. `+` because it is truthy-summing in Python and in JS alike — see `_rule_atom`."""
	return "+".join(f"({a})" for a in atoms)


def _rule_and(atoms):
	"""AND over rule atoms. `*` for the same reason `_rule_or` is `+` — it multiplies truthily in both."""
	return "*".join(f"({a})" for a in atoms)


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
		# ANY declared triplet makes the row conditional; reading only the first buries the field at `eval:0` (T-04).
		conditional = bool(rule_conditions(row))
		for target in rule_targets(row.targets):
			out.setdefault(target, {}).setdefault(row.action or "", []).append(
				(atom, conditional, row.get("set_value") or ""))
	return out


def rule_targets(targets):
	"""The fieldnames a comma-separated `targets` declaration names — THE one reading of it.

	Targets is text and not a multiselect because a child table cannot hold one (`model/__init__.py:99`,
	D26); the Desk script paints the picker that appends to it. The compile here and
	`CRMTaskType._validate_rules` both address the same text, so they resolve through this rather than each
	splitting it — two splitters could disagree on trimming or on a trailing comma and the validator would
	then bless a target the compile never sees."""
	return [t.strip() for t in (targets or "").split(",") if t.strip()]


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
	conditional_hides = [atom for atom, conditional, _ in hides if conditional]
	if shows:
		base = _rule_or([atom for atom, _c, _v in shows])
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
	return "eval:" + _rule_or([atom for atom, _c, _v in rows])


def _compiled_copy_from(entry):
	"""ONE field's Set Value rows as `[{source, when}]` — the fourth verb, and a COPY rather than a literal.

	Every Set Value row LeadSquared actually declares reads `Set Value (Mail Merge <- <field>)`: all seven
	transcribed rows name a FIELD to copy, never a constant (`ANAYA-SEED-INVENTORY.md:221-223, :787, :788,
	:1173, :1291`). Six of the seven copy a lead field onto its activity twin (`Discharge Summary LM` ->
	`Discharge Summary AM`), which is a SNAPSHOT — and LSQ marks every one of those targets Make Read-Only
	in the same rule set. So a Set Value target is derived, not answered: `compute_activity` computes it and
	ignores whatever the client sends for it. A `source = Lead` field is the opposite — the rep may answer it.

	A LIST, not the pair this shipped as: several rows may name one field, and returning the first row's
	value beside the OR of every row's condition wrote rule A's value when only rule B's condition fired.
	First row whose condition PASSES wins, which is the first-declared-wins the grid reads top to bottom."""
	return [{"source": value, "when": "eval:" + atom} for atom, _c, value in entry.get(RULE_SET_VALUE) or []]


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
		d.copy_from = _compiled_copy_from(entry)
		# A copy target is answered by the server, so the rep may not edit it — see `_field_descriptor`.
		if d.copy_from:
			d.read_only = 1
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
	posture.require("CRM Task Type", "read", doc=task_type)
	return compiled_fields(frappe.get_cached_doc("CRM Task Type", task_type))


def _validate_person(f, val):
	"""A person field may only name someone holding the role FIELD_ROLE declares for it.

	The WRITE half of `_link_query`: both read the one declaration, over the same set of fields, so a value the
	picker could not have offered is also a value the save will not take. Nothing is field-specific here — a
	person field added tomorrow is guarded by declaring it, and by nothing else."""
	role = FIELD_ROLE.get(f.fieldname)
	if not (val and role and f.fieldtype == "Link" and (f.options or "") == "User"):
		return
	if role not in frappe.get_roles(val):
		frappe.throw(
			_("{0} is not a {1} and cannot be named in {2}.").format(
				frappe.db.get_value("User", val, "full_name") or val, role, f.label),
			title=_("Invalid {0}").format(f.label),
		)


# What a numeric answer must parse as. Spelled out because frappe's own caster is FORGIVING here —
# `cast("Float", "pari@example.com")` is `0.0` and never raises — so a typed check cannot be built on it.
_NUMERIC_FIELDTYPES = ("Int", "Float", "Currency", "Percent")


def _validate_typed(f, val):
	"""An answer must BE the type its question declares — the gate the framework does not provide.

	`frappe.utils.cast` turns `pari@example.com` into `0.0` for a Float and `0` for an Int without a word.
	The rep sees a saved form, the ACTIVITY keeps the text they typed and the LEAD keeps the zero, and the
	two records disagree about the same answer with nobody told. Measured on a real punch: a snapshot
	reading `pari@example.com` beside a lead column reading `0`.

	Date and Datetime already raise on junk; they are caught here only so the rep reads their field's own
	name instead of a dateutil traceback.

	Blank is not judged — `_required_here` above owns whether an answer was needed at all. A `Check` is not
	judged either: a checkbox cannot emit anything but its two values, and a gate nobody can trip is noise.
	"""
	if _blank(val) or not f.fieldtype:
		return
	if f.fieldtype in _NUMERIC_FIELDTYPES:
		try:
			float(cstr(val).strip().replace(",", ""))
		except (TypeError, ValueError):
			frappe.throw(
				_("{0} takes a number, and {1} is not one.").format(f.label, val),
				title=_("Invalid {0}").format(f.label),
			)
		return
	if f.fieldtype in ("Date", "Datetime"):
		try:
			frappe.utils.cast(f.fieldtype, val)
		except Exception:
			frappe.throw(
				_("{0} takes a date, and {1} is not one.").format(f.label, val),
				title=_("Invalid {0}").format(f.label),
			)


def _validate_picklist(f, val, answers, axes):
	"""A picklist field takes only a value its own picker could have offered — the WRITE half of `_link_query`.

	It re-asks the picker's question, CASCADE INCLUDED: a plan belonging to another condition is refused even
	though the row is real, because the rep who changed the driver was never shown it. Without this the
	narrowing is advice, and a stale form, the partner API or a replay may ignore it. Grain is the lead's own,
	matched the way `picklist._grain_filters` matches it so the read and the write clamp identical rows."""
	if not (val and f.fieldtype == "Link" and (f.options or "") == "CRM Picklist Value"):
		return
	category = picklist.category_of(f.fieldname)
	conds = {"name": val, "category": category}
	conds.update(picklist._grain_filters(axes))
	parent = _cascade_parent(category)
	if parent:
		# Blank is admitted so an ungated row still passes — the same `in [value, ""]` the query reads with.
		conds["depends_on_value"] = ["in", [cstr(answers.get(parent) or ""), ""]]
	if frappe.db.exists("CRM Picklist Value", conds):  # authz-ok: tier-a — existence of one row already clamped to the lead's grain
		return
	frappe.throw(
		_("{0} is not an option this form offers for the {1} chosen.").format(f.label, parent or _("grain"))
		if parent else _("{0} is not an option this form offers.").format(f.label),
		title=_("Invalid {0}").format(f.label),
	)


@frappe.whitelist()
def user_query(doctype, txt, searchfield, start, page_len, filters):
	"""The people a `Link -> User` activity field may offer — `search_link`'s custom-query seam.

	Answers ONE bounded question — who may be named in THIS field, on THIS task type, by THIS caller — so the
	`Desk User` trim stands and no caller can turn it into a staff directory read. The guard is the one
	`get_schema` already applies, the grain is the type's own, and the role is FIELD_ROLE's."""
	filters = frappe.parse_json(filters) if isinstance(filters, str) else (filters or {})
	task_type = filters.get("task_type")
	posture.require("CRM Task Type", "read", doc=task_type)  # cannot open the form -> cannot query its picker
	tt = frappe.get_cached_doc("CRM Task Type", task_type)
	# Read on CRM Task Type is flat, so the type bounds nothing: without this a rep walks every line's users.
	if not entitlement.grain_overlaps_entitlement((tt.vertical, tt.group, tt.program)):
		raise frappe.PermissionError(_("Not entitled to {0}").format(task_type))
	users = entitlement.users_entitled_to(
		(tt.vertical, tt.group, tt.program),  # blank axis MEANS any — never back-filled
		txt=txt, limit=cint(page_len) or 20, role=FIELD_ROLE.get(filters.get("fieldname") or ""),
	)
	names = dict(frappe.get_all("User", filters={"name": ["in", users]}, as_list=True,
	                            fields=["name", "full_name"])) if users else {}
	return [(u, names.get(u) or u) for u in users]  # one read for every label, never one per row


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


def _settled(fields, values):
	"""The form's own reading of a set of answers: which declared fields it SHOWS, and those answers with every hidden one read back blank (D22). ONE fixpoint, asked by the writer that refuses a submission and by the guard that refuses a completion, so the two can never disagree about what the form asked for."""
	shown = _shown_fieldnames(fields, values)
	return shown, _inert(fields, values, shown)


def copied_values(fields, values):
	"""{fieldname: copied value} for every field a Set Value rule fills — THE one resolution of the verb.

	Judged against the SETTLED answers, so a copy conditioned on a hidden field does not fire off that
	field's stale answer: hiding a driver blanks it (D22) and the condition then reads false, which is the
	same collapse visibility and mandatory already get from the one fixpoint.

	The source is read from the same settled bag, so copying a hidden field yields blank rather than a value
	the form was not showing. First rule whose condition passes wins; a field whose rules all fail is absent
	from the result and keeps whatever it already held.

	Settled to a fixpoint, exactly as `_shown_fieldnames` is and for the same reason: a copy is an answer, so
	it can feed the next copy and can reveal the field a later copy is conditioned on. Resolving one pass
	made `a -> b -> c` store `c` blank on the server while the browser — which re-runs on its own reactivity
	— converged and showed the rep a value. The loop terminates because `_validate_copy_graph` refuses a
	cycle; the field-count bound is the same backstop the visibility fixpoint keeps."""
	out = {}
	for _pass in range(len(fields) + 1):
		shown, live = _settled(fields, {**values, **out})
		step = {}
		for f in fields:
			if f.fieldname not in shown:
				continue
			for rule in f.copy_from:
				if _field_visible(rule["when"], live):
					step[f.fieldname] = live.get(rule["source"], "")
					break
		if step == out:
			break
		out = step
	return out


def _required_here(f, shown, live):
	"""True when the submitted form must carry this field: it is actually SHOWN, and it is mandatory —
	declared `reqd`, or made so by a Make Mandatory rule whose condition passes (§17.3).

	A hidden field is never required, whether its own condition, its section's or a rule hid it: the rep was
	never shown it, so requiring it would brick the save. Judged on the server by the SAME evaluator the
	client mirrors — `_field_visible` — never a second rule."""
	if f.fieldname not in shown:
		return False
	return bool(f.reqd) or (bool(f.mandatory_depends_on) and _field_visible(f.mandatory_depends_on, live))


def compute_activity(lead, task_type, values, task=None, new_observation=True):
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
	is stamped onto the location audit so every Accepted/Not Required row carries its exact task id.

	`new_observation` says whether this is a NEW punch or an EDIT of one already recorded, and it cannot be
	derived from `task`: a new punch on a location-tracked grain passes its freshly-inserted shell. Only
	`save_activity` knows, so only it may say. It decides where a `source = Lead` answer lands on a
	MULTI-ROW section — a new punch is a new observation and takes a new row (a second payment is a second
	purchase), while an edit corrects the row that punch already wrote. Defaulted True because every other
	caller is the native new-task path."""
	if isinstance(values, str):
		values = frappe.parse_json(values) or {}
	vertical, group, program = _lead_axes(lead)
	if not _scope_applies(task_type, vertical, group, program):
		frappe.throw(_("This activity is not available for this lead."), title=_("Out of scope"))

	tt = frappe.get_cached_doc("CRM Task Type", task_type)
	# The rules compiled in — the SAME projection the form rendered from, so the save cannot demand or accept
	# anything the rep was not shown (§17.3).
	schema = compiled_fields(tt)
	# Set Value resolved HERE and not only in the browser, so a form the rep filled and one the partner API
	# or the migration wrote store the same record from the same declaration. It runs before the settle
	# because a copied answer is an answer: it may reveal a field or make one mandatory, exactly as a typed
	# one does. What the caller sent for a copied field is discarded — the verb makes it derived, and LSQ
	# marks every one of these targets read-only in the same rule set.
	copied = copied_values(schema, values)
	if copied:
		values = {**values, **copied}
	shown, live = _settled(schema, values)
	promoted, staged = {}, {}
	# The lead's own values: what a `source = Lead` field falls back to when the rep leaves it alone.
	lead_values = lead_field_values(lead, task_type) if any(
		(f.source or "") == LEAD_SOURCE for f in schema) else {}
	# Only a rep's own punch moves the lead on: a trusted caller is replaying history or holds its own lead endpoint.
	lead_writes = None if posture.is_trusted() else {}
	for f in schema:
		val = values.get(f.fieldname)
		if (f.source or "") == LEAD_SOURCE:
			# What the rep COULD have done decides what arrives: a field the form hid collected nothing, one it drew read-only is the lead's own value, and only one it drew writable can carry an answer worth writing back.
			if f.fieldname not in shown:
				val = None
			elif f.read_only or _blank(val):
				val = lead_values.get(f.fieldname)
			elif lead_writes is not None and cstr(val) != cstr(lead_values.get(f.fieldname) or ""):
				lead_writes[f.fieldname] = val
		if f.fieldname not in shown:
			# D22 governs ANSWERS: a form that never showed a question cannot have collected one. A lead snapshot is not an answer and is dropped above, so this speaks for the activity's own fields.
			if not _blank(val):
				frappe.throw(_("{0} was not shown on this form and its value cannot be saved.").format(f.label),
							 title=_("Hidden field"))
			continue
		if _required_here(f, shown, live) and _blank(val):
			frappe.throw(_("{0} is required.").format(f.label), title=_("Missing field"))
		_validate_person(f, val)  # a person field takes only who its picker could have offered
		_validate_picklist(f, val, values, (vertical, group, program))  # and a picklist only what ITS picker could, cascade included
		# A REP's answer must be the type it declares; a REPLAY's is not judged. The migration lands what
		# LeadSquared recorded, junk included — history is not ours to rewrite, and a throw here would abort
		# the load. Same carve-out `lead_writes` makes above, read off the same posture.
		if lead_writes is not None:
			_validate_typed(f, val)
		# Route by the ONE seam: a retained common column stays on the task row, every other value is its section row's. A lead-sourced field is routed like any other — what it means is snapshot, not answer, and the section it declares is where that snapshot lands.
		section_key, column = field_target(f)
		if section_key is None:
			promoted[column] = val
		_stage_section_value(staged, f, val)

	# After the loop: nothing reaches the lead until every field has passed D22, required and the person guard.
	if lead_writes:
		from tatva_connect.lead.detail import write_lead_fields
		write_lead_fields(lead, lead_writes, new_observation=new_observation)

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
	address = (_reverse_geocode(lat, lng) or {}).get("address")
	fields.update(location_fields(lat, lng, address=address, accuracy=accuracy))
	# Accepted (in range, or the first capture that establishes the anchor). Distance is logged from
	# the guard's single haversine — no recompute. Commits with the task save (same txn — task succeeds).
	log_visit_audit(
		lead, task_type, "Accepted", lat=lat, lng=lng,
		distance_m=guard.get("distance_m"), allowed_m=guard.get("allowed_m"),
		anchor_lat=guard.get("anchor_lat"), anchor_lng=guard.get("anchor_lng"), task=task,
	)
	return fields


def lead_field_values(lead, task_type):
	"""The lead's CURRENT answers to this type's `source=Lead` fields — what the form opens prefilled with,
	and what a field the rep left alone falls back to on save.

	NOT whitelisted: it rides the `type_config` answer the form already fetches, so loading a form stays ONE
	call however many lead fields it declares. Read through the lead detail brain (`lead.detail.lead_detail`),
	so a field this viewer is not entitled to see is not in the answer at all and nothing here re-decides who
	may read what. Empty for a type that declares no lead field, which is every type until an admin declares
	one.

	Reached from `compute_activity` on every save of a type that declares such a field, so it is on the
	partner's write path as well as the form's read path — which is why the checkpoint here is the posture
	seam and not a bare engine call. A trusted caller skips the ROLE check only; the entitlement gate
	inside `lead_detail` still decides which fields it may see, and a partner's grain is its own."""
	posture.require("CRM Lead", "read", doc=lead)
	wanted = {f.fieldname for f in compiled_fields(frappe.get_cached_doc("CRM Task Type", task_type))
			  if (f.source or "") == LEAD_SOURCE}
	if not wanted:
		return {}
	from tatva_connect.lead.detail import lead_detail

	out = {}
	for section in lead_detail(lead)["sections"]:
		for f in section["fields"]:
			if f["fieldname"] in wanted and not _blank(f["value"]):
				# A set-valued field's answer IS a list and stays one: `cstr` would prefill the form with a Python repr.
				out[f["fieldname"]] = f["value"] if isinstance(f["value"], (str, list)) else cstr(f["value"])
	return out


@frappe.whitelist()
def compute_activity_fields(lead, task_type, values):
	"""Whitelisted compute for the native new-task path: the CRM Task form script stamps the result
	onto the doc, then lets the native create save it ONCE (no double insert, no lingering popup)."""
	posture.require("CRM Lead", "write", doc=lead)
	return compute_activity(lead, task_type, values)


def _own_columns(task_fields):
	"""The CRM Task's OWN columns the calling FORM edited beside the answers — title, status, due date and
	the rest of the standard fields the modal renders.

	Deliberately NOT an allowlist here, and there was never one: this arrives from the surface that draws
	those controls, exactly as it did when the same dict was the `fieldname` argument of
	`frappe.client.set_value` (frappe/client.py:189-198 — get_doc, update, save, no field list). A tuple of
	column names in this module would be a SECOND brain beside `CRM Task Field`, which is the deleted
	`COMMON_COLUMNS` all over again (D-H); and `CRM Task Field.can_set` cannot answer this question either —
	it says which columns a DECLARED FIELD may route onto (`field_target` rule 2), and it ticks `status` OFF
	precisely so no declaration can write it, while the form's status picker must. Two different questions,
	so a third one is not being invented: what a caller may write is decided by `doc.save`'s own permission
	and permlevel checks, which is what set_value relied on too.

	Absent for every trusted caller — the partner API sends answers only — so an omitted argument is
	byte-identical to what shipped."""
	if isinstance(task_fields, str):
		task_fields = frappe.parse_json(task_fields)
	return task_fields or {}


@frappe.whitelist()
def save_activity(lead, task_type, values, task=None, task_fields=None):
	"""THE one writer for completing/updating an activity (board completion + ad-hoc punch). Returns
	the task name.

	`task_fields` is the CRM Task's own columns the calling form edited beside the answers, and they are
	applied in the SAME `doc.update` as the computed ones so an activity is ONE write. It used to be a
	`frappe.client.set_value` call the client made first, and that fork was three defects: `status: Done`
	committed before any answer existed, so the completion backstop of the day refused a rep who had
	just filled the form; and every refusal after it left the task half-updated, because the standard edits
	were already in. One update, one save, one transaction — a throw now rolls the whole thing back.
	The computed fields are applied LAST, so the type's declaration still decides `status` and every routed
	column, exactly as it did when compute ran second.

	A NEW punch on a location-tracked grain inserts the task SHELL first, so the visit audit logged
	inside compute_activity carries the new task's exact id, and then computes and saves onto it. That
	costs two writes to the same row, and it buys exactly one thing: an audit that can name its task.
	Where location is NOT tracked there is no audit, so there is nothing to name, and the task is
	inserted ONCE — already carrying its computed fields. Same writer, same compute, one row.

	The shell insert is in the same request transaction as the guard: an out-of-range throw rolls the
	shell back with everything else, so a blocked visit never leaves an orphan task."""
	from tatva_connect.location.api import is_location_tracked

	posture.require("CRM Lead", "write", doc=lead)

	trusted = posture.is_trusted()
	own = _own_columns(task_fields)

	if task:
		# An EDIT corrects the row this punch already wrote; a FIRST punch adds one. What separates them is
		# whether this task has been punched before — NOT whether a task name was supplied. Every task the
		# workflow engine raises exists before the rep opens it, so keying on `if task` classed every first
		# punch as an edit: the lead write then corrected a row that was never written, and a multi-row
		# section's row key — `Plan retool`'s `Assign plan Date Time`, which IS the plan row's identity —
		# was refused as a re-key of a row that did not exist.
		already_punched = frappe.db.get_value("CRM Task", task, "status") == "Done"
		fields = compute_activity(lead, task_type, values, task=task, new_observation=not already_punched)
		doc = frappe.get_doc("CRM Task", task)
		doc.update(own)
		doc.update(fields)
		doc.save(ignore_permissions=trusted)  # authz-ok: tier-b — the posture seam; UI is ordinary, partner is pre-gated by mapping + grain
		return _bond_attachments(doc.name, task_type, values)

	# title = the clean type_name (display), never the composite PK.
	title = labels.label(task_type, TASK_TYPE)
	# Trusted (partner/system) write has no caller-assignee: leave unassigned for the Assignment Rule.
	shell = frappe.get_doc({
		"doctype": "CRM Task",
		"title": title,
		"custom_task_type": task_type,
		"assigned_to": None if trusted else frappe.session.user,
		"reference_doctype": "CRM Lead",
		"reference_docname": lead,
	})
	shell.update(own)

	if is_location_tracked(lead) is None:
		# No audit will be written, so nothing needs the task's id before it exists: compute first,
		# then insert once, fully formed.
		shell.update(compute_activity(lead, task_type, values, task=None))
		shell.insert(ignore_permissions=trusted)  # authz-ok: tier-b — the posture seam; UI is ordinary, partner is pre-gated by mapping + grain
		return _bond_attachments(shell.name, task_type, values)

	shell.insert(ignore_permissions=trusted)  # authz-ok: tier-b — the posture seam; UI is ordinary, partner is pre-gated by mapping + grain
	fields = compute_activity(lead, task_type, values, task=shell.name)
	doc = frappe.get_doc("CRM Task", shell.name)
	doc.update(fields)
	doc.save(ignore_permissions=trusted)  # authz-ok: tier-b — the posture seam; UI is ordinary, partner is pre-gated by mapping + grain
	return _bond_attachments(doc.name, task_type, values)


def _bond_attachments(task, task_type, values):
	"""M1: the task that captured a file owns it, and this is the first moment it exists to — the same `bond_file` rule, sourced from the task type's schema because an answer is a routed value and CRM Task declares no Attach docfield. Returns the task name, every `save_activity` exit's last word."""
	if isinstance(values, str):
		values = frappe.parse_json(values) or {}
	for f in compiled_fields(frappe.get_cached_doc("CRM Task Type", task_type)):
		if f.fieldtype in ("Attach", "Attach Image"):
			file_events.bond_file(values.get(f.fieldname), "CRM Task", task, f.fieldname)
	return task




def _type_config(task_type):
	"""Render config for a task type: the ordered field schema, the same fields laid out in the tabs,
	sections and columns the declaration draws, whether completing it logs Done, and whether it can capture
	location (visit_mode In-Person, or a declared location condition). None for a type with no config row.

	`fields` is what every READER of a task's answers walks; `tabs` is what the FORM renders. They are the
	one descriptor list, projected twice — see `compiled_layout`."""
	if not frappe.db.exists("CRM Task Type", task_type):
		return None
	doc = frappe.get_cached_doc("CRM Task Type", task_type)
	from tatva_connect.location.api import captures_location

	fields, tabs = compiled_layout(doc)
	return {
		"fields": fields,
		"tabs": tabs,
		"is_logged_complete": int(doc.is_logged_complete or 0),
		"captures_location": captures_location(doc.visit_mode, doc.location_condition_field),
	}


@frappe.whitelist()
def task_detail(task):
	"""Render-ready detail for ONE task by name — the global Tasks list / sidebar opens TatvaTaskModal
	from this (a task row + its type config). `config` is null for a plain
	(non-activity) task, so the client falls back to the native doctype modal. One brain reused
	(_type_config / _task_values / _task_location)."""
	posture.require("CRM Task", "read", doc=task)
	r = frappe.db.get_value(
		"CRM Task", task,
		["name", "title", "custom_task_type", "status", "priority", "due_date", "start_date",
		 "assigned_to", "owner", "creation", "description", "custom_is_planned",
		 *task_columns(),
		 "custom_location_latitude", "custom_location_longitude",
		 "custom_location_address", "custom_location_captured_at",
		 "reference_doctype", "reference_docname"],
		as_dict=True,
	)
	if not r:
		frappe.throw(_("Task {0} not found").format(task))
	cfg = _type_config(r.custom_task_type) if r.custom_task_type else None
	who = r.assigned_to or r.owner
	values = _task_values(r, cfg)
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
			# Which half this row was BORN as, stamped once at insert. The form shows its scheduling half iff
			# this is set — never re-derived from the due date, which a rep may clear.
			"is_planned": int(r.custom_is_planned or 0),
			"start_date": str(r.start_date) if r.start_date else None,
			"assigned_to": r.assigned_to,
			"reference_doctype": r.reference_doctype,
			"reference_docname": r.reference_docname,
			"rep": who,
			"rep_name": (who and frappe.db.get_value("User", who, "full_name")) or who,
			"creation": str(r.creation),
			"datetime": format_datetime(r.creation, "d MMM, h:mm a"),
			"values": values,
			# The name behind each Attach answer, so the form's control reads it instead of the slugged key.
			"file_names": _attach_labels(values, cfg),
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
	from tatva_connect.location.api import captures_location

	rows = frappe.get_all(
		"CRM Task Type", filters={"name": ["in", wanted]},
		fields=["name", "type_name", "visit_mode", "location_condition_field", "is_logged_complete"],
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
				or captures_location(r.visit_mode, r.location_condition_field)
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
	posture.require("CRM Task Type", "read", doc=task_type)
	cfg = _type_config(task_type)
	if cfg is None:
		frappe.throw(_("Task type {0} not found").format(task_type))
	cfg["lead_values"] = lead_field_values(lead, task_type) if lead else {}
	_stamp_lead_controls(lead, cfg["fields"])
	_stamp_picklist_lead(lead, cfg["fields"])
	return cfg


def _stamp_picklist_lead(lead, fields):
	"""Name the lead in every picklist picker's filters — `picklist_query` re-reads the grain off it, server-side.

	Stamped per CALL and never in `_link_query`, for the reason `_stamp_lead_controls` is: the descriptor
	belongs to the TYPE and the grain belongs to the LEAD. Without a lead the filters stay grainless and the
	query answers nothing, which is the honest answer for a form opened against no patient."""
	if not lead:
		return
	for f in fields:
		lq = f.get("link_query")
		if lq and lq.get("query") == PICKLIST_QUERY:
			lq["filters"]["lead"] = lead


# What a lead field IS, which the LEAD decides and a form must never restate — see `_stamp_lead_controls`.
# `display` rides along because a read-only row is DRAWN as text, and a Link's raw value is its composite PK.
LEAD_CONTROL_KEYS = ("fieldtype", "options", "link_query", "multi_value", "display")


def _stamp_lead_controls(lead, fields):
	"""Give each `source = Lead` descriptor the control `lead_detail` draws for that column — one brain, so a form cannot disagree with the Data tab about a Link's scoped picker or a multi-value field's set-ness. Rides `type_config` for the reason `lead_values` does: the answer is the LEAD's, and without one the declaration stands."""
	wanted = {f.fieldname for f in fields if (f.get("source") or "") == LEAD_SOURCE} if lead else set()
	if not wanted:
		return
	from tatva_connect.lead.detail import lead_detail

	control = {row["fieldname"]: {k: row[k] for k in LEAD_CONTROL_KEYS if k in row}
			   for section in lead_detail(lead)["sections"] for row in section["fields"]
			   if row["fieldname"] in wanted}
	for f in fields:
		f.update(control.get(f.fieldname) or {})


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
		at = _at_address(section, held, address)
		if takes_a_set(f):
			return [r.get(section.value_field) for r in at]
		return at[0].get(section.value_field) if at else None
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
			# A lead-sourced field is read at the SAME address every other one is: its snapshot row holds the lead's value as it stood that day, and re-reading the lead now would answer a different question.
			value = _section_answer(f, r, rows, sections)
			if not _blank(value):
				# A set stays a set; every other answer is the string the renderer reads.
				vals[f["fieldname"]] = value if isinstance(value, (str, list)) else cstr(value)
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
	"""The storage key inside a proxy URL, via the ONE parser in blob_store. A thin alias because callers
	key a dict on it and want "" rather than None — and because a malformed url must degrade to "" here,
	never raise into `_lead_files` or the activity feed."""
	try:
		return blob_store.blob_key_from_url(url) or ""
	except Exception:
		return ""


def _lead_files(lead):
	"""blob_key -> {file_url, file_name} for every File the lead owns, read from the lead's own union and never a query of ours: an activity's attachment belongs to the TASK that captured it, so a filter on files parented to the lead reads back none of them."""
	from crm.api.activities import get_attachments

	files = {}
	for f in get_attachments("CRM Lead", lead):
		key = _blob_key(f["file_url"])
		if key:
			files[key] = {"file_url": f["file_url"], "file_name": f["file_name"]}
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
		docs.append(files_by_key.get(key) or {"file_url": url, "file_name": file_names.display_name(url)})
	return docs


def _attach_labels(values, cfg):
	"""{file_url -> real name} for an activity's Attach answers — the form's counterpart of the map
	`get_doc_link_titles` ships for a document. Same one utility, so a file reads the same in the modal
	as it does on the timeline card."""
	if not cfg:
		return {}
	urls = [values.get(f["fieldname"]) for f in cfg["fields"]
			if f.get("fieldtype") in ("Attach", "Attach Image")]
	return file_names.display_names(urls)


@frappe.whitelist()
def lead_timeline(lead):
	"""The single activity projection for a lead — used by BOTH the SPA timeline and the Desk Activity
	Timeline. Newest first; ONE rich entry per activity task: its status, captured location, and the
	documents it attached — so the timeline renders the whole action as one chained line."""
	posture.require("CRM Lead", "read", doc=lead)
	activity_types = _activity_type_names()
	if not activity_types:
		return []
	tasks = frappe.get_all(
		"CRM Task",
		filters={"reference_docname": lead, "custom_task_type": ["in", list(activity_types)]},
		fields=["name", "creation", "modified", "status", "custom_task_type", "description",
				"custom_automated",
				"assigned_to", "owner", *task_columns(),
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
