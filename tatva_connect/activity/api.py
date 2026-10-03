"""The activity engine: an activity is a CRM Task of a grain-scoped type, written by `save_activity`.
Every permission question goes through `access.posture.require`, locked by test_permission_checks_one_seam.py."""
import json

import frappe
from frappe import _
from frappe.model import NO_VALUE_FIELDS
from frappe.utils import cint, cstr, flt, format_datetime, formatdate, get_datetime, now_datetime

from tatva_connect.access import entitlement, posture
from tatva_connect.api._base import throw_field
from tatva_connect.lead import keyvalue, multirow
from tatva_connect.storage import blob_store, file_events, file_names
from tatva_connect.taxonomy import form_versions, grain, labels, picklist
from tatva_connect.taxonomy.grain import resolve_scoped
from tatva_connect.taxonomy.labels import TASK_TYPE

_TASK_COLUMNS_CACHE = "tatva_connect:task_settable_columns"
_KEY_VALUE_CACHE = "tatva_connect:key_value_section"


def task_columns():
	"""The CRM Task columns a declared field may use, read from `CRM Task Field` where `can_set` is ticked.
	A target no section or column claims falls to the key-value home."""
	from tatva_connect.access import request_cache

	def build():
		return tuple(r.fieldname for r in frappe.get_all(
			"CRM Task Field", filters={"can_set": 1}, fields=["fieldname"], order_by="fieldname"))

	return request_cache(_TASK_COLUMNS_CACHE, "all", build)


# The rule grammar (D27/D28), mirrored from CRM Task Type Rule's Select options and locked by test_rule_compilation.py.
RULE_SHOW, RULE_HIDE, RULE_MANDATORY, RULE_SET_VALUE = "Show", "Hide", "Make Mandatory", "Set Value"
RULE_ACTIONS = (RULE_SHOW, RULE_HIDE, RULE_MANDATORY, RULE_SET_VALUE)
# Operators that compare against a value; the other two only ask whether an answer exists (D27).
RULE_VALUE_OPERATORS = ("is", "is not")
RULE_OPERATORS = (*RULE_VALUE_OPERATORS, "is set", "is not set")

# A field's `source`: a lead-sourced field opens prefilled, snapshots onto the task, and a changed answer writes back to the lead.
LEAD_SOURCE = "Lead"

# The query a `Link -> User` control hands `search_link`.
USER_QUERY = "tatva_connect.activity.api.user_query"
# The scoped picklist query, as in `lead.detail._link_query`, because reps cannot read CRM Picklist Value directly.
PICKLIST_QUERY = "tatva_connect.taxonomy.picklist.picklist_query"

# The role a person field may offer, read by both the picker and the save.
FIELD_ROLE = {"select_asm": "Sales Manager"}


def field_target(f):
	"""Where a declared activity field lives: `(section_key, address)`, with section None for the task row itself.
	The only read and write address since Phase 7."""
	target = f.get("target") or ""
	section = f.get("section") or ""
	if section and target and frappe.get_meta(
		frappe.get_cached_value("CRM Task Section", section, "target_doctype")
	).get_field(target):
		return section, target
	columns = task_columns()
	if target and not columns:
		# Never fall back to key-value here, or the answer lands where no reader looks.
		frappe.throw(_("No CRM Task Field is declared settable, so `{0}` cannot be routed. The declaration "
		               "is seeded by task_field_seed.ensure_rows on after_migrate.").format(f.get("fieldname")))
	if target in columns:
		return None, target
	# A field naming a key-value section is written there; otherwise the fallback always picks the first one.
	if section and frappe.get_cached_value("CRM Task Section", section, "is_key_value"):
		return section, f.get("fieldname")
	key_value = _key_value_section()
	if key_value is None:
		# Never return (None, fieldname): the caller would write to a task column that does not exist.
		frappe.throw(_("No CRM Task Section is declared key-value, so `{0}` has nowhere to land. The "
		               "declaration is seeded by section_seed.ensure_rows on after_migrate.").format(
			f.get("fieldname")))
	return key_value, f.get("fieldname")


def _key_value_section():
	"""The key-value section a field without its own shape answers in, or None before `after_migrate` seeds it.
	Memoised per request; a None is not kept, because the rows can appear later in the same process."""
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
	"""Whether the declared sections and task columns exist yet, so a caller before `after_migrate` can skip."""
	return _key_value_section() is not None and bool(task_columns())


# Declared fieldtype -> its typed comparison column and cast (D17), shared by the writer and Smart Views.
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
	"""Whether a value is unanswered: None, an empty string or an empty list, as the client's `isEmpty` decides."""
	return value is None or value == "" or value == []


def takes_a_set(f):
	"""Whether this declared field holds many values, read from the lead catalog's `is_multi_value`."""
	from tatva_connect.lead import multi_value

	return (f.get("source") or "") == LEAD_SOURCE and f.get("fieldname") in multi_value.fieldnames()


