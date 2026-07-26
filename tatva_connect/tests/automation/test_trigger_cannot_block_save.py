# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A workflow decides whether IT runs. It never decides whether a rep may save.

Phase 11 of docs/plans/task-form-layer/2026-07-25-task-slots-to-sections-and-form-layer.md (= W11 §6).

`Require Fields` and `Require Location` used to be GUARD verbs: declared as Requirements on a workflow's
Trigger, run inside `validate`, and raising to refuse the save. That is a second brain over rules the
record's own doctype already owns — a rep's save could be refused by whatever an author had wired on a
canvas — so they are deleted as verbs. What each one demanded is said instead as a PREDICATE about the
workflow itself (`is set` / `is not set` already ship in `automation/rules.py:_PRESENCE_OPS`), and the
location rule stays declared once on the task type (`visit_mode` / `location_when`), enforced by
`location.api` and nowhere else.

Four properties, and they are deliberately of three different kinds, because "nothing raises" is the
easiest assertion in the world to pass by accident:

  1. STRUCTURAL — the lane is empty. No verb can run inside `validate` at all, so there is nothing left to
     leave beside the task type's gate (plan §9 Phase 11: "deleted, not left beside").
  2. A REAL SAVE — a Trigger still CARRYING a stored requirement (the shape a site authored before this
     change holds, written raw here because the author-time validator would now refuse it) does not block
     the save, and the row really lands.
  3. THE REPLACEMENT — with the same demand expressed as the Trigger's predicate, the save always
     succeeds and the workflow acts only when the demand is met. This one is a CONTROL: predicates already
     worked. It is here because a suite that only proved "the block is gone" would not prove the capability
     survived it, which is the difference between a fix and a deletion.
  4. NO HOLE LEFT BEHIND — the location backstop used to STAND DOWN when a workflow declared
     `Require Location`, on the promise that the workflow's guard would block instead. With the verb gone
     that promise is worthless, so a task with no coordinates must be refused whatever a workflow declares.
     This is the one that catches the dangerous half of the change.

`arm_engine(cls)` refuses a bench that is already armed and always restores OFF — dormant is the resting
state. The task-guards switch is handled the same way, by a registered cleanup rather than a remembered
value.
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import actions
from tatva_connect.tests.authz.grains import assert_masters_exist
from tatva_connect.workflow_engine import versions
from tatva_connect.workflow_engine.tests import fixtures as fx

_LEGACY_WORKFLOW = "phase11-legacy-requirement"
_PREDICATE_WORKFLOW = "phase11-predicate-replacement"
_LOCATION_WORKFLOW = "phase11-legacy-location-requirement"

_GUARD_SWITCH = "Task::CRM Task::guards"

# The predicate narrows to this marker so an unrelated lead saved by another suite cannot satisfy it.
_MARKED = "PHASE11"
_PROBE_NAME = "Phase 11 Probe"


def _probe_mobile():
	"""A number no other probe holds. Leads dedupe on (mobile_no, vertical, group), so a fixed number
	collides with the probe the previous test in the same run already inserted. Same shape the activity
	suites already use, and the reason `_clear_probe_leads` keys on the NAME rather than the number."""
	return f"+9198765{int(frappe.generate_hash(length=8), 16) % 100000:05d}"

# What the deleted verbs demanded, in the namespaced reference vocabulary the trigger context speaks.
_REQUIRED_FIELD = "crm_lead.mobile_no"


def _arm_task_guards(cls):
	"""Enable the CRM Task guard hooks for the duration of a class, restoring OFF — never "what it was".

	Same reasoning as `fx.arm_engine`: restoring the previous value is what propagates a poisoned baseline
	(one suite leaves it on, the next records `1` as the original and puts it back). Dormant is the only
	correct resting state of a bench, so the cleanup is registered BEFORE the write and always writes 0.
	"""
	cls.addClassCleanup(frappe.db.set_value, "CRM Tatva Automation", _GUARD_SWITCH, "enabled", 0)
	cls.addClassCleanup(frappe.db.commit)
	frappe.db.set_value("CRM Tatva Automation", _GUARD_SWITCH, "enabled", 1)
	frappe.db.commit()


