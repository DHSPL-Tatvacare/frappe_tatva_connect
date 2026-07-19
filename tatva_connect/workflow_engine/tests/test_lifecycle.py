# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""BL — the Definition lifecycle is REAL, not decoration. Proven by OUTCOMES against the real doctypes +
real trigger/interpreter, nothing mocked:

  - create lands a BLANK Draft (no seeded node, no Version) — the canvas opens empty.
  - Save persists a Draft and freezes NOTHING and arms NOTHING — Save is categorically not Publish, so
    "configure ten nodes and click Save" can never start execution.
  - editing is Draft-only — save_draft refuses a released Definition (Revise first).
  - Publish is the GATE: it rejects a broken graph (stays Draft, no Version) and freezes an immutable
    Version for a valid one — the Version carries entry_node through freeze + load.
  - an illegal transition is refused by the ONE state machine.
  - only ACTIVE fires: an Active Definition starts an Instance on a matching event; Suspended and Draft do
    not — the entry trigger reads lifecycle_state == "Active" and nothing else.
  - the Instance enters at entry_node (the Start node), NOT the first row. The fixture's first row is a
    Terminal and its entry is a Wait: on the old nodes[0] convention the Instance would finish instantly;
    reading entry_node it PARKS at the Wait. This test fails on the old code and passes on the new.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.campaigns import api
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist
from tatva_connect.workflow_engine import ENGINE_SWITCH, versions

_DEF_DT = "CRM Workflow Definition"
_VERSION_DT = "CRM Workflow Version"
_INSTANCE_DT = "CRM Workflow Instance"
_STEP_LOG_DT = "CRM Workflow Step Log"
_NODE_DT = "CRM Workflow Node"
_SWITCH_DT = "CRM Tatva Automation"

_GRAIN = GRAINS[2]  # TatvaPractice / India / FieldSales


def _purge(prefix):
	for inst in frappe.get_all(_INSTANCE_DT, filters={"workflow": ["like", f"{prefix}%"]}, pluck="name"):
		frappe.db.delete(_STEP_LOG_DT, {"workflow_instance": inst})
	frappe.db.delete(_INSTANCE_DT, {"workflow": ["like", f"{prefix}%"]})
	frappe.db.delete(_VERSION_DT, {"workflow": ["like", f"{prefix}%"]})
	frappe.db.delete(_NODE_DT, {"parent": ["like", f"{prefix}%"]})
	frappe.db.delete(_DEF_DT, {"workflow_name": ["like", f"{prefix}%"]})
	frappe.db.commit()