def _row_values(section, address, fieldtype, value):
	"""The columns one section row carries for a field: the section's declared value column for a key-value row,
	with its typed mirror, or the named column for a column section."""
	if not section.is_key_value:
		return {address: value}
	row = {section.row_key_field: address, section.value_field: cstr(value)}
	spec = _TYPED_COLUMNS.get(fieldtype or "")
	if spec:
		column, cast = spec
		row[column] = None if value in (None, "") else cast(value)
	return row


def _at_address(section, held, address):
	"""The rows held at one address, in stored order."""
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
	"""Replace the rows a set-valued field holds at one address, as `multi_value.replace` does on the lead.
	Returns True if the stored set changed."""
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
	"""Write one field's value to the row `field_target` names on a saved CRM Task, upserting that row.
	Returns True if a row changed; rule 2 writes nothing, because the caller already set the common column."""
	section_key, address = field_target(f)
	if section_key is None:
		return False
	section = frappe.get_cached_doc("CRM Task Section", section_key)
	if takes_a_set(f):
		return _put_section_set(doc, section, address, f, value)
	values = _row_values(section, address, f.fieldtype, value)
	rows = doc.get(section.child_table_field) or []
	# A key-value answer updates its current row, the one `keyvalue.newest_first` puts first for every reader.
	row = (next(iter(keyvalue.newest_first(_at_address(section, rows, address))), None)
		   if section.is_key_value else (rows[0] if rows else None))
	# A blank earns no row: key-value drops the row (clearing it), a column row keeps its siblings and blanks only its own.
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
	"""Stage one field's value as {child_table_field: [rows]} on a task that is still a dict."""
	section_key, address = field_target(f)
	if section_key is None:
		return
	section = frappe.get_cached_doc("CRM Task Section", section_key)
	# On a new task there is no earlier answer to clear, so a blank adds no row and no column.
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
	"""Write one declared field onto a saved CRM Task through `field_target`, as `compute_activity` does.
	Raises if the type does not declare the field; returns True if it changed."""
	f = next((x for x in form_versions.form_of(task_type, task).schema if x.fieldname == fieldname), None)
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
	"""The type's grain {vertical, group, program} from its composite key, or None for an unscoped type."""
	g = frappe.db.get_value(
		"CRM Task Type", task_type, ["vertical", "`group` as grp", "program"], as_dict=True
	)
	if g and (g.vertical or g.grp or g.program):
		return {"vertical": g.vertical or "", "group": g.grp or "", "program": g.program or ""}
	return None


def _grain_matches(grain, vertical, group, program):
	"""Whether a type is available on a lead: a set axis must match, a blank axis matches any, all-blank never does."""
	if _dormant(grain):
		return False
	return resolve_scoped([grain], vertical, group, program) is not None


def _dormant(scope):
	"""Whether a type grain is all blank, which means the type is never offered."""
	return not scope or not any(scope.get(axis) for axis in grain.AXES)


def _scope_applies(task_type, vertical, group, program):
	"""Whether this activity type is available to the given grain, through `_grain_matches`."""
	return _grain_matches(_grain_of(task_type), vertical, group, program)


def scope_applies_to_lead(task_type, lead):
	"""Whether this activity type is available to the lead's grain."""
	return _scope_applies(task_type, *_lead_axes(lead))


def _activity_type_names():
	"""Every CRM Task Type that has a grain, which is every activity type."""
	return {r.name for r in frappe.get_all("CRM Task Type", filters={"vertical": ["!=", ""]}, fields=["name"])}


def resolve_type_for_lead(lead, type_name):
	"""The grain-specific CRM Task Type key for this lead and a bare `type_name`, most specific first.
	Returns None when no type matches; every caller that holds only a name, such as the Partner API, uses this."""
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
		# A record not yet re-keyed is still named exactly type_name, so use it as is.
		return type_name if frappe.db.exists("CRM Task Type", type_name) else None
	winner = resolve_scoped(candidates, vertical, group, program)
	return winner["name"] if winner else None


@frappe.whitelist()
def open_activity_tasks(lead):
	"""Open activity tasks on a lead, so the client can open the activity form for a Tasks-tab row."""
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
	"""The activity types the picker offers for this lead's grain, skipping disabled types.
	Value is the composite key and label is the `type_name`."""
	posture.require("CRM Lead", "read", doc=lead)
	return _types_for_grain(*_lead_axes(lead))


@frappe.whitelist()
def list_types_for_grain(vertical=None, group=None, program=None):
	"""The same picker for an authored grain instead of a lead, used by the Smart View editor.
	Filters by `grain_overlaps_entitlement`, where a blank axis means any."""
	grain = (vertical or "", group or "", program or "")
	if not entitlement.grain_overlaps_entitlement(grain):
		frappe.throw(_("You are not entitled to this grain."), frappe.PermissionError)
	return _types_for_grain(*grain, authored=True)


