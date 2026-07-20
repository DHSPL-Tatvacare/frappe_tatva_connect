"""Immutable workflow graphs - the ONE brain over `CRM Workflow Version`.

WHY THIS EXISTS
---------------
A Workflow is a PROGRAM: a graph of nodes. A subject entering it starts a RUN that can park for weeks on
a timer or an event. If that Run re-read the graph live out of the mutable Workflow at resume time, an
edit could re-route it, drop the node it sleeps in, or orphan it entirely.

So: on save, the Workflow's graph is frozen into a content-addressed, immutable version, and every Run
binds to a VERSION, never to the Workflow. Editing a Workflow mints a new Version; in-flight Runs keep
the one they started on.

A node is its own record now, carrying its edges and its actions as child rows. The freeze therefore
walks Node records rather than child rows of the Workflow, and inlines both tables — so a frozen node is
self-contained and a parked Run executes the graph AND the bodies it began with.
"""
import frappe
from frappe import _

DOCTYPE = "CRM Workflow Version"
NODE_DOCTYPE = "CRM Workflow Node"

# Volatile columns a frozen node must never carry: they move on every save without changing the graph.
_VOLATILE = frozenset(
	("creation", "modified", "modified_by", "owner", "docstatus", "idx", "parent", "parentfield", "parenttype", "name")
)


def _freeze(child):
	"""One node row, stripped to its graph-relevant columns. `None` normalises to `""` so a field never
	set and one cleared hash identically."""
	return {k: ("" if v is None else v) for k, v in child.get_valid_dict().items() if k not in _VOLATILE}


def build_payload(workflow):
	"""The graph-relevant definition of `workflow`, in canonical shape.

	Self-contained by construction: every node inlines its own edges and actions, so a frozen version
	needs nothing else to execute and editing a workflow can never reach a Run already in flight.
	"""
	return {
		"workflow_name": workflow.workflow_name,
		"vertical": workflow.vertical or "",
		"group": workflow.group or "",
		"program": workflow.program or "",
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


def _canonical(payload):
	"""Deterministic serialisation via the native encoder (`frappe.as_json` sorts keys) - the hash input
	and the stored blob are the same bytes."""
	return frappe.as_json(payload, indent=None, separators=(",", ":"))


def definition_hash(payload):
	return frappe.utils.sha256_hash(_canonical(payload))


def ensure_version(workflow):
	"""Mint the version for `workflow`'s current graph, or reuse the existing one with the same content
	hash (an idempotent save mints nothing; reverting re-flags the old version). Marks it current and
	returns its name."""
	payload = build_payload(workflow)
	digest = definition_hash(payload)
	name = frappe.db.get_value(DOCTYPE, {"workflow": workflow.name, "definition_hash": digest})
	if not name:
		latest = frappe.get_all(
			DOCTYPE, filters={"workflow": workflow.name}, fields=["version_no"], order_by="version_no desc", limit=1
		)
		name = frappe.get_doc({
			"doctype": DOCTYPE,
			"workflow": workflow.name,
			"version_no": (latest[0].version_no if latest else 0) + 1,
			"definition_hash": digest,
			"payload_json": _canonical(payload),
			"node_count": len(payload["nodes"]),
		}).insert(ignore_permissions=True).name  # authz-ok: tier-a — workflow engine: immutable version, engine-written
	_mark_current(workflow.name, name)
	return name


def _mark_current(workflow_name, version_name):
	"""Exactly one current version per workflow. `db.set_value` writes the flag directly: the controller's
	immutability guard defends the frozen GRAPH, and which graph is live is a fact about the workflow's
	present, not about this frozen program."""
	for other in frappe.get_all(DOCTYPE, filters={"workflow": workflow_name, "is_current": 1}, pluck="name"):
		if other != version_name:
			frappe.db.set_value(DOCTYPE, other, "is_current", 0, update_modified=False)
	frappe.db.set_value(DOCTYPE, version_name, "is_current", 1, update_modified=False)


def current_name(workflow_name):
	"""The version a NEW Run binds to. One indexed read; a workflow with no version yet mints lazily."""
	name = frappe.db.get_value(DOCTYPE, {"workflow": workflow_name, "is_current": 1})
	return name or ensure_version(frappe.get_doc("CRM Workflow", workflow_name))


def entry_node_of(version):
	"""Where a new Run begins: the declared Start node (entry_node), falling back to the first node. The ONE entry-resolution brain — both the durable
	start (triggers._start_one) and the ephemeral run (interpreter.run_inline) call this, never their own
	copy, so the fallback policy can never drift between the two paths."""
	return version.entry_node or version.nodes[0].node_id


def load(version_name):
	"""The frozen graph as `_dict(workflow, entry_node, nodes)`, nodes rehydrated as `frappe._dict` so a
	missing key reads as None exactly like a doc. Request-cached: the payload is immutable."""
	cache = frappe.flags.setdefault("_workflow_version_cache", {})
	if version_name not in cache:
		row = frappe.db.get_value(DOCTYPE, version_name, ["workflow", "payload_json"], as_dict=True)
		if not row:
			frappe.throw(_("Workflow version {0} no longer exists.").format(version_name))
		payload = frappe.parse_json(row.payload_json)
		cache[version_name] = frappe._dict(
			workflow=row.workflow,
			entry_node=payload.get("entry_node"),
			nodes=[frappe._dict(n) for n in payload["nodes"]],
		)
	return cache[version_name]
