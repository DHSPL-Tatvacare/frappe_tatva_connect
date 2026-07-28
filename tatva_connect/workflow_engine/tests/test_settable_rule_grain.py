# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The settable-field picker reads the workflow's grain as a RULE grain, and covers every record it writes.

Two defects in one call. `describe._settable_fields` asked `fields.settable_rows("CRM Lead", axes)`, and:

  * the AXES come from the Trigger's DECLARED grain, where a blank axis means ANY — but `settable_rows`
    feeds them to `entitlement.field_in_grains_via_contract` as the DATA side, where blank is the literal
    empty string. A workflow declaring `vertical=X, group=""` was therefore offered only fields whose
    contract is equally blank; a field ticked by the more specific contract `(X, G1, "")` was hidden,
    though execution would happily have allowed the write. That is the exact defect named in the
    constitution: a rule grain fed to a function expecting a data grain.
  * the DOCTYPE was hardcoded `CRM Lead` whatever the workflow watched, so a Task-triggered workflow
    offered the Target `CRM Task` and then listed LEAD fields underneath it.

The honest question for a picker is "which fields could this workflow's grain EVER be allowed to set",
which is wildcard-aware on BOTH sides and is answered by `taxonomy.grain.overlaps` — the one matcher
module — never by a second comparison written here.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import actions, describe, fields
from tatva_connect.taxonomy import grain as taxonomy_grain
from tatva_connect.tests.authz.grains import assert_masters_exist
from tatva_connect.tests.automation import field_allowlist
from tatva_connect.workflow_engine.tests import fixtures as fx

# A real CRM Lead meta field with NO catalog row of its own, so the seed below is the ONLY thing that
# ticks it anywhere. `status` was the obvious pick and is a bad one: it is ticked by every contract on
# the bench, so the exclusion test could never fail however wrong the matcher was.
_LEAD_FIELD = "custom_external_id"
_TASK_CATALOG = "CRM Task Field"
_TASK_FIELD = "status"


def _keys(descriptors, doctype=None):
	return {d["key"] for d in descriptors if doctype is None or d.get("doctype") == doctype}


