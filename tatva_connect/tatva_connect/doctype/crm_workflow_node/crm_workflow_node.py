# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""One node of a workflow graph.

The node holds no rules of its own. What its type may be configured with, what outputs it may have and
whether it may carry actions are all declared in `workflow_engine.registry`; this controller only
enforces what the registry says. A node type is added there, never here.
"""
import frappe
from frappe import _
from frappe.model.document import Document

from tatva_connect.workflow_engine import registry


class CRMWorkflowNode(Document):
	def validate(self):
		self.validate_against_registry()
		self.validate_unique_node_id()

	def config(self):
		"""This node's configuration as a dict. Invalid JSON is a validation error, not a crash later —
		the interpreter reads this at run time, long after the author has gone."""
		try:
			return registry.config_of(self)
		except Exception:
			frappe.throw(_("Configuration is not valid JSON."), title=_("Bad configuration"))

	def validate_against_registry(self):
		"""Every rule the registry declares for this node type, reported together.

		All problems at once, deliberately: fixing a five-field node one error per save is five round
		trips through a dialog.
		"""
		# DRAFT: a node save is an authoring step. Whether every required setting is filled in is asked
		# at publish, by the graph contract — not here, where it would refuse to save work in progress.
		problems = registry.validate_node(
			self.node_type, self.config(), [e.from_output for e in self.edges or []],
			mode=registry.DRAFT, graph_config=self._graph_config(),
		)
		if problems:
			frappe.throw(
				"<br>".join(p["message"] for p in problems), title=_("This node is not valid")
			)

	def _graph_config(self):
		"""`{node_id: config}` for this node's siblings — what a Wait needs to know which buttons the node
		it waits on OFFERS. A node whose outputs derive from a sibling cannot be judged alone, and judging
		it alone is what rejected a correctly-wired button branch at save."""
		if not self.workflow:
			return {}
		rows = frappe.get_all(
			"CRM Workflow Node", filters={"workflow": self.workflow},
			fields=["node_id", "config_json"],
		)
		graph = {r.node_id: registry.config_of(r) for r in rows}
		graph[self.node_id] = self.config()
		return graph

	def validate_unique_node_id(self):
		"""`node_id` is what an edge points at and what a parked Run stores as its cursor. Two nodes
		sharing one inside a workflow would make both meaningless."""
		clash = frappe.db.exists(
			"CRM Workflow Node",
			{"workflow": self.workflow, "node_id": self.node_id, "name": ["!=", self.name or ""]},
		)
		if clash:
			frappe.throw(
				_("Another node in this workflow is already called {0}.").format(self.node_id),
				title=_("Duplicate node id"),
			)


	def on_update(self):
		self.refresh_workflow_trigger_index()

	def on_trash(self):
		self.refresh_workflow_trigger_index()

	def refresh_workflow_trigger_index(self):
		"""Keep the workflow's derived trigger columns in step with its Trigger node.

		The dispatcher filters on those columns on every document save, so they must follow this node —
		otherwise editing a trigger would leave the workflow answering to the subject or event it used
		to watch.
		"""
		if self.node_type != registry.TRIGGER or not self.workflow:
			return
		if not frappe.db.exists("CRM Workflow", self.workflow):
			return
		frappe.get_doc("CRM Workflow", self.workflow).save(ignore_permissions=True)  # authz-ok: tier-a — engine: keeps a derived index in step with its source
