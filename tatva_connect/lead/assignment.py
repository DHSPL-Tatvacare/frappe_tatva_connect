# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Assignment off a NAMED FIELD — the one behaviour in the fork that answered to nothing.

Two different things assign in this product and they are not the same mechanism:

  * frappe's **Assignment Rule** decides WHO to assign (round robin, least-loaded) when nobody is named.
    It is frappe's own code, reached through `doc_events["*"]`, and it stands down when an assignment
    already exists.
  * the fork's **assign_agent / assign_to** mirror a name ALREADY on the record — `CRM Lead.lead_owner`,
    `CRM Task.assigned_to` — into a real assignment, so the record lands in that person's list.

Only the second is ours to govern, and until now it was governed by nothing: no switch, no flag, no
operator row. That is what produced 496 unwanted ToDo rows on a 250-lead load with every switch off, and
what a 5,000-row import would multiply.

A LEAD is gated: its mixin asks the registry and then delegates, so with the switch on the behaviour is
byte-identical to the fork's. A TASK is held by its `assigned_to` column alone — every surface that asks
who holds a task reads that column — so the fork's mirroring ToDo is not written.

The workflow engine assigns through frappe's own `ignore_permissions`, exactly as frappe's Assignment Rule
does: the session it runs under may be Guest (an intake form) and must never be switched.

The bulk lane comes free: `is_enabled` answers differently inside a bulk job (automation/settings.py), so
an import goes quiet without this module knowing anything about imports.

Mixed in ahead of the native class in `list_engine/columns.py`, which owns the `override_doctype_class`
entry for both doctypes. The logic lives here because that file declares listing columns and nothing else.
"""
import frappe
from frappe import _
from frappe.utils import nowdate

from tatva_connect.automation import settings as automation

LEAD_OWNER = "Lead::Assignment::owner"


def current_assignees(doctype, name):
	"""Who holds this record now: every Open ToDo on it."""
	return [row.owner for row in _held(doctype, name)]


def _held(doctype, name):
	"""Every Open ToDo on the record, uncapped: frappe's `assign_to.get` stops at five holders."""
	return frappe.get_all(
		"ToDo", filters={"reference_type": doctype, "reference_name": name, "status": "Open"},
		fields=["name", "allocated_to as owner"],
	)


def assert_entitled(user, axes):
	"""Refuse a workflow assignment to someone the record's data grain does not entitle; a record with no axes has none to enforce."""
	from tatva_connect.access import entitlement

	grain = tuple((a or "") for a in (axes or ("", "", "")))
	if user and any(grain) and not entitlement.grain_entitled(grain, user=user):
		raise PermissionError(
			f"{user} is not entitled to {'/'.join(a or '*' for a in grain)} — a workflow may not assign a "
			"record to someone who may not see it"
		)


def assign_for_workflow(doctype, name, user, axes, replace=False, note=None):
	"""A workflow gives a record to one person: entitled to its grain, then through the one `assign` without the caller's permission."""
	assert_entitled(user, axes)
	assign(doctype, name, user, replace=replace, note=note or _("Assigned by a workflow"), ignore_permissions=True)


def draw_from_pool(rule_name, doctype, name, axes):
	"""A workflow gives a record to the next person in an Assignment Rule: frappe's own `do_assignment`, which assigns without the caller's permission, its pick checked."""
	if not rule_name:
		return None
	rule = frappe.get_cached_doc("Assignment Rule", rule_name)
	if rule.is_rule_not_applicable_today():
		return None
	# `as_dict()` is what frappe hands its own rules, and `do_assignment` renders the rule's description against it.
	assigned = rule.do_assignment(frappe.get_doc(doctype, name).as_dict())
	user = next(iter(current_assignees(doctype, name)), None) if assigned else None
	assert_entitled(user, axes)
	return user


def assign(doctype, name, user, replace=False, notify=True, note=None, ignore_permissions=False):
	"""Give a record to `user`, beside its holders or in their place; `notify=False` writes the same ToDo without the alert, share and follow frappe's `assign_to` cannot switch off, and `ignore_permissions` is frappe's own."""
	from frappe.desk.form import assign_to

	held = _held(doctype, name)
	for row in held if replace else ():
		if row.owner != user:
			_release(doctype, name, row, notify, ignore_permissions)
	if any(row.owner == user for row in held):
		if replace and not notify:
			_set_assigned_to(doctype, name, user)
		return
	if notify:
		assign_to._add({"doctype": doctype, "name": name, "assign_to": [user], "description": note}, ignore_permissions=ignore_permissions)
		return
	# Not gated here: the door that takes a caller's docnames checks it, as `assign_to._add` checks and its ToDo insert does not.
	frappe.get_doc({
		"doctype": "ToDo", "allocated_to": user, "reference_type": doctype, "reference_name": name,
		"description": note or _("Assignment for {0} {1}").format(doctype, name),
		"status": "Open", "date": nowdate(), "assigned_by": frappe.session.user,
	}).insert(ignore_permissions=True)
	_set_assigned_to(doctype, name, user)