def _types_for_grain(vertical, group, program, authored=False):
	"""The rows BOTH pickers offer; `authored` reads the asked grain as a rule grain (blank axis = ANY), a lead's as data."""
	# A disabled form is not offered here; `type_config` and `task_detail` still read it by name.
	filters = {"enabled": 1}
	for axis, value in zip(grain.AXES, (vertical, group, program), strict=True):
		if value or not authored:
			filters[axis] = ["in", ["", value]]
	rows = frappe.get_all(
		"CRM Task Type",
		filters=filters,
		fields=["name", "type_name", "vertical", "`group` as grp", "program"],
		order_by="type_name",
	)
	form_versions.prime(r.name for r in rows)
	out = []
	for r in rows:
		scope = {"vertical": r.vertical, "group": r.grp, "program": r.program}
		if authored:
			# A rule grain asks what the save gate asks: overlap (blank = ANY on both sides), then entitlement.
			offered = (not _dormant(scope) and grain.overlaps(scope, vertical, group, program)
			           and entitlement.grain_overlaps_entitlement((r.vertical, r.grp, r.program)))
		else:
			offered = _grain_matches(scope, vertical, group, program)
		if not offered:
			continue
		# The settings reps meet are the served version's, never a draft's being edited.
		form = form_versions.form_of(r.name)
		out.append({
			"name": r.name, "label": r.type_name or r.name,
			"is_logged_complete": int(form.is_logged_complete or 0), "visit_mode": form.visit_mode or "",
		})
	return out


def _field_descriptor(f):
	"""One activity field descriptor built from a CRM Task Type schema row, read by the form and the writers."""
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
		"copy_from": [],  # [{source, when}]: the Set Value rows naming this field, stamped by _compiled_rows
		"container_depends_on": [],  # the conditions of the tab/section/column holding it; stamped by _layout
		"container_label": "",  # the section (else tab) it is drawn under; stamped by _layout
		"link_query": _link_query(f),  # a Link -> User picker's scoped query; None leaves the native one
	})


def _link_query(f):
	"""The scoped query a Link picker asks, or None when the framework can answer it natively.
	Reps lose read on these masters, so `search_link` alone would return nothing useful."""
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
	"""THE one reader is `picklist.cascade_parent`; this is the name the compile already calls it by."""
	return picklist.cascade_parent(category)


def _one_condition(field, operator, value):
	"""One When condition as an expression that both the server's `safe_eval` and the client's JS read alike.
	A blank field is the constant 1; operators compile to comparisons and values are JSON-quoted."""
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
	"""The When conditions one rule row declares, read once for the compile, the validator and the dead-field lock.
	Values are returned as given."""
	out = []
	for field, operator, value in (
		(row.condition_field, row.operator, row.condition_value),
		(row.get("condition_field_2"), row.get("operator_2"), row.get("condition_value_2")),
	):
		if (field or "").strip():
			out.append(((field or "").strip(), (operator or RULE_OPERATORS[0]).strip(), value))
	return out


def _rule_atom(row):
	"""One rule row's whole When as an expression: every condition it declares, joined with AND.
	A row with no condition is the form's opening state and compiles to 1."""
	atoms = [_one_condition(*c) for c in rule_conditions(row)]
	if not atoms:
		return "1"
	return atoms[0] if len(atoms) == 1 else _rule_and(atoms)


def _rule_or(atoms):
	"""OR over rule atoms with `+`, which sums truthy values the same way in Python and JS."""
	return "+".join(f"({a})" for a in atoms)


def _rule_and(atoms):
	"""AND over rule atoms with `*`, which multiplies truthy values the same way in Python and JS."""
	return "*".join(f"({a})" for a in atoms)


def _rule_not(expr):
	"""NOT of a rule expression as a comparison; the outer parentheses are needed because `*` binds tighter than `==`."""
	return f"(({expr})==0)"


def _rules_by_target(tt):
	"""The type's rule rows keyed by target field: {fieldname: {action: [(atom, is_conditional)]}}.
	A blank-When row is the form's opening state, not a veto on later reveals."""
	out = {}
	for row in tt.get("rules") or []:
		atom = _rule_atom(row)
		# Any declared condition makes the row conditional; reading only the first hides the field forever (T-04).
		conditional = bool(rule_conditions(row))
		for target in rule_targets(row.targets):
			out.setdefault(target, {}).setdefault(row.action or "", []).append(
				(atom, conditional, row.get("set_value") or ""))
	return out


# Which published key a rule action feeds. Show reveals, Hide vetoes, Make Mandatory requires.
_CONDITION_KEYS = {
	RULE_SHOW: ("shown_when", "any_of"),
	RULE_HIDE: ("shown_when", "none_of"),
	RULE_MANDATORY: ("required_when", "any_of"),
}


