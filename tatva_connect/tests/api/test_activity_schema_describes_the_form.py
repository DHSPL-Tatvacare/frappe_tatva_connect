# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Discovery describes the form a partner will actually be held to.

THE DEFECT. `activity_schema` published 26 fields of which 25 read `OPTIONAL, required: false`, and said
nothing about the chain that decides whether a field is on the form at all — while `compute_activity`
enforced that chain on every write. A partner was held to a state it had no way to query: it could send
`month_or_quantity` and be told the field "was not shown on this form", having been handed a descriptor
that described it as an ordinary optional Select.

THE ORACLE. The form's own compiled `depends_on` is the truth every save is judged by, so it is what
discovery is checked against here — never a hand-written expectation. A field the form treats as
conditional must be published as conditional, for every task type on the bench, or these go red.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.api.test_activity_schema_describes_the_form
"""
import unittest

import frappe

from tatva_connect.activity import api as activity_brain
from tatva_connect.api import partner_activity
from tatva_connect.api._base import select_values

# What the compiler emits for "no condition": a field every rep always sees. Anything else is a condition.
_ALWAYS = ("", "eval:1", "eval:(1)")


def _is_conditional(field):
	return (field.get("depends_on") or "").strip() not in _ALWAYS


class TestSelectValuesAreAList(unittest.TestCase):
	"""`options` is frappe's newline storage. A partner reads `allowed_values`."""

	def test_a_leading_blank_is_not_published_as_a_value(self):
		# Frappe spells "blank is allowed" as a leading empty line; a naive split yields ['', 'Yes', 'No'].
		self.assertEqual(select_values("\nYes\nNo"), ["Yes", "No"])

	def test_the_vocabulary_survives_intact(self):
		self.assertEqual(select_values("Converted\nDropped\nInterested"),
		                 ["Converted", "Dropped", "Interested"])

	def test_nothing_is_not_a_vocabulary(self):
		self.assertEqual(select_values(""), [])
		self.assertEqual(select_values(None), [])


class TestConditionsMatchTheCompiledForm(unittest.TestCase):
	"""DIFFERENTIAL. Discovery is checked against the compiled form, over every type on the bench."""

	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.types = frappe.get_all("CRM Task Type", pluck="name")

	def test_every_conditional_field_says_so(self):
		"""RED before this: `month_or_quantity` is conditional on two answers and published as optional."""
		checked = missed = 0
		for name in self.types:
			tt = frappe.get_cached_doc("CRM Task Type", name)
			conditions = activity_brain.field_conditions(tt)
			for f in activity_brain.get_schema(name):
				if not _is_conditional(f):
					continue
				checked += 1
				if not (conditions.get(f["fieldname"]) or {}).get("conditional"):
					missed += 1
					print(f"   unpublished condition: {name} :: {f['fieldname']} -> {f['depends_on']}")
		if not checked:
			self.skipTest("no conditional field on this bench")
		self.assertEqual(missed, 0, f"{missed} of {checked} conditional fields were published as plain")

	def test_a_published_condition_names_a_real_field(self):
		"""A condition a caller cannot act on is worse than none: every `field` it names must be a field
		of the same form, or the caller is told to check something it was never offered."""
		for name in self.types:
			tt = frappe.get_cached_doc("CRM Task Type", name)
			declared = {f["fieldname"] for f in activity_brain.get_schema(name)}
			for fieldname, entry in activity_brain.field_conditions(tt).items():
				for clauses in (entry.get("shown_when") or {}).values():
					for clause in clauses:
						for atom in clause["all_of"]:
							self.assertIn(atom["field"], declared,
							              f"{name} :: {fieldname} is conditioned on an undeclared field")

	def test_a_field_no_rule_can_reveal_is_published_as_never_shown(self):
		"""A blank-When Hide with no Show row closes a field forever (`eval:0`). Publishing nothing for it
		reads as "always shown", which is the opposite of the truth."""
		found = False
		for name in self.types:
			tt = frappe.get_cached_doc("CRM Task Type", name)
			conditions = activity_brain.field_conditions(tt)
			for f in activity_brain.get_schema(name):
				if (f.get("depends_on") or "").strip() != "eval:0":
					continue
				found = True
				shown = (conditions.get(f["fieldname"]) or {}).get("shown_when")
				self.assertEqual(shown, {"any_of": []},
				                 f"{name} :: {f['fieldname']} is never shown and does not say so")
		if not found:
			self.skipTest("no permanently hidden field on this bench")


class TestListAndDescribeAreDifferentAnswers(unittest.TestCase):
	"""AIP-131/132: listing the types a lead runs is not describing one of them."""

	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")

	def setUp(self):
		self._form = frappe.form_dict

	def tearDown(self):
		frappe.form_dict = self._form

	def _hit(self, **args):
		frappe.local.response = frappe._dict()
		frappe.form_dict = frappe._dict(args)
		partner_activity.activity_schema()
		return dict(frappe.local.response)

	def _a_lead_with_types(self):
		for lead in frappe.get_all("CRM Lead", pluck="name", limit=500):
			if activity_brain.list_types_for_lead(lead):
				return lead
		return None

	def test_listing_answers_names_and_describing_answers_fields(self):
		lead = self._a_lead_with_types()
		if not lead:
			self.skipTest("no lead on this bench runs an activity type")
		listed = self._hit(lead=lead)["data"]["task_types"]
		self.assertTrue(listed)
		for row in listed:
			self.assertNotIn("fields", row, "listing carried a whole field schema")
			self.assertIn("field_count", row)

		described = self._hit(lead=lead, task_type=listed[0]["name"])["data"]["task_types"]
		self.assertEqual(len(described), 1, "describing one type answered with several")
		self.assertEqual(len(described[0]["fields"]), listed[0]["field_count"],
		                 "the count a listing promises is not the number of fields described")

	def test_an_unknown_type_is_a_not_found_that_says_where_to_look(self):
		"""Asserted on the RESPONSE, not on a raise: `@_api` is what turns the throw into the contract, so
		the envelope is what a partner actually receives and the only thing worth pinning."""
		lead = self._a_lead_with_types()
		if not lead:
			self.skipTest("no lead on this bench runs an activity type")
		answer = self._hit(lead=lead, task_type="ZZ No Such Activity Type")
		frappe.clear_messages()
		frappe.local.message_log = []
		self.assertEqual(answer["status"], "error")
		self.assertEqual(answer["error"]["code"], "not_found")
		self.assertIn("task_type", answer["error"].get("fields") or [])