def _store_requirements(workflow_name, requirements):
	"""Write Requirements onto a workflow's Trigger the way a site authored BEFORE this change carries them.

	Written with `db.set_value`, bypassing the node's own save, and that is the point rather than a
	shortcut: the author-time validator now offers no guard verb at all, so a requirement can no longer be
	authored through the front door. The stored blob is exactly what an older site's `config_json` holds,
	and the property under test is that the RUNTIME ignores it. The version is re-frozen afterwards, or the
	engine would keep reading the graph as it was before the write.
	"""
	node = frappe.get_all(
		fx.NODE_DT, filters={"workflow": workflow_name, "node_type": "Trigger"}, pluck="name"
	)[0]
	config = frappe.parse_json(frappe.db.get_value(fx.NODE_DT, node, "config_json") or "{}")
	config["requirements"] = requirements
	frappe.db.set_value(fx.NODE_DT, node, "config_json", frappe.as_json(config))
	versions.ensure_version(frappe.get_doc(fx.WORKFLOW_DT, workflow_name))
	frappe.flags.pop("_workflow_version_cache", None)  # the frozen payload is request-cached
	frappe.db.commit()


def _clear_probe_leads():
	"""Leads dedupe on (mobile_no, vertical, group): a probe left behind fails the NEXT run on that key."""
	names = set(frappe.get_all("CRM Lead", filters={"lead_name": _PROBE_NAME}, pluck="name"))
	for name in names:
		for task in frappe.get_all("CRM Task", filters={"reference_docname": name}, pluck="name"):
			frappe.delete_doc("CRM Task", task, force=True, ignore_permissions=True)
		for run in frappe.get_all(fx.RUN_DT, filters={"subject_name": name}, pluck="name"):
			frappe.db.delete(fx.STEP_LOG_DT, {"workflow_run": run})
			frappe.db.delete(fx.RUN_DT, {"name": run})
		frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)
	frappe.db.commit()


class TestNoVerbCanBlockASave(FrappeTestCase):
	"""The structural half — no site, no fixture, no switch. The lane itself must be empty."""

	def test_the_guard_lane_declares_no_verb_at_all(self):
		"""A guard verb ran inside `validate` and raised. One left in the lane is one an author can still
		wire to refuse a rep's save, so the assertion is on the lane being EMPTY, not on the two names."""
		self.assertEqual(
			actions.verbs_in_lane("guard"), [],
			"a verb still runs inside validate and can refuse a save; a workflow may not decide that",
		)

	def test_neither_deleted_verb_survives_under_any_lane(self):
		"""The names, separately — a verb quietly re-lanelled as an effect would pass the check above while
		still being offered on the canvas as 'Require Fields'."""
		for verb in ("Require Fields", "Require Location"):
			with self.subTest(verb=verb):
				self.assertNotIn(verb, actions.VERBS, f"{verb} is still a verb this engine can run")
				self.assertIsNone(actions.handler_of(verb), f"{verb} still has a handler to run")

	def test_the_demand_is_expressible_as_a_predicate_instead(self):
		"""The capability has to land somewhere or this is a removal dressed as a fix. It lands on the
		predicate vocabulary that already shipped — nothing new was built for it."""
		from tatva_connect.automation.rules import KNOWN_OPERATORS

		self.assertIn("is set", KNOWN_OPERATORS)
		self.assertIn("is not set", KNOWN_OPERATORS)