def field_conditions(tt):
	"""A type's rules as data: {fieldname: {conditional, required, shown_when, required_when}}.
	Read from the same rows and readers the form compiles from."""
	out, closed, always = {}, set(), set()
	for row in tt.get("schema") or []:
		if (row.get("depends_on") or "").strip():
			out.setdefault(row.fieldname, {})["conditional"] = True
	for row in tt.get("rules") or []:
		conditions = rule_conditions(row)
		if not conditions:
			if row.action == RULE_HIDE:  # a blank-When Hide is the opening state (D25): closed until a Show reveals it
				closed.update(rule_targets(row.targets))
			elif row.action == RULE_MANDATORY:  # no When means always, so the field is plainly required
				always.update(rule_targets(row.targets))
			continue
		if row.action not in _CONDITION_KEYS:
			continue  # Set Value fills a field, it does not decide whether the field is there
		key, join = _CONDITION_KEYS[row.action]
		clause = {"all_of": [_condition_atom(*c) for c in conditions]}
		for target in rule_targets(row.targets):
			entry = out.setdefault(target, {})
			entry.setdefault(key, {}).setdefault(join, []).append(clause)
			if key == "shown_when":
				entry["conditional"] = True
	for target in closed:
		entry = out.setdefault(target, {})
		entry["conditional"] = True
		entry.setdefault("shown_when", {}).setdefault("any_of", [])
	for target in always:
		out.setdefault(target, {})["required"] = True
	return out


def _condition_atom(field, operator, value):
	"""One When condition as `{field, operator, value}`, the operator spelled as the row declares it.
	`value` is left out for an operator that takes none."""
	atom = {"field": field, "operator": operator}
	if operator in RULE_VALUE_OPERATORS:
		atom["value"] = value
	return atom


def rule_targets(targets):
	"""The fieldnames a comma-separated `targets` declaration names, read once for the compile and the validator."""
	return [t.strip() for t in (targets or "").split(",") if t.strip()]


def _compiled_visibility(entry, own):
	"""One field's compiled `depends_on`: visible when any Show row passes and no conditional Hide row does.
	A blank-When Hide row closes the field at start, and only a Show row opens it."""
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
	"""One field's compiled `mandatory_depends_on`: the OR of its Make Mandatory rows; static `reqd` stays separate."""
	rows = entry.get(RULE_MANDATORY) or []
	if not rows:
		return own
	return "eval:" + _rule_or([atom for atom, _c, _v in rows])


def _compiled_copy_from(entry):
	"""One field's Set Value rows as [{source, when}]: each copies another field's value, never a constant.
	The first row whose condition passes wins, and the server ignores what the client sends for the field."""
	return [{"source": value, "when": "eval:" + atom} for atom, _c, value in entry.get(RULE_SET_VALUE) or []]


def _compiled_rows(tt):
	"""Every declared row of a task type, layout markers included and in order, with its rules compiled in."""
	by_target = _rules_by_target(tt)
	out = []
	for f in tt.schema:
		d = _field_descriptor(f)
		entry = by_target.get(f.fieldname) or {}
		d.depends_on = _compiled_visibility(entry, d.depends_on)
		d.mandatory_depends_on = _compiled_mandatory(entry, d.mandatory_depends_on)
		d.copy_from = _compiled_copy_from(entry)
		# The server answers a copy target, so the rep may not edit it; see `_field_descriptor`.
		if d.copy_from:
			d.read_only = 1
		out.append(d)
	return out


def layout_tree(rows):
	"""The declared rows walked into tabs, sections, columns and rows, the way Frappe's `layout.js` walks docfields.
	Nothing is dropped, so the form builder edits this and `_layout` renders it."""
	tabs = []

	def start_tab(d=None):
		tabs.append({"row": d, "sections": []})
		start_section()

	def start_section(d=None):
		tabs[-1]["sections"].append({"row": d, "columns": []})
		start_column()

	def start_column(d=None):
		tabs[-1]["sections"][-1]["columns"].append({"row": d, "fields": []})

	start_tab()
	for d in rows:
		if d.fieldtype == "Tab Break":
			start_tab(d)
		elif d.fieldtype == "Section Break":
			start_section(d)
		elif d.fieldtype == "Column Break":
			start_column(d)
		else:
			tabs[-1]["sections"][-1]["columns"][-1]["fields"].append(d)
	return tabs


def _layout(rows):
	"""The form's layout: tabs, sections, columns and fieldnames, with each container's condition stamped on its fields.
	A field stays in the column it was declared in, whatever else is visible."""
	index = 0

	def named(kind, d):
		nonlocal index
		index += 1
		return {"key": (d.fieldname if d else "") or f"{kind}-{index}", "label": (d.label or "") if d else ""}

	tabs = []
	for t in layout_tree(rows):
		tab = {**named("tab", t["row"]), "sections": []}
		for s in t["sections"]:
			section = {**named("section", s["row"]), "columns": []}
			for c in s["columns"]:
				column = {**named("column", c["row"]), "fields": []}
				gates = [x["row"].depends_on for x in (t, s, c) if x["row"] and x["row"].depends_on]
				for d in c["fields"]:
					if d.fieldtype in NO_VALUE_FIELDS:
						continue  # a marker this form has no layout meaning for stores nothing and renders nothing
					d.container_depends_on = list(gates)
					d.container_label = section["label"] or tab["label"]
					column["fields"].append(d.fieldname)
				section["columns"].append(column)
			tab["sections"].append(section)
		tabs.append(tab)
	return _prune(tabs)


