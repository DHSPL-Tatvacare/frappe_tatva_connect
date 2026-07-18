"""The ONE query brain over the per-resource field catalogs — the automation allowlist (read + write),
folded out of the retired `CRM Automation Field` into the resource brains (one brain per resource):
  • lead fields     → `CRM Lead API Field` (routing DERIVED from its `section` → `CRM Lead Section`;
                      grain from the internal contract `access.entitlement.field_in_grains_via_contract`).
  • task fields     → `CRM Task Field` (NATIVE columns, e.g. status) + `CRM Task Type Field` (per-task-type
                      DECLARED fields) — two sources read as ONE, so a Task lookup sees both.

Three capabilities are three Check flags on each catalog row:
  • can_read  — a rule criterion / Branch condition may test this field (grain-independent).
  • can_watch — a change to this field may fire a rule (Updated). Implies can_read: the dispatcher captures
    a watched field's before/after pair, so a `changed to` rule fires ONLY on the transition — once.
  • can_set   — this field may be written by a Set Field / child-row action (grain-scoped via the contract).

Grain is a SET scope only. Read/watch ignore grain; the lead set reads honour the internal contract — the
SAME brain internal field entitlement uses. Lead child routing is DERIVED from `CRM Lead Section`; a native
Task column's grain derives from the task type — never stored twice (the AST lock forbids hardcoded
child-table names outside the section seed).
"""
import frappe

LEAD_DT = "CRM Lead"
TASK_DT = "CRM Task"


def _catalogs_for(doctype):
	"""The resource catalog(s) holding a subject's automatable fields (one brain per resource). Task has TWO
	sources read as one — its native columns (`CRM Task Field`) + its per-task-type declared fields
	(`CRM Task Type Field`); one resolver, same signatures, no parallel path."""
	if doctype == LEAD_DT:
		return ["CRM Lead API Field"]
	if doctype == TASK_DT:
		return ["CRM Task Field", "CRM Task Type Field"]
	return []


def _grain_key(axes):
	return (axes[0] or "", axes[1] or "", axes[2] or "")


def _union_pluck(doctype, **query):
	out = []
	for catalog in _catalogs_for(doctype):
		out += frappe.get_all(catalog, pluck="fieldname", distinct=True, **query)
	return list(dict.fromkeys(out))


# -- read + watch side (grain-independent) -----------------------------------


def readable_fields(doctype):
	"""The fieldnames a rule criterion may test — the builder's vocabulary and the validator's fence.
	can_watch is folded in here (and nowhere else) because it implies can_read."""
	return _union_pluck(doctype, or_filters={"can_read": 1, "can_watch": 1})


def is_watchable(doctype, fieldname):
	"""True if a can_watch row exists for this field in ANY of the doctype's catalogs — what a transition
	operator (`changed to` / `changed from…to`) needs, since only a watched field carries a before-value."""
	return any(frappe.db.exists(catalog, {"fieldname": fieldname, "can_watch": 1}) for catalog in _catalogs_for(doctype))


def watchable_fields(doctype):
	"""The can_watch fieldnames for a doctype (the dispatch diff cache)."""
	return _union_pluck(doctype, filters={"can_watch": 1})


# -- set side (grain-scoped) -------------------------------------------------


def is_settable(doctype, fieldname, axes, child_table_field="", require_row_key=False):
	"""Runtime/author write-gate. Lead: a can_set catalog row whose section routing matches the child
	context and whose field_key is ticked by the lead's grain contract. Task: a can_set row in either Task
	catalog (a native Task column's grain derives from the task type, not from these axes). Fail-closed."""
	from tatva_connect.access import entitlement

	if doctype == TASK_DT:
		return any(frappe.db.exists(c, {"fieldname": fieldname, "can_set": 1}) for c in _catalogs_for(TASK_DT))
	if doctype != LEAD_DT:
		return False
	grain = {_grain_key(axes)}
	for row in frappe.get_all(
		"CRM Lead API Field", filters={"fieldname": fieldname, "can_set": 1}, fields=["field_key", "fieldname", "section"]
	):
		sec = frappe.get_cached_doc("CRM Lead Section", row.section)
		if (sec.child_table_field or "") != (child_table_field or ""):
			continue
		if require_row_key and row.fieldname != (sec.row_key_field or ""):
			continue
		if entitlement.field_in_grains_via_contract(row.field_key, grain):
			return True
	return False


def settable_rows(doctype, axes):
	"""can_set PARENT fields (Lead: whose section has no child table, ticked by the grain contract; Task:
	any can_set row across both catalogs) — the rows the describe endpoint enriches for the Set Field
	target dropdown."""
	from tatva_connect.access import entitlement

	if doctype == TASK_DT:
		return [frappe._dict(fieldname=f) for f in _union_pluck(TASK_DT, filters={"can_set": 1})]
	if doctype != LEAD_DT:
		return []
	grain = {_grain_key(axes)}
	out = []
	for row in frappe.get_all("CRM Lead API Field", filters={"can_set": 1}, fields=["field_key", "fieldname", "section"]):
		sec = frappe.get_cached_doc("CRM Lead Section", row.section)
		if sec.child_table_field:
			continue
		if entitlement.field_in_grains_via_contract(row.field_key, grain):
			out.append(frappe._dict(fieldname=row.fieldname))
	return out
