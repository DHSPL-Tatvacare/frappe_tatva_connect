# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Phase 1 substrate: the interpreter walks a real frozen graph, parks at a Wait, resumes, branches,
mutates state, and fails closed - asserting OUTCOMES (Instance state, Step Log rows), never that a
function was called. Drives the REAL doctypes and the REAL interpreter; nothing about the engine is
mocked.

`advance()` genuinely commits at the suspension boundary and rolls back on a mid-segment failure (F3), so
these tests CANNOT rely on FrappeTestCase's transaction rollback for the rows advance touches: setup is
committed and each class deletes its own rows in teardown (the same hard-safety pattern the automation
resume suite uses). Author-time validation tests never commit - a rejected save throws in validate before
any insert, so FrappeTestCase rolls them back cleanly.
"""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist
from tatva_connect.tests.automation import field_allowlist
from tatva_connect.workflow_engine import interpreter, versions

_DEF_DT = "CRM Workflow Definition"
_NODE_DT = "CRM Workflow Node"
_VERSION_DT = "CRM Workflow Version"
_INSTANCE_DT = "CRM Workflow Instance"
_STEP_LOG_DT = "CRM Workflow Step Log"
_GROUP_DT = "CRM Action Group"
_ITEM_DT = "CRM Action Group Item"
_FIELD = field_allowlist.DOCTYPE

_GRAIN = GRAINS[2]  # TatvaPractice / India / FieldSales
_AXES = (_GRAIN["vertical"], _GRAIN["group"], _GRAIN["program"])


def _make_lead():
	return frappe.get_doc({
		"doctype": "CRM Lead", "first_name": "WF", "lead_name": "WF Probe", "status": "New",
		"custom_vertical": _GRAIN["vertical"], "custom_group": _GRAIN["group"],
		"custom_current_program": _GRAIN["program"],
	}).insert(ignore_permissions=True)


def _make_group(name, items):
	return frappe.get_doc({"doctype": _GROUP_DT, "group_name": name, "actions": items}).insert(ignore_permissions=True)


def _make_workflow(name, nodes):
	return frappe.get_doc({
		"doctype": _DEF_DT, "workflow_name": name, "enabled": 1,
		"vertical": _GRAIN["vertical"], "group": _GRAIN["group"], "program": _GRAIN["program"],
		"entry_doctype": "CRM Lead", "entry_event": "Created",
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
		frappe.db.delete("CRM Lead", {"name": lead})
	frappe.db.commit()


class TestWalkAndPark(FrappeTestCase):
	"""Step -> Branch -> Assign -> Wait[For Duration]: the segment runs the Step (a real Update Field on
	the Lead), routes the Branch, mutates state at the Assign, and PARKS at the Wait — committing once at
	the boundary. Then a back-dated resume drives it through the Terminal to Done."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		cls.set_row = field_allowlist.seed_settable("CRM Lead", "custom_last_report_date", *_AXES)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		_cleanup("WFI-walk", "WFI-walk-ag")
		frappe.db.delete(_FIELD, {"name": cls.set_row})
		frappe.db.commit()

	def setUp(self):
		self.lead = _make_lead().name
		self.group = _make_group("WFI-walk-ag", [
			{"action_type": "Update Field", "target_doctype": "CRM Lead", "fieldname": "custom_last_report_date",
			 "value_mode": "Literal", "value": "2027-01-01"},
		])
		self.wf = _make_workflow("WFI-walk", [
			{"node_id": "n1", "node_type": "Step", "action_group": self.group.name, "next_node": "n2"},
			{"node_id": "n2", "node_type": "Branch", "condition": "ctx.get('cycle', 0) == 0", "on_true": "n3", "on_false": "nErr"},
			{"node_id": "n3", "node_type": "Assign", "assign_json": "{'cycle': ctx.get('cycle', 0) + 1}", "next_node": "n4"},
			{"node_id": "n4", "node_type": "Wait", "wait_mode": "For Duration", "wait_expression": "{'minutes': 5}", "next_node": "n5"},
			{"node_id": "n5", "node_type": "Terminal"},
			{"node_id": "nErr", "node_type": "Terminal"},
		])
		frappe.db.commit()

	def tearDown(self):
		_cleanup("WFI-walk", "WFI-walk-ag", self.lead)

	def test_walks_to_the_wait_parks_then_resumes_to_done(self):
		inst = _start(self.wf, self.lead, "n1")
		frappe.db.commit()

		interpreter.advance(frappe.get_doc(_INSTANCE_DT, inst.name))

		row = frappe.db.get_value(
			_INSTANCE_DT, inst.name, ["status", "current_node", "state_json", "resume_at", "active_key"], as_dict=True
		)
		self.assertEqual(row.status, "Parked", "the segment must park at the Wait")
		self.assertEqual(row.current_node, "n4", "it must be parked AT the Wait node")
		self.assertEqual(frappe.parse_json(row.state_json).get("cycle"), 1, "the Assign must have mutated state")
		self.assertIsNotNone(row.resume_at)
		self.assertGreater(frappe.utils.get_datetime(row.resume_at), frappe.utils.now_datetime(), "resume_at must be in the future")
		self.assertTrue(row.active_key, "a live Instance carries an active_key")

		self.assertEqual(
			frappe.utils.getdate(frappe.db.get_value("CRM Lead", self.lead, "custom_last_report_date")),
			frappe.utils.getdate("2027-01-01"),
			"the Step's Update Field must have run",
		)

		walked = _logs(inst.name)
		self.assertEqual(
			[(l.node_id, l.outcome) for l in walked],
			[("n1", "ok"), ("n2", "ok"), ("n3", "ok"), ("n4", "parked")],
			"Step Log must record the walked segment ending in a park",
		)

		# Resume: back-date the deadline and re-drive — the Wait wakes and runs to the Terminal.
		frappe.db.set_value(_INSTANCE_DT, inst.name, "resume_at", frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-1))
		frappe.db.commit()
		interpreter.advance(frappe.get_doc(_INSTANCE_DT, inst.name))

		done = frappe.db.get_value(_INSTANCE_DT, inst.name, ["status", "current_node", "active_key"], as_dict=True)
		self.assertEqual(done.status, "Done")
		self.assertEqual(done.current_node, "n5")
		self.assertIsNone(done.active_key, "a terminal Instance drops its active_key")
		outcomes = [(l.node_id, l.outcome) for l in _logs(inst.name)]
		self.assertIn(("n4", "resumed"), outcomes, "the Wait must log a resume on wake")
		self.assertIn(("n5", "done"), outcomes, "the Terminal must log done")


