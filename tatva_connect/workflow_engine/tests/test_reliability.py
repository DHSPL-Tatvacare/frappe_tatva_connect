# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Phase 2 - the reliability spine: buffered signals (F1), correlation idempotency (F2), the Wait timeout,
transient-vs-permanent failure (F4), the reconciler backstop (F5), the entry double-start guard, and the
payload->state->Lead map. Every test asserts OUTCOMES (Instance status, Signal rows, Lead fields, Step Log),
never that a function was called, and drives the REAL interpreter/triggers/sweeps against REAL doctypes.

`advance()` commits at the suspension boundary, so these tests CANNOT lean on FrappeTestCase's rollback for
the rows advance touches: setup is committed and each class deletes its own Instances/Signals/Leads in
teardown (the same hard-safety pattern the Phase-1 suite uses). The two engine kill switches are flipped on
for the class and restored to their prior (dormant) state in teardown - never left live behind us.
"""
import unittest
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist
from tatva_connect.tests.automation import field_allowlist
from tatva_connect.workflow_engine import (
	ENGINE_SWITCH,
	SWEEP_SWITCH,
	interpreter,
	signals,
	triggers,
	versions,
	wakeups,
)

_DEF_DT = "CRM Workflow Definition"
_NODE_DT = "CRM Workflow Node"
_VERSION_DT = "CRM Workflow Version"
_INSTANCE_DT = "CRM Workflow Instance"
_STEP_LOG_DT = "CRM Workflow Step Log"
_SIGNAL_DT = "CRM Workflow Signal"
_GROUP_DT = "CRM Action Group"
_ITEM_DT = "CRM Action Group Item"
_SWITCH_DT = "CRM Tatva Automation"
# retired: seeds now live on the catalog + contract; teardown uses field_allowlist.clear()

_GRAIN = GRAINS[2]  # TatvaPractice / India / FieldSales
_AXES = (_GRAIN["vertical"], _GRAIN["group"], _GRAIN["program"])


def _make_lead():
	return frappe.get_doc({
		"doctype": "CRM Lead", "first_name": "WF2", "lead_name": "WF2 Probe", "status": "New",
		"custom_vertical": _GRAIN["vertical"], "custom_group": _GRAIN["group"],
		"custom_current_program": _GRAIN["program"],
	}).insert(ignore_permissions=True)


def _make_group(name, items):
	return frappe.get_doc({"doctype": _GROUP_DT, "group_name": name, "actions": items}).insert(ignore_permissions=True)


def _make_workflow(name, nodes, event="Created", entry=None):
	return frappe.get_doc({
		"doctype": _DEF_DT, "workflow_name": name, "lifecycle_state": "Active",
		"vertical": _GRAIN["vertical"], "group": _GRAIN["group"], "program": _GRAIN["program"],
		"entry_doctype": "CRM Lead", "entry_event": event,
		"entry_node": entry or nodes[0]["node_id"],  # the Start node; defaults to the first node (the old convention)
		"nodes": nodes,
	}).insert(ignore_permissions=True)


def _start(workflow, subject, current_node, state=None):
	return frappe.get_doc({
		"doctype": _INSTANCE_DT, "workflow": workflow.name,
		"workflow_version": versions.current_name(workflow.name),
		"subject_doctype": "CRM Lead", "subject_name": subject,
		"current_node": current_node, "state_json": frappe.as_json(state or {}), "status": "Running",
	}).insert(ignore_permissions=True)


def _logs(instance_name):
	return frappe.get_all(
		_STEP_LOG_DT, filters={"workflow_instance": instance_name},
		fields=["node_id", "node_type", "outcome", "detail"], order_by="creation asc, name asc",
	)


def _row(name, fields):
	return frappe.db.get_value(_INSTANCE_DT, name, fields, as_dict=True)


def _cleanup(wf_prefix, ag_prefix, lead=None):
	for inst in frappe.get_all(_INSTANCE_DT, filters={"workflow": ["like", f"{wf_prefix}%"]}, pluck="name"):
		frappe.db.delete(_STEP_LOG_DT, {"workflow_instance": inst})
	frappe.db.delete(_INSTANCE_DT, {"workflow": ["like", f"{wf_prefix}%"]})
	frappe.db.delete(_VERSION_DT, {"workflow": ["like", f"{wf_prefix}%"]})
	frappe.db.delete(_NODE_DT, {"parent": ["like", f"{wf_prefix}%"]})
	frappe.db.delete(_DEF_DT, {"workflow_name": ["like", f"{wf_prefix}%"]})
	frappe.db.delete(_ITEM_DT, {"parent": ["like", f"{ag_prefix}%"]})
	frappe.db.delete(_GROUP_DT, {"group_name": ["like", f"{ag_prefix}%"]})
	if lead:
		frappe.db.delete(_SIGNAL_DT, {"subject_name": lead})
		frappe.db.delete("CRM Lead", {"name": lead})
	frappe.db.commit()


class _EngineOnCase(FrappeTestCase):
	"""Base: flip both engine switches ON for the class, restore their prior (dormant) state in teardown."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		cls._prior = {k: frappe.db.get_value(_SWITCH_DT, k, "enabled") for k in (ENGINE_SWITCH, SWEEP_SWITCH)}
		frappe.db.set_value(_SWITCH_DT, ENGINE_SWITCH, "enabled", 1)
		frappe.db.set_value(_SWITCH_DT, SWEEP_SWITCH, "enabled", 1)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		for k, v in cls._prior.items():
			frappe.db.set_value(_SWITCH_DT, k, "enabled", v or 0)
		frappe.db.commit()


