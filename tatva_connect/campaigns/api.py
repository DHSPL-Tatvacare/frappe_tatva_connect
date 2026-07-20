# The Campaigns canvas API — the ONLY backend the CRM Campaigns SPA calls. Mirrors near_me/api.py and
# smartview/api.py: the fork frontend dispatches here, backend logic lives in tatva_connect. It exposes two
# things and nothing else: authoring a DRAFT graph, and moving a Definition along its lifecycle. The
# lifecycle state machine itself lives on the controller (crm_workflow.apply_transition) - these
# are thin, permission-gated verbs over that ONE brain, never a second copy of it.
#
# The causation is deliberate and total: create -> a blank Draft (nothing runs). save_draft -> persists the
# working graph, mints NO Version, arms NOTHING (a Draft save is not a Publish). publish -> runs the full
# fail-closed release contract and freezes an immutable Version. activate -> arms the entry trigger (the
# only state triggers.py reads). Editing is a Draft-only operation; a released Definition is immutable until
# Revised. Saving ten nodes can never start execution - only Activate can.
import json

import frappe
from frappe import _

from tatva_connect.tatva_connect.doctype.crm_workflow.crm_workflow import (
	ACTIVE,
	ARCHIVED,
	DRAFT,
	PUBLISHED,
	SUSPENDED,
)

DOCTYPE = "CRM Workflow"

NODE_DT = "CRM Workflow Node"

# What the canvas authors on a node. A node's SETTINGS are one opaque JSON column, so this list does not
# grow when a node type gains a field — the registry declares those, and the node controller validates
# them. The five edge columns this used to enumerate are gone: connectivity is the `edges` child table.
_NODE_FIELDS = ("node_id", "node_type", "sequence", "config_json")


@frappe.whitelist()
def get_campaign(name):
	"""Load a workflow for the canvas — header (lifecycle_state, entry_node, canvas_json) + its node graph.

	Nodes are their own records now, not a child table, so they are fetched and attached under `nodes`
	for the canvas — which keeps the wire shape the SPA already speaks. Permission-gated read; the state
	decides editability on the client and the server re-checks on every write."""
	doc = frappe.get_doc(DOCTYPE, name)
	doc.check_permission("read")
	payload = doc.as_dict()
	payload["nodes"] = _nodes_of(name)
	payload["version"] = _current_version(name)
	return payload


def _current_version(workflow):
	"""The frozen version runs are executing, or None while a workflow has never been published.

	Surfaced because an author otherwise has no way to tell WHICH graph is live. A Published badge says
	a version exists; it does not say whether it is the graph on screen. `node_count` and the short hash
	are what make the difference visible.
	"""
	row = frappe.db.get_value(
		"CRM Workflow Version",
		{"workflow": workflow, "is_current": 1},
		["name", "version_no", "node_count", "definition_hash", "creation"],
		as_dict=True,
	)
	if not row:
		return None
	return {
		"name": row.name,
		"version_no": row.version_no,
		"node_count": row.node_count,
		"hash": (row.definition_hash or "")[:8],
		"created": row.creation,
	}


def _nodes_of(workflow):
	"""Every node of a workflow with its edges inlined, ordered the way the author sequenced them."""
	rows = frappe.get_all(
		NODE_DT,
		filters={"workflow": workflow},
		fields=["name", *_NODE_FIELDS],
		order_by="sequence asc, creation asc",
	)
	for row in rows:
		row["edges"] = frappe.get_all(
			"CRM Workflow Edge",
			filters={"parent": row["name"], "parenttype": NODE_DT},
			fields=["from_output", "to_node"],
			order_by="idx asc",
		)
	return rows


@frappe.whitelist()
def create_campaign(workflow_name):
	"""Create a campaign as a blank Draft — the canvas opens EMPTY and the author drags the graph. No seeded
	node: a Draft is not validated and mints no Version (the on_update + validate gates only bite in a
	released state), so an empty canvas is a legal resting state, not an error to paper over with a stub."""
	doc = frappe.new_doc(DOCTYPE)
	doc.workflow_name = workflow_name  # lifecycle_state defaults to Draft (the field default) — the birth state
	doc.insert()
	return {"name": doc.name, "lifecycle_state": doc.lifecycle_state}


