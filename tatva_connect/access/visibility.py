"""Row visibility — the ONE brain for "which RECORDS may an internal user see".

Sibling of `entitlement.py` (which fields). crm scopes Leads/Deals through the org
hierarchy but never their child records (Tasks, Call Logs, …), so an agent otherwise
sees every child. This restores parity: a child is visible iff its parent Lead/Deal is,
or it is the user's own / assigned record. Native permission hooks — no fork, no
re-derivation of crm's scope.

Two functions, driven by one structural registry:
  * `scoped_pqc(doctype, user)` — the `permission_query_conditions` list filter.
  * `scoped_has_permission(doc, ptype, user)` — the `has_permission` single-doc gate.

The ptype contract: visibility ALWAYS checks the parent's **read** scope. The child's
own action level (read/write/delete) is its role docperm — so write needs role-write on
the child AND read-visibility on the parent. No per-ptype branching here.

`PARENT_OF` maps each child doctype to a RESOLVER `(doc) -> (parent_doctype, parent_name)
| None` — linkage differs per doctype (Task uses reference_doctype/docname; Call Log uses
those AND/OR a `links` child table), so it's a resolver, not a field pair. Read from the
REAL doctype, never assumed. `_SWITCH_OF` maps each to its operator toggle — OFF -> stock
crm (no scoping). Fail-closed: an unrecognised/missing parent for a non-owner -> deny.
"""
import frappe
from frappe.model.db_query import DatabaseQuery

from tatva_connect import automation

PARENT_DOCTYPES = ("CRM Lead", "CRM Deal")


def _is_privileged(user):
	return user == "Administrator" or "System Manager" in frappe.get_roles(user)


def _ref_parent(doc):
	"""reference_doctype/reference_docname pointing at a Lead/Deal (CRM Task, and the
	primary linkage on CRM Call Log)."""
	if doc.get("reference_doctype") in PARENT_DOCTYPES and doc.get("reference_docname"):
		return doc.get("reference_doctype"), doc.get("reference_docname")
	return None


def _ref_or_links_parent(doc):
	"""CRM Call Log carries BOTH a reference_* pair AND a `links` child table; crm's own
	get_call_log reads the Lead/Deal off either. Prefer reference_*, else the first Lead/Deal
	row in `links` — exactly crm's own resolution order. (Real call logs always set reference_*,
	so the single-doc gate here and the reference-only list PQC agree in practice; the links
	fallback is intentional defence for a theoretical links-only row — single-doc still scopes
	it correctly, the list just won't surface it to a non-owner. Benign: never leaks.)"""
	parent = _ref_parent(doc)
	if parent:
		return parent
	for link in doc.get("links") or []:
		if link.get("link_doctype") in PARENT_DOCTYPES and link.get("link_name"):
			return link.get("link_doctype"), link.get("link_name")
	return None


def _ref_name_parent(doc):
	"""WhatsApp Message links its parent via reference_doctype + `reference_name` (NOT the
	`reference_docname` field name CRM Task/FCRM Note use). frappe_whatsapp's own doctype: the
	dynamic-link target is `reference_name`."""
	if doc.get("reference_doctype") in PARENT_DOCTYPES and doc.get("reference_name"):
		return doc.get("reference_doctype"), doc.get("reference_name")
	return None


# child doctype -> resolver(doc) -> (parent_doctype, parent_name) | None
PARENT_OF = {
	"CRM Task": _ref_parent,
	"CRM Call Log": _ref_or_links_parent,
	"FCRM Note": _ref_parent,
	"WhatsApp Message": _ref_name_parent,
}

# child doctype -> its operator switch (control plane). OFF -> stock crm (no scoping).
_SWITCH_OF = {
	"CRM Task": "Task::CRM Task::visibility",
	"CRM Call Log": "Telephony::CRM Call Log::visibility",
	"FCRM Note": "Note::FCRM Note::visibility",
	"WhatsApp Message": "WhatsApp::WhatsApp Message::visibility",
}


def _owns(doc, user):
	return doc.owner == user or doc.get("assigned_to") == user


def _visible_parent_subquery(parent, user):
	"""The parent rows this user may read, as a SQL '(select name ...)'. Reuses the EXACT
	conditions core applies to the parent's own list (crm PQC + user-permission layers +
	shares) so child visibility can never drift from parent visibility (one brain). No read
	access at all -> match nothing. 'parent' is a trusted constant, never user input. Cached
	per (parent, user) for the request — the same subquery is built for every child doctype."""

	def build():
		try:
			cond = DatabaseQuery(parent, user=user).build_match_conditions(as_condition=True)
		except frappe.PermissionError:
			cond = "1=0"
		where = f" and ({cond})" if cond else ""
		return f"(select `name` from `tab{parent}` where 1=1{where})"

	return _request_cache("tatva_connect:visible_parents", (parent, user), build)


def _request_cache(bucket, key, builder):
	"""Per-request memoisation on frappe.local (cleared each request). Matches entitlement.py."""
	store = getattr(frappe.local, bucket, None)
	if store is None:
		store = {}
		setattr(frappe.local, bucket, store)
	if key not in store:
		store[key] = builder()
	return store[key]


def scoped_pqc(doctype, user=None):
	"""List scoping for `doctype`. A row is visible iff it is the user's own/assigned, OR its
	parent Lead/Deal is visible (reusing the parent's own list conditions). Privileged or
	switch-OFF -> "" (no extra conditions; stock crm)."""
	if not automation.is_enabled(_SWITCH_OF[doctype]):
		return ""
	user = user or frappe.session.user
	if _is_privileged(user):
		return ""
	tbl = f"`tab{doctype}`"
	u = frappe.db.escape(user)
	clauses = [f"{tbl}.`owner`={u}"]
	# `assigned_to` is a CRM Task field; Call Log/Note/WhatsApp have no such column (owner is
	# the only self-ownership marker there). Only emit the clause where the column exists.
	if frappe.db.has_column(doctype, "assigned_to"):
		clauses.append(f"{tbl}.`assigned_to`={u}")
	# The dynamic-link docname column differs per doctype: CRM Task/Call Log/FCRM Note use
	# `reference_docname`; WhatsApp Message uses `reference_name`. Pick the one that exists.
	ref_name = "reference_docname" if frappe.db.has_column(doctype, "reference_docname") else "reference_name"
	for parent in PARENT_DOCTYPES:
		clauses.append(
			f"({tbl}.`reference_doctype`={frappe.db.escape(parent)} "
			f"and {tbl}.`{ref_name}` in {_visible_parent_subquery(parent, user)})"
		)
	return "(" + " or ".join(clauses) + ")"


def scoped_has_permission(doc, ptype, user):
	"""Single-doc / deep-link gate (the PQC governs lists only). Mirror the list rule so a
	child on a hidden parent can't be opened by name. Privileged or switch-OFF -> True.
	Owner/assignee -> True. Else resolve the parent and defer to its READ scope; no resolvable
	parent -> deny (fail-closed for a privacy CRM)."""
	doctype = doc.doctype
	if not automation.is_enabled(_SWITCH_OF[doctype]):
		return True
	user = user or frappe.session.user
	if _is_privileged(user):
		return True
	if _owns(doc, user):
		return True
	parent = PARENT_OF[doctype](doc)
	if parent and frappe.db.exists(parent[0], parent[1]):
		return frappe.has_permission(parent[0], "read", parent[1], user=user)
	# standalone, orphaned (parent deleted), or unrecognised parent: not owner/assignee
	# (returned above) -> deny, matching the list PQC.
	return False