class TestBufferedSignal(_EngineOnCase):
	"""F1 - a signal delivered BEFORE the Instance reaches the Wait waits in the durable inbox and is
	consumed the moment the interpreter parks there. No lost wakeup."""

	@classmethod
	def tearDownClass(cls):
		_cleanup("WF2-early", "WF2-early-ag")
		super().tearDownClass()

	def setUp(self):
		self.lead = _make_lead().name
		self.wf = _make_workflow("WF2-early", [
			{"node_id": "e1", "node_type": "Wait", "wait_mode": "Until Event", "signal_name": "early_sig",
			 "accepts_json": "{}", "on_event": "e2"},
			{"node_id": "e2", "node_type": "Terminal"},
		])
		frappe.db.commit()

	def tearDown(self):
		_cleanup("WF2-early", "WF2-early-ag", self.lead)

	def test_early_signal_is_buffered_then_consumed(self):
		# Deliver BEFORE any Instance parks -> a Pending inbox row, and resume_for_signal finds nobody.
		sig_name = signals.deliver_signal("CRM Lead", self.lead, "early_sig", correlation="c1")
		frappe.db.commit()
		self.assertEqual(frappe.db.get_value(_SIGNAL_DT, sig_name, "status"), "Pending",
			"an early delivery buffers - it must not touch any Instance")

		# Now the Instance reaches the Wait: it consumes the buffered row and runs on_event to Done.
		inst = _start(self.wf, self.lead, "e1", {"_corr": "c1"})
		frappe.db.commit()
		interpreter.advance(frappe.get_doc(_INSTANCE_DT, inst.name))

		row = _row(inst.name, ["status", "current_node"])
		self.assertEqual(row.status, "Done", "the buffered signal must resume the Wait - no lost wakeup")
		self.assertEqual(row.current_node, "e2")
		self.assertEqual(frappe.db.get_value(_SIGNAL_DT, sig_name, ["status", "consumed_by"], as_dict=True).status,
			"Consumed", "the consumed inbox row is marked Consumed")
		self.assertEqual(frappe.db.get_value(_SIGNAL_DT, sig_name, "consumed_by"), inst.name)


