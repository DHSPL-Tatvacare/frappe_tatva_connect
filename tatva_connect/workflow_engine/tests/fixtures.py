# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Graph fixtures for the engine suites — the ONE place a test knows how the model is shaped.

Every suite built its own graphs inline, so the model's shape was written out six times and changing it
meant editing six files. It is built here once: a suite says what graph it wants, not how a node is
stored.

`node()` mirrors the registry's contract on purpose — a type, its `config`, its named `edges` and its
`actions`. A test that writes an output the type does not declare fails at save, which is the
validator doing its job rather than the fixture routing around it.
"""
import frappe

from tatva_connect.tests.authz.grains import GRAINS
from tatva_connect.workflow_engine import ENGINE_SWITCH

WORKFLOW_DT = "CRM Workflow"
NODE_DT = "CRM Workflow Node"
RUN_DT = "CRM Workflow Run"
EVENT_DT = "CRM Workflow Event"
VERSION_DT = "CRM Workflow Version"
STEP_LOG_DT = "CRM Workflow Step Log"

GRAIN = GRAINS[2]  # TatvaPractice / India / FieldSales
AXES = (GRAIN["vertical"], GRAIN["group"], GRAIN["program"])


def node(node_id, node_type, config=None, edges=None):
	"""One node, as a test describes it. `edges` is {output: target_node_id}.

	No actions: a node IS a verb, so what it does is its `node_type` and how it does it is its `config`.
	"""
	return {
		"node_id": node_id,
		"node_type": node_type,
		"config": config or {},
		"edges": edges or {},
	}


def trigger(node_id="start", to="n1", subject_doctype="CRM Lead", event="Created",
            predicate=None, grain=True):
	"""The Trigger node — subject, event, grain and predicate, all declared here.

	Grain lives ON the trigger now; the workflow header only carries derived copies for dispatch. Pass
	`grain=False` for a workflow that applies to every grain, which is what a blank axis means.
	"""
	config = {"subject_doctype": subject_doctype, "event": event}
	if grain:
		config.update({"vertical": GRAIN["vertical"], "group": GRAIN["group"], "program": GRAIN["program"]})
	if predicate is not None:
		config["predicate"] = predicate
	return node(node_id, "Trigger", config=config, edges={"next": to})


def make_lead():
	return frappe.get_doc({
		"doctype": "CRM Lead", "first_name": "WF", "lead_name": "WF Probe", "status": "New",
		"custom_vertical": GRAIN["vertical"], "custom_group": GRAIN["group"],
		"custom_current_program": GRAIN["program"],
	}).insert(ignore_permissions=True)


def make_workflow(name, nodes, entry=None, lifecycle_state="Active"):
	"""Create a workflow and its nodes. Returns the workflow doc.

	The workflow is inserted FIRST as a Draft: a node needs its workflow to exist to link to, and the
	lifecycle refuses Active until a Trigger node is present. The state is applied at the end, which is
	also what an author's own sequence looks like.
	"""
	# No grain here: it is declared on the Trigger node and derived onto the header on save.
	workflow = frappe.get_doc({
		"doctype": WORKFLOW_DT, "workflow_name": name, "lifecycle_state": "Draft",
		"entry_node": entry or nodes[0]["node_id"],
	}).insert(ignore_permissions=True)

	for sequence, spec in enumerate(nodes, start=1):
		frappe.get_doc({
			"doctype": NODE_DT,
			"workflow": workflow.name,
			"node_id": spec["node_id"],
			"node_type": spec["node_type"],
			"sequence": sequence,
			"config_json": frappe.as_json(spec.get("config") or {}),
			"edges": [{"from_output": out, "to_node": to} for out, to in (spec.get("edges") or {}).items()],
			"actions": [
				{"verb": a["verb"], "params_json": frappe.as_json({k: v for k, v in a.items() if k != "verb"})}
				for a in (spec.get("actions") or [])
			],
		}).insert(ignore_permissions=True)

	workflow.reload()
	if lifecycle_state != "Draft":
		workflow.lifecycle_state = lifecycle_state
		workflow.save(ignore_permissions=True)
	return workflow


def start_run(workflow, subject, current_node, state=None):
	"""A Run positioned at a node, as the trigger lane would have created it."""
	from tatva_connect.workflow_engine import versions

	run = frappe.get_doc({
		"doctype": RUN_DT, "workflow": workflow.name,
		"workflow_version": versions.current_name(workflow.name),
		"subject_doctype": "CRM Lead", "subject_name": subject,
		"current_node": current_node, "state_json": frappe.as_json(state or {}), "status": "Running",
	}).insert(ignore_permissions=True)
	# Committed on purpose: the entry segment refuses to retry a Run that is not yet durable.
	frappe.db.commit()
	return run


def logs(run_name):
	return frappe.get_all(
		STEP_LOG_DT, filters={"workflow_run": run_name},
		fields=["node_id", "node_type", "outcome", "detail"], order_by="creation asc, name asc",
	)


def purge(*workflow_names):
	"""Remove a workflow and everything hanging off it. Runs commit, so a rollback cannot undo them."""
	for name in workflow_names:
		for run in frappe.get_all(RUN_DT, filters={"workflow": name}, pluck="name"):
			frappe.db.delete(STEP_LOG_DT, {"workflow_run": run})
		frappe.db.delete(RUN_DT, {"workflow": name})
		frappe.db.delete(VERSION_DT, {"workflow": name})
		for node_name in frappe.get_all(NODE_DT, filters={"workflow": name}, pluck="name"):
			frappe.delete_doc(NODE_DT, node_name, force=True, ignore_permissions=True)
		if frappe.db.exists(WORKFLOW_DT, name):
			frappe.delete_doc(WORKFLOW_DT, name, force=True, ignore_permissions=True)
	frappe.db.commit()


def arm_engine(enabled=True):
	"""Arm or disarm the engine switch, returning what it was so the caller can put it back.

	The engine is dormant by default, so a behavioural suite has to arm it deliberately — and a suite
	that forgets to restore it leaves a live config change behind on the bench, which is how one test
	once broke an unrelated API suite.
	"""
	was = frappe.db.get_value("CRM Tatva Automation", ENGINE_SWITCH, "enabled")
	frappe.db.set_value("CRM Tatva Automation", ENGINE_SWITCH, "enabled", 1 if enabled else 0)
	frappe.db.commit()
	return was