class TestBranchAndAssign(FrappeTestCase):
	"""A Branch routes on ctx BOTH ways (two Instances, one True one False); an Assign mutates state."""

	@classmethod
	def tearDownClass(cls):
		_cleanup("WFI-branch", "WFI-branch-ag")
		frappe.db.commit()

	def setUp(self):
		self.lead = _make_lead().name
		self.branch = _make_workflow("WFI-branch", [
			{"node_id": "b1", "node_type": "Branch", "condition": "ctx.get('cycle', 0) == 0", "on_true": "tA", "on_false": "tB"},
			{"node_id": "tA", "node_type": "Terminal"},
			{"node_id": "tB", "node_type": "Terminal"},
		])
		self.assign = _make_workflow("WFI-branch-assign", [
			{"node_id": "a1", "node_type": "Assign", "assign_json": "{'cycle': ctx.get('cycle', 0) + 1}", "next_node": "aT"},
			{"node_id": "aT", "node_type": "Terminal"},
		])
		frappe.db.commit()

	def tearDown(self):
		_cleanup("WFI-branch", "WFI-branch-ag", self.lead)

	def test_branch_routes_true(self):
		inst = _start(self.branch, self.lead, "b1", {})
		frappe.db.commit()
		interpreter.advance(frappe.get_doc(_INSTANCE_DT, inst.name))
		self.assertEqual(frappe.db.get_value(_INSTANCE_DT, inst.name, "current_node"), "tA", "cycle==0 must route On True")
		self.assertEqual(frappe.db.get_value(_INSTANCE_DT, inst.name, "status"), "Done")
		self.assertIn(("b1", "on_true"), [(l.node_id, l.detail) for l in _logs(inst.name)])

	def test_branch_routes_false(self):
		inst = _start(self.branch, self.lead, "b1", {"cycle": 3})
		frappe.db.commit()
		interpreter.advance(frappe.get_doc(_INSTANCE_DT, inst.name))
		self.assertEqual(frappe.db.get_value(_INSTANCE_DT, inst.name, "current_node"), "tB", "cycle!=0 must route On False")
		self.assertIn(("b1", "on_false"), [(l.node_id, l.detail) for l in _logs(inst.name)])

	def test_assign_mutates_state(self):
		inst = _start(self.assign, self.lead, "a1", {"cycle": 4})
		frappe.db.commit()
		interpreter.advance(frappe.get_doc(_INSTANCE_DT, inst.name))
		row = frappe.db.get_value(_INSTANCE_DT, inst.name, ["status", "state_json"], as_dict=True)
		self.assertEqual(row.status, "Done")
		self.assertEqual(frappe.parse_json(row.state_json).get("cycle"), 5, "the Assign must increment cycle 4 -> 5")


