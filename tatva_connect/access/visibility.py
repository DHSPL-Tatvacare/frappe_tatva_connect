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
from frappe.query_builder import DocType
from pypika.terms import PseudoColumn

from tatva_connect import automation
from tatva_connect.access import request_cache

PARENT_DOCTYPES = ("CRM Lead", "CRM Deal")


def _is_privileged(user):
	return user == "Administrator" or "System Manager" in frappe.get_roles(user)


def _ref_parent(doc):
	"""reference_doctype/reference_docname pointing at a Lead/Deal (CRM Task, and the
	primary linkage on CRM Call Log)."""
	if doc.get("reference_doctype") in PARENT_DOCTYPES and doc.get("reference_docname"):
		return doc.get("reference_doctype"), doc.get("reference_docname")
	return None


def _ref_name_parent(doc):
	"""WhatsApp Message links its parent via reference_doctype + `reference_name` (NOT the
	`reference_docname` field name CRM Task/FCRM Note use). frappe_whatsapp's own doctype: the
	dynamic-link target is `reference_name`."""
	if doc.get("reference_doctype") in PARENT_DOCTYPES and doc.get("reference_name"):
		return doc.get("reference_doctype"), doc.get("reference_name")
	return None


def _subject_parent(doc):
	"""The workflow tables name their parent `subject_doctype`/`subject_name` — a Run is about one
	record and an Event is addressed to one."""
	if doc.get("subject_doctype") in PARENT_DOCTYPES and doc.get("subject_name"):
		return doc.get("subject_doctype"), doc.get("subject_name")
	return None


def _run_subject_parent(doc):
	"""A step log names no subject doctype at all — only `workflow_run` and a bare `subject_name`.
	So its parent is its run's parent, read from the run. Indirect linkage is exactly why PARENT_OF
	is a resolver rather than a field pair."""
	run = doc.get("workflow_run")
	if not run:
		return None
	subject = frappe.db.get_value("CRM Workflow Run", run, ["subject_doctype", "subject_name"], as_dict=True)
	return _subject_parent(subject) if subject else None


# child doctype -> resolver(doc) -> (parent_doctype, parent_name) | None
PARENT_OF = {
	"CRM Task": _ref_parent,
	"CRM Call Log": _ref_parent,
	"FCRM Note": _ref_parent,
	"WhatsApp Message": _ref_name_parent,
	"CRM Workflow Run": _subject_parent,
	"CRM Workflow Event": _subject_parent,
	"CRM Workflow Step Log": _run_subject_parent,
}

# child doctype -> (own link column, parent doctype) for a row that reaches its Lead/Deal INDIRECTLY.
# A step log has no `subject_doctype` column, so the direct clause below cannot be written for it; it
# is visible exactly when its run is, and `scoped_pqc` recurses to say so once rather than twice.
_VIA = {
	"CRM Workflow Step Log": ("workflow_run", "CRM Workflow Run"),
}

# child doctype -> its operator switch (control plane). OFF -> stock crm (no scoping).
_SWITCH_OF = {
	"CRM Task": "Task::CRM Task::visibility",
	"CRM Call Log": "Telephony::CRM Call Log::visibility",
	"FCRM Note": "Note::FCRM Note::visibility",
	# A run carries its lead's field values in `state_json`, and a step log carries them in `detail`.
	# Unscoped, any manager could read every other grain's leads through the workflow lists.
	"CRM Workflow Run": "Workflow::CRM Workflow Run::visibility",
	"CRM Workflow Event": "Workflow::CRM Workflow Event::visibility",
	"CRM Workflow Step Log": "Workflow::CRM Workflow Step Log::visibility",
	"WhatsApp Message": "WhatsApp::WhatsApp Message::visibility",
}


def _owns(doc, user):
	return doc.owner == user or doc.get("assigned_to") == user


def match_conditions(doctype, user=None):
	"""THE row gate: the SQL core applies to this doctype's own list for this user. "" = unrestricted.

	`build_match_conditions` is the whole of it — User Permissions, the owner constraint, shares — and it
	calls the app's `permission_query_conditions` hooks on the way. Asking `get_permission_query_conditions`
	alone returns only the hook half, which is how Smart Views once handed a user scoped to one vertical
	every lead in the system. One question, one API, one place. Request-cached per (doctype, user)."""
	user = user or frappe.session.user

	def build():
		try:
			return DatabaseQuery(doctype, user=user).build_match_conditions(as_condition=True) or ""
		except frappe.PermissionError:
			return "1=0"  # no read access at all -> select nothing

	return request_cache("tatva_connect:match_conditions", (doctype, user), build)


def readable_criterion(doctype, table, user=None):
	"""`table.name IN (the rows this user may read)` — the row gate as a qb criterion, or None when
	unrestricted.

	Every query this app builds ITSELF must AND this in. Frappe applies the gate inside its own list
	layer, so anything assembled in `frappe.qb` bypasses it entirely — that is not an oversight to guard
	against, it is the contract of building your own query.

	Scoped through a subquery rather than ANDed raw: the conditions name the doctype's own columns
	UNQUALIFIED, so the moment a query joins a child table those bare `name`/`owner` collide (MySQL 1052).
	Inside a single-table subquery they resolve cleanly, and the meaning is identical — the conditions only
	ever constrain that doctype's own rows."""
	cond = match_conditions(doctype, user)
	if not cond:
		return None
	src = DocType(doctype)
	sub = frappe.qb.from_(src).select(src.name).where(PseudoColumn(f"({cond})"))  # sqli-ok: framework-built conditions, no user value
	return table.name.isin(sub)