def _prune(tabs):
	"""Drop every container that holds no field."""
	for tab in tabs:
		for section in tab["sections"]:
			section["columns"] = [c for c in section["columns"] if c["fields"]]
		tab["sections"] = [s for s in tab["sections"] if s["columns"]]
	return [t for t in tabs if t["sections"]]


def compiled_layout(tt):
	"""A type's declaration read once: `fields` is the flat list readers walk, `tabs` is the tree the form renders.
	Layout markers are dropped from `fields` by Frappe's `NO_VALUE_FIELDS`."""
	rows = _compiled_rows(tt)
	tabs = _layout(rows)
	return [d for d in rows if d.fieldtype not in NO_VALUE_FIELDS], tabs


def compiled_fields(tt):
	"""Every declared field of a task type with its rules compiled in; `get_schema` publishes it and `compute_activity` enforces it."""
	return compiled_layout(tt)[0]


@frappe.whitelist()
def get_schema(task_type):
	"""The activity type's per-field schema, in order and with its rules compiled in, for the client form."""
	posture.require("CRM Task Type", "read", doc=task_type)
	return compiled_fields(form_versions.form_of(task_type))


def fields_ever_asked(task_type):
	"""Every field any version of the form declared, the newest declaration first: an old task keeps its answers, so they stay reportable."""
	posture.require("CRM Task Type", "read", doc=task_type)
	seen = {}
	for form in form_versions.every_version(task_type):
		for f in compiled_fields(form):
			seen.setdefault(f.fieldname, f)
	return list(seen.values())


def _validate_person(f, val):
	"""Refuse a person field value unless that user holds the role FIELD_ROLE declares for the field."""
	role = FIELD_ROLE.get(f.fieldname)
	if not (val and role and f.fieldtype == "Link" and (f.options or "") == "User"):
		return
	if role not in frappe.get_roles(val):
		frappe.throw(
			_("{0} is not a {1} and cannot be named in {2}.").format(
				frappe.db.get_value("User", val, "full_name") or val, role, f.label),
			title=_("Invalid {0}").format(f.label),
		)


# Numeric answers are checked here because `cast("Float", "pari@example.com")` returns 0.0 instead of raising.
_NUMERIC_FIELDTYPES = ("Int", "Float", "Currency", "Percent")


def _validate_typed(f, val):
	"""Refuse an answer that is not the type its question declares, because `frappe.utils.cast` would turn it into 0.
	Blank answers and Check fields are not judged."""
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
	"""Refuse a picklist value its own picker could not have offered, cascade and the lead's grain included."""
	if not (val and f.fieldtype == "Link" and (f.options or "") == "CRM Picklist Value"):
		return
	category = picklist.category_of(f.fieldname)
	conds = {"name": val, "category": category}
	conds.update(picklist._grain_filters(axes))
	parent = _cascade_parent(category)
	if parent:
		# Blank is admitted so an ungated row still passes, the same `in [value, ""]` the query uses.
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
	"""The people a `Link -> User` activity field may offer, for this field, task type and caller only."""
	filters = frappe.parse_json(filters) if isinstance(filters, str) else (filters or {})
	task_type = filters.get("task_type")
	posture.require("CRM Task Type", "read", doc=task_type)  # cannot open the form -> cannot query its picker
	tt = frappe.get_cached_doc("CRM Task Type", task_type)
	# Read on CRM Task Type is flat, so the type bounds nothing: without this a rep walks every line's users.
	if not entitlement.grain_overlaps_entitlement((tt.vertical, tt.group, tt.program)):
		raise frappe.PermissionError(_("Not entitled to {0}").format(task_type))
	users = entitlement.users_entitled_to(
		(tt.vertical, tt.group, tt.program),  # blank axis means any, never back-filled
		txt=txt, limit=cint(page_len) or 20, role=FIELD_ROLE.get(filters.get("fieldname") or ""),
	)
	return entitlement.link_rows(users)


def _field_visible(depends_on, values):
	"""Whether a field's `depends_on` passes for the submitted values, as the client evaluates it.
	Supports `eval:` and a bare fieldname; a blank or unparseable condition counts as visible."""
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
	"""Whether the form shows this field: its own condition and every container's condition pass."""
	return all(_field_visible(c, values) for c in f.container_depends_on) \
		and _field_visible(f.depends_on, values)


def _evaluable(fields, values):
	"""The answers a condition reads: every declared field, blank until answered, as the client seeds them."""
	bag = {f.fieldname: "" for f in fields}
	bag.update({k: ("" if v is None else v) for k, v in (values or {}).items()})
	return bag


