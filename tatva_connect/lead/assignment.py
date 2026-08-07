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

GATE, NEVER NO-OP. A no-op (what the migration harness does to `CRMTask.assign_to`) is right for a
one-off script and wrong here — it would kill the Assign button for a rep. These mixins ask the registry
and then delegate, so with the switch on the behaviour is byte-identical to the fork's.

The bulk lane comes free: `is_enabled` answers differently inside a bulk job (automation/settings.py), so
an import goes quiet without this module knowing anything about imports.

Mixed in ahead of the native class in `list_engine/columns.py`, which owns the `override_doctype_class`
entry for both doctypes. The logic lives here because that file declares listing columns and nothing else.
"""
from tatva_connect.automation import settings as automation

LEAD_OWNER = "Lead::Assignment::owner"
TASK_ASSIGNEE = "Task::Assignment::assignee"


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


class TaskAssignmentGate:
	"""`CRM Task.assigned_to` -> a real assignment, gated. Mix in BEFORE `CRMTask`.

	`unassign_from_previous_user` is deliberately NOT gated: removing a stale assignment is cleanup, and
	gating it would strand a ToDo pointing at the person who no longer holds the task. Only the CREATING
	direction is governed."""

	def assign_to(self):
		if not automation.is_enabled(TASK_ASSIGNEE):
			return
		super().assign_to()
