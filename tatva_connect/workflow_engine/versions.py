"""Immutable workflow graphs - the ONE brain over `CRM Workflow Version`.

WHY THIS EXISTS
---------------
A Definition is a PROGRAM: a graph of nodes. A subject entering it starts an INSTANCE that can park for
weeks on a timer or an event. If that Instance re-read the node graph live out of the mutable Definition
at resume time, an edit could re-route it, drop the node it sleeps in, or orphan it entirely.

So: on save, the Definition's graph is frozen into a content-addressed, immutable version, and every
Instance binds to a VERSION, never to the Definition. No migration machinery - editing a Definition mints
a new Version; in-flight Instances keep their pinned Version (greenfield: nothing to migrate).
"""
import frappe
from frappe import _

DOCTYPE = "CRM Workflow Version"

# Volatile / structural columns a frozen node must never carry: they move on every save without changing
# the graph. Order is carried by list position, so `idx` is dropped; `name` is the child-row id, kept for
# stable identity but not decision-relevant here (no migration reads it).
_VOLATILE = frozenset(
	("creation", "modified", "modified_by", "owner", "docstatus", "idx", "parent", "parentfield", "parenttype", "name")
)


def _freeze(child):
	"""One node row, stripped to its graph-relevant columns. `None` normalises to `""` so a field never
	set and one cleared hash identically."""
	return {k: ("" if v is None else v) for k, v in child.get_valid_dict().items() if k not in _VOLATILE}


def build_payload(definition):
	"""The graph-relevant definition of `definition`, in canonical shape. MOLECULE-DEEP (D1, full freeze):
	each Step node inlines a snapshot of its Action Group's actions, so a frozen version is self-contained
	and a parked Instance runs exactly the bodies it started with - editing a molecule reaches a NEW Instance
	only when the workflow is re-saved (a new version), and never touches one in flight. Reads action groups
	at freeze time (no longer pure), which is fine on save."""
	return {
		"workflow_name": definition.workflow_name,
		"vertical": definition.vertical or "",
		"group": definition.group or "",
		"program": definition.program or "",
		"entry_doctype": definition.entry_doctype or "",
		"entry_event": definition.entry_event or "",
		"criteria": [_freeze(c) for c in (definition.criteria or [])],
		"nodes": [_freeze_node(n) for n in definition.nodes],
	}


def _freeze_node(node):
	"""One node's graph-relevant columns, plus - for a Step - a frozen snapshot of its Action Group's
	actions (`_frozen_items`), so the body is immutable under a parked Instance (D1)."""
	frozen = _freeze(node)
	if node.node_type == "Step" and node.action_group:
		frozen["_frozen_items"] = _freeze_action_group(node.action_group)
	return frozen


def _freeze_action_group(group_name):
	"""The Action Group's actions in order, stripped to decision-relevant columns - the snapshot a Step
	node carries into the frozen version. `frappe.as_json` sorts keys, so the hash stays stable."""
	rows = frappe.get_all(
		"CRM Action Group Item", filters={"parenttype": "CRM Action Group", "parent": group_name},
		fields=["*"], order_by="idx asc",
	)
	return [{k: ("" if v is None else v) for k, v in row.items() if k not in _VOLATILE} for row in rows]


def _canonical(payload):
	"""Deterministic serialisation via the native encoder (`frappe.as_json` sorts keys) - the hash input
	and the stored blob are the same bytes."""
	return frappe.as_json(payload, indent=None, separators=(",", ":"))


def definition_hash(payload):
	return frappe.utils.sha256_hash(_canonical(payload))


def ensure_version(definition):
	"""Mint the version for `definition`'s current graph, or reuse the existing one with the same content
	hash (an idempotent save mints nothing; reverting re-flags the old version). Marks it current and
	returns its name."""
	payload = build_payload(definition)
	digest = definition_hash(payload)
	name = frappe.db.get_value(DOCTYPE, {"workflow": definition.name, "definition_hash": digest})
	if not name:
		latest = frappe.get_all(
			DOCTYPE, filters={"workflow": definition.name}, fields=["version_no"], order_by="version_no desc", limit=1
		)
		name = frappe.get_doc({
			"doctype": DOCTYPE,
			"workflow": definition.name,
			"version_no": (latest[0].version_no if latest else 0) + 1,
			"definition_hash": digest,
			"payload_json": _canonical(payload),
			"node_count": len(payload["nodes"]),
		}).insert(ignore_permissions=True).name  # authz-ok: tier-a — workflow engine: immutable version, engine-written
	_mark_current(definition.name, name)
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
	"""The version a NEW Instance binds to. One indexed read; a Definition that predates versioning mints
	lazily on first ask."""
	name = frappe.db.get_value(DOCTYPE, {"workflow": workflow_name, "is_current": 1})
	return name or ensure_version(frappe.get_doc("CRM Workflow Definition", workflow_name))


def load(version_name):
	"""The frozen graph as `_dict(workflow, entry_doctype, entry_event, nodes)`, nodes rehydrated as
	`frappe._dict` - which returns `None` for a missing attribute exactly like a child doc, so the
	interpreter reads each node unchanged. Request-cached: the payload is immutable, so caching is free."""
	cache = frappe.flags.setdefault("_workflow_version_cache", {})
	if version_name not in cache:
		row = frappe.db.get_value(DOCTYPE, version_name, ["workflow", "payload_json"], as_dict=True)
		if not row:
			frappe.throw(_("Workflow version {0} no longer exists.").format(version_name))
		payload = frappe.parse_json(row.payload_json)
		cache[version_name] = frappe._dict(
			workflow=row.workflow,
			entry_doctype=payload.get("entry_doctype"),
			entry_event=payload.get("entry_event"),
			criteria=[frappe._dict(c) for c in payload.get("criteria", [])],
			nodes=[frappe._dict(n) for n in payload["nodes"]],
		)
	return cache[version_name]
