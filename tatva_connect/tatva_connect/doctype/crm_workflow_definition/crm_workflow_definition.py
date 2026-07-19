# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""The workflow graph author-time contract. Fail-closed on the structural mistakes that would strand or
crash an Instance (no node, no Terminal, a dangling edge, a missing Action Group, an unparseable
expression, a zero/negative delay). A runtime-only mistake (an expression that yields the wrong type,
a ctx reference that resolves oddly) is not caught here - it fails the Instance safely (Failed + logged),
never a storm. On save the graph freezes into an immutable CRM Workflow Version - the graph an Instance
actually executes."""
import json

import frappe
from frappe import _
from frappe.model.document import Document

_EVENT_MODES = frozenset({"Until Event", "Event-or-Timeout"})
_TIME_MODES = frozenset({"For Duration", "Until Time", "Event-or-Timeout"})

# The Definition lifecycle - the ONE state machine. A Definition is born a Draft (the field default), then
# advanced ONLY along these edges; each canvas verb (campaigns.api) calls apply_transition, and nothing else
# sets lifecycle_state by hand. The state names are defined once here and reused everywhere - no literals.
LIFECYCLE_STATES = ("Draft", "Published", "Active", "Suspended", "Archived")
DRAFT, PUBLISHED, ACTIVE, SUSPENDED, ARCHIVED = LIFECYCLE_STATES
ARMED_STATE = ACTIVE  # the entry trigger reads THIS and nothing else - a new Instance starts only while Active
_ENFORCED_STATES = frozenset({PUBLISHED, ACTIVE, SUSPENDED})  # a running Instance can bind here - the release contract holds
_TRANSITIONS = {
	DRAFT: {PUBLISHED, ARCHIVED},
	PUBLISHED: {ACTIVE, DRAFT, ARCHIVED},
	ACTIVE: {SUSPENDED, DRAFT, ARCHIVED},
	SUSPENDED: {ACTIVE, DRAFT, ARCHIVED},
	ARCHIVED: set(),
}


class CRMWorkflowDefinition(Document):
	def validate(self):
		"""Frappe's per-save hook. A Draft is mutable and half-built by design, so only the cheap identity
		check runs while authoring. In a released state two laws hold: the graph is IMMUTABLE (edit a Draft -
		Revise first) and it must satisfy the full release contract. The gate lives on release, not on every
		keystroke."""
		self._require_unique_node_ids()
		if self.lifecycle_state in _ENFORCED_STATES:
			self._forbid_released_graph_edit()
			self._enforce_release_contract()

	def is_editable(self):
		"""A Definition's graph may be authored only while it is a Draft — the ONE editability predicate the
		campaigns API asks instead of testing the state itself (one brain, no literal at the call site)."""
		return (self.lifecycle_state or DRAFT) == DRAFT

	def _forbid_released_graph_edit(self):
		"""'Editable <=> Draft' as a controller LAW, not just an API convention — so Desk / REST / any writer
		cannot bypass save_draft. A released Definition is an immutable Version; its graph may not change in
		place. Allowed: a transition (lifecycle_state changed) — Publish is meant to (re)freeze, Activate /
		Suspend never touch the graph — and a doc inserted straight into a released state (a publish-on-insert
		seed/test). Refused: a save that KEEPS a released state while the frozen graph would change. Reuses the
		ONE freeze brain (build_payload + definition_hash), so 'changed' means exactly what the freeze means."""
		if self.is_new() or self.has_value_changed("lifecycle_state"):
			return
		from tatva_connect.workflow_engine import versions

		current = frappe.db.get_value(versions.DOCTYPE, {"workflow": self.name, "is_current": 1}, "definition_hash")
		if current and versions.definition_hash(versions.build_payload(self)) != current:
			frappe.throw(
				_("A {0} workflow is immutable — Revise it to a Draft before editing its graph.").format(frappe.bold(self.lifecycle_state)),
				title=_("Not editable"),
			)

	def _enforce_release_contract(self):
		"""The fail-closed structural contract a Version must satisfy before it can run (the Publish gate).
		Ordered so the most precise message wins: edges resolve, the graph is entered and fully reachable,
		every Step has its Action Group, expressions parse, delays are strictly positive."""
		self._require_edges_resolve()
		self._require_reachable_terminal()
		self._require_step_action_groups_exist()
		self._require_expressions_parse()
		self._require_positive_wait_delays()

	def apply_transition(self, target):
		"""The ONE lifecycle mover: advance this Definition along a legal edge, then save - so the Publish
		gate (validate's release contract) and the freeze (on_update) fire exactly when the target is a
		released state. An illegal edge is refused before any write (fail-closed). Callers are the thin
		whitelisted verbs in campaigns.api; nothing sets lifecycle_state by hand."""
		current = self.lifecycle_state or "Draft"
		if target not in _TRANSITIONS.get(current, set()):
			frappe.throw(
				_("A {0} workflow cannot move to {1}.").format(frappe.bold(current), frappe.bold(target)),
				title=_("Illegal transition"),
			)
		self.lifecycle_state = target
		self.save()
		return self.lifecycle_state

	@staticmethod
	def default_list_data():
		"""Default columns/rows for the CRM list surface (Campaigns page). Every CRM list doctype defines
		this; get_data calls it when no saved CRM View Settings exists (crm/api/doc.py)."""
		columns = [
			{"label": "Name", "type": "Data", "key": "workflow_name", "width": "16rem"},
			{"label": "State", "type": "Select", "key": "lifecycle_state", "width": "9rem"},
			{"label": "Entry DocType", "type": "Link", "options": "DocType", "key": "entry_doctype", "width": "12rem"},
			{"label": "Entry Event", "type": "Select", "key": "entry_event", "width": "9rem"},
			{"label": "Last Modified", "type": "Datetime", "key": "modified", "width": "9rem"},
		]
		rows = [
			"name",
			"workflow_name",
			"lifecycle_state",
			"entry_doctype",
			"entry_event",
			"vertical",
			"group",
			"program",
			"modified",
		]
		return {"columns": columns, "rows": rows}

	def _require_reachable_terminal(self):
		"""A publishable Flow must be enterable and finishable: at least one node, an explicit Start node
		(entry_node) that names a real node, and a Terminal reachable from it. Every node must be reachable
		from the entry - a node stranded off the entry graph is dead config, never silently shipped. (The
		entry pointer replaces the old 'entry is the first node' convention, so the canvas Start node is the
		single source of where an Instance begins.)"""
		if not self.nodes:
			frappe.throw(_("A Flow needs at least one node before it can be published."), title=_("Empty graph"))
		if not any(n.node_type == "Terminal" for n in self.nodes):
			frappe.throw(_("A Flow needs at least one Terminal node so it can end."), title=_("No Terminal"))
		ids = set(self._node_ids())
		if not (self.entry_node or "").strip():
			frappe.throw(_("Set a Start node before publishing - the Flow needs an Entry Node."), title=_("No entry"))
		if self.entry_node not in ids:
			frappe.throw(_("Entry Node {0} is not a node in this workflow.").format(frappe.bold(self.entry_node)), title=_("Bad entry"))
		reachable = self._reachable_from(self.entry_node)
		dead = [n.node_id for n in self.nodes if n.node_id and n.node_id not in reachable]
		if dead:
			frappe.throw(
				_("These nodes are unreachable from the Start node: {0}.").format(frappe.bold(", ".join(dead))),
				title=_("Unreachable nodes"),
			)
		if not any(n.node_type == "Terminal" and n.node_id in reachable for n in self.nodes):
			frappe.throw(_("No Terminal is reachable from the Start node."), title=_("No reachable Terminal"))

	def _reachable_from(self, entry):
		"""The set of node_ids reachable from `entry` by following declared edges - the graph-walk the
		reachability check reads. Uses the same `_edges_of` the edge validator uses (one edge brain), so a
		Wait's mode-specific handles are honoured exactly once."""
		by_id = {n.node_id: n for n in self.nodes if n.node_id}
		seen, stack = set(), [entry]
		while stack:
			nid = stack.pop()
			if nid in seen or nid not in by_id:
				continue
			seen.add(nid)
			for _label, target in self._edges_of(by_id[nid]):
				if target:
					stack.append(target)
		return seen

	def on_update(self):
		"""Freeze this graph into an immutable Version and mark it current - but ONLY in a released state
		(Publish and beyond). A Draft save persists the working graph and mints nothing, so authoring never
		litters Versions; the freeze happens at the Publish gate. Editing a released Definition (via Revise
		-> Draft -> Publish) mints a NEW Version; in-flight Instances keep their pinned Version (greenfield,
		no migration)."""
		if self.lifecycle_state not in _ENFORCED_STATES:
			return
		from tatva_connect.workflow_engine import versions

		versions.ensure_version(self)

	def _node_ids(self):
		return [n.node_id for n in self.nodes if n.node_id]

	def _require_unique_node_ids(self):
		"""A node_id is a cursor a parked Instance stores, so it must be unique within the workflow."""
		seen = set()
		for n in self.nodes:
			if not n.node_id:
				frappe.throw(_("Every node needs a Node ID."), title=_("Missing node id"))
			if n.node_id in seen:
				frappe.throw(_("Duplicate Node ID {0} - node ids must be unique.").format(frappe.bold(n.node_id)), title=_("Duplicate node id"))
			seen.add(n.node_id)

	def _edges_of(self, node):
		"""The outgoing edges this node type carries, as (label, target) pairs, per §4.1."""
		t = node.node_type
		if t == "Step":
			return [("Next Node", node.next_node)]
		if t == "Assign":
			return [("Next Node", node.next_node)]
		if t == "Branch":
			return [("On True", node.on_true), ("On False", node.on_false)]
		if t == "Wait":
			if node.wait_mode == "Event-or-Timeout":
				return [("On Event", node.on_event), ("On Timeout", node.on_timeout)]
			if node.wait_mode == "Until Event":
				return [("On Event", node.on_event)]
			return [("Next Node", node.next_node)]
		return []  # Terminal has no outgoing edge

	def _require_edges_resolve(self):
		"""Every edge must name a real node in this workflow - a dangling cursor would strand an Instance."""
		ids = set(self._node_ids())
		for node in self.nodes:
			for label, target in self._edges_of(node):
				if not target:
					frappe.throw(
						_("Node {0} ({1}) needs its {2} edge set.").format(frappe.bold(node.node_id), node.node_type, label),
						title=_("Missing edge"),
					)
				if target not in ids:
					frappe.throw(
						_("Node {0}'s {1} edge points at {2}, which is not a node in this workflow.").format(
							frappe.bold(node.node_id), label, frappe.bold(target)
						),
						title=_("Dangling edge"),
					)

	def _require_step_action_groups_exist(self):
		"""A Step names an Action Group; it must exist, or the Step has nothing to run."""
		for node in self.nodes:
			if node.node_type != "Step":
				continue
			if not node.action_group:
				frappe.throw(_("Step node {0} needs an Action Group.").format(frappe.bold(node.node_id)), title=_("Incomplete step"))
			if not frappe.db.exists("CRM Action Group", node.action_group):
				frappe.throw(
					_("Step node {0} references Action Group {1}, which does not exist.").format(
						frappe.bold(node.node_id), frappe.bold(node.action_group)
					),
					title=_("Unknown action group"),
				)

	def _require_expressions_parse(self):
		"""Branch/Assign expressions must be a single eval-form expression (safe_eval); an event Wait's
		accepts map must be a JSON object. Syntax-only - the author-time context is empty, so a live
		dry-run would falsely block a legitimate ctx[...] reference (same reasoning as automation.expr)."""
		from tatva_connect.automation import expr

		for node in self.nodes:
			if node.node_type == "Branch":
				if not (node.condition or "").strip():
					frappe.throw(_("Branch node {0} needs a Condition.").format(frappe.bold(node.node_id)), title=_("Incomplete branch"))
				expr.assert_parses(node.condition)
			elif node.node_type == "Assign":
				if not (node.assign_json or "").strip():
					frappe.throw(_("Assign node {0} needs an expression.").format(frappe.bold(node.node_id)), title=_("Incomplete assign"))
				expr.assert_parses(node.assign_json)
			elif node.node_type == "Wait" and node.wait_mode in _EVENT_MODES:
				if not (node.signal_name or "").strip():
					frappe.throw(_("Wait node {0} needs a Signal Name for an event mode.").format(frappe.bold(node.node_id)), title=_("Incomplete wait"))
				self._assert_accepts_is_object(node)

	def _assert_accepts_is_object(self, node):
		"""accepts_json maps declared payload paths to state keys - a JSON object, or blank (merge nothing)."""
		raw = (node.accepts_json or "").strip()
		if not raw:
			return
		try:
			data = json.loads(raw)
		except (ValueError, TypeError):
			frappe.throw(_("Wait node {0}'s Accepts (JSON) is not valid JSON.").format(frappe.bold(node.node_id)), title=_("Bad accepts map"))
			return
		if not isinstance(data, dict):
			frappe.throw(_("Wait node {0}'s Accepts (JSON) must be a JSON object.").format(frappe.bold(node.node_id)), title=_("Bad accepts map"))

	def _require_positive_wait_delays(self):
		"""A delay-mode Wait (For Duration / the timeout of Event-or-Timeout) must resolve to a STRICTLY
		future instant - a {\"seconds\": 0} delay would re-arm instantly and hot-loop the sweep (F6). A
		context-free expression is a constant, so it is evaluated for real and rejected; one that reads
		ctx can only be syntax-checked, because the author-time context is empty."""
		from tatva_connect.automation import expr
		from tatva_connect.workflow_engine import interpreter

		for node in self.nodes:
			if node.node_type != "Wait":
				continue
			if node.wait_mode not in _TIME_MODES:
				continue
			if not (node.wait_expression or "").strip():
				frappe.throw(_("Wait node {0} needs a Wait Expression.").format(frappe.bold(node.node_id)), title=_("Incomplete wait"))
			if expr.references_context(node.wait_expression):
				expr.assert_parses(node.wait_expression)
				continue
			base = frappe.utils.now_datetime()
			deadline = interpreter.wait_deadline(node.wait_mode, node.wait_expression, {}, base)
			if frappe.utils.get_datetime(deadline) <= base:
				frappe.throw(
					_("Wait node {0}'s delay must be strictly positive - it resolves to now or the past.").format(frappe.bold(node.node_id)),
					title=_("Zero or negative delay"),
				)
