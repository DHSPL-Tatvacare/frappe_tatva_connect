"""The ONE query brain over the per-resource field catalogs (one brain per resource):
  • lead fields     → `CRM Lead API Field` (routing DERIVED from its `section` → `CRM Lead Section`;
                      grain from the internal contract `access.entitlement.field_in_grains_via_contract`).
  • task fields     → `CRM Task Field` (NATIVE columns, e.g. status) + `CRM Task Type Field` (per-task-type
                      DECLARED fields) — two sources read as ONE, so a Task lookup sees both.

WHAT AUTOMATION MAY TOUCH ON A LEAD IS THE GRAIN, AND ONLY THE GRAIN. `can_read`/`can_set` were a second,
weaker allowlist ticked per field on top of the internal contract, and every question they answered the
contract already answered — so a field could be entitled to a grain and still unreachable, for no reason an
operator could see. A workflow reads and writes the columns of its subject that its grain entitles, and the
SAME rows answer both, so a picker can never offer what execution refuses.

  • can_read, and can_set on the LEAD — gone, columns and all (`patches.drop_dead_automation_flag_columns`).
  • can_set on `CRM Task Field` — not automation's own flag: `activity.api.task_columns` owns it, so a form
    and a workflow decide a Task write on one answer (`_task_writable` below).
  • can_watch — survives on both, and it is not a permission: it is the dispatcher's diff list. A watched
    field's before/after pair is captured on save, which is what `changed to` reads. Only a watched field
    carries a before-value; every other field is read straight off the record.

WHAT THE GRAIN NARROWS IS THE PICKER, NOT THE GATE. `readable_rows_in_rule_grain` is what an author is
OFFERED; the gate at publish and at run time is wider on purpose — run state falls through to the live
document, so a node reading any real field of the subject reads it fine, and `upstream._subject_field_refs`
records why narrowing THERE would reject every predicate on a site whose contracts are not seeded yet.
The two are never in conflict because the offer is a subset of the gate: an author cannot build something
the runtime would refuse.

Lead child routing is DERIVED from `CRM Lead Section`; a native Task column's grain derives from the task
type — never stored twice (the AST lock forbids hardcoded child-table names outside the section seed).
"""
import frappe
from frappe.model import no_value_fields

from tatva_connect.automation import subjects

LEAD_DT = "CRM Lead"
TASK_DT = "CRM Task"


def _catalogs_for(doctype):
	"""The resource catalog(s) holding a subject's fields (one brain per resource). Task has TWO sources
	read as one — its native columns (`CRM Task Field`) + its per-task-type declared fields
	(`CRM Task Type Field`); one resolver, same signatures, no parallel path."""
	if doctype == LEAD_DT:
		return ["CRM Lead API Field"]
	if doctype == TASK_DT:
		return ["CRM Task Field", "CRM Task Type Field"]
	return []


def _task_writable(fieldname):
	"""A native column `activity.api.task_columns` admits, or any declared activity field."""
	from tatva_connect.activity.api import task_columns

	return fieldname in task_columns() or bool(frappe.db.exists("CRM Task Type Field", {"fieldname": fieldname}))


# `read_only` is what makes this usable rather than dangerous: it leaves the fields a person could set by hand and drops the ones the app computes for itself.
_WRITABLE = {"fieldtype": ["not in", no_value_fields], "read_only": 0, "is_virtual": 0}


def _meta_writable_rows(doctype):
	"""The fields a workflow may set on a declared write target, off its own meta — the shape the Task branch takes, which reads its catalog and asks the grain nothing. `default_fields` are not DocFields, so they need no exclusion."""
	return [frappe._dict(fieldname=df.fieldname) for df in frappe.get_meta(doctype).get("fields", _WRITABLE)]


def _meta_writable(doctype, fieldname):
	"""Whether a declared write target admits this field — `_task_writable`'s twin, asked of the meta."""
	return any(r.fieldname == fieldname for r in _meta_writable_rows(doctype))


def _grain_key(axes):
	return (axes[0] or "", axes[1] or "", axes[2] or "")


def _union_pluck(doctype, **query):
	out = []
	for catalog in _catalogs_for(doctype):
		out += frappe.get_all(catalog, pluck="fieldname", distinct=True, **query)
	return list(dict.fromkeys(out))


