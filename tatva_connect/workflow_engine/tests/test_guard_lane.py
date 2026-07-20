# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A Requirement declared on the Trigger really blocks a real save.

The guard lane is the one part of the engine that runs INSIDE `validate` and stops a write. Everything
else the engine does happens after the save and can be inspected afterwards; a guard either blocks or it
does not, and if it silently does not, the operator's rule is decoration.

So this suite saves actual `CRM Lead` documents through Frappe's own `validate` hook. Nothing calls
`run_guards` directly — a test that invokes the handler itself proves the handler works while the wiring
is dead, which is exactly how the old whole-graph scan stayed green after the key it read was removed.

ONE workflow drives all four cases, and three of the four EXPECT THE SAVE TO SUCCEED. That is the point:
a suite where every case blocks cannot tell "the requirement fired" from "some unrelated validation
threw". Each success closes a different door — the predicate gate, the satisfied requirement, and the
dormant switch — so the single blocked save is attributable to the requirement and nothing else.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.tests.authz.grains import assert_masters_exist
from tatva_connect.workflow_engine.tests import fixtures as fx

_WORKFLOW = "guard-lane-probe"

# The predicate narrows to this marker, so a lead without it exercises the "predicate did not hold" path.
_MARKED = "GUARDED"
_REQUIRED_FIELD = "crm_lead.mobile_no"
_PROBE_NAME = "Guard Probe"
_PROBE_MOBILE = "9876500001"


class TestGuardLane(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		fx.purge(_WORKFLOW)
		cls.workflow = fx.make_workflow(_WORKFLOW, [
			fx.trigger(
				to="end",
				predicate={"type": "rule", "field": "crm_lead.first_name", "operator": "is", "value": _MARKED},
			),
			fx.node("end", "Terminal"),
		])
		cls._set_requirements([{"verb": "Require Fields", "params": {"require_fields": _REQUIRED_FIELD}}])
		cls._was_armed = fx.arm_engine(True)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		fx.arm_engine(bool(cls._was_armed))
		fx.purge(_WORKFLOW)
		frappe.db.commit()

	@classmethod
	def _set_requirements(cls, requirements):
		"""Declare the Requirements on the Trigger node, the way an author would.

		Written through the node's own save so the registry validator runs — if a requirement shape ever
		stops being valid, this suite fails at setup rather than quietly testing a graph the builder
		would have refused.
		"""
		name = frappe.get_all(
			fx.NODE_DT, filters={"workflow": cls.workflow.name, "node_type": "Trigger"}, pluck="name"
		)[0]
		node = frappe.get_doc(fx.NODE_DT, name)
		config = frappe.parse_json(node.config_json or "{}")
		config["requirements"] = requirements
		node.config_json = frappe.as_json(config)
		node.save(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
		# Content-addressed: without a new version the guard lane keeps reading the old frozen graph.
		from tatva_connect.workflow_engine import versions

		versions.ensure_version(frappe.get_doc(fx.WORKFLOW_DT, cls.workflow.name))
		frappe.flags.pop("_workflow_version_cache", None)  # the frozen payload is request-cached
		frappe.db.commit()

	def tearDown(self):
		# Leads dedupe on (mobile_no, vertical, group): a probe left behind fails the NEXT run on that key.
		names = set(frappe.get_all("CRM Lead", filters={"lead_name": _PROBE_NAME}, pluck="name"))
		names |= set(frappe.get_all("CRM Lead", filters={"mobile_no": _PROBE_MOBILE}, pluck="name"))
		for name in names:
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _save_lead(self, first_name, mobile_no=None):
		"""Insert a lead through the normal path — `validate` fires, and with it the guard lane."""
		doc = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": first_name, "lead_name": _PROBE_NAME, "status": "New",
			"custom_vertical": fx.GRAIN["vertical"], "custom_group": fx.GRAIN["group"],
			"custom_current_program": fx.GRAIN["program"],
		})
		if mobile_no:
			doc.mobile_no = mobile_no
		return doc.insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input

	# --- the one case that must block ------------------------------------------------------------------

	def test_an_unmet_requirement_blocks_the_save(self):
		"""The whole point. The lead matches the Trigger's subject, event, grain and predicate, and it is
		missing the field the Requirement demands — so `validate` raises and the row never lands."""
		with self.assertRaises(frappe.exceptions.ValidationError) as caught:
			self._save_lead(_MARKED)
		self.assertIn(_REQUIRED_FIELD, str(caught.exception))
		self.assertFalse(
			frappe.db.exists("CRM Lead", {"lead_name": _PROBE_NAME}),
			"the save was refused, so no lead may exist",
		)

	# --- three that must NOT block, each closing a different door --------------------------------------

	def test_a_met_requirement_lets_the_save_through(self):
		"""Same lead, same workflow, requirement satisfied. Proves the block above was the requirement
		and not the workflow matching at all."""
		lead = self._save_lead(_MARKED, mobile_no=_PROBE_MOBILE)
		self.assertTrue(frappe.db.exists("CRM Lead", lead.name))

	def test_a_lead_the_predicate_rejects_is_not_guarded(self):
		"""A requirement only applies to a subject the workflow actually qualifies. Without this, a
		Requirement would enforce against every lead in the system rather than the ones the author aimed
		at — the difference between a targeted rule and an outage."""
		lead = self._save_lead("UNMARKED")
		self.assertTrue(frappe.db.exists("CRM Lead", lead.name))

	def test_the_guard_does_not_run_while_the_engine_is_dormant(self):
		"""Dormant-by-default is a constitution rule, not a nicety: until an operator arms the engine, an
		authored workflow must not be able to block a single save."""
		fx.arm_engine(False)
		try:
			lead = self._save_lead(_MARKED)
			self.assertTrue(frappe.db.exists("CRM Lead", lead.name))
		finally:
			fx.arm_engine(True)
