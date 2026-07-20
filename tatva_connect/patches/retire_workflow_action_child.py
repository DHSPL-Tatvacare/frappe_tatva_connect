"""Retire the `CRM Workflow Action` child table — a node IS a verb now.

W4 made each effect verb its own node type: the node's `node_type` names what it does and its
`config_json` holds exactly that verb's declared parameters. The child table that used to hang a list of
actions off a generic `Step` node has no reader left, and the `actions` column is gone from
`CRM Workflow Node`'s JSON — which Frappe does not drop on its own.

Declared end state: the doctype and its table are gone. Nothing is migrated out of it. There is no
production data behind this engine and the graphs that exist are development fixtures; a Step-shaped
node authored before W4 does not resolve to a verb and would fail at execution either way, so carrying
its actions forward would preserve a shape the engine can no longer run.

Idempotent, and assumes nothing about what ran before.
"""
import frappe

from tatva_connect.patches import _schema

ACTION_DT = "CRM Workflow Action"
ACTION_TABLE = "tabCRM Workflow Action"


def execute():
	_drop_step_nodes()
	_drop_action_doctype()


def _drop_step_nodes():
	"""A `Step` node names no verb, so the interpreter has nothing to run at it. Removing it leaves a
	graph with a gap rather than one that dies mid-run — and the node validator refuses to save a Step
	from here on, so the gap is visible the next time the graph is opened."""
	if not frappe.db.exists("DocType", "CRM Workflow Node"):
		return
	for name in frappe.get_all("CRM Workflow Node", filters={"node_type": "Step"}, pluck="name"):
		frappe.delete_doc("CRM Workflow Node", name, force=True, ignore_permissions=True)  # authz-ok: tier-c — patch, no user input


def _drop_action_doctype():
	if frappe.db.exists("DocType", ACTION_DT):
		frappe.delete_doc("DocType", ACTION_DT, force=True, ignore_permissions=True)  # authz-ok: tier-c — patch, no user input
	# delete_doc removes the DocType row, never the table — the drop is ours, through the one door.
	if frappe.db.table_exists(ACTION_DT):
		_schema.ddl(f"DROP TABLE IF EXISTS `{ACTION_TABLE}`", ACTION_TABLE)