def _shown_fieldnames(fields, values):
	"""The declared fields the form shows for these answers, repeated until the set stops changing.
	A hidden field reads as blank, and the loop is bounded by the field count."""
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
	"""The evaluable answers with every hidden declared field read as blank (D22)."""
	live = _evaluable(fields, values)
	for f in fields:
		if f.fieldname not in shown:
			live[f.fieldname] = ""
	return live


def _settled(fields, values):
	"""The declared fields the form shows for these answers, and the answers with every hidden one read blank (D22).
	The save and the completion guard both ask this, so they agree on what the form asked for."""
	shown = _shown_fieldnames(fields, values)
	return shown, _inert(fields, values, shown)


def merge_submission(task, task_type, incoming):
	"""The complete form a partial submission means: the task's saved answers overlaid with `incoming`.
	Trimmed to what the merged answers still show; only the Partner API update path calls this."""
	cfg = _type_config(task_type, task)
	if not cfg:
		return incoming or {}
	merged = {**_task_values(frappe.get_doc("CRM Task", task), cfg), **(incoming or {})}
	shown, _inert = _settled(cfg["fields"], merged)
	return {name: value for name, value in merged.items() if name in shown}


def copied_values(fields, values):
	"""{fieldname: copied value} for every field a Set Value rule fills, judged on the settled answers.
	Repeated until stable, so one copy can feed the next; `CRMTaskType._copy_graph_problems` refuses cycles."""
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
	"""Whether the submitted form must carry this field: it is shown, and it is required or made mandatory by a rule."""
	if f.fieldname not in shown:
		return False
	return bool(f.reqd) or (bool(f.mandatory_depends_on) and _field_visible(f.mandatory_depends_on, live))


def compute_activity(lead, task_type, values, task=None, new_observation=True):
	"""Turn a submitted activity form into CRM Task field values: validate, route each answer, run the location guard.
	`new_observation` is False only when `save_activity` edits a punch it already recorded."""
	if isinstance(values, str):
		values = frappe.parse_json(values) or {}
	vertical, group, program = _lead_axes(lead)
	if not _scope_applies(task_type, vertical, group, program):
		frappe.throw(_("This activity is not available for this lead."), title=_("Out of scope"))

	version = form_versions.version_of(task_type, task)
	tt = form_versions.read(task_type, version)
	# The compiled rules, the same projection the form rendered from, so the save matches what the rep saw (§17.3).
	schema = compiled_fields(tt)
	# Set Value resolves here, before the settle, so a rep's form and a Partner API call store the same record; a sent value for a copied field is discarded.
	copied = copied_values(schema, values)
	if copied:
		values = {**values, **copied}
	shown, live = _settled(schema, values)
	promoted, staged = {}, {}
	# The lead's own values: what a `source = Lead` field falls back to when the rep leaves it alone.
	lead_values = lead_field_values(lead, task_type, task) if any(
		(f.source or "") == LEAD_SOURCE for f in schema) else {}
	lead_writes = {}
	for f in schema:
		val = values.get(f.fieldname)
		if (f.source or "") == LEAD_SOURCE:
			# A hidden field collects nothing, a read-only one keeps the lead's value, and only a writable one can write back.
			if f.fieldname not in shown:
				val = None
			elif f.read_only or _blank(val):
				val = lead_values.get(f.fieldname)
			elif cstr(val) != cstr(lead_values.get(f.fieldname) or ""):
				lead_writes[f.fieldname] = val
		if f.fieldname not in shown:
			# D22 governs ANSWERS: a form that never showed a question cannot have collected one. A lead snapshot is not an answer and is dropped above, so this speaks for the activity's own fields.
			if not _blank(val):
				throw_field(_("{0} was not shown on this form and its value cannot be saved.").format(f.label),
							[f.fieldname], title=_("Hidden field"))
			continue
		if _required_here(f, shown, live) and _blank(val):
			throw_field(_("{0} is required.").format(f.label), [f.fieldname], title=_("Missing field"))
		_validate_person(f, val)  # a person field takes only who its picker could have offered
		_validate_picklist(f, val, values, (vertical, group, program))  # and a picklist only what ITS picker could, cascade included
		_validate_typed(f, val)
		# Route by `field_target`: a retained common column stays on the task row, every other value goes to its section row.
		section_key, column = field_target(f)
		if section_key is None:
			promoted[column] = val
		_stage_section_value(staged, f, val)

	# After the loop: nothing reaches the lead until every field has passed D22, required and the person guard.
	from tatva_connect.lead.detail import write_lead_fields

	# One lead save per submit: the answers that move the lead and the last-activity stamp land together.
	write_lead_fields(lead, lead_writes, new_observation=new_observation, stamp={
		"custom_prospectactivityname_max": labels.label(task_type, TASK_TYPE),
		"custom_prospectactivitydate_max": now_datetime(),
	})

	fields = {
		"status": "Done" if int(tt.is_logged_complete or 0) else "Todo",
		**promoted,
		**staged,
	}
	# The task remembers the version its answers were judged against, so it is always read with that one.
	if version:
		fields[form_versions.VERSION_FIELD] = version
	notes = values.get("notes")
	if notes and frappe.get_meta("CRM Task").has_field("description"):
		fields["description"] = notes

	# Location guard: an in-person activity on a tracked grain must carry an in-range fix; the rule lives in location.api.
	from tatva_connect.location.api import (
		_reverse_geocode,
		is_location_tracked,
		location_fields,
		location_required,
		log_visit_audit,
		set_or_check_anchor,
	)

	# Tracking off for this grain: write no audit row, so no task shell is needed first.
	if is_location_tracked(lead) is None:
		return fields

	# Tracking is on: a phone or office activity records "Not Required" in the trail.
	radius = location_required(task_type, lead, values, task)
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
	# Accepted: in range, or the first capture that sets the anchor; distance comes from the guard and commits with the task.
	log_visit_audit(
		lead, task_type, "Accepted", lat=lat, lng=lng,
		distance_m=guard.get("distance_m"), allowed_m=guard.get("allowed_m"),
		anchor_lat=guard.get("anchor_lat"), anchor_lng=guard.get("anchor_lng"), task=task,
	)
	return fields