class TestCorrelation(_EngineOnCase):
	"""F2 - correlation makes a duplicate delivery advance exactly once and a stale delivery a no-op."""

	@classmethod
	def tearDownClass(cls):
		_cleanup("WF2-corr", "WF2-corr-ag")
		super().tearDownClass()

	def setUp(self):
		self.lead = _make_lead().name
		self.wf = _make_workflow("WF2-corr", [
			{"node_id": "c1", "node_type": "Wait", "wait_mode": "Until Event", "signal_name": "corr_sig",
			 "accepts_json": "{}", "on_event": "c2"},
			{"node_id": "c2", "node_type": "Terminal"},
		])
		frappe.db.commit()

	def tearDown(self):
		_cleanup("WF2-corr", "WF2-corr-ag", self.lead)

	def test_duplicate_same_correlation_advances_once(self):
		inst = _start(self.wf, self.lead, "c1", {"_corr": "tok"})
		frappe.db.commit()
		# Two deliveries, SAME correlation -> two Pending rows.
		signals.deliver_signal("CRM Lead", self.lead, "corr_sig", correlation="tok")
		signals.deliver_signal("CRM Lead", self.lead, "corr_sig", correlation="tok")
		frappe.db.commit()

		interpreter.advance(frappe.get_doc(_INSTANCE_DT, inst.name))

		self.assertEqual(_row(inst.name, ["status"]).status, "Done", "the wait advances")
		self.assertEqual(frappe.db.count(_SIGNAL_DT, {"subject_name": self.lead, "status": "Consumed"}), 1,
			"exactly one of the duplicate deliveries is consumed")
		self.assertEqual(frappe.db.count(_SIGNAL_DT, {"subject_name": self.lead, "status": "Pending"}), 1,
			"the second duplicate is left unconsumed - it never double-advances")
		self.assertEqual([l.node_id for l in _logs(inst.name) if l.node_id == "c2"], ["c2"],
			"on_event ran once, not twice")

	def test_stale_correlation_is_ignored(self):
		# Park on the CURRENT iteration token; a delivery carrying an OLD token must not wake it.
		inst = _start(self.wf, self.lead, "c1", {"_corr": "iter2"})
		frappe.db.commit()
		interpreter.advance(frappe.get_doc(_INSTANCE_DT, inst.name))  # parks awaiting corr_sig / iter2
		self.assertEqual(_row(inst.name, ["status"]).status, "Parked")

		signals.deliver_signal("CRM Lead", self.lead, "corr_sig", correlation="iter1")  # stale
		frappe.db.commit()

		row = _row(inst.name, ["status", "current_node"])
		self.assertEqual(row.status, "Parked", "a stale-correlation delivery must not resume the wait (F2)")
		self.assertEqual(row.current_node, "c1")
		self.assertEqual(frappe.db.get_value(_SIGNAL_DT, {"subject_name": self.lead, "correlation": "iter1"}, "status"),
			"Pending", "the stale signal is left in the inbox, never consumed")


class TestWaitTimeout(_EngineOnCase):
	"""An Event-or-Timeout Wait with no signal past its deadline leaves by on_timeout - the journey never
	hangs. Driven through the REAL timer sweep."""

	@classmethod
	def tearDownClass(cls):
		_cleanup("WF2-timeout", "WF2-timeout-ag")
		super().tearDownClass()

	def setUp(self):
		self.lead = _make_lead().name
		self.wf = _make_workflow("WF2-timeout", [
			{"node_id": "t1", "node_type": "Wait", "wait_mode": "Event-or-Timeout", "signal_name": "up_sig",
			 "wait_expression": "{'minutes': 5}", "accepts_json": "{}", "on_event": "tE", "on_timeout": "tO"},
			{"node_id": "tE", "node_type": "Terminal"},
			{"node_id": "tO", "node_type": "Terminal"},
		])
		frappe.db.commit()

	def tearDown(self):
		_cleanup("WF2-timeout", "WF2-timeout-ag", self.lead)

	def test_no_signal_past_deadline_takes_on_timeout(self):
		inst = _start(self.wf, self.lead, "t1")
		frappe.db.commit()
		interpreter.advance(frappe.get_doc(_INSTANCE_DT, inst.name))  # parks: awaiting_signal + resume_at
		parked = _row(inst.name, ["status", "awaiting_signal", "resume_at"])
		self.assertEqual(parked.status, "Parked")
		self.assertEqual(parked.awaiting_signal, "up_sig")
		self.assertIsNotNone(parked.resume_at)

		# Back-date the deadline and run the timer sweep - no signal, so the clock wins.
		frappe.db.set_value(_INSTANCE_DT, inst.name, "resume_at", frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-1))
		frappe.db.commit()
		wakeups.timer_sweep()

		row = _row(inst.name, ["status", "current_node"])
		self.assertEqual(row.status, "Done")
		self.assertEqual(row.current_node, "tO", "the deadline routes to on_timeout, not on_event")
		self.assertIn(("t1", "timeout"), [(l.node_id, l.detail) for l in _logs(inst.name)])