class TestLifecycleMechanics(FrappeTestCase):
	"""create / save_draft / publish / transitions — the authoring seam. No trigger fires here (the engine
	switch stays dormant), so FrappeTestCase's rollback isolates every case; teardown purges in case a
	sibling class's commit flushes an otherwise-rolled-back insert."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()

	@classmethod
	def tearDownClass(cls):
		_purge("LC-mech")

	def test_create_lands_a_blank_draft(self):
		doc = frappe.get_doc(_DEF_DT, api.create_campaign("LC-mech-create")["name"])
		self.assertEqual(doc.lifecycle_state, "Draft")
		self.assertEqual(len(doc.nodes), 0, "create seeds NO node — the canvas opens empty")
		self.assertFalse(frappe.db.exists(_VERSION_DT, {"workflow": doc.name}), "a Draft freezes no Version")

	def test_save_draft_persists_without_freezing(self):
		name = api.create_campaign("LC-mech-save")["name"]
		api.save_draft(name, [{"node_id": "t", "node_type": "Terminal"}], entry_node="t")
		doc = frappe.get_doc(_DEF_DT, name)
		self.assertEqual(doc.lifecycle_state, "Draft")
		self.assertEqual([n.node_id for n in doc.nodes], ["t"], "the working graph persisted")
		self.assertFalse(frappe.db.exists(_VERSION_DT, {"workflow": name}), "Save is not Publish — no Version minted")

	def test_save_draft_refuses_a_released_definition(self):
		name = api.create_campaign("LC-mech-locked")["name"]
		api.save_draft(name, [{"node_id": "t", "node_type": "Terminal"}], entry_node="t")
		api.publish(name)
		with self.assertRaises(frappe.exceptions.ValidationError):
			api.save_draft(name, [{"node_id": "t", "node_type": "Terminal"}], entry_node="t")

	def test_publish_rejects_a_graph_with_no_entry(self):
		name = api.create_campaign("LC-mech-noentry")["name"]
		api.save_draft(name, [{"node_id": "t", "node_type": "Terminal"}])  # entry_node omitted on purpose
		with self.assertRaises(frappe.exceptions.ValidationError):
			api.publish(name)
		self.assertEqual(frappe.db.get_value(_DEF_DT, name, "lifecycle_state"), "Draft", "a rejected publish stays Draft")
		self.assertFalse(frappe.db.exists(_VERSION_DT, {"workflow": name}), "a rejected publish freezes nothing")

	def test_publish_freezes_a_version_carrying_entry_node(self):
		name = api.create_campaign("LC-mech-pub")["name"]
		api.save_draft(name, [{"node_id": "t", "node_type": "Terminal"}], entry_node="t")
		api.publish(name)
		self.assertEqual(frappe.db.get_value(_DEF_DT, name, "lifecycle_state"), "Published")
		version = versions.current_name(name)
		self.assertTrue(version, "Publish freezes a current Version")
		self.assertEqual(versions.load(version).entry_node, "t", "the Version carries entry_node through freeze + load")

	def test_illegal_transition_is_refused(self):
		name = api.create_campaign("LC-mech-illegal")["name"]
		with self.assertRaises(frappe.exceptions.ValidationError):
			api.activate(name)  # Draft -> Active is not a legal edge; you must Publish first


class TestLifecycleArmingAndEntry(FrappeTestCase):
	"""The runtime half: only Active fires, and an Instance enters at entry_node. The engine switch is ON
	for the class; advance() commits at the boundary, so each test purges its own committed rows in
	tearDown to stay isolated (a leaked Active def would fan out onto the next test's lead)."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		cls._prior = frappe.db.get_value(_SWITCH_DT, ENGINE_SWITCH, "enabled")
		frappe.db.set_value(_SWITCH_DT, ENGINE_SWITCH, "enabled", 1)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.db.set_value(_SWITCH_DT, ENGINE_SWITCH, "enabled", cls._prior or 0)  # restore dormant BEFORE purge
		_purge("LC-arm")
		frappe.db.delete("CRM Lead", {"first_name": "LCarm"})
		frappe.db.commit()

	def tearDown(self):
		_purge("LC-arm")
		frappe.db.delete("CRM Lead", {"first_name": "LCarm"})
		frappe.db.commit()

	def _lead(self):
		return frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "LCarm", "lead_name": "LCarm Probe", "status": "New",
			"custom_vertical": _GRAIN["vertical"], "custom_group": _GRAIN["group"],
			"custom_current_program": _GRAIN["program"],
		}).insert(ignore_permissions=True)

	def _make_active_parking_flow(self, name):
		"""An armed durable flow whose Start node is deliberately NOT the first row. nodes[0] is a Terminal;
		the entry (entry_node) is a Wait that parks. Inserted straight as Active (the grain + entry header
		fields are authored on a surface the api doesn't expose yet, so the test sets them directly), which
		runs the release contract on save and freezes a Version."""
		return frappe.get_doc({
			"doctype": _DEF_DT, "workflow_name": name, "lifecycle_state": "Active",
			"vertical": _GRAIN["vertical"], "group": _GRAIN["group"], "program": _GRAIN["program"],
			"entry_doctype": "CRM Lead", "entry_event": "Created", "entry_node": "s",
			"nodes": [
				{"node_id": "t", "node_type": "Terminal"},
				{"node_id": "s", "node_type": "Wait", "wait_mode": "For Duration", "wait_expression": "{'minutes': 5}", "next_node": "t"},
			],
		}).insert(ignore_permissions=True)

	def _instances(self, workflow):
		return frappe.get_all(_INSTANCE_DT, filters={"workflow": workflow}, fields=["name", "status", "current_node"])

	def test_active_arms_and_enters_at_entry_node(self):
		wf = self._make_active_parking_flow("LC-arm-active")
		self._lead()  # a matching Created event
		rows = self._instances(wf.name)
		self.assertEqual(len(rows), 1, "an Active Definition starts exactly one Instance on a matching event")
		self.assertEqual(rows[0].status, "Parked", "it PARKS at the Wait — proof it did not enter at the nodes[0] Terminal")
		self.assertEqual(rows[0].current_node, "s", "the Instance enters at entry_node (the Start node), not the first row")

	def test_suspended_does_not_start(self):
		wf = self._make_active_parking_flow("LC-arm-suspend")
		api.suspend(wf.name)
		self._lead()
		self.assertEqual(self._instances(wf.name), [], "a Suspended Definition arms nothing")

	def test_draft_does_not_start(self):
		wf = self._make_active_parking_flow("LC-arm-draft")
		api.revise(wf.name)  # Active -> Draft
		self._lead()
		self.assertEqual(self._instances(wf.name), [], "a Draft Definition arms nothing — Save is never execute")