class TestMidSegmentRollback(FrappeTestCase):
	"""A mid-segment exception rolls the WHOLE segment back to the last durable state — the Instance is
	NOT left Running (F3), and the effect that ran before the failing one is undone. A Step whose group is
	[Update Field (allowlisted, ok), Update Field (NOT allowlisted -> raises PermissionError)] fails: the
	first write must not persist, the Instance must read Failed, and the cursor must not advance."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		cls.set_row = field_allowlist.seed_settable("CRM Lead", "custom_last_report_date", *_AXES)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		_cleanup("WFI-crash", "WFI-crash-ag")
		frappe.db.delete(_FIELD, {"name": cls.set_row})
		frappe.db.commit()

	def setUp(self):
		self.lead = _make_lead().name
		self.group = _make_group("WFI-crash-ag", [
			{"action_type": "Update Field", "target_doctype": "CRM Lead", "fieldname": "custom_last_report_date",
			 "value_mode": "Literal", "value": "2099-01-01"},
			{"action_type": "Update Field", "target_doctype": "CRM Lead", "fieldname": "custom_patient_age",
			 "value_mode": "Literal", "value": "55"},
		])
		self.wf = _make_workflow("WFI-crash", [
			{"node_id": "s1", "node_type": "Step", "action_group": self.group.name, "next_node": "sT"},
			{"node_id": "sT", "node_type": "Terminal"},
		])
		frappe.db.commit()

	def tearDown(self):
		_cleanup("WFI-crash", "WFI-crash-ag", self.lead)

	def test_mid_segment_exception_rolls_back_and_is_not_left_running(self):
		baseline = frappe.db.get_value("CRM Lead", self.lead, "custom_last_report_date")
		inst = _start(self.wf, self.lead, "s1")
		frappe.db.commit()

		interpreter.advance(frappe.get_doc(_INSTANCE_DT, inst.name))

		row = frappe.db.get_value(_INSTANCE_DT, inst.name, ["status", "current_node", "active_key"], as_dict=True)
		self.assertNotEqual(row.status, "Running", "the Instance must NOT be left Running after a crash (F3)")
		self.assertEqual(row.status, "Failed", "a permanent error is terminal")
		self.assertEqual(row.current_node, "s1", "the cursor must not advance past the failing node")
		self.assertIsNone(row.active_key, "a Failed Instance drops its active_key")
		self.assertEqual(
			frappe.db.get_value("CRM Lead", self.lead, "custom_last_report_date"), baseline,
			"the first effect must be rolled back — no partial write survives the segment failure",
		)
		self.assertIn("failed", [l.outcome for l in _logs(inst.name)], "the failure must be audited")


class TestWaitDelayValidation(FrappeTestCase):
	"""A zero or negative For-Duration delay is rejected at Definition save (F6) — a {'seconds': 0} Wait
	would re-arm instantly and hot-loop the sweep. A strictly-positive delay saves. No commit needed: a
	rejected save throws in validate() before any insert."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()

	@classmethod
	def tearDownClass(cls):
		# A sibling test class's frappe.db.commit() flushes this class's otherwise-rolled-back inserts, so
		# the passing positive control leaks unless we delete it ourselves.
		frappe.db.delete(_VERSION_DT, {"workflow": ["like", "WFI-guard%"]})
		frappe.db.delete(_NODE_DT, {"parent": ["like", "WFI-guard%"]})
		frappe.db.delete(_DEF_DT, {"workflow_name": ["like", "WFI-guard%"]})
		frappe.db.commit()

	def _wf(self, name, wait_expression):
		return frappe.get_doc({
			"doctype": _DEF_DT, "workflow_name": name, "enabled": 0,
			"vertical": _GRAIN["vertical"], "group": _GRAIN["group"], "program": _GRAIN["program"],
			"entry_doctype": "CRM Lead", "entry_event": "Created",
			"nodes": [
				{"node_id": "w1", "node_type": "Wait", "wait_mode": "For Duration", "wait_expression": wait_expression, "next_node": "w2"},
				{"node_id": "w2", "node_type": "Terminal"},
			],
		}).insert(ignore_permissions=True)

	def test_zero_delay_is_rejected(self):
		with self.assertRaises(frappe.exceptions.ValidationError):
			self._wf("WFI-guard-zero", "{'seconds': 0}")

	def test_negative_delay_is_rejected(self):
		with self.assertRaises(frappe.exceptions.ValidationError):
			self._wf("WFI-guard-neg", "{'minutes': -5}")

	def test_positive_delay_saves(self):
		wf = self._wf("WFI-guard-pos", "{'minutes': 1}")
		self.assertTrue(frappe.db.exists(_DEF_DT, wf.name))
		self.assertTrue(versions.current_name(wf.name), "a saved Definition mints a current version")

	def test_no_terminal_is_rejected(self):
		"""A Flow with no Terminal can never end (it would run to the hop budget) — reject it at save."""
		with self.assertRaises(frappe.exceptions.ValidationError):
			frappe.get_doc({
				"doctype": _DEF_DT, "workflow_name": "WFI-guard-noterm", "enabled": 0,
				"vertical": _GRAIN["vertical"], "group": _GRAIN["group"], "program": _GRAIN["program"],
				"entry_doctype": "CRM Lead", "entry_event": "Created",
				"nodes": [{"node_id": "b1", "node_type": "Branch", "condition": "True", "on_true": "b1", "on_false": "b1"}],
			}).insert(ignore_permissions=True)


