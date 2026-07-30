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
JOURNEY_DT = "CRM Workflow Journey"
SIGNAL_DT = "CRM Workflow Signal"
VERSION_DT = "CRM Workflow Version"
STEP_LOG_DT = "CRM Workflow Step Log"

GRAIN = GRAINS[2]  # Tatvapractice / India / Field-Sales
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


def make_lead(**overrides):
	"""A grain-stamped probe lead. `overrides` set fields AT INSERT, never after.

	A suite that needs its leads distinguishable (a cohort selecting only its own) must pass the value in
	here rather than `db.set_value` it afterwards: a criteria predicate reads through `frappe.get_doc`,
	which can answer from the document cache that a bare column write never invalidated.
	"""
	return frappe.get_doc({
		"doctype": "CRM Lead", "first_name": "WF", "lead_name": "WF Probe", "status": "New",
		"custom_vertical": GRAIN["vertical"], "custom_group": GRAIN["group"],
		"custom_current_program": GRAIN["program"],
		**overrides,
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


def start_journey(workflow, subject, current_node, state=None):
	"""A journey positioned at a node, as the trigger lane would have created it."""
	from tatva_connect.workflow_engine import versions

	run = frappe.get_doc({
		"doctype": JOURNEY_DT, "workflow": workflow.name,
		"workflow_version": versions.current_name(workflow.name),
		"subject_doctype": "CRM Lead", "subject_name": subject,
		"current_node": current_node, "state_json": frappe.as_json(state or {}), "status": "Running",
	}).insert(ignore_permissions=True)
	# Committed on purpose: the entry segment refuses to retry a journey that is not yet durable.
	frappe.db.commit()
	return run


def logs(journey_name):
	return frappe.get_all(
		STEP_LOG_DT, filters={"journey": journey_name},
		fields=["node_id", "node_type", "outcome", "detail"], order_by="creation asc, name asc",
	)


def purge(*workflow_names):
	"""Remove a workflow and everything hanging off it. Runs commit, so a rollback cannot undo them."""
	for name in workflow_names:
		for run in frappe.get_all(JOURNEY_DT, filters={"workflow": name}, pluck="name"):
			frappe.db.delete(STEP_LOG_DT, {"journey": run})
		frappe.db.delete(JOURNEY_DT, {"workflow": name})
		frappe.db.delete(VERSION_DT, {"workflow": name})
		for node_name in frappe.get_all(NODE_DT, filters={"workflow": name}, pluck="name"):
			frappe.delete_doc(NODE_DT, node_name, force=True, ignore_permissions=True)
		if frappe.db.exists(WORKFLOW_DT, name):
			frappe.delete_doc(WORKFLOW_DT, name, force=True, ignore_permissions=True)
	frappe.db.commit()


def _set_engine(enabled):
	frappe.db.set_value("CRM Tatva Automation", ENGINE_SWITCH, "enabled", 1 if enabled else 0)
	frappe.db.commit()


def arm_engine(enabled=True, cls=None):
	"""Arm or disarm the engine switch, returning what it was.

	PASS THE TEST CLASS. The restore is then REGISTERED rather than remembered: `addClassCleanup` runs
	even when `setUpClass` raises after this call, which is the one door the old
	save-it-in-an-attribute shape did not cover — `unittest` skips `tearDownClass` entirely when
	`setUpClass` raises.

	That gap did not merely leak one flag, it made the leak self-propagating. An aborted setUpClass left
	the engine ON; every later suite then read `was = 1`, recorded it as "the original value" and
	faithfully restored the engine to ON at the end, reporting a clean teardown. The bench drifted to
	armed with nothing anywhere going red, and "no switches left on" stopped being falsifiable — which
	matters most on the day a live trial sends to a real phone.

	The restore goes to OFF, never to "whatever it was", and that is the second half of the fix. Restoring
	the previous value is what PROPAGATES a poisoned baseline: one suite leaves it ON, the next reads
	`was = 1`, calls that the original and puts it back ON, for ever. The engine is dormant by default
	(B10), so OFF is the only correct resting state of a bench and there is nothing else to remember.

	Arming a bench that is ALREADY armed therefore raises. A suite that finds the switch on has found a
	leak — its own baseline is untrustworthy and so is every "no switches left on" claim made after it.
	Loud beats inherited.

	The `cls`-less form remains for a caller disarming and re-arming inside its own try/finally within a
	single test, where the class-level arm above it already owns the restore.
	"""
	was = frappe.db.get_value("CRM Tatva Automation", ENGINE_SWITCH, "enabled")
	if cls is not None:
		if enabled and was:
			raise AssertionError(
				f"{ENGINE_SWITCH} was already ON before {cls.__name__} armed it. A suite left it behind, so "
				"this bench's baseline cannot be trusted - disarm it and find what leaked before relying on "
				"any journey that follows."
			)
		# Registered BEFORE the write, so an abort between the two still disarms.
		cls.addClassCleanup(_set_engine, False)
	_set_engine(enabled)
	return was
