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


class CRMWorkflowDefinition(Document):
	def validate(self):
		self._require_reachable_terminal()
		self._require_unique_node_ids()
		self._require_edges_resolve()
		self._require_step_action_groups_exist()
		self._require_expressions_parse()
		self._require_positive_wait_delays()

	def _require_reachable_terminal(self):
		"""A Flow needs at least one node and at least one Terminal — a zero-node graph would IndexError at
		start (the entry is the first node), and a graph with no Terminal could never end (it would run to
		the hop budget and Fail)."""
		if not self.nodes:
			frappe.throw(_("A Flow needs at least one node."), title=_("Empty graph"))
		if not any(n.node_type == "Terminal" for n in self.nodes):
			frappe.throw(_("A Flow needs at least one Terminal node so it can end."), title=_("No Terminal"))

	def on_update(self):
		"""Freeze this graph into an immutable version and mark it current. No migration - editing a
		Definition mints a new Version; in-flight Instances keep their pinned Version (UAT, greenfield)."""
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
