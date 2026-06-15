"""CRM Task visibility: mirror each task's parent Lead/Deal permissions onto the task
list and single-doc reads. crm scopes Leads/Deals but never Tasks, so an agent otherwise
sees every task; this restores parity — a task is visible iff its parent Lead/Deal is, or
it is the user's own / assigned task. Native permission hooks — no fork, no re-derivation."""
import frappe
from frappe.model.db_query import DatabaseQuery

PARENT_DOCTYPES = ("CRM Lead", "CRM Deal")


def _is_privileged(user):
	return user == "Administrator" or "System Manager" in frappe.get_roles(user)


def get_task_permission_query_conditions(user=None):
	"""List scoping. A task is visible iff it is the user's own/assigned, OR its parent
	Lead/Deal is visible to the user (reusing the parent's own list conditions)."""
	user = user or frappe.session.user
	if _is_privileged(user):
		return ""
	tbl = "`tabCRM Task`"
	u = frappe.db.escape(user)
	clauses = [f"{tbl}.`owner`={u}", f"{tbl}.`assigned_to`={u}"]
	for parent in PARENT_DOCTYPES:
		clauses.append(
			f"({tbl}.`reference_doctype`={frappe.db.escape(parent)} "
			f"and {tbl}.`reference_docname` in {_visible_parent_subquery(parent, user)})"
		)
	return "(" + " or ".join(clauses) + ")"


def has_task_permission(doc, ptype, user):
	"""Single-doc / deep-link read gate (the PQC governs lists only). Mirror the same rule
	so a task on a hidden parent can't be opened by name."""
	user = user or frappe.session.user
	if _is_privileged(user):
		return True
	if doc.owner == user or doc.assigned_to == user:
		return True
	if (
		doc.reference_docname
		and doc.reference_doctype in PARENT_DOCTYPES
		and frappe.db.exists(doc.reference_doctype, doc.reference_docname)
	):
		return frappe.has_permission(doc.reference_doctype, "read", doc.reference_docname, user=user)
	# standalone, orphaned (parent deleted), or unrecognised parent: not owner/assignee (returned
	# above) -> deny, matching the list PQC, which admits such tasks to owner/assignee only.
	# Fail-closed for a privacy CRM.
	return False


def _visible_parent_subquery(parent, user):
	"""The parent rows this user may read, as a SQL '(select name ...)'. Reuses the EXACT
	conditions core applies to the parent's own list (crm PQC + user-permission layers +
	shares) so task visibility can never drift from parent visibility (one brain). No read
	access at all -> match nothing. 'parent' is a trusted constant, never user input."""
	try:
		cond = DatabaseQuery(parent, user=user).build_match_conditions(as_condition=True)
	except frappe.PermissionError:
		cond = "1=0"
	where = f" and ({cond})" if cond else ""
	return f"(select `name` from `tab{parent}` where 1=1{where})"