def scope(query, doctype, table, user=None):
	"""AND the row gate onto a query this app built itself. The applicator; `readable_criterion` is the rule.

	Every hand-built `frappe.qb` query over a scoped doctype goes through here. Frappe gates its own list
	layer, so a query assembled by hand is ungated by definition — the dashboard charts counted every lead
	and every task in the system for whoever asked, because nothing had ever ANDed this in."""
	criterion = readable_criterion(doctype, table, user)
	return query if criterion is None else query.where(criterion)


def _visible_parent_subquery(parent, user):
	"""The parent rows this user may read, as a SQL '(select name ...)', for the child-doctype hooks.
	Same gate as `match_conditions`, shaped as a string because a PQC hook returns SQL, not a criterion."""
	cond = match_conditions(parent, user)
	where = f" and ({cond})" if cond else ""
	return f"(select `name` from `tab{parent}` where 1=1{where})"


def _link_columns(doctype):
	"""The (doctype, name) column pair naming this row's parent. Read from the REAL schema, never
	assumed: CRM Task and friends use `reference_doctype`/`reference_docname`, WhatsApp Message uses
	`reference_name`, and CRM Workflow Run/Event use `subject_doctype`/`subject_name`. One resolver, so
	a new consumer is scoped by declaring nothing. The probe is the DOCTYPE column of each pair, because
	that is the one that is missing when a doctype only looks like it carries the pair — CRM Workflow
	Step Log has `subject_name` and no `subject_doctype`, and probing the name column emitted a clause
	on a column that does not exist. Such a doctype belongs in `_VIA`, not here."""
	if frappe.db.has_column(doctype, "subject_doctype"):
		return "subject_doctype", "subject_name"
	if frappe.db.has_column(doctype, "reference_docname"):
		return "reference_doctype", "reference_docname"
	return "reference_doctype", "reference_name"


def scoped_pqc(doctype, user=None):
	"""List scoping for `doctype`. A row is visible iff it is the user's own/assigned, OR its
	parent Lead/Deal is visible (reusing the parent's own list conditions). Privileged or
	switch-OFF or an unregistered doctype -> "" (no extra conditions; stock crm)."""
	switch = _SWITCH_OF.get(doctype)
	if not switch or not automation.is_enabled(switch):
		return ""
	user = user or frappe.session.user
	if _is_privileged(user):
		return ""
	return _row_clause(doctype, user)


def _row_clause(doctype, user):
	"""The scoping SQL for one doctype, switch and privilege already decided. Split out so `_VIA` can
	recurse into its parent's clause: the parent's OWN switch is not consulted there, because the
	question being answered is the CHILD's switch — a step log scoped while runs are not is a narrower
	answer, never a wider one."""
	via = _VIA.get(doctype)
	if via:
		column, parent = via
		u = frappe.db.escape(user)
		inner = _row_clause(parent, user)
		return (
			f"(`tab{doctype}`.`owner`={u} or `tab{doctype}`.`{column}` in "
			f"(select `name` from `tab{parent}` where {inner}))"
		)
	tbl = f"`tab{doctype}`"
	u = frappe.db.escape(user)
	clauses = [f"{tbl}.`owner`={u}"]
	# `assigned_to` is a CRM Task field; Call Log/Note/WhatsApp have no such column (owner is
	# the only self-ownership marker there). Only emit the clause where the column exists.
	if frappe.db.has_column(doctype, "assigned_to"):
		clauses.append(f"{tbl}.`assigned_to`={u}")
	# The dynamic-link docname column differs per doctype: CRM Task/Call Log/FCRM Note use
	# `reference_docname`; WhatsApp Message uses `reference_name`. Pick the one that exists.
	ref_type, ref_name = _link_columns(doctype)
	for parent in PARENT_DOCTYPES:
		clauses.append(
			f"({tbl}.`{ref_type}`={frappe.db.escape(parent)} "
			f"and {tbl}.`{ref_name}` in {_visible_parent_subquery(parent, user)})"
		)
	return "(" + " or ".join(clauses) + ")"


def scoped_has_permission(doc, ptype, user):
	"""Single-doc / deep-link gate (the PQC governs lists only). Mirror the list rule so a
	child on a hidden parent can't be opened by name. Privileged or switch-OFF -> True.
	Owner/assignee -> True. Else resolve the parent and defer to its READ scope; no resolvable
	parent -> deny (fail-closed for a privacy CRM)."""
	doctype = doc.doctype
	switch = _SWITCH_OF.get(doctype)
	if not switch or not automation.is_enabled(switch):
		return True
	user = user or frappe.session.user
	if _is_privileged(user):
		return True
	if _owns(doc, user):
		return True
	parent = PARENT_OF[doctype](doc)
	# standalone, orphaned (parent deleted), or unrecognised parent: not owner/assignee
	# (returned above) -> deny, matching the list PQC.
	return parent_readable(parent[0], parent[1], user) if parent else False


def parent_readable(parent_doctype, parent_name, user=None):
	"""May `user` READ this parent Lead/Deal? The rule every child defers to, written once.

	`scoped_has_permission` asks it after resolving a child's parent from the child row. A read
	surface that already KNOWS the parent — the run-history endpoints, whose whole argument is one
	lead — asks it directly instead of synthesising a child doc to be resolved back again.

	Missing and unreadable both answer False, so a caller cannot tell a record that is not there from
	one that is not theirs. Privilege is answered by `frappe.has_permission` itself; there is
	deliberately no second privilege check here.
	"""
	if not parent_doctype or not parent_name:
		return False
	if not frappe.db.exists(parent_doctype, parent_name):
		return False
	return bool(frappe.has_permission(parent_doctype, "read", parent_name, user=user))