def lead_field_values(lead, task_type, task=None):
	"""The lead's current values for this type's `source=Lead` fields: the form's prefill and the save's fallback.
	Read through `lead.detail.lead_detail`, so the caller sees only fields it is entitled to."""
	posture.require("CRM Lead", "read", doc=lead)
	wanted = {f.fieldname for f in compiled_fields(form_versions.form_of(task_type, task))
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
	"""The CRM Task's own columns the calling form edited beside the answers, applied in the same save.
	No allowlist here: `doc.save` permission and permlevel checks decide what may be written."""
	if isinstance(task_fields, str):
		task_fields = frappe.parse_json(task_fields)
	return task_fields or {}


@frappe.whitelist()
def save_activity(lead, task_type, values, task=None, task_fields=None):
	"""The one writer that completes or updates an activity, in one save; returns the task name.
	A new punch on a location-tracked grain inserts a task shell first, so the visit audit can name it."""
	from tatva_connect.location.api import is_location_tracked

	posture.require("CRM Lead", "write", doc=lead)

	trusted = posture.is_trusted()
	own = _own_columns(task_fields)

	if task:
		# Edit or first punch depends on whether this task was punched before, not on a task name, because workflow tasks exist first.
		already_punched = frappe.db.get_value("CRM Task", task, "status") == "Done"
		fields = compute_activity(lead, task_type, values, task=task, new_observation=not already_punched)
		doc = frappe.get_doc("CRM Task", task)
		doc.update(own)
		doc.update(fields)
		doc.save(ignore_permissions=trusted)  # authz-ok: tier-b — the posture seam; UI is ordinary, partner is pre-gated by mapping + grain
		return _bond_attachments(doc.name, task_type, values)

	# title = the clean type_name (display), never the composite PK.
	title = labels.label(task_type, TASK_TYPE)
	# A trusted (Partner API) write has no assignee; the Assignment Rule assigns it.
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
		# No audit will be written, so compute first and insert the task once.
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
	"""Bond each Attach answer's file to the task that captured it, by the task type's schema; returns the task name."""
	if isinstance(values, str):
		values = frappe.parse_json(values) or {}
	for f in compiled_fields(form_versions.form_of(task_type, task)):
		if f.fieldtype in ("Attach", "Attach Image"):
			file_events.bond_file(values.get(f.fieldname), "CRM Task", task, f.fieldname)
	return task




def _type_config(task_type, task=None):
	"""Render config for a task type, read with `task`'s own version (`form_versions.form_of`): fields, the layout tabs,
	whether completing logs Done, and location capture. None for a type with no config row."""
	if not frappe.db.exists("CRM Task Type", task_type):
		return None
	return _config(form_versions.form_of(task_type, task))


def configs_for(rows):
	"""{task name: render config} for many task rows (each carrying `status` and the version column), one config per form version."""
	named = sorted({r.custom_task_type for r in rows if r.custom_task_type})
	if not named:
		return {}
	known = set(frappe.get_all("CRM Task Type", filters={"name": ["in", named]}, pluck="name"))
	form_versions.prime(known)
	read_with = {r.name: (r.custom_task_type, form_versions.version_of(r.custom_task_type, r)) for r in rows if r.custom_task_type in known}
	configs = {key: _config(form_versions.read(*key)) for key in set(read_with.values())}
	return {name: configs[key] for name, key in read_with.items()}


def _config(doc):
	"""The render config of one form document, live or frozen."""
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
	"""Render-ready detail for one task by name; `config` is None for a plain task, which opens the native modal."""
	posture.require("CRM Task", "read", doc=task)
	r = frappe.db.get_value(
		"CRM Task", task,
		["name", "title", "custom_task_type", "status", "priority", "due_date", "start_date",
		 "assigned_to", "owner", "creation", "description", "custom_is_planned",
		 *task_columns(),
		 "custom_location_latitude", "custom_location_longitude",
		 "custom_location_address", "custom_location_captured_at",
		 "reference_doctype", "reference_docname", *form_versions.task_columns()],
		as_dict=True,
	)
	if not r:
		frappe.throw(_("Task {0} not found").format(task))
	cfg = _type_config(r.custom_task_type, r) if r.custom_task_type else None
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
			# The half this row was created as, stamped once; the form shows the scheduling half only when it is set.
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
	"""{task_type: (label, needs_capture)} for a page of tasks, one served form read per distinct type."""
	from tatva_connect.location.api import captures_location

	named = sorted({t for t in task_types if t})
	if not named:
		return {}
	wanted = set(frappe.get_all("CRM Task Type", filters={"name": ["in", named]}, pluck="name"))
	form_versions.prime(wanted)
	forms = {t: form_versions.form_of(t) for t in sorted(wanted)}
	return {
		name: (
			form.type_name or name,
			bool(
				bool(form.schema)
				or captures_location(form.visit_mode, form.location_condition_field)
				or form.is_logged_complete
			),
		)
		for name, form in forms.items()
	}


@frappe.whitelist()
def type_config(task_type, lead=None):
	"""Render config for one task type, for the create modal.
	With `lead`, it also carries that lead's current values for the type's `source=Lead` fields."""
	posture.require("CRM Task Type", "read", doc=task_type)
	cfg = _type_config(task_type)
	if cfg is None:
		frappe.throw(_("Task type {0} not found").format(task_type))
	cfg["lead_values"] = lead_field_values(lead, task_type) if lead else {}
	_stamp_lead_controls(lead, cfg["fields"])
	_stamp_picklist_lead(lead, cfg["fields"])
	return cfg


def _stamp_picklist_lead(lead, fields):
	"""Add the lead to every picklist picker's filters, so `picklist_query` can read its grain."""
	if not lead:
		return
	for f in fields:
		lq = f.get("link_query")
		if lq and lq.get("query") == PICKLIST_QUERY:
			lq["filters"]["lead"] = lead


# A lead field's control comes from the lead, never the form; `display` carries the drawn text for read-only rows.
LEAD_CONTROL_KEYS = ("fieldtype", "options", "link_query", "multi_value", "display")


def _stamp_lead_controls(lead, fields):
	"""Give each `source = Lead` field the control `lead_detail` draws for that lead column, so the form matches the Data tab."""
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
	"""Every declared activity section: its child table, row key and the column an answer is read from."""
	return frappe.get_all(
		"CRM Task Section",
		fields=["name", "title", "display_order", "target_doctype",
				"child_table_field", "is_key_value", "is_multi_row", "row_key_field", "value_field"],
		order_by="display_order",
	)


def section_rows(task_names):
	"""Every section child row these tasks carry, {task: {child table: [rows]}}, in one query per section.
	Keyed by task name as text, because a child row's `parent` is a varchar."""
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


# `_latest` archived in .archive/activity-section-latest-2026-09-29: a section is read by `multirow.reading`.


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
		current = keyvalue.newest_first(at)
		return current[0].get(section.value_field) if current else None
	row = multirow.reading(held, section)
	return row.get(address) if row else None


def _task_values(r, cfg, rows=None):
	"""Saved values keyed by schema fieldname, read at the address `field_target` names.
	`rows` is the task's section rows when the caller already holds them."""
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
	"""Captured-location state for a task card, or None when no fix was recorded."""
	if not (r.custom_location_latitude and r.custom_location_longitude):
		return None
	return {
		"lat": flt(r.custom_location_latitude),
		"lng": flt(r.custom_location_longitude),
		"address": r.custom_location_address or "",
		"captured_at": str(r.custom_location_captured_at) if r.custom_location_captured_at else None,
	}


def _blob_key(url):
	"""The storage key inside a proxy URL via blob_store's parser, or "" for a malformed URL."""
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
	"""{file_url: file name} for an activity's Attach answers, built with the same helper the timeline uses."""
	if not cfg:
		return {}
	urls = [values.get(f["fieldname"]) for f in cfg["fields"]
			if f.get("fieldtype") in ("Attach", "Attach Image")]
	return file_names.display_names(urls)


@frappe.whitelist()
def lead_timeline(lead):
	"""A lead's activities for both the SPA and Desk timelines, newest first, one entry per activity task."""
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
				"custom_location_address", "custom_location_captured_at", *form_versions.task_columns()],
		order_by="creation desc",
	)
	# One render config per form version on the rail, each task read with its own.
	cfgs = configs_for(tasks)
	type_names = labels.labels([t.custom_task_type for t in tasks], TASK_TYPE)
	files_by_key = _lead_files(lead)
	answers_by_task = section_rows([t.name for t in tasks])  # one query per section for the whole timeline
	out = []
	for t in tasks:
		who = t.assigned_to or t.owner
		cfg = cfgs.get(t.name)
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
