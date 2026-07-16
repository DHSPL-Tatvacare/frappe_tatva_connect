# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Unified Flow engine — the Phase-A proof of concept (docs/plans/unified-flow-engine.md §11).

Proves the ONE engine (Trigger·When·Then) covers, on the workflow spine, what the automation-rule engine
did — WITHOUT the second engine. Two flows, built as real CRM Workflow Definitions and driven end-to-end
through real .save() calls, assert the two mechanics the fold turns on:

  ② LOCATION GUARD (ephemeral guard): a Require Location Flow on CRM Task·Updated enforces at SAVE time —
     a non-compliant Done save is BLOCKED in validate; a compliant one passes and persists NO Instance.
  ④ REVIEW BRANCH (ephemeral effect): a Branch Flow on CRM Task·Updated routes to the correct effect by a
     trigger value (approved → one Create Note, rejected → the other), the effect landing on the parent
     LEAD (D7), again persisting NO Instance.

Plus the WHEN gate: a task the criteria do not select is neither blocked nor acted on.

Outcomes, never calls: real throws, real Comment rows, a real absence of any CRM Workflow Instance. All
flows here are wait-free (ephemeral), so the engine never commits mid-save — FrappeTestCase's rollback
cleans each test; fixtures committed in setUpClass are deleted in teardown, switches restored.
"""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import seed
from tatva_connect.location import api as location_api
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist
from tatva_connect.workflow_engine import ENGINE_SWITCH

_GRAIN = GRAINS[0]
_ANCHOR_LAT, _ANCHOR_LNG = 12.9716, 77.5946  # an arbitrary clinic anchor (Bengaluru)
_SWITCH_DT = "CRM Tatva Automation"
_ON_SWITCHES = (ENGINE_SWITCH, "Location::Google::capture")
# The legacy location backstop (tasks.enforce_location) is gated by this switch and stands down only for a
# covering automation RULE, never a Flow. It is forced OFF here so it cannot block the missing-coords save
# on its own — with it off, the ONLY thing that can veto the save is OUR Flow guard, so the guard test
# genuinely exercises the Flow and a broken Flow guard would go red (it is not masked by the backstop).
_OFF_SWITCHES = ("Task::CRM Task::guards",)
_ALL_SWITCHES = _ON_SWITCHES + _OFF_SWITCHES
_DEF_DT = "CRM Workflow Definition"
_GROUP_DT = "CRM Action Group"
_INSTANCE_DT = "CRM Workflow Instance"
_PFX = "PoC"  # every fixture name is prefixed so cleanup is a single LIKE


def _set_switches():
	for key in _ON_SWITCHES:
		frappe.db.set_value(_SWITCH_DT, key, "enabled", 1)
	for key in _OFF_SWITCHES:
		frappe.db.set_value(_SWITCH_DT, key, "enabled", 0)


def _make_lead():
	return frappe.get_doc({
		"doctype": "CRM Lead", "first_name": "PoCProbe", "lead_name": "PoC Probe", "status": "New",
		"lead_owner": "Administrator",
		"custom_vertical": _GRAIN["vertical"], "custom_group": _GRAIN["group"],
		"custom_current_program": _GRAIN["program"],
		"custom_clinic_latitude": _ANCHOR_LAT, "custom_clinic_longitude": _ANCHOR_LNG,
	}).insert(ignore_permissions=True)


def _make_task_type(name, visit_mode="Phone"):
	# Only the visit type is In-Person: the legacy location backstop (tasks.enforce_location) requires
	# location for EVERY In-Person Done save, so review/decoy stay Phone or they'd be blocked by the
	# backstop rather than exercising (or not) our Flow.
	pk = f"{_GRAIN['vertical']}::{_GRAIN['group']}::{_GRAIN['program']}::{name}"
	if not frappe.db.exists("CRM Task Type", pk):
		frappe.get_doc({
			"doctype": "CRM Task Type", "type_name": name,
			"vertical": _GRAIN["vertical"], "group": _GRAIN["group"], "program": _GRAIN["program"],
			"visit_mode": visit_mode,
		}).insert(ignore_permissions=True)
	return pk


def _make_group(suffix, items):
	return frappe.get_doc({"doctype": _GROUP_DT, "group_name": f"{_PFX}-{suffix}", "actions": items}).insert(ignore_permissions=True).name


def _make_flow(name, criteria, nodes):
	return frappe.get_doc({
		"doctype": _DEF_DT, "workflow_name": f"{_PFX}-{name}", "enabled": 1,
		"vertical": _GRAIN["vertical"], "group": _GRAIN["group"], "program": _GRAIN["program"],
		"entry_doctype": "CRM Task", "entry_event": "Updated",
		"criteria": criteria, "nodes": nodes,
	}).insert(ignore_permissions=True).name


def _make_task(lead, task_type, **extra):
	# No assigned_to (see test_guard_verbs): a native after_insert assign would stale-mismatch the next save.
	payload = {
		"doctype": "CRM Task", "title": "PoC task", "status": "Todo",
		"reference_doctype": "CRM Lead", "reference_docname": lead, "custom_task_type": task_type,
	}
	payload.update(extra)
	return frappe.get_doc(payload).insert(ignore_permissions=True)


def _track_grain(radius_m=100):
	settings = frappe.get_doc("CRM Maps Settings")
	settings.append("location_tracked_grains", {
		"vertical": _GRAIN["vertical"], "group": _GRAIN["group"], "program": _GRAIN["program"], "radius_m": radius_m,
	})
	settings.save(ignore_permissions=True)
	row_name = settings.location_tracked_grains[-1].name

	def _cleanup():
		frappe.db.delete("CRM Maps Tracked Grain", {"name": row_name})
		frappe.clear_document_cache("CRM Maps Settings", "CRM Maps Settings")

	return _cleanup


def _instances_for(task_name):
	return frappe.get_all(_INSTANCE_DT, filters={"subject_name": task_name}, pluck="name")


def _lead_comments(lead):
	return frappe.get_all(
		"Comment",
		filters={"reference_doctype": "CRM Lead", "reference_name": lead, "comment_type": "Comment"},
		pluck="content",
	)


class _PoCBase(FrappeTestCase):
	"""Shared fixture: switches on, a location-tracked grain, an anchored lead, and the two flows —
	② Require Location guard, ④ review Branch — plus a decoy task type no flow's When selects."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		seed.sync_catalog()  # ensure every switch row exists (idempotent)
		cls._prior = {k: frappe.db.get_value(_SWITCH_DT, k, "enabled") for k in _ALL_SWITCHES}
		_set_switches()
		cls._untrack = _track_grain()
		cls.lead = _make_lead()

		cls.tt_visit = _make_task_type("PoC-Visit", visit_mode="In-Person")  # ② guarded by Require Location
		cls.tt_review = _make_task_type("PoC-Review")   # ④ routed by the Branch (Phone — no location backstop)
		cls.tt_decoy = _make_task_type("PoC-Decoy")     # matched by no flow's When (Phone)

		g_reqloc = _make_group("reqloc", [{"action_type": "Require Location"}])
		g_approved = _make_group("approved", [{"action_type": "Create Note", "comment_mode": "Literal", "comment_text": "REVIEW-APPROVED"}])
		g_rejected = _make_group("rejected", [{"action_type": "Create Note", "comment_mode": "Literal", "comment_text": "REVIEW-REJECTED"}])

		# ② Trigger CRM Task·Updated · When custom_task_type is <visit> AND status is Done · Then Require Location.
		cls.flow_guard = _make_flow(
			"loc-guard",
			[{"field": "custom_task_type", "operator": "is", "value": cls.tt_visit},
			 {"field": "status", "operator": "is", "value": "Done"}],
			[{"node_id": "n1", "node_type": "Step", "action_group": g_reqloc, "next_node": "end"},
			 {"node_id": "end", "node_type": "Terminal"}],
		)
		# ④ Trigger CRM Task·Updated · When custom_task_type is <review> AND status is Done · Then Branch by verdict.
		# `priority` stands in for the review verdict — a real value on the trigger record that the Branch reads
		# from state (in production the Document-Review schema's `verdict` lands in context the same way).
		cls.flow_branch = _make_flow(
			"review-branch",
			[{"field": "custom_task_type", "operator": "is", "value": cls.tt_review},
			 {"field": "status", "operator": "is", "value": "Done"}],
			[{"node_id": "b1", "node_type": "Branch", "condition": 'ctx.get("priority") == "High"', "on_true": "approve", "on_false": "reject"},
			 {"node_id": "approve", "node_type": "Step", "action_group": g_approved, "next_node": "end"},
			 {"node_id": "reject", "node_type": "Step", "action_group": g_rejected, "next_node": "end"},
			 {"node_id": "end", "node_type": "Terminal"}],
		)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.db.delete("CRM Task", {"reference_docname": cls.lead.name})
		frappe.db.delete("Comment", {"reference_doctype": "CRM Lead", "reference_name": cls.lead.name})
		for name in (cls.flow_guard, cls.flow_branch):
			frappe.db.delete("CRM Workflow Version", {"workflow": name})
			frappe.db.delete(_DEF_DT, {"name": name})
		frappe.db.delete("CRM Action Group Item", {"parent": ["like", f"{_PFX}-%"]})
		frappe.db.delete(_GROUP_DT, {"group_name": ["like", f"{_PFX}-%"]})
		frappe.db.delete("CRM Lead", {"name": cls.lead.name})
		for tt in (cls.tt_visit, cls.tt_review, cls.tt_decoy):
			frappe.db.delete("CRM Task Type", {"name": tt})
		cls._untrack()
		for key, val in cls._prior.items():
			frappe.db.set_value(_SWITCH_DT, key, "enabled", val or 0)
		frappe.db.commit()

	def setUp(self):
		frappe.flags.in_test = True

	def tearDown(self):
		frappe.flags.in_test = False
		# Hard isolation: the tests share one lead, so clear its tasks + notes between methods (the
		# ephemeral engine never commits, but this makes each test's clean slate independent of rollback).
		frappe.db.delete("Comment", {"reference_doctype": "CRM Lead", "reference_name": self.lead.name})
		frappe.db.delete("CRM Task", {"reference_docname": self.lead.name})


