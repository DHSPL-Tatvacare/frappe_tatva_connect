# The Campaigns canvas API — the ONLY backend the CRM Campaigns SPA calls. Mirrors near_me/api.py and
# smartview/api.py: the fork frontend dispatches here, backend logic lives in tatva_connect. It exposes two
# things and nothing else: authoring a DRAFT graph, and moving a Definition along its lifecycle. The
# lifecycle state machine itself lives on the controller (crm_workflow_definition.apply_transition) - these
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

from tatva_connect.tatva_connect.doctype.crm_workflow_definition.crm_workflow_definition import (
	ACTIVE,
	ARCHIVED,
	DRAFT,
	PUBLISHED,
	SUSPENDED,
)

DOCTYPE = "CRM Workflow Definition"

# The only node columns the canvas authors; everything else on a row is internal. Cleaning to this set
# keeps the child-table replacement honest and drops stale name/parent/idx from the wire.
_NODE_FIELDS = (
	"node_id",
	"node_type",
	"action_group",
	"condition",
	"assign_json",
	"wait_mode",
	"wait_expression",
	"signal_name",
	"accepts_json",
	"next_node",
	"on_true",
	"on_false",
	"on_event",
	"on_timeout",
)


@frappe.whitelist()
def get_campaign(name):
	"""Load a campaign for the canvas — header (incl. lifecycle_state + entry_node) + the nodes graph + the
	canvas_json layout. Permission-gated read (Sales Manager sees it read-only; the state decides editability
	on the client, the server re-checks on every write)."""
	doc = frappe.get_doc(DOCTYPE, name)
	doc.check_permission("read")
	return doc.as_dict()


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
	clean = [
		{k: row.get(k) for k in _NODE_FIELDS if row.get(k) not in (None, "")}
		for row in nodes
	]
	doc.set("nodes", clean)
	if canvas_json is not None:
		doc.canvas_json = canvas_json if isinstance(canvas_json, str) else json.dumps(canvas_json)
	doc.entry_node = entry_node or None
	doc.save()
	return {"name": doc.name, "lifecycle_state": doc.lifecycle_state}


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
	"""Draft -> Published: run the full fail-closed release contract (entry set, reachable Terminal, no dead
	nodes, edges resolve, expressions parse, delays positive) and freeze an immutable Version."""
	return _transition(name, PUBLISHED)


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