class TestFailureClasses(_EngineOnCase):
	"""F4 - a transient DB error leaves the Instance Parked and bumps retry_count (never Failed); a
	permanent error goes Failed with no retry."""

	@classmethod
	def tearDownClass(cls):
		_cleanup("WF2-fail", "WF2-fail-ag")
		super().tearDownClass()

	def setUp(self):
		self.lead = _make_lead().name
		self.group = _make_group("WF2-fail-ag", [
			{"action_type": "Update Field", "target_doctype": "CRM Lead", "fieldname": "custom_patient_age",
			 "value_mode": "Literal", "value": "40"},
		])
		self.transient_wf = _make_workflow("WF2-fail-transient", [
			{"node_id": "d1", "node_type": "Wait", "wait_mode": "For Duration", "wait_expression": "{'minutes': 5}", "next_node": "d2"},
			{"node_id": "d2", "node_type": "Step", "action_group": self.group.name, "next_node": "d3"},
			{"node_id": "d3", "node_type": "Terminal"},
		])
		self.permanent_wf = _make_workflow("WF2-fail-permanent", [
			{"node_id": "p1", "node_type": "Assign", "assign_json": "[1, 2, 3]", "next_node": "p2"},
			{"node_id": "p2", "node_type": "Terminal"},
		])
		frappe.db.commit()

	def tearDown(self):
		_cleanup("WF2-fail", "WF2-fail-ag", self.lead)

	def test_transient_error_stays_parked_and_retries(self):
		inst = _start(self.transient_wf, self.lead, "d1")
		frappe.db.commit()
		interpreter.advance(frappe.get_doc(_INSTANCE_DT, inst.name))  # parks at d1
		frappe.db.set_value(_INSTANCE_DT, inst.name, "resume_at", frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-1))
		frappe.db.commit()

		# On wake, the next Step hits a real lock-wait/deadlock (frappe.QueryDeadlockError) - the TRANSIENT
		# class the interpreter must retry (the same class webhooks/spine.py + api/_base.py classify).
		with mock.patch.object(interpreter, "_run_step", side_effect=frappe.QueryDeadlockError("simulated deadlock")):
			interpreter.advance(frappe.get_doc(_INSTANCE_DT, inst.name))

		row = _row(inst.name, ["status", "current_node", "retry_count"])
		self.assertEqual(row.status, "Parked", "a transient error must leave the Instance Parked, never Failed (F4)")
		self.assertEqual(row.current_node, "d1", "the segment rolls back to the last durable park")
		self.assertEqual(row.retry_count, 1, "the transient retry counter is bumped")

	def test_permanent_error_fails_with_no_retry(self):
		inst = _start(self.permanent_wf, self.lead, "p1")
		frappe.db.commit()
		interpreter.advance(frappe.get_doc(_INSTANCE_DT, inst.name))  # Assign evaluates to a list -> _Permanent

		row = _row(inst.name, ["status", "current_node", "retry_count", "active_key"])
		self.assertEqual(row.status, "Failed", "a bad-config error is terminal")
		self.assertEqual(row.retry_count, 0, "a permanent error never retries - no storm")
		self.assertIsNone(row.active_key, "a Failed Instance drops its active_key")
		self.assertIn("failed", [l.outcome for l in _logs(inst.name)])


class TestReconciler(_EngineOnCase):
	"""F5 - the reconciler re-drives a Parked Instance from durable state when its wake was lost: (a) a due
	timer never swept, and (b) a buffered signal whose enqueue never ran."""

	@classmethod
	def tearDownClass(cls):
		_cleanup("WF2-recon", "WF2-recon-ag")
		super().tearDownClass()

	def setUp(self):
		self.lead = _make_lead().name
		self.timer_wf = _make_workflow("WF2-recon-timer", [
			{"node_id": "r1", "node_type": "Wait", "wait_mode": "For Duration", "wait_expression": "{'minutes': 5}", "next_node": "r2"},
			{"node_id": "r2", "node_type": "Terminal"},
		])
		self.signal_wf = _make_workflow("WF2-recon-signal", [
			{"node_id": "s1", "node_type": "Wait", "wait_mode": "Until Event", "signal_name": "recon_sig",
			 "accepts_json": "{}", "on_event": "s2"},
			{"node_id": "s2", "node_type": "Terminal"},
		])
		frappe.db.commit()

	def tearDown(self):
		_cleanup("WF2-recon", "WF2-recon-ag", self.lead)

	def test_due_timer_with_no_enqueue_is_recovered(self):
		inst = _start(self.timer_wf, self.lead, "r1")
		frappe.db.commit()
		interpreter.advance(frappe.get_doc(_INSTANCE_DT, inst.name))  # parks at r1
		frappe.db.set_value(_INSTANCE_DT, inst.name, "resume_at", frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-1))
		frappe.db.commit()

		wakeups.reconciler_sweep()  # no wake job was ever enqueued - the reconciler drives it from state

		self.assertEqual(_row(inst.name, ["status"]).status, "Done", "the reconciler re-drives a due Parked timer (F5)")

	def test_buffered_signal_with_no_enqueue_is_recovered(self):
		inst = _start(self.signal_wf, self.lead, "s1")
		frappe.db.commit()
		interpreter.advance(frappe.get_doc(_INSTANCE_DT, inst.name))  # parks awaiting recon_sig

		# Insert the inbox row DIRECTLY (bypassing deliver_signal's enqueue) - the wake is lost.
		frappe.get_doc({
			"doctype": _SIGNAL_DT, "subject_doctype": "CRM Lead", "subject_name": self.lead,
			"signal_name": "recon_sig", "correlation": None, "payload_json": "{}", "status": "Pending",
		}).insert(ignore_permissions=True)
		frappe.db.commit()

		wakeups.reconciler_sweep()  # the reconciler notices the buffered signal and drives it

		self.assertEqual(_row(inst.name, ["status"]).status, "Done", "the reconciler consumes a buffered signal (F5)")
		self.assertEqual(frappe.db.get_value(_SIGNAL_DT, {"subject_name": self.lead, "signal_name": "recon_sig"}, "status"),
			"Consumed")