@frappe.whitelist()
def save_draft(name, nodes, canvas_json=None, entry_node=None):
	"""Persist the working graph + layout + Start node while authoring. Editing is a DRAFT-ONLY operation
	(canvas editable <=> Draft): a released Definition is an immutable Version — Revise it back to a Draft
	first. A Draft save mints no Version and arms nothing (the on_update gate), so Save is categorically not
	Publish. Layout (canvas_json) is not read by the freeze, so it never mints a Version even after Publish."""
	doc = frappe.get_doc(DOCTYPE, name)
	doc.check_permission("write")
	if not doc.is_editable():  # the controller owns the "editable <=> Draft" rule; the API asks, never re-decides
		frappe.throw(
			_("This workflow is {0}, not a Draft. Revise it before editing.").format(frappe.bold(doc.lifecycle_state)),
			title=_("Not editable"),
		)
	if isinstance(nodes, str):
		nodes = json.loads(nodes)
	_replace_nodes(name, nodes)
	# Writing a node refreshes the workflow's derived trigger index, so the header loaded above is now
	# stale and saving it would collide with the engine's own write. Re-read before touching it.
	doc.reload()
	if canvas_json is not None:
		doc.canvas_json = canvas_json if isinstance(canvas_json, str) else json.dumps(canvas_json)
	# Omission means "leave it alone", never "clear it". The canvas does not send `entry_node`, so an
	# unconditional assignment wiped the workflow's start node on every draft save — silently, with no UI
	# that showed it was gone. Runs then fell back to "the first node authored", skipping the Trigger.
	if entry_node:
		doc.entry_node = entry_node
	elif not doc.entry_node:
		doc.entry_node = _trigger_node_id(name)
	doc.save()  # after the nodes, so the header's derived trigger index reads the Trigger just written
	return {"name": doc.name, "lifecycle_state": doc.lifecycle_state}


def _trigger_node_id(workflow):
	"""A run begins at the Trigger. Derived rather than authored — there is exactly one, the node
	controller enforces it, and asking the author to also nominate it invites them to nominate a
	different one."""
	found = frappe.get_all(
		NODE_DT, filters={"workflow": workflow, "node_type": "Trigger"}, pluck="node_id", limit=1
	)
	return found[0] if found else None


def _replace_nodes(workflow, nodes):
	"""Make the stored graph equal the canvas. Deletes what the canvas dropped, writes the rest.

	Saved through each node's OWN document so the registry validator runs on every row — writing the
	rows directly would let the canvas persist a graph the engine would later refuse to execute, and the
	author would not learn about it until a run died.
	"""
	sent = {row.get("node_id"): row for row in nodes if row.get("node_id")}
	existing = {
		row.node_id: row.name
		for row in frappe.get_all(NODE_DT, filters={"workflow": workflow}, fields=["name", "node_id"])
	}

	for node_id, docname in existing.items():
		if node_id not in sent:
			frappe.delete_doc(NODE_DT, docname, ignore_permissions=True)  # authz-ok: tier-b — gated by save_draft's write check

	for sequence, (node_id, row) in enumerate(sent.items(), start=1):
		doc = (
			frappe.get_doc(NODE_DT, existing[node_id])
			if node_id in existing
			else frappe.new_doc(NODE_DT)
		)
		doc.workflow = workflow
		doc.node_id = node_id
		doc.node_type = row.get("node_type")
		doc.sequence = sequence
		doc.config_json = row.get("config_json") or "{}"
		doc.set("edges", [
			{"from_output": e.get("from_output"), "to_node": e.get("to_node")}
			for e in (row.get("edges") or [])
			if e.get("from_output") and e.get("to_node")
		])
		doc.save(ignore_permissions=True)  # authz-ok: tier-b — gated by save_draft's write check


def _transition(name, target):
	"""One indirection over the controller's ONE state machine: load, permission-check, advance along a legal
	edge. apply_transition refuses an illegal edge before any write and runs the gate for that edge (Publish
	validates + freezes; Activate arms). No verb here re-implements the machine."""
	doc = frappe.get_doc(DOCTYPE, name)
	doc.check_permission("write")
	state = doc.apply_transition(target)
	return {"name": doc.name, "lifecycle_state": state}


@frappe.whitelist()
def publish(name):
	"""Draft -> Published: run the whole-graph release contract, then freeze an immutable version.

	A graph that is not ready is NOT an error. It is the expected state of authoring, so the problems come
	back as DATA — `{ok: False, problems: [{node_id, field, message}]}` — and the canvas marks the nodes
	they name. Raising here would give the author a 417 and a stack trace for the ordinary act of
	publishing something unfinished, and would carry no node ids for the canvas to use.
	"""
	doc = frappe.get_doc(DOCTYPE, name)
	doc.check_permission("write")
	problems = doc.publish_problems()
	if problems:
		return {"ok": False, "problems": problems}
	return {"ok": True, **_transition(name, PUBLISHED)}


@frappe.whitelist()
def activate(name):
	"""Published/Suspended -> Active: arm the entry trigger — from now a matching event starts a new Instance."""
	return _transition(name, ACTIVE)


@frappe.whitelist()
def suspend(name):
	"""Active -> Suspended: disarm the trigger. No new Instances start; in-flight Instances keep running on
	their pinned Version, untouched."""
	return _transition(name, SUSPENDED)


@frappe.whitelist()
def revise(name):
	"""Published/Active/Suspended -> Draft: reopen for editing. The current Version keeps serving in-flight
	Instances (they are pinned to it); a new Version mints only when the Draft is Published again."""
	return _transition(name, DRAFT)


@frappe.whitelist()
def archive(name):
	"""-> Archived (terminal): retire the workflow. No new Instances ever; the frozen Versions are retained
	for audit and for any Instance still bound to them."""
	return _transition(name, ARCHIVED)
