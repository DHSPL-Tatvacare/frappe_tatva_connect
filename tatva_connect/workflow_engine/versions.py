"""Immutable workflow graphs - the ONE brain over `CRM Workflow Version`.

WHY THIS EXISTS
---------------
A Workflow is a PROGRAM: a graph of nodes. A subject entering it starts a journey that can park for weeks on
a timer or an event. If that journey re-read the graph live out of the mutable Workflow at resume time, an
edit could re-route it, drop the node it sleeps in, or orphan it entirely.

So: on save, the Workflow's graph is frozen into a content-addressed, immutable version, and every journey
binds to a VERSION, never to the Workflow. Editing a Workflow mints a new Version; in-flight Journeys keep
the one they started on.

A node is its own record now, carrying its edges and its actions as child rows. The freeze therefore
walks Node records rather than child rows of the Workflow, and inlines both tables — so a frozen node is
self-contained and a parked journey executes the graph AND the bodies it began with.
"""
import frappe
from frappe import _

from tatva_connect.authoring import versions as authoring_versions

DOCTYPE = "CRM Workflow Version"
NODE_DOCTYPE = "CRM Workflow Node"

def build_payload(workflow):
	"""The graph-relevant definition of `workflow`, in canonical shape.

	Self-contained by construction: every node inlines its own edges and actions, so a frozen version
	needs nothing else to execute and editing a workflow can never reach a journey already in flight.
	"""
	# The grain lives on the header under its dispatch names (`trigger_vertical`, ...). Which column carries
	# which axis is declared ONCE, by the controller's TRIGGER_INDEX; read it rather than restate it.
	from tatva_connect.tatva_connect.doctype.crm_workflow.crm_workflow import TRIGGER_INDEX

	column_for = {axis: column for column, axis in TRIGGER_INDEX.items()}
	return {
		"workflow_name": workflow.workflow_name,
		"vertical": workflow.get(column_for["vertical"]) or "",
		"group": workflow.get(column_for["group"]) or "",
		"program": workflow.get(column_for["program"]) or "",
		"entry_node": workflow.entry_node or "",
		"nodes": [_freeze_node(n) for n in _nodes_of(workflow.name)],
	}


def _nodes_of(workflow_name):
	"""This workflow's nodes, in authoring order. `sequence` only makes the freeze deterministic — the
	GRAPH is the edges."""
	names = frappe.get_all(
		NODE_DOCTYPE, filters={"workflow": workflow_name}, order_by="sequence asc, creation asc", pluck="name"
	)
	return [frappe.get_doc(NODE_DOCTYPE, name) for name in names]


def _freeze_node(node):
	"""One node, with its edges inline.

	`config_json` is frozen as the TEXT the author saved, not as a parsed object: the
	hash must change when the text changes, and re-serialising would make formatting alone mint a version.
	"""
	return {
		"node_id": node.node_id,
		"node_type": node.node_type,
		"config_json": node.config_json or "",
		"edges": [
			{"output": e.from_output, "to": e.to_node}
			for e in sorted(node.edges, key=lambda e: (e.from_output or "", e.to_node or ""))
		],
	}


def ensure_version(workflow):
	"""Mint the version for `workflow`'s current graph through the shared freeze (`authoring.versions.mint`)."""
	payload = build_payload(workflow)
	return authoring_versions.mint(DOCTYPE, "workflow", workflow.name, payload, node_count=len(payload["nodes"]))


def current_name(workflow_name):
	"""The version a NEW journey binds to. One indexed read; a workflow with no version yet mints lazily."""
	name = frappe.db.get_value(DOCTYPE, {"workflow": workflow_name, "is_current": 1})
	return name or ensure_version(frappe.get_doc("CRM Workflow", workflow_name))


def current_names(workflow_names):
	"""`current_name` asked of MANY workflows in ONE read — the dispatcher asks it per matched workflow on
	every save, so the cost of a save grew with each workflow authored. Minting stays in `current_name`;
	order follows the caller's list."""
	names = list(workflow_names)
	if not names:
		return []
	current = {
		row.workflow: row.name
		for row in frappe.get_all(
			DOCTYPE, filters={"workflow": ["in", names], "is_current": 1}, fields=["name", "workflow"]
		)
	}
	return [current.get(name) or current_name(name) for name in names]


def entry_node_of(version):
	"""Where a new journey begins: the declared Start node (entry_node), falling back to the first node. The ONE entry-resolution brain — both the durable
	start (triggers._start_one) and the ephemeral run (interpreter.run_inline) call this, never their own
	copy, so the fallback policy can never drift between the two paths."""
	return version.entry_node or version.nodes[0].node_id


def load(version_name):
	"""The frozen graph as `_dict(workflow, entry_node, nodes)`, nodes rehydrated as `frappe._dict` so a
	missing key reads as None exactly like a doc. `get_cached_doc` spares the READ across requests (the
	payload is immutable and frappe drops that cache on both write paths, `database.py:993`); the request
	dict spares the PARSE, which the document cache cannot."""
	cache = frappe.flags.setdefault("_workflow_version_cache", {})
	if version_name not in cache:
		try:
			row = frappe.get_cached_doc(DOCTYPE, version_name)
		except frappe.DoesNotExistError:
			frappe.clear_last_message()
			frappe.throw(_("Workflow version {0} no longer exists.").format(version_name))
		payload = frappe.parse_json(row.payload_json)
		cache[version_name] = frappe._dict(
			workflow=row.workflow,
			entry_node=payload.get("entry_node"),
			nodes=[frappe._dict(n) for n in payload["nodes"]],
		)
	return cache[version_name]
