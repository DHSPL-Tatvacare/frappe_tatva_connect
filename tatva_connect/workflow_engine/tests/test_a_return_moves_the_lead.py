# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""End to end: a patient arriving again moves their lead, and nothing else does.

Every other suite proves one link of this chain. This one drives the WHOLE of it, with no step simulated:
a real arrival through the create brain writes a real acquisition touch, the dispatcher diffs the section,
a real armed workflow judges `changed` on it, and its Update Field node writes the lead. If any link is
dead the lead simply does not move, which is exactly how this went unnoticed before.

`changed` is what makes the rule expressible at all: a touch is a timestamp, so there is no literal for
`changed to` to name and an author could only ever have said "changed to <a time nobody can predict>".

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.workflow_engine.tests.test_a_return_moves_the_lead
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.api import partner
from tatva_connect.automation.settings import is_enabled
from tatva_connect.tests.api import partner_fixture
from tatva_connect.tests.automation import field_allowlist
from tatva_connect.workflow_engine import ENGINE_SWITCH
from tatva_connect.workflow_engine.tests import fixtures as fx

WORKFLOW = "zz-a-return-moves-the-lead"
PHONE = "+916100080001"
SOURCE = "ZZ Return Probe"
MARK_FIELD = "custom_lead_status"
MARK_VALUE = "reinquiry"
TOUCH_REF = "crm_lead.custom_acquisition_profile.touch_at"


class TestAReturnMovesTheLead(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._purge_leads()
		cls.column = partner_fixture.touch_address()[1]
		# Armed only when this bench has it off: `arm_engine` refuses an already-ON baseline, and a suite
		# may not decide that for a bench it did not set.
		if not is_enabled(ENGINE_SWITCH):
			fx.arm_engine(True, cls)
		cls.addClassCleanup(field_allowlist.clear)
		field_allowlist.seed_watchable("CRM Lead", cls.column)
		field_allowlist.seed_settable("CRM Lead", MARK_FIELD, *fx.AXES)
		partner._ensure_lead_source({"source": SOURCE})
		cls.addClassCleanup(fx.purge, WORKFLOW)
		fx.make_workflow(WORKFLOW, [
			fx.trigger(to="mark", event="Updated", predicate={"type": "all", "children": [
				{"type": "rule", "field": TOUCH_REF, "operator": "changed"},
			]}),
			fx.node("mark", "Update Field", edges={"next": "done"}, config={
				"target_doctype": "CRM Lead",
				"updates": [{"name": MARK_FIELD, "mode": "Literal", "value": MARK_VALUE}],
			}),
			fx.node("done", "Terminal"),
		])
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		cls._purge_leads()
		frappe.db.commit()
		super().tearDownClass()

	@classmethod
	def _purge_leads(cls):
		for name in frappe.get_all("CRM Lead", filters={"mobile_no": ["like", "+91610008%"]}, pluck="name"):
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)

	def setUp(self):
		frappe.set_user("Administrator")
		for cache in ("_watchable_fields_cache", "_readable_index"):
			frappe.flags.pop(cache, None)
		self.sp = f"return_{frappe.generate_hash(length=6)}"
		frappe.db.savepoint(self.sp)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point=self.sp)

	def _arrive(self):
		"""One arrival through the brain every door enters by — no workflow is told anything."""
		parent_fields, child_allow = partner._split_keys(["lead:mobile_no", "lead:first_name"])
		mp = frappe._dict(source=SOURCE, vertical=fx.GRAIN["vertical"],
		                  crm_group=fx.GRAIN["group"], program=fx.GRAIN["program"])
		doc, action = partner._upsert_one(
			{"mobile_no": PHONE, "first_name": "Asha Return"}, mp, False, parent_fields, child_allow,
			allowed_programs=[],
		)
		return frappe.get_doc("CRM Lead", doc.name), action

	def test_a_first_arrival_does_not_mark_a_return(self):
		"""A patient's first visit is not a return, however the rule is written."""
		lead, action = self._arrive()
		self.assertEqual(action, "created")
		self.assertNotEqual(lead.get(MARK_FIELD), MARK_VALUE, "a brand-new lead was marked as a return")

	def test_a_return_moves_the_lead(self):
		"""The whole chain, and the only test that fails if ANY link of it is dead."""
		first, _action = self._arrive()
		self.assertNotEqual(first.get(MARK_FIELD), MARK_VALUE)

		returned, action = self._arrive()
		self.assertEqual(action, "updated", "the return did not land on the lead already there")
		self.assertEqual(returned.name, first.name)
		self.assertEqual(
			returned.get(MARK_FIELD), MARK_VALUE,
			"the patient came back and the workflow did not move the lead",
		)

	def test_a_rep_editing_the_lead_is_not_a_return(self):
		"""The defect the whole design turns on: an edit must never read as the patient coming back."""
		lead, _action = self._arrive()
		frappe.db.set_value("CRM Lead", lead.name, MARK_FIELD, "")

		doc = frappe.get_doc("CRM Lead", lead.name)
		doc.first_name = "Asha Corrected"
		doc.save(ignore_permissions=True)

		self.assertNotEqual(
			frappe.db.get_value("CRM Lead", lead.name, MARK_FIELD), MARK_VALUE,
			"a rep's edit was treated as an arrival",
		)