class TestFullFreeze(FrappeTestCase):
	"""D1 full freeze: a Step's Action Group actions are snapshotted INTO the version. Editing the molecule
	never changes a pinned version (an in-flight Instance is immutable); re-saving the workflow mints a new
	version carrying the new body (a NEW Instance gets it); a running Instance executes the frozen body."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		cls.set_row = field_allowlist.seed_settable("CRM Lead", "custom_last_report_date", *_AXES)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		_cleanup("WFI-freeze", "WFI-freeze-ag")
		frappe.db.delete(_FIELD, {"name": cls.set_row})
		frappe.db.commit()

	def setUp(self):
		self.lead = _make_lead().name
		self.group = _make_group("WFI-freeze-ag", [
			{"action_type": "Update Field", "target_doctype": "CRM Lead", "fieldname": "custom_last_report_date",
			 "value_mode": "Literal", "value": "2025-05-05"},
		])
		self.wf = _make_workflow("WFI-freeze", [
			{"node_id": "s1", "node_type": "Step", "action_group": self.group.name, "next_node": "sT"},
			{"node_id": "sT", "node_type": "Terminal"},
		])
		frappe.db.commit()

	def tearDown(self):
		_cleanup("WFI-freeze", "WFI-freeze-ag", self.lead)

	def _step_value(self, version_name):
		payload = frappe.parse_json(frappe.db.get_value(_VERSION_DT, version_name, "payload_json"))
		step = next(n for n in payload["nodes"] if n["node_id"] == "s1")
		return step["_frozen_items"][0]["value"]

	def _edit_molecule(self, new_value):
		grp = frappe.get_doc(_GROUP_DT, self.group.name)
		grp.actions[0].value = new_value
		grp.save(ignore_permissions=True)
		frappe.db.commit()

	def test_snapshot_is_frozen_into_the_version(self):
		self.assertEqual(self._step_value(versions.current_name(self.wf.name)), "2025-05-05",
			"the Step's action body must be snapshotted into the version")

	def test_editing_the_molecule_does_not_touch_a_pinned_version(self):
		v1 = versions.current_name(self.wf.name)
		self._edit_molecule("2030-12-31")
		self.assertEqual(self._step_value(v1), "2025-05-05", "a pinned version is immutable to a later molecule edit")

	def test_resaving_the_workflow_publishes_a_new_version(self):
		v1 = versions.current_name(self.wf.name)
		self._edit_molecule("2030-12-31")
		frappe.get_doc(_DEF_DT, self.wf.name).save(ignore_permissions=True)  # re-publish
		frappe.db.commit()
		v2 = versions.current_name(self.wf.name)
		self.assertNotEqual(v1, v2, "re-saving after a molecule edit mints a new version")
		self.assertEqual(self._step_value(v1), "2025-05-05", "the old version keeps the old body")
		self.assertEqual(self._step_value(v2), "2030-12-31", "the new current version carries the new body")

	def test_in_flight_instance_runs_the_frozen_body(self):
		inst = _start(self.wf, self.lead, "s1")  # pins v1
		frappe.db.commit()
		self._edit_molecule("2030-12-31")  # molecule edited AFTER the Instance started; workflow NOT re-saved
		interpreter.advance(frappe.get_doc(_INSTANCE_DT, inst.name))
		self.assertEqual(
			frappe.utils.getdate(frappe.db.get_value("CRM Lead", self.lead, "custom_last_report_date")),
			frappe.utils.getdate("2025-05-05"),
			"a running Instance executes the FROZEN body, not the edited molecule",
		)


if __name__ == "__main__":
	unittest.main()
