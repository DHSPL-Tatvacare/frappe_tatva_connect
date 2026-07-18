# The Campaigns canvas API — the ONLY backend the CRM Campaigns SPA calls. Mirrors near_me/api.py and
# smartview/api.py: the fork frontend dispatches here, backend logic lives in tatva_connect. Load / create
# / save a CRM Workflow Definition; validate() + the version freeze fire on save exactly as from Desk.
import json

import frappe

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
	"""Load a campaign for the canvas — header fields + the nodes graph + the canvas_json layout."""
	doc = frappe.get_doc(DOCTYPE, name)
	doc.check_permission("read")
	return doc.as_dict()


@frappe.whitelist()
def create_campaign(workflow_name):
	"""Create a campaign. validate() rejects an empty graph, so seed the minimal valid flow — one Terminal."""
	doc = frappe.new_doc(DOCTYPE)
	doc.workflow_name = workflow_name
	doc.append("nodes", {"node_id": "end", "node_type": "Terminal"})
	doc.insert()
	return {"name": doc.name}


@frappe.whitelist()
def save_campaign(name, nodes, canvas_json=None):
	"""Replace the graph + layout and save. validate() + the immutable version freeze run on save; layout
	(canvas_json) is not read by the freeze, so moving a node never mints a new Version."""
	if isinstance(nodes, str):
		nodes = json.loads(nodes)
	clean = [
		{k: row.get(k) for k in _NODE_FIELDS if row.get(k) not in (None, "")}
		for row in nodes
	]
	doc = frappe.get_doc(DOCTYPE, name)
	doc.check_permission("write")
	doc.set("nodes", clean)
	if canvas_json is not None:
		doc.canvas_json = canvas_json if isinstance(canvas_json, str) else json.dumps(canvas_json)
	doc.save()
	return {"name": doc.name}
