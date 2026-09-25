# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""An activity answer is held to what `activity_schema` publishes: a Select outside its `allowed_values` is
refused naming the field, and a number reads back as a number. Both live in the partner API only; the task
form's own save and reader (`activity.api`) are untouched.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.api.test_activity_values_hold_to_the_schema
"""
import unittest

import frappe
from frappe.model import numeric_fieldtypes

from tatva_connect.activity import api as activity_brain
from tatva_connect.api import partner_activity
from tatva_connect.tests.api.partner_fixture import minimal_answers

GRAIN = {"custom_vertical": "Goodflip", "custom_group": "India"}


def _hit(fn, **args):
	frappe.local.response = frappe._dict()
	frappe.form_dict = frappe._dict(args)
	fn()
	return dict(frappe.local.response)


def _published(lead, task_type):
	"""{fieldname: descriptor} exactly as activity_schema publishes the type for this lead."""
	data = _hit(partner_activity.activity_schema, lead=lead, task_type=task_type)["data"]
	return {f["fieldname"]: f for f in data["task_types"][0]["fields"]}


def _find(pick):
	"""(lead, task_type, answers, fieldname) for the first Goodflip form whose minimal answers hold a field `pick` accepts."""
	for lead in frappe.get_all("CRM Lead", filters=GRAIN, pluck="name", limit=50):
		for t in activity_brain.list_types_for_lead(lead):
			answers = minimal_answers(t["name"])
			fields = _published(lead, t["name"])
			hit = next((fn for fn in answers if pick(fields.get(fn) or {})), None)
			if hit:
				return lead, t["name"], answers, hit
	return None


class TestActivityValuesHoldToTheSchema(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")

	def setUp(self):
		self._form = frappe.form_dict
		self.sp = f"vals_{frappe.generate_hash(length=6)}"
		frappe.db.savepoint(self.sp)

	def tearDown(self):
		frappe.form_dict = self._form
		try:
			frappe.db.rollback(save_point=self.sp)
		except Exception:
			frappe.db.rollback()

	def test_a_select_answer_outside_allowed_values_is_refused(self):
		"""RED before this: the value was never refused by name (saved as sent, or refused for some other field)."""
		found = _find(lambda f: f.get("type") == "Select" and f.get("allowed_values"))
		if not found:
			self.skipTest("no Goodflip form on this bench answers a listed Select")
		lead, task_type, answers, field = found
		r = _hit(partner_activity.activity_create, lead=lead, task_type=task_type,
		         values={**answers, field: "Not A Listed Value"})
		self.assertEqual(r.get("status"), "error", r)
		self.assertEqual((r["error"]["code"], r["error"].get("fields")), ("validation_error", [field]))

	def test_a_number_answer_reads_back_as_a_number(self):
		"""RED before this: the shared reader's text reached the partner, so an Int read "42"."""
		found = _find(lambda f: f.get("type") in numeric_fieldtypes)
		if not found:
			self.skipTest("no Goodflip form on this bench answers a number")
		lead, task_type, answers, field = found
		created = _hit(partner_activity.activity_create, lead=lead, task_type=task_type, values=answers)
		self.assertEqual(created.get("status"), "success", created.get("error"))
		value = _hit(partner_activity.activity_get, name=created["data"]["name"])["data"]["values"].get(field)
		self.assertIsInstance(value, (int, float), f"{field} read back as {value!r}")