class TestAStoredRequirementCannotBlockASave(FrappeTestCase):
	"""A REAL save, through Frappe's own `validate`. Nothing here calls the trigger lane directly — a test
	that invokes the handler proves the handler while the wiring is dead, which is how the old whole-graph
	scan stayed green after the key it read was removed."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		fx.purge(_LEGACY_WORKFLOW)
		cls.workflow = fx.make_workflow(_LEGACY_WORKFLOW, [
			fx.trigger(
				to="end",
				predicate={"type": "rule", "field": "crm_lead.first_name", "operator": "is", "value": _MARKED},
			),
			fx.node("end", "Terminal"),
		])
		_store_requirements(
			_LEGACY_WORKFLOW, [{"verb": "Require Fields", "params": {"require_fields": _REQUIRED_FIELD}}]
		)
		fx.arm_engine(True, cls)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		fx.purge(_LEGACY_WORKFLOW)
		_clear_probe_leads()
		frappe.db.commit()

	def tearDown(self):
		_clear_probe_leads()

	def test_the_save_lands_even_though_the_requirement_is_unmet(self):
		"""The lead matches the Trigger's subject, event, grain and predicate, and it does NOT carry the
		field the stored requirement demands. Before Phase 11 `validate` raised and the row never landed."""
		lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": _MARKED, "lead_name": _PROBE_NAME, "status": "New",
			"custom_vertical": fx.GRAIN["vertical"], "custom_group": fx.GRAIN["group"],
			"custom_current_program": fx.GRAIN["program"],
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input

		self.assertTrue(
			frappe.db.exists("CRM Lead", lead.name),
			"the workflow refused the rep's save; a workflow may only decide whether IT runs",
		)


class TestThePredicateIsTheReplacement(FrappeTestCase):
	"""CONTROL — this passes on the pre-change code too, and is here on purpose.

	What the deleted verb demanded of a REP is now a condition the workflow applies to ITSELF, on the
	Trigger's existing `predicate`. The save always succeeds either way; the only thing an unmet condition
	changes is whether the WORKFLOW acts. The graph parks on a Wait so "it acted" is a durable
	`CRM Workflow Run` row rather than something invisible.

	The reference is `crm_lead.first_name`, deliberately the same one `workflow_engine/tests/
	test_guard_lane.py` used: at FIRE time `rules._rule_match` treats the allowlist-derived `field_types`
	as the declaration of what may be referenced and RAISES on anything outside it, so a reference this
	bench has not allowlisted would fail here for a seed reason rather than a code one (plan §0.2). The
	literal "the field must have a value" form of the demand is `is set` / `is not set`, whose presence in
	the shipped vocabulary is asserted by `TestNoVerbCanBlockASave` above.
	"""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		fx.purge(_PREDICATE_WORKFLOW)
		cls.workflow = fx.make_workflow(_PREDICATE_WORKFLOW, [
			# The old "Require Fields" demand, said as a condition about the workflow instead of the rep.
			fx.trigger(
				to="w1",
				predicate={"type": "rule", "field": "crm_lead.first_name", "operator": "is", "value": _MARKED},
			),
			fx.node("w1", "Wait", config={"mode": "For Duration", "expression": "{'minutes': 5}"},
			        edges={"next": "end"}),
			fx.node("end", "Terminal"),
		])
		fx.arm_engine(True, cls)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		fx.purge(_PREDICATE_WORKFLOW)
		_clear_probe_leads()
		frappe.db.commit()

	def tearDown(self):
		_clear_probe_leads()

	def _save_lead(self, first_name):
		lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": first_name, "lead_name": _PROBE_NAME, "status": "New",
			"mobile_no": _probe_mobile(),
			"custom_vertical": fx.GRAIN["vertical"], "custom_group": fx.GRAIN["group"],
			"custom_current_program": fx.GRAIN["program"],
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
		frappe.db.commit()  # the durable start is enqueued after commit, by design
		return lead

	def _runs_for(self, lead_name):
		return frappe.get_all(
			fx.RUN_DT, filters={"workflow": self.workflow.name, "subject_name": lead_name}, pluck="name"
		)

	def test_an_unmet_condition_lets_the_save_through_and_the_workflow_does_not_act(self):
		lead = self._save_lead("NOT " + _MARKED)
		self.assertTrue(frappe.db.exists("CRM Lead", lead.name), "the save must not be refused")
		self.assertEqual(
			self._runs_for(lead.name), [],
			"the condition did not hold, so the workflow must not have acted",
		)

	def test_a_met_condition_lets_the_save_through_and_the_workflow_does_act(self):
		"""The other direction, which is what makes the case above mean anything."""
		lead = self._save_lead(_MARKED)
		self.assertTrue(frappe.db.exists("CRM Lead", lead.name))
		self.assertEqual(
			len(self._runs_for(lead.name)), 1,
			"the condition held, so exactly one run must have started",
		)


class TestTheLocationBackstopNeverStandsDown(FrappeTestCase):
	"""The dangerous half of Phase 11.

	`tasks.enforce_location` used to ask the workflow engine whether an authored `Require Location` Flow
	covered this exact save, and stand down if it did — so the two would not double-throw. With the verb
	deleted, standing down means NOBODY guards: the workflow's requirement is inert and the backstop has
	stepped aside for it. A task with no coordinates would be completed silently.

	`location.api.location_required` is patched to demand a radius, exactly as
	`tests/activity/test_dormant_location.py` already does — the location config is grain-scoped operator
	data and a test that read it off a dev site would assert the seed, not the code.
	"""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		fx.purge(_LOCATION_WORKFLOW)
		# On CRM Task Updated, no predicate — the widest possible cover, which is the worst case for the
		# hole this closes: today's `covering_location_guard` matches every task save on the grain.
		cls.workflow = fx.make_workflow(_LOCATION_WORKFLOW, [
			fx.trigger(to="end", subject_doctype="CRM Task", event="Updated"),
			fx.node("end", "Terminal"),
		])
		_store_requirements(
			_LOCATION_WORKFLOW, [{"verb": "Require Location", "params": {"geofence_meters": 200}}]
		)
		fx.arm_engine(True, cls)
		_arm_task_guards(cls)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		fx.purge(_LOCATION_WORKFLOW)
		_clear_probe_leads()
		frappe.db.commit()

	def setUp(self):
		self.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": _MARKED, "lead_name": _PROBE_NAME, "status": "New",
			"mobile_no": _probe_mobile(),
			"custom_vertical": fx.GRAIN["vertical"], "custom_group": fx.GRAIN["group"],
			"custom_current_program": fx.GRAIN["program"],
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
		self.task = frappe.get_doc({
			"doctype": "CRM Task", "title": "Phase 11 location probe", "status": "Todo",
			"reference_doctype": "CRM Lead", "reference_docname": self.lead.name,
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input

	def tearDown(self):
		_clear_probe_leads()

	def _complete(self):
		doc = frappe.get_doc("CRM Task", self.task.name)
		doc.status = "Done"
		return doc.save(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input

	def test_completing_without_coordinates_is_refused_whatever_a_workflow_declares(self):
		"""An armed workflow declares Require Location over this very save. The backstop must still be the
		one that refuses it — before Phase 11 it stood aside and the task closed with no location at all."""
		with patch("tatva_connect.location.api.location_required", return_value=200):
			with self.assertRaises(frappe.exceptions.ValidationError):
				self._complete()

		self.assertEqual(
			frappe.db.get_value("CRM Task", self.task.name, "status"), "Todo",
			"the completion was refused, so the task must still be open",
		)

	def test_completing_with_coordinates_still_goes_through(self):
		"""The gate must refuse a missing location, not completion — otherwise the case above would pass
		against a backstop that simply blocks everything."""
		with patch("tatva_connect.location.api.location_required", return_value=200):
			doc = frappe.get_doc("CRM Task", self.task.name)
			doc.status = "Done"
			doc.custom_location_latitude = 12.9716
			doc.custom_location_longitude = 77.5946
			doc.save(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input

		self.assertEqual(frappe.db.get_value("CRM Task", self.task.name, "status"), "Done")