# -- watch side (the dispatcher's diff list, not a permission) ----------------


def is_watchable(doctype, fieldname):
	"""True if a can_watch row exists for this field in ANY of the doctype's catalogs — what a transition
	operator (`changed to` / `changed from…to`) needs, since only a watched field carries a before-value."""
	return any(frappe.db.exists(catalog, {"fieldname": fieldname, "can_watch": 1}) for catalog in _catalogs_for(doctype))


def watchable_fields(doctype):
	"""The can_watch fieldnames for a doctype (the dispatch diff cache)."""
	return _union_pluck(doctype, filters={"can_watch": 1})


# -- grain scope (the one gate, shared by read and write) ---------------------


def is_settable(doctype, fieldname, axes, child_table_field=""):
	"""Runtime/author gate. Lead: a catalog row whose section routing matches the child context and whose
	field_key is ticked by the lead's grain contract. Task: any row in either Task catalog (a native Task
	column's grain derives from the task type, not from these axes). Fail-closed."""
	from tatva_connect.access import entitlement

	if doctype == TASK_DT:
		return _task_writable(fieldname)
	if subjects.is_write_target(doctype):
		return _meta_writable(doctype, fieldname)
	if doctype != LEAD_DT:
		return False
	grain = {_grain_key(axes)}
	for row in frappe.get_all(
		"CRM Lead API Field", filters={"fieldname": fieldname}, fields=["field_key", "fieldname", "section"]
	):
		sec = frappe.get_cached_doc("CRM Lead Section", row.section)
		if (sec.child_table_field or "") != (child_table_field or ""):
			continue
		if entitlement.field_in_grains_via_contract(row.field_key, grain):
			return True
	return False


def is_set_declared(doctype, fieldname, child_table_field=""):
	"""MEMBERSHIP only: is this fieldname a field of this doctype AT ALL, in any grain?

	The publish-time half of the write gate, and DELIBERATELY weaker than `is_settable`. At publish there
	is no lead — only the workflow's declared grain, which is a RULE grain whose blank axis means ANY.
	Feeding that into `is_settable` (which expects a lead's real DATA grain) would compare a wildcard as
	if it were a value and answer confidently wrong in both directions. So publish asks the only question
	it can honestly answer — "did the operator ever allow automation to set this field?" — and catches the
	misspelling and the forbidden field, which is the whole failure class. Whether THIS lead's grain
	allows the write stays at execution, where `is_settable` has a real lead.

	Same routing rule as `is_settable`: a field belongs to the child table its `CRM Lead Section` names,
	so a parent write (`child_table_field=""`) never matches a child-only field. One brain.
	"""
	if doctype == TASK_DT:
		return _task_writable(fieldname)
	if subjects.is_write_target(doctype):
		return _meta_writable(doctype, fieldname)
	section = _child_section(doctype)
	if section:
		child_table_field = section.child_table_field
	elif doctype != LEAD_DT:
		return False
	for row in frappe.get_all("CRM Lead API Field", filters={"fieldname": fieldname}, fields=["section"]):
		sec = frappe.get_cached_doc("CRM Lead Section", row.section)
		if (sec.child_table_field or "") == (child_table_field or ""):
			return True
	return False


def _child_section(doctype):
	"""The `CRM Lead Section` this doctype IS the child of, or None — asked of the section brain itself."""
	from tatva_connect.partner_api.doctype.crm_lead_section import crm_lead_section

	return None if doctype in (LEAD_DT, TASK_DT) else crm_lead_section.section_for_child(doctype)