def unassign(doctype, name, user, notify=True):
	"""Take a record off `user`; `notify=False` cancels without frappe's alert."""
	for row in _held(doctype, name):
		if row.owner == user:
			_release(doctype, name, row, notify)
	if not notify and frappe.get_meta(doctype).get_field("assigned_to") and frappe.db.get_value(doctype, name, "assigned_to") == user:
		frappe.db.set_value(doctype, name, "assigned_to", None, update_modified=False)


def _release(doctype, name, row, notify, ignore_permissions=False):
	"""One holder off the record, Cancelled and never Closed: frappe's `remove`, or the same ToDo save minus its `notify_assignment` line."""
	from frappe.desk.form import assign_to

	if notify:
		assign_to._remove(doctype, name, row.owner, ignore_permissions=ignore_permissions)
		return
	todo = frappe.get_doc("ToDo", row.name)
	todo.status = "Cancelled"
	todo.save(ignore_permissions=True)


def _set_assigned_to(doctype, name, user):
	if frappe.get_meta(doctype).get_field("assigned_to"):
		frappe.db.set_value(doctype, name, "assigned_to", user, update_modified=False)


class LeadAssignmentGate:
	"""`CRM Lead.lead_owner` -> a real assignment, gated. Mix in BEFORE `CRMLead`."""

	def assign_agent(self, agent):
		if not automation.is_enabled(LEAD_OWNER):
			return
		super().assign_agent(agent)

	def share_with_agent(self, agent):
		# A DocShare per record; visibility reads `lead_owner` directly (internal_contract_seed), never the share.
		if not automation.is_enabled(LEAD_OWNER):
			return
		super().share_with_agent(agent)


class TaskHeldByColumn:
	"""`CRM Task.assigned_to` is who holds a task, and the fork's mirroring ToDo is not written. Mix in BEFORE `CRMTask`.

	`unassign_from_previous_user` stays stock: it cancels a ToDo an earlier writer left on the task."""

	def assign_to(self):
		return


# Frappe's Assignment Rule records its pick as a ToDo and never touches `lead_owner`; a form-born lead
# therefore lands ownerless while every reporting surface in this product reads that column.
OWNER_FROM_RULE = "Lead::Assignment::from_rule"


def on_assignment_set_owner(doc, method=None):
	"""ToDo.after_insert — stamp `lead_owner` with the assignee, but ONLY when nobody owns the lead yet.

	THE GAP THIS CLOSES. A lead has three births: the Create Lead modal names the creating rep
	(LeadModal.vue), the LSQ load names the migrated owner (mapping.json `OwnerIdEmailAddress`), and a
	public enrolment form names nobody. Only the third arrives ownerless, and it is exactly the traffic
	the Anaya Assignment Rules serve. Visibility, notifications and the read grant all answer off the
	ToDo and are unaffected — but `Leads by Owner`, `SLA Misses by Owner`, the Lead Owner list column and
	the spotlight owner facet all name the COLUMN, so an ownerless lead reads blank on four surfaces.

	WHO ASSIGNED DECIDES WHETHER IT OVERWRITES. A round-robin pick must never displace an owner somebody
	chose — a rep's own lead, a manager's hand-off, the migrated LSQ owner — so a rule only FILLS an empty
	column. A person assigning is the opposite case: handing a lead to someone IS the decision, and a
	sales desk expects the owner to move with it. The two are told apart by the SESSION, not by
	`ToDo.assignment_rule`: a rule stamps that column, but the workflow engine's `Assign to User` node
	does not, so reading it would let a Flow displace a manager's decision while a round robin correctly
	could not. Everything automated runs as Administrator; a rep clicking Assign runs as themselves.

	`db.set_value` because it writes the column and fires NO document events: crm assigns off
	`lead_owner` changing (crm_lead.py:86-96), so a `save()` here would raise a second ToDo and re-enter
	this handler. The write is deliberately invisible to the document layer for that reason.
	"""
	if doc.reference_type != "CRM Lead" or not doc.allocated_to:
		return
	if not automation.is_enabled(OWNER_FROM_RULE):
		return
	owner = frappe.db.get_value("CRM Lead", doc.reference_name, "lead_owner")
	if owner == doc.allocated_to:
		return
	# NOTHING AUTOMATED DISPLACES AN OWNER SOMEBODY CHOSE; a person assigning IS the choice, and a sales
	# desk means exactly that by "assign" — the lead moves to whoever it was handed to. The test is the
	# SESSION, not `assignment_rule`: a rule stamps that column, but the workflow engine's `Assign to
	# User` node calls `assign_to._add` with no rule name (automation/actions.py), so reading the column
	# would let a Flow overwrite a manager's decision while a round robin correctly could not. TWO tests,
	# because neither alone is complete: a wait-free Flow runs INLINE in the acting rep's session, so the
	# session says "human" while the engine is the one assigning — `frappe.flags.in_workflow` (set around
	# every effect the engine runs, triggers.py:207/361) is what names that case, and it is frappe's own
	# convention for it. The session test then covers what carries no flag: the scheduler, a background
	# job and the migration, all of which run as Administrator.
	if owner and (frappe.flags.get("in_workflow") or frappe.session.user in ("Administrator", "Guest")):
		return
	frappe.db.set_value(
		"CRM Lead", doc.reference_name, "lead_owner", doc.allocated_to, update_modified=False
	)