class TestLocationGuardFlow(_PoCBase):
	"""② The guard lane: a Require Location Flow blocks a non-compliant Done save, passes a compliant one,
	and (ephemeral) persists no Instance either way."""

	def test_missing_coords_blocks_the_save(self):
		task = _make_task(self.lead.name, self.tt_visit)
		calls = []
		orig = location_api.location_required

		def spy(task_type, lead, values):
			calls.append((task_type, lead))
			return orig(task_type, lead, values)

		location_api.location_required = spy
		try:
			task.status = "Done"
			with self.assertRaises(frappe.exceptions.ValidationError):
				task.save(ignore_permissions=True)
		finally:
			location_api.location_required = orig
		self.assertTrue(calls, "the guard never consulted location.api.location_required — it reimplemented the decision")
		self.assertEqual(frappe.db.get_value("CRM Task", task.name, "status"), "Todo", "the blocked save must not have committed")
		self.assertEqual(_instances_for(task.name), [], "an ephemeral guard Flow must persist no Instance")

	def test_captured_coords_pass_and_persist_no_instance(self):
		task = _make_task(
			self.lead.name, self.tt_visit,
			custom_location_latitude=_ANCHOR_LAT + 0.0001, custom_location_longitude=_ANCHOR_LNG,
		)
		task.status = "Done"
		task.save(ignore_permissions=True)  # must not raise
		self.assertEqual(frappe.db.get_value("CRM Task", task.name, "status"), "Done")
		self.assertEqual(_instances_for(task.name), [], "a wait-free Flow runs inline — no Instance row")


