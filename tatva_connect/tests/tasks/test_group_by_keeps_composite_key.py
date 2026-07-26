# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Grouping the task list by task type keys on the composite PK and labels through `_link_titles`.

`CRM Task Type` autonames `{vertical}::{group}::{program}::{type_name}`, so two grains may both run a
"Welcome Call" and the two are different types with different schemas. A group-by must therefore stay
keyed on that PK — and the header must still read "Welcome Call", not
`ZZ One Brain Line::ZZ Group By Line A::::ZZ Welcome Call`.

The tempting fix — a `fetch_from` name column on `CRM Task`, or resolving the title into the row on
the server — is the defect this file forbids (plan D14, and the header comment of
`frontend/src/tatva/linkTitle.js` says the same): both grains' rows would carry the identical string,
the option list would collapse to ONE value, and the two types' tasks would merge under one header.
The title rides BESIDE the key instead, in the `_link_titles` map `get_data` already attaches.

So this asserts the server contract the header renders from:
  * the group options are the two composite PKs — two groups, not one,
  * `_link_titles` carries a clean title for each,
  * both titles are the SAME string, which is exactly why the key may not be replaced by it.

`mint_type` COMMITS (the composer reads its types live), so the two types are minted once in
`setUpClass` and torn down in `tearDownClass`; the tasks are per-test and roll back.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.tasks.test_group_by_keeps_composite_key
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.tests.activity import task_type_fixture

# ONE type_name, two grains — the collision the composite PK exists to survive.
TYPE_NAME = "ZZ Welcome Call"
GROUP_A = "ZZ Group By Line A"
GROUP_B = "ZZ Group By Line B"

SCHEMA = ({"label": "ZZ Note", "fieldname": "zz_note", "fieldtype": "Small Text"},)

TASK_TYPE_DOCTYPE = "CRM Task Type"


def _get_data(**kwargs):
	"""The list endpoint as the browser reaches it — through the override map, so `_link_titles` is
	attached by the same code the real request runs."""
	return frappe.get_attr(frappe.override_whitelisted_method("crm.api.doc.get_data"))(**kwargs)


class TestGroupByKeepsCompositeKey(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.type_a = task_type_fixture.mint_type(TYPE_NAME, SCHEMA, group=GROUP_A)
		cls.type_b = task_type_fixture.mint_type(TYPE_NAME, SCHEMA, group=GROUP_B)

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		task_type_fixture.teardown()
		super().tearDownClass()

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")
		for task_type in (self.type_a, self.type_b):
			frappe.get_doc({
				"doctype": "CRM Task", "title": f"ZZ group-by probe {task_type}",
				"status": "Todo", "custom_task_type": task_type,
			}).insert(ignore_permissions=True)

	def _grouped(self):
		return _get_data(
			doctype="CRM Task",
			filters={"custom_task_type": ["in", [self.type_a, self.type_b]]},
			order_by="modified desc",
			view={"view_type": "group_by", "group_by_field": "custom_task_type"},
		)

	def test_the_two_types_share_a_name_and_differ_only_by_grain(self):
		"""The premise. Same title, different key — without that this file tests nothing."""
		self.assertNotEqual(self.type_a, self.type_b)
		self.assertEqual(
			frappe.db.get_value(TASK_TYPE_DOCTYPE, self.type_a, "type_name"),
			frappe.db.get_value(TASK_TYPE_DOCTYPE, self.type_b, "type_name"),
		)

	def test_two_grains_sharing_a_type_name_produce_two_groups(self):
		"""The merge test. One option per type, each one the composite PK the list filters on."""
		options = self._grouped()["group_by_field"]["options"]
		self.assertEqual(
			sorted(o for o in options if o in (self.type_a, self.type_b)),
			sorted([self.type_a, self.type_b]),
			f"the two grains' types did not stay separate — options were {options}",
		)

	def test_each_group_carries_a_clean_title_beside_its_key(self):
		"""The label. The header reads the title out of `_link_titles`; the row and the option keep the
		key. Both titles are the plain type_name — identical, which is why the key may not become it."""
		result = self._grouped()
		titles = result["_link_titles"]
		for task_type in (self.type_a, self.type_b):
			# The stored type_name, not the string minted with: CRMTaskType.validate normalizes it (taxonomy/normalize.py).
			stored = frappe.db.get_value(TASK_TYPE_DOCTYPE, task_type, "type_name")
			shipped = titles.get(f"{TASK_TYPE_DOCTYPE}::{task_type}")
			self.assertEqual(
				shipped, stored,
				f"no clean title shipped for {task_type} — the header would print the :: key",
			)
			self.assertNotIn("::", shipped or "::", "the header would print the composite key")

	def test_the_row_still_holds_the_key_not_the_title(self):
		"""What a denormalised name column would have destroyed: click-to-filter and the group key both
		send the row's value back to the server, so the row must never carry the label."""
		for row in self._grouped()["data"]:
			self.assertIn(row.get("custom_task_type"), (self.type_a, self.type_b))