class TestEntryDoubleStart(_EngineOnCase):
	"""The entry trigger creates at most one live Instance per (workflow, subject) - the second entry event
	is rejected by the active_key UNIQUE index, caught, and treated as already-running."""

	@classmethod
	def tearDownClass(cls):
		_cleanup("WF2-entry", "WF2-entry-ag")
		super().tearDownClass()

	def setUp(self):
		# Suppress the auto entry-hook while inserting the lead, so this test drives on_created explicitly.
		self.wf = _make_workflow("WF2-entry", [
			{"node_id": "en1", "node_type": "Wait", "wait_mode": "Until Event", "signal_name": "entry_sig",
			 "accepts_json": "{}", "on_event": "en2"},
			{"node_id": "en2", "node_type": "Terminal"},
		])
		frappe.db.commit()
		frappe.flags.in_workflow = True
		self.lead_doc = _make_lead()
		frappe.flags.in_workflow = False
		frappe.db.commit()
		self.lead = self.lead_doc.name

	def tearDown(self):
		_cleanup("WF2-entry", "WF2-entry-ag", self.lead)

	def test_second_entry_event_is_rejected_by_unique_index(self):
		triggers.on_created(self.lead_doc)  # Instance #1 - parks at the Wait
		triggers.on_created(self.lead_doc)  # duplicate entry - rejected at the DB, treated as already-running

		instances = frappe.get_all(_INSTANCE_DT, filters={"workflow": self.wf.name, "subject_name": self.lead}, fields=["name", "status"])
		self.assertEqual(len(instances), 1, "the active_key UNIQUE index admits exactly one live Instance")
		self.assertEqual(instances[0].status, "Parked", "the one Instance parked at its Wait")


class TestPayloadMap(_EngineOnCase):
	"""The accepts map walks declared payload paths into state; the on_event Step then writes a Lead field
	from that state via Update Field (From Context). payload -> state -> Lead, both hops declared."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.set_row = field_allowlist.seed_settable("CRM Lead", "custom_patient_age", *_AXES)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		_cleanup("WF2-map", "WF2-map-ag")
		field_allowlist.clear()
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		self.lead = _make_lead().name
		self.group = _make_group("WF2-map-ag", [
			{"action_type": "Update Field", "target_doctype": "CRM Lead", "fieldname": "custom_patient_age",
			 "value_mode": "From Context", "context_field": "age"},
		])
		self.wf = _make_workflow("WF2-map", [
			{"node_id": "m1", "node_type": "Wait", "wait_mode": "Until Event", "signal_name": "map_sig",
			 "accepts_json": '{"data.x": "age"}', "on_event": "m2"},
			{"node_id": "m2", "node_type": "Step", "action_group": self.group.name, "next_node": "m3"},
			{"node_id": "m3", "node_type": "Terminal"},
		])
		frappe.db.commit()

	def tearDown(self):
		_cleanup("WF2-map", "WF2-map-ag", self.lead)

	def test_declared_payload_path_maps_into_state_and_onto_the_lead(self):
		inst = _start(self.wf, self.lead, "m1")
		frappe.db.commit()
		signals.deliver_signal("CRM Lead", self.lead, "map_sig", payload={"data": {"x": 47}, "ignored": "drop me"})
		frappe.db.commit()

		interpreter.advance(frappe.get_doc(_INSTANCE_DT, inst.name))

		row = _row(inst.name, ["status", "state_json"])
		self.assertEqual(row.status, "Done")
		state = frappe.parse_json(row.state_json)
		self.assertEqual(state.get("age"), 47, "the declared path data.x maps into state key 'age'")
		self.assertNotIn("ignored", state, "an undeclared payload key never enters state")
		self.assertEqual(
			frappe.utils.cint(frappe.db.get_value("CRM Lead", self.lead, "custom_patient_age")), 47,
			"the on_event Step wrote the mapped value onto the Lead via Update Field (From Context)",
		)


if __name__ == "__main__":
	unittest.main()