class TestReviewBranchFlow(_PoCBase):
	"""④ The branch lane: the Flow routes to the correct Create Note by the trigger's verdict, the note
	landing on the PARENT lead (D7), and persists no Instance."""

	def test_approved_verdict_routes_to_the_approved_effect(self):
		task = _make_task(self.lead.name, self.tt_review, priority="High")
		task.status = "Done"
		task.save(ignore_permissions=True)
		notes = _lead_comments(self.lead.name)
		self.assertIn("REVIEW-APPROVED", notes)
		self.assertNotIn("REVIEW-REJECTED", notes)
		self.assertEqual(_instances_for(task.name), [], "a wait-free Flow runs inline — no Instance row")

	def test_rejected_verdict_routes_to_the_rejected_effect(self):
		task = _make_task(self.lead.name, self.tt_review, priority="Low")
		task.status = "Done"
		task.save(ignore_permissions=True)
		notes = _lead_comments(self.lead.name)
		self.assertIn("REVIEW-REJECTED", notes)
		self.assertNotIn("REVIEW-APPROVED", notes)


class TestWhenGatesTheFlow(_PoCBase):
	"""The When predicate: a task whose custom_task_type no flow selects is neither guarded nor acted on —
	the decoy has no coordinates yet its Done save is not blocked, because no Flow's criteria match."""

	def test_unselected_task_type_is_neither_blocked_nor_acted_on(self):
		task = _make_task(self.lead.name, self.tt_decoy)  # no coords, no matching flow
		task.status = "Done"
		task.save(ignore_permissions=True)  # must not raise — no guard Flow selects this task type
		self.assertEqual(frappe.db.get_value("CRM Task", task.name, "status"), "Done")
		self.assertEqual(_lead_comments(self.lead.name), [], "no branch Flow selects the decoy — no note should land")


if __name__ == "__main__":
	unittest.main()
