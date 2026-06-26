# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The oracle — Frappe's own permission engine is the ground truth, never a hardcoded expectation.

A grain-gated path is safe iff it never returns MORE than native Frappe would allow. The oracle
asks Frappe that question. CRITICAL nuances proven by the 2026-06-26 audit — pick the RIGHT oracle
per surface or you get a false negative:

  * ROW visibility (lists, and single-doc on the 4 PQC-scoped child doctypes Task/Note/Call/WA):
    use native_visible_names / native_can_read_row. `frappe.has_permission` is BLIND to
    permission_query_conditions (only db_query runs them), so it cannot judge row scope on those.
  * FIELD reads (the permlevel-1 grain fields custom_vertical/group/program, and any Smart-View /
    lead-detail render): use native_permitted_fields. `has_permission` only reads permlevel-0
    perms, so it is BLIND to permlevel-1 field access — exactly the grain product. Never judge a
    field leak with has_permission.
  * ACTION capability on a specific row (write/delete/create with a doc): native_would_allow — doc
    is MANDATORY (a doc=None check silently becomes a doctype-capability check and masks row-level
    escalation).
  * DOCTYPE capability (deny-sweeps only, where DENY is the strongest verdict): native_doctype_
    capability — the only sanctioned doc=None use.

We never use frappe.get_all for an authz assertion — it BYPASSES the permission layer entirely.
Frappe APIs cited: frappe.has_permission, frappe.model.get_permitted_fields (permlevel-aware),
frappe.get_list (runs PQC + user permissions).
"""
import frappe
from frappe.model import get_permitted_fields
from frappe.tests import set_user


def native_doctype_capability(user, doctype, ptype):
	"""Doctype-level capability (doc=None). SANCTIONED ONLY for deny-sweeps, where a doctype-level
	DENY is the strongest possible verdict (no false negative). Never use for an ALLOW assertion."""
	return bool(frappe.has_permission(doctype, ptype, user=user))


def native_would_allow(user, doctype, ptype, doc):
	"""Native verdict for `ptype` on a SPECIFIC `doc` (mandatory) for `user` — the row-level action
	ceiling for write/create/delete. NOTE: has_permission does not run PQC, so for READ on the four
	PQC-scoped child doctypes use native_can_read_row instead."""
	if doc is None:
		raise ValueError("native_would_allow requires a concrete doc; use native_doctype_capability for doc=None")
	return bool(frappe.has_permission(doctype, ptype, doc=doc, user=user))


def native_visible_names(user, doctype, filters=None):
	"""The set of record names `user` may natively READ — get_list honours PQC + user permissions.
	Self-contained: impersonates `user`. A no-read principal (e.g. Guest) yields ∅, not an error."""
	with set_user(user):
		try:
			return set(
				frappe.get_list(
					doctype, filters=filters or {}, pluck="name",
					limit_page_length=0, ignore_permissions=False,
				)
			)
		except frappe.PermissionError:
			return set()


def native_can_read_row(user, doctype, name):
	"""PQC-honouring single-doc READ oracle: True iff `name` is in the user's get_list result.
	This is the correct single-doc ceiling for the PQC-scoped child doctypes (Task/Note/Call/WA),
	where has_permission alone is blind to the permission_query_conditions scoping."""
	return name in native_visible_names(user, doctype, filters={"name": name})


def native_permitted_fields(user, doctype):
	"""The set of fieldnames `user` may natively READ — permlevel-aware (get_permitted_fields honours
	meta.get_permlevel_access). This is the ONLY sound oracle for field/permlevel leaks: a render
	surface must expose only fields in this set. permission_type pinned to 'read' so a select-only
	persona doesn't get a wider ceiling."""
	return set(get_permitted_fields(doctype, user=user, permission_type="read"))


def assert_no_escalation(test, user, doctype, ptype, doc, actually_allowed):
	"""Fail `test` when a path allowed an ACTION on `doc` that native would deny — escalation signal.
	For READ-row and FIELD cases use native_can_read_row / native_permitted_fields, not this."""
	if actually_allowed and not native_would_allow(user, doctype, ptype, doc):
		test.fail(
			"ESCALATION: {0} could {1} {2}/{3} via a grain/bypass path, but the native "
			"permission stack denies it".format(user, ptype, doctype, getattr(doc, "name", doc))
		)