class TestSettableRuleGrain(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		# A lead field settable in ONE fully-specific grain. The workflow below declares a BLANKER grain,
		# so it is the wildcard rule — not a coincidence of seeding — that decides whether it is offered.
		field_allowlist.seed_settable(
			"CRM Lead", _LEAD_FIELD,
			vertical=fx.GRAIN["vertical"], group=fx.GRAIN["group"], program=fx.GRAIN["program"],
		)
		cls.task_row_existed = frappe.db.exists(_TASK_CATALOG, _TASK_FIELD)
		if cls.task_row_existed:
			cls.task_can_set_was = frappe.db.get_value(_TASK_CATALOG, _TASK_FIELD, "can_set")
			frappe.db.set_value(_TASK_CATALOG, _TASK_FIELD, "can_set", 1)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		field_allowlist.clear("CRM Lead")
		if cls.task_row_existed:
			frappe.db.set_value(_TASK_CATALOG, _TASK_FIELD, "can_set", cls.task_can_set_was)
		frappe.db.commit()

	# --- the grain half ----------------------------------------------------------------------------------

	def test_a_blank_axis_on_the_workflow_grain_is_offered_a_more_specific_contracts_field(self):
		"""The defect. `group` and `program` blank means ANY group, ANY program — so a field ticked by the
		contract for one specific group/program is squarely inside what this workflow may ever set."""
		offered = describe._settable_fields("CRM Lead", fx.GRAIN["vertical"], "", "")
		self.assertIn(_LEAD_FIELD, _keys(offered))

	def test_the_fully_specific_grain_is_still_offered_it(self):
		"""The other direction: a widening that offered everything would also pass the test above."""
		offered = describe._settable_fields(
			"CRM Lead", fx.GRAIN["vertical"], fx.GRAIN["group"], fx.GRAIN["program"]
		)
		self.assertIn(_LEAD_FIELD, _keys(offered))

	def test_a_workflow_on_another_vertical_is_not_offered_it(self):
		"""Wildcard-aware is not permissive. A SET axis that disagrees still excludes the field."""
		offered = describe._settable_fields("CRM Lead", "Goodflip-Care", "", "")
		self.assertNotIn(_LEAD_FIELD, _keys(offered))

	def test_overlaps_is_symmetric_and_covers_is_not(self):
		"""The two questions, stated side by side, so the split cannot quietly collapse into one.

		`covers` asks about a real record and is one-directional: only the CANDIDATE may wildcard.
		`overlaps` asks about two rules and is symmetric. Using the first where the second belongs is
		what hid the field above."""
		contract = {"vertical": "Tatvapractice", "group": "India", "program": "Field-Sales"}
		self.assertFalse(taxonomy_grain.covers(contract, "Tatvapractice", "", ""))
		self.assertTrue(taxonomy_grain.overlaps(contract, "Tatvapractice", "", ""))
		self.assertFalse(taxonomy_grain.overlaps(contract, "Goodflip-Care", "", ""))
		self.assertTrue(taxonomy_grain.overlaps(contract, "", "", ""))

	def test_the_rule_grain_resolver_is_not_the_data_grain_one(self):
		"""B9 as a lock: two named resolvers, so a caller has to say which grain it is holding. A single
		function with a flag is how a wildcard gets compared as the empty string."""
		self.assertTrue(hasattr(fields, "settable_rows"))
		self.assertTrue(hasattr(fields, "settable_rows_in_rule_grain"))
		data = {r.fieldname for r in fields.settable_rows("CRM Lead", (fx.GRAIN["vertical"], "", ""))}
		rule = {r.fieldname for r in fields.settable_rows_in_rule_grain("CRM Lead", (fx.GRAIN["vertical"], "", ""))}
		self.assertNotIn(_LEAD_FIELD, data, "the data-grain resolver must keep comparing blank literally")
		self.assertIn(_LEAD_FIELD, rule)

	# --- the doctype half --------------------------------------------------------------------------------

	def test_a_task_workflow_is_offered_task_fields(self):
		"""It asked for `CRM Lead` whatever the workflow watched, so the Target offered `CRM Task` and the
		Field picker underneath it listed lead fields."""
		if not self.task_row_existed:
			self.skipTest(f"no {_TASK_CATALOG} row to make settable on this bench")
		schema = describe.builder_schema(on_doctype="CRM Task")
		self.assertIn(_TASK_FIELD, _keys(schema["set_targets"], doctype="CRM Task"))

	def test_the_lead_is_offered_alongside_the_subject(self):
		"""A Task-subject workflow may still write the lead — `resolve_target` reaches both, so both are
		offered. The reachable set is read from the ONE declaration, not restated here."""
		schema = describe.builder_schema(
			on_doctype="CRM Task", vertical=fx.GRAIN["vertical"], group="", program="",
		)
		self.assertEqual(actions.reachable_targets("CRM Task"), ["CRM Lead", "CRM Task"])
		self.assertIn(_LEAD_FIELD, _keys(schema["set_targets"], doctype="CRM Lead"))

	def test_every_offered_field_says_which_record_it_belongs_to(self):
		"""A flat list of names across two doctypes cannot be rendered under a Target without this."""
		schema = describe.builder_schema(on_doctype="CRM Task")
		for descriptor in schema["set_targets"]:
			with self.subTest(field=descriptor["key"]):
				self.assertIn(descriptor.get("doctype"), actions.reachable_targets("CRM Task"))

	# --- the picker's last two lies ----------------------------------------------------------------------

	def test_a_field_is_offered_once_however_many_contracts_tick_it(self):
		"""`custom_substage` went out TWICE on the wire (4 rows, one duplicate). A rule grain's blank axis
		means ANY, so a field ticked by two contracts at different grains matches twice — and the picker
		listed the row, not the field. Killed at source; the frontend dedupe is now a belt, not the fix."""
		offered = describe._settable_fields("CRM Lead", fx.GRAIN["vertical"], "", "")
		keys = [d["key"] for d in offered]

		self.assertEqual(
			sorted(keys), sorted(set(keys)),
			f"the picker offers a field twice: {sorted(k for k in set(keys) if keys.count(k) > 1)}",
		)

	def test_the_duplicate_is_gone_from_the_rule_forms_picker_too(self):
		"""`builder_schema` feeds the automation RULE form's Set-field picker through the SAME function,
		so the duplicate was visible on a second surface. One source, one fix, both surfaces."""
		schema = describe.builder_schema(on_doctype="CRM Lead", vertical=fx.GRAIN["vertical"])
		keys = [d["key"] for d in schema["set_targets"]]

		self.assertEqual(sorted(keys), sorted(set(keys)), "the rule form still offers a field twice")

	def test_a_tab_break_is_offered_by_no_picker(self):
		"""A Tab Break is layout, not data. It was missing from `_STRUCTURAL_FIELDTYPES`, so a form's tab
		was offered as a field a rule could test — and there is nothing to read off it."""
		self.assertIn("Tab Break", describe._STRUCTURAL_FIELDTYPES)

		doctype = frappe.db.get_value("DocField", {"fieldtype": "Tab Break", "parenttype": "DocType"}, "parent")
		self.assertTrue(doctype, "no doctype on this bench declares a Tab Break — the lock proves nothing")

		tabs = {df.fieldname for df in frappe.get_meta(doctype).fields if df.fieldtype == "Tab Break"}
		offered = {d["key"] for d in describe.fields_for_doctype(doctype)}

		self.assertEqual(
			tabs & offered, set(), f"{doctype} offers layout elements as fields: {sorted(tabs & offered)}"
		)
