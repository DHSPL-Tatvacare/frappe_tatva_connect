# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""An operator may change a declared field's TYPE later, and every answer already stored survives it.

The case this exists for is real and was named by the owner on 2026-07-30. The junior-coach fields are a
picked list on day one because the coaches are not User records yet. When they are added, the operator opens
the task type, changes `fieldtype` from Select to Link, sets `options` to User, and saves. Nothing else may
be required of them, and no answer already captured may move or be lost.

That holds because of D5 and it is worth locking rather than assuming:

  * a Select-like answer is stored as `Data` — `fieldtype` and `options` live on CRM Task Type Field and
    drive only the control, its validation and the Smart View dropdown;
  * `field_target` routes on `section` + `target` and never reads `fieldtype`;
  * `_TYPED_COLUMNS` names neither Select nor Link, so no typed mirror column changes either.

So the switch moves no data and rewrites no column. If any of those three ever stops being true this file
goes red, and the operator's upgrade path silently becomes a migration.

Run:
    bench --site <site> run-tests --app tatva_connect \
        --module tatva_connect.tests.activity.test_fieldtype_switch_is_storage_neutral
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.activity import api as activity_api
from tatva_connect.tests.activity import task_type_fixture

TYPE_NAME = "ZZ Fieldtype Switch Probe"

COACH = "zz_junior_coach"
ANSWER = "ZZ Coach Person"

# Day one: a picked list, because the people are not User records yet.
SCHEMA = (
	{"label": "ZZ Junior Coach", "fieldname": COACH, "fieldtype": "Select",
	 "options": "ZZ Coach Person\nZZ Other Person"},
)


class TestFieldtypeSwitchIsStorageNeutral(FrappeTestCase):
	"""Select on day one, Link on the day the coaches become users — same storage, same answers."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.task_type = task_type_fixture.mint_type(TYPE_NAME, SCHEMA)

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		task_type_fixture.teardown()
		super().tearDownClass()

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")
		self.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "ZZ Fieldtype Switch",
			"mobile_no": f"+9198127{int(frappe.generate_hash(length=8), 16) % 100000:05d}",
			"custom_vertical": task_type_fixture.VERTICAL, "custom_group": task_type_fixture.GROUP,
		}).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture, runs as Administrator

	def _row(self):
		return frappe.get_doc("CRM Task Type", self.task_type).schema[0]

	def _switch_to_link(self):
		"""Exactly what the operator does: change the type, name the doctype, save."""
		doc = frappe.get_doc("CRM Task Type", self.task_type)
		doc.schema[0].fieldtype = "Link"
		doc.schema[0].options = "User"
		doc.save()

	def test_the_home_does_not_move_when_the_type_changes(self):
		"""`field_target` must answer identically before and after — it routes on section + target, and
		neither is touched by the switch."""
		before = activity_api.field_target(self._row())

		self._switch_to_link()

		self.assertEqual(activity_api.field_target(self._row()), before,
						 "changing fieldtype moved where the answer is stored")

	def test_an_answer_stored_as_a_select_reads_back_after_the_switch(self):
		"""The property the operator is relying on. An answer captured on day one must still read back
		once the field has become a Link."""
		task = activity_api.save_activity(self.lead.name, self.task_type, {COACH: ANSWER})
		self.assertEqual(activity_api.task_detail(task)["task"]["values"].get(COACH), ANSWER,
						 "the answer did not read back even before the switch")

		self._switch_to_link()

		self.assertEqual(activity_api.task_detail(task)["task"]["values"].get(COACH), ANSWER,
						 "an answer captured while the field was a Select was lost when it became a Link")

	def test_the_switch_needs_no_second_step(self):
		"""One save, and the type is valid. If the switch required clearing a target or re-seeding an
		option set, the operator would have to be told — so the save either works alone or this is red."""
		self._switch_to_link()

		self.assertEqual(self._row().fieldtype, "Link", "the switch did not persist")
		self.assertEqual(self._row().options, "User", "the target doctype did not persist")

	def test_a_switch_to_link_without_a_target_doctype_is_refused(self):
		"""The other half: if the operator forgets to name the doctype, they are told at once rather than
		shipping a picker over nothing. This is the same rule that grandfathers the 14 seeded types —
		refused because THIS save introduces the state."""
		doc = frappe.get_doc("CRM Task Type", self.task_type)
		doc.schema[0].fieldtype = "Link"
		doc.schema[0].options = ""

		with self.assertRaises(frappe.ValidationError) as caught:
			doc.save()

		self.assertIn("ZZ Junior Coach", str(caught.exception), "the refusal must name the field")

	def test_a_select_with_no_options_refuses_nothing(self):
		"""Day one for the coach fields: the type is Select and the choices are not populated yet. Frappe
		skips a Select whose options are empty (base_document.py:1099), so the declaration waits without
		blocking anything."""
		doc = frappe.get_doc("CRM Task Type", self.task_type)
		doc.schema[0].options = ""
		doc.save()

		self.assertEqual(self._row().options, "", "a Select with no options was refused or rewritten")
