# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The Workflow record — a program, and the lifecycle that decides whether it may run.

A workflow is a graph of `CRM Workflow Node` records. It owns almost nothing itself: the subject it
watches, the event it fires on and the predicate that qualifies a subject all live on its TRIGGER node,
because a trigger is a node like any other and the canvas should be able to show it as one.

The two exceptions are `trigger_doctype` / `trigger_event`, and they are derived here rather than
authored. The dispatcher runs on EVERY document save, so it must answer "which workflows care about
this (doctype, event)?" with a single indexed query — a predicate living in a JSON column cannot serve
that. They are a materialised index, with exactly one writer: this controller.
"""
import frappe
from frappe import _
from frappe.model.document import Document

from tatva_connect.workflow_engine import cohort, registry
from tatva_connect.workflow_engine.registry import TRIGGER as TRIGGER_NODE_TYPE

LIFECYCLE_STATES = ("Draft", "Published", "Active", "Suspended", "Archived")
DRAFT, PUBLISHED, ACTIVE, SUSPENDED, ARCHIVED = LIFECYCLE_STATES

# Only an Active workflow fires. Save is not publish, and publish is not activate.
ARMED_STATE = ACTIVE

_TRANSITIONS = {
	DRAFT: {PUBLISHED, ARCHIVED},
	PUBLISHED: {ACTIVE, DRAFT, ARCHIVED},
	ACTIVE: {SUSPENDED, DRAFT, ARCHIVED},
	SUSPENDED: {ACTIVE, DRAFT, ARCHIVED},
	ARCHIVED: set(),
}


# What the dispatcher filters on, and the Trigger config key each is copied from. Adding a dispatch axis
# means one entry here — never a second place that decides what a workflow answers to.
TRIGGER_INDEX = {
	"trigger_doctype": "subject_doctype",
	"trigger_event": "event",
	"trigger_mode": "mode",
	"trigger_vertical": "vertical",
	"trigger_group": "group",
	"trigger_program": "program",
}


class CRMWorkflow(Document):
	def validate(self):
		self.sync_trigger_index()

	def sync_trigger_index(self):
		"""Copy the Trigger node's dispatch axes onto the header, so the dispatcher can find us.

		Blank when there is no Trigger node yet — a Draft under construction is not a defect, and a
		workflow with no trigger simply never matches a save. A blank GRAIN axis is not "unset" either:
		it is the wildcard the one matcher understands as ANY.
		"""
		trigger = self.trigger_node()
		config = registry.config_of(trigger) if trigger else {}
		for column, key in TRIGGER_INDEX.items():
			self.set(column, (config or {}).get(key) or "")
		# A Trigger that does not say how it starts is a record-event one — that is what every Trigger was
		# before schedule mode existed, and the drain matches on `Schedule` alone, so blank is never due.
		if trigger and not self.trigger_mode:
			self.trigger_mode = registry.MODE_RECORD
		# Not a copy but a DERIVATION, which is why it sits beside the map rather than in it: the drain asks
		# one indexed question — mode plus a clock — and a record-event workflow is never due, so it carries none.
		self.trigger_next_run_at = cohort.next_run_at(config)

	def trigger_node(self):
		"""This workflow's Trigger node, or None. There is at most one — `validate_trigger` enforces it."""
		names = frappe.get_all(
			"CRM Workflow Node",
			filters={"workflow": self.name, "node_type": TRIGGER_NODE_TYPE},
			pluck="name",
			limit=2,
		)
		if len(names) > 1:
			frappe.throw(_("A workflow may have only one Trigger node."), title=_("Two triggers"))
		return frappe.get_doc("CRM Workflow Node", names[0]) if names else None

	@staticmethod
	def default_list_data():
		"""Columns and fields the CRM list view opens with. Required by `crm.api.doc.get_data`.

		Without it the list endpoint raises and the page renders NOTHING — not the rows, and not the
		empty state either, because the SPA only decides between them once the response arrives.
		"""
		columns = [
			{"label": "Workflow", "type": "Data", "key": "workflow_name", "width": "16rem"},
			{"label": "State", "type": "Select", "key": "lifecycle_state", "width": "8rem"},
			{"label": "Subject", "type": "Data", "key": "trigger_doctype", "width": "10rem"},
			{"label": "Event", "type": "Data", "key": "trigger_event", "width": "8rem"},
			{"label": "Last Modified", "type": "Datetime", "key": "modified", "width": "8rem"},
		]
		rows = [
			"name",
			"workflow_name",
			"lifecycle_state",
			"trigger_doctype",
			"trigger_event",
			"trigger_vertical",
			"trigger_group",
			"trigger_program",
			"modified",
		]
		return {"columns": columns, "rows": rows}

	def publish_problems(self):
		"""Every fault and every warning about this graph, as `{node_id, field, message, code, severity,
		fix}`. The ONE reader.

		Returns rather than throws, so the API can hand the list to the canvas and let it mark the
		offending nodes. `apply_transition` raises on the BLOCKS in the same list, because a programmatic
		caller must never publish a broken graph by ignoring a return value. A `warns` is a true statement
		that does not stop a publish (see `_deployment_warnings`).
		"""
		from tatva_connect.workflow_engine import graph

		return graph.problems(self.authored_graph(), self.entry_node) + self._deployment_warnings()

	def _deployment_warnings(self):
		"""True statements about THIS bench that are NOT reasons to refuse a publish. The engine switch
		ships off by design (dormant automation, CLAUDE.md #4/#6), so a published workflow is mute until an
		operator arms it — an author may legitimately publish ahead of that. It WARNS rather than blocks, so
		the fact reaches the author without turning the intended resting state into an error.

		This is not a graph rule, so it does not live in `graph.problems` — that gate is pure and answers a
		question about the GRAPH, while this answers one about the DEPLOYMENT. It still speaks the one
		problem vocabulary, through the one constructor.
		"""
		from tatva_connect.automation import settings
		from tatva_connect.workflow_engine import ENGINE_SWITCH, registry

		if settings.is_enabled(ENGINE_SWITCH):
			return []
		return [{"node_id": None, **registry.problem(
			_("The workflow engine is switched off, so this workflow will not run until an operator turns it on."),
			code="engine.muted", severity=registry.WARNS,
			fix=_("Ask an operator to enable the {0} switch when you are ready to go live.").format(ENGINE_SWITCH),
		)}]

	def assert_publishable(self):
		"""Refuse to publish a graph that cannot run. Every fault at once, not one per attempt.

		Counts BLOCKS only — a `warns` (the engine being off) is a fact the author should see, not a reason
		to refuse the save. Publish is the last moment a blocker is cheap: after it, the same fault is a
		failed run on a real patient's record, days later, found by someone who did not author it.
		"""
		from tatva_connect.workflow_engine import registry

		blockers = [p for p in self.publish_problems() if p["severity"] == registry.BLOCKS]
		if blockers:
			frappe.throw(
				"<br>".join(frappe.utils.escape_html(p["message"]) for p in blockers),
				title=_("This workflow cannot run yet"),
			)

	def authored_graph(self):
		"""The live graph as the validator reads it — nodes with their edges inlined."""
		nodes = frappe.get_all(
			"CRM Workflow Node",
			filters={"workflow": self.name},
			fields=["name", "node_id", "node_type", "config_json"],
			order_by="sequence asc, creation asc",
		)
		for node in nodes:
			node["edges"] = frappe.get_all(
				"CRM Workflow Edge",
				filters={"parent": node["name"], "parenttype": "CRM Workflow Node"},
				fields=["from_output", "to_node"],
			)
		return nodes

	def freeze_version(self):
		"""Freeze the CURRENT graph as an immutable version. Publish means this graph, not a later one.

		Without this the freeze was lazy — `versions.current_name()` minted v1 the first time a trigger
		matched, marked it current, and nothing ever minted another. So after a workflow had run once,
		editing it and publishing again changed NOTHING: the author saw a green Published badge while the
		original graph kept executing, forever, with no way to tell. Publish is the moment the author says
		"this one", so it is the moment the bytes are pinned.

		Content-addressed, so re-publishing an unchanged graph reuses its version rather than minting a
		duplicate; runs already under way stay pinned to the version they started on.
		"""
		from tatva_connect.workflow_engine import versions

		return versions.ensure_version(self)

	def is_editable(self) -> bool:
		"""Authoring is a DRAFT-only operation. The ONE owner of that rule — the canvas asks, never decides.

		A released workflow is serving in-flight runs from a frozen version; editing its graph in place
		would change what those runs are judged by. Revise it back to a Draft first."""
		return (self.lifecycle_state or DRAFT) == DRAFT

	def can_transition_to(self, target) -> bool:
		return target in _TRANSITIONS.get(self.lifecycle_state or DRAFT, set())

	def apply_transition(self, target):
		"""Move the lifecycle, or say why not. The ONE place a state changes — every verb goes through it,
		so an illegal move is impossible rather than merely discouraged."""
		if target not in LIFECYCLE_STATES:
			frappe.throw(_("Unknown workflow state {0}.").format(target))
		if not self.can_transition_to(target):
			frappe.throw(
				_("A {0} workflow cannot become {1}.").format(self.lifecycle_state or DRAFT, target),
				title=_("Not allowed"),
			)
		if target == PUBLISHED:
			self.assert_publishable()
			self.freeze_version()
		if target == ACTIVE and not self.trigger_doctype:
			frappe.throw(
				_("This workflow has no Trigger node, so nothing would ever start it."),
				title=_("No trigger"),
			)
		self.lifecycle_state = target
		self.save(ignore_permissions=True)  # authz-ok: tier-b — gated by the caller's own permission check
		return self.lifecycle_state