def _settable_rows_for(doctype, ticked):
	"""The fields a write node may name on `doctype`, keeping only the field_keys `ticked` accepts:
	the Lead's own (a section with no child table), a Task's (any row across both catalogs), or — when
	the doctype IS a child section's target — that section's rows, under their own column names.

	ONE walk, so the two grain questions below differ in exactly the membership predicate and nowhere
	else. A single function with a wildcard FLAG was the alternative, and a flag that silently changes
	what a blank axis means is precisely how a rule grain gets compared as the empty string."""
	if doctype == TASK_DT:
		from tatva_connect.activity.api import task_columns

		declared = frappe.get_all("CRM Task Type Field", pluck="fieldname", distinct=True)
		return [frappe._dict(fieldname=f) for f in sorted(set(task_columns()) | set(declared))]
	if subjects.is_write_target(doctype):
		return _meta_writable_rows(doctype)
	section = _child_section(doctype)
	if section:
		return [frappe._dict(fieldname=r.fieldname)
		        for r, sec in _lead_rows_in_grain(ticked) if sec.name == section.name]
	if doctype != LEAD_DT:
		return []
	return [frappe._dict(fieldname=r.fieldname) for r, sec in _lead_rows_in_grain(ticked) if not sec.child_table_field]


def _lead_rows_in_grain(ticked):
	"""Every lead catalog row `ticked` accepts, paired with its resolved `CRM Lead Section`.

	THE one walk. Read and write differ in what they keep, never in how a row is found or which grain
	accepts it — a second walk is how the picker and the executor came to disagree before."""
	out = []
	for row in frappe.get_all("CRM Lead API Field", fields=["field_key", "fieldname", "section"]):
		sec = frappe.get_cached_doc("CRM Lead Section", row.section)
		if ticked(row.field_key):
			out.append((row, sec))
	return out


def readable_rows_in_rule_grain(doctype, axes):
	"""What a criterion may TEST at this workflow's grain: the subject's own columns plus its child-section
	columns, each named `<child_table>.<column>` — the dotted path `field_catalog` already offers and
	`refs.parse` already reads as one field.

	Wider than the write list on purpose. A write into a child table goes through Upsert Child Row, which
	addresses the table itself, so `settable_rows_in_rule_grain` stays parent-only; a READ just needs the
	value, and the counters LeadSquared routes on live in a child row.

	A MULTI-ROW section is included and resolves to its latest row (`context.section_values`, via
	`multirow.row_for_section`) — the same row the Data tab and a Smart View show, so a column means the
	same reading wherever it is read.

	A TASK narrows by nothing: its grain IS its type, so every field it carries — `custom_task_type` and
	`status` included — already belongs to the grain that reached it. Narrowing it by the write list left
	the exact per-field gate this work deleted, still standing on the Task side."""
	from tatva_connect.access import entitlement

	grain = _grain_key(axes)
	if doctype == TASK_DT:
		from tatva_connect.automation import describe

		# `fields_for_doctype`, never the typed catalog: its one-level child walk leaks 44 fields the gate lacks.
		return [frappe._dict(fieldname=d["key"]) for d in describe.fields_for_doctype(TASK_DT)]
	if doctype != LEAD_DT:
		return []
	found = _lead_rows_in_grain(lambda key: entitlement.field_in_any_grain_overlapping(key, grain))
	out = []
	for row, sec in found:
		if not sec.child_table_field:
			out.append(frappe._dict(fieldname=row.fieldname))
		else:
			out.append(frappe._dict(fieldname=f"{sec.child_table_field}.{row.fieldname}"))
	return out


def settable_rows(doctype, axes):
	"""Writable fields at a real record's DATA grain — every axis carries a value, blank is literal."""
	from tatva_connect.access import entitlement

	grain = {_grain_key(axes)}
	return _settable_rows_for(doctype, lambda key: entitlement.field_in_grains_via_contract(key, grain))


def settable_rows_in_rule_grain(doctype, axes):
	"""Writable fields a RULE declaring `axes` could EVER reach — blank means ANY. Read AND write ask this
	one, so the criterion picker and the write picker cannot offer different sets.

	The author-time twin of `settable_rows`, and the reason it is a separate name rather than an argument:
	a workflow's declared grain is a rule grain, and the picker that fed it to `settable_rows` offered a
	workflow scoped to a whole vertical only the fields of contracts equally blank — hiding every field a
	more specific contract ticks, though execution would have allowed the write. Membership is decided by
	`entitlement.field_in_any_grain_overlapping`, over the same ticks and the same one matcher module.
	"""
	from tatva_connect.access import entitlement

	grain = _grain_key(axes)
	return _settable_rows_for(doctype, lambda key: entitlement.field_in_any_grain_overlapping(key, grain))
