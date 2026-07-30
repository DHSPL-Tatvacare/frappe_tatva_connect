# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The location gate is a RULE PREDICATE, not a second predicate language.

`location_when` used to hold a hand-typed `<field>==<value>` / `<field> in a|b` string parsed by
`location.api._match_condition`: its own grammar, its own operator set, its own evaluator, sitting beside
`_rule_atom` + `_field_visible` which already do that job for every rule on the form. Three consequences,
all of them real:

  1. **A declared condition could silently disable the gate.** An unparseable string returned False, which
     `location_required` read as "location not required" — and `compute_activity` then wrote a visit audit
     row saying "Not Required", a record asserting the gate did not apply.
  2. **One gate judged three different bags.** `compute_activity` passed the raw submission, the
     `enforce_location` backstop passed values reconstructed from storage, and the rules in the same request
     evaluated against `_settled` (hidden fields blanked, D22). So a condition on a HIDDEN field fired while
     a visibility rule on that same field correctly read it blank.
  3. **A condition could name a field the form never asks**, in which case it can never hold and the type
     read as location-guarded while guarding nothing.

Now: three declared columns (`location_condition_field` · `location_operator` · `location_condition_value`),
compiled by `_rule_atom`, evaluated by `_field_visible`, against the bag `_settled` produces. There is no
second grammar and `_match_condition` is deleted.

Run:
    bench --site <site> run-tests --app tatva_connect \
        --module tatva_connect.tests.activity.test_location_condition_one_seam
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.location import api as location_api
from tatva_connect.tests.activity import task_type_fixture

TYPE_NAME = "ZZ Location Predicate Probe"

MEETING = "zz_meeting_type"
NOTE = "zz_visit_note"

# A form where the condition field is only SHOWN once the outcome says so, which is what makes the
# settled-bag question answerable: the rep who never saw the field cannot have chosen Physical Visit.
OUTCOME = "zz_outcome"

SCHEMA = (
	{"label": "ZZ Outcome", "fieldname": OUTCOME, "fieldtype": "Select", "options": "Connected\nNot connected"},
	{"label": "ZZ Meeting Type", "fieldname": MEETING, "fieldtype": "Select",
	 "options": "Physical Visit\nPhone Call"},
	{"label": "ZZ Visit Note", "fieldname": NOTE, "fieldtype": "Small Text"},
)

# The meeting type is revealed only when Connected, so it is HIDDEN for every other answer.
RULES = (
	{"rule_label": "ZZ onload", "action": "Show", "targets": OUTCOME},
	{"rule_label": "ZZ onload hide", "action": "Hide", "targets": MEETING},
	{"rule_label": "ZZ connected", "condition_field": OUTCOME, "operator": "is",
	 "condition_value": "Connected", "action": "Show", "targets": MEETING},
)


class TestLocationConditionOneSeam(FrappeTestCase):
	"""One vocabulary, one evaluator, one bag."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.task_type = task_type_fixture.mint_type(TYPE_NAME, SCHEMA, rules=RULES)
		frappe.db.set_value("CRM Task Type", cls.task_type, {  # authz-ok: tier-c — test fixture setup
			"location_condition_field": MEETING,
			"location_operator": "is",
			"location_condition_value": "Physical Visit",
		})
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		task_type_fixture.teardown()
		super().tearDownClass()

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")
		self.tt = frappe.get_cached_doc("CRM Task Type", self.task_type)

	def test_there_is_no_second_predicate_parser(self):
		"""The lock that keeps this from growing back. A module-level grammar for conditions is a second
		language for what `_rule_atom` already expresses."""
		self.assertFalse(hasattr(location_api, "_match_condition"),
						 "_match_condition is back — the location gate has its own parser again")

	def test_the_condition_compiles_through_the_rule_seam(self):
		"""Not merely 'it works' — it must produce the SAME expression a rule row would, because that is the
		whole claim. If these two ever diverge there are two grammars again."""
		from tatva_connect.activity.api import _rule_atom

		as_a_rule = _rule_atom(frappe._dict(
			condition_field=MEETING, operator="is", condition_value="Physical Visit"))

		self.assertEqual(as_a_rule, f'doc.{MEETING}=="Physical Visit"',
						 "the rule seam stopped producing the expression this gate is built on")

	def test_the_condition_holds_when_the_answer_matches(self):
		answers = {OUTCOME: "Connected", MEETING: "Physical Visit"}

		self.assertTrue(location_api._condition_holds(self.tt, answers),
						"a declared condition did not hold for the answer that satisfies it")

	def test_the_condition_does_not_hold_for_another_answer(self):
		answers = {OUTCOME: "Connected", MEETING: "Phone Call"}

		self.assertFalse(location_api._condition_holds(self.tt, answers),
						 "the gate fired for an answer its condition does not name")

	def test_a_hidden_answer_is_INERT_for_the_gate(self):
		"""D22, which the old parser could not honour because it never settled the bag.

		`Not connected` hides the meeting type, so a submission still carrying `Physical Visit` describes a
		state the form cannot produce. Every rule reads such a value as blank; this gate must too, or the
		same request answers the same question two ways."""
		answers = {OUTCOME: "Not connected", MEETING: "Physical Visit"}

		self.assertFalse(location_api._condition_holds(self.tt, answers),
						 "a value for a HIDDEN field fired the location gate — the gate is judging a bag "
						 "the rules do not, so one request has two answers")

	def test_no_condition_declared_means_visit_mode_alone_decides(self):
		bare = task_type_fixture.mint_type(f"{TYPE_NAME} Bare", SCHEMA)

		self.assertFalse(
			location_api._condition_holds(frappe.get_cached_doc("CRM Task Type", bare), {MEETING: "Physical Visit"}),
			"a type declaring NO condition reported one as holding")

	def test_captures_location_is_one_rule(self):
		"""The boolean was written out twice in activity.api — once for the modal, once for the card."""
		self.assertTrue(location_api.captures_location("", MEETING), "a declared condition is location-capable")
		self.assertTrue(location_api.captures_location("In-Person", ""), "In-Person is location-capable")
		self.assertFalse(location_api.captures_location("Phone", ""), "a phone type with no condition is not")
		self.assertFalse(location_api.captures_location("", ""), "a type declaring neither is not")


class TestLocationConditionIsDeclared(FrappeTestCase):
	"""A predicate over an answer the form does not collect can never hold, so it is refused at authoring."""

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		task_type_fixture.teardown()
		super().tearDownClass()

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")

	def test_a_condition_on_an_undeclared_field_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			task_type_fixture.mint_type(
				f"{TYPE_NAME} Undeclared", SCHEMA,
				extra={"location_condition_field": "zz_no_such_question", "location_operator": "is"})

		self.assertIn("zz_no_such_question", str(caught.exception),
					  "the refusal must name the field that is not declared")

	def test_a_condition_on_a_LAYOUT_row_is_refused(self):
		"""A Section Break stores nothing, so a condition on one can never hold — the same reason
		`_validate_rules` refuses one as a When Field."""
		with self.assertRaises(frappe.ValidationError):
			task_type_fixture.mint_type(
				f"{TYPE_NAME} Marker", (*SCHEMA, {"label": "ZZ Head", "fieldname": "zz_head",
												  "fieldtype": "Section Break"}),
				extra={"location_condition_field": "zz_head", "location_operator": "is"})

	def test_a_value_the_field_does_not_offer_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			task_type_fixture.mint_type(
				f"{TYPE_NAME} Bad Value", SCHEMA,
				extra={"location_condition_field": MEETING, "location_operator": "is",
					   "location_condition_value": "ZZ Not An Option"})

		self.assertIn("ZZ Not An Option", str(caught.exception), "the refusal must name the bad value")

	def test_a_declared_condition_saves(self):
		name = task_type_fixture.mint_type(
			f"{TYPE_NAME} Good", SCHEMA,
			extra={"location_condition_field": MEETING, "location_operator": "is",
				   "location_condition_value": "Physical Visit"})

		self.assertTrue(frappe.db.exists("CRM Task Type", name), "a correct condition was refused")


class TestLinkFieldNamesADoctype(FrappeTestCase):
	"""A `Link` row with no Options hands the rep a picker over nothing, and the seeds carry such rows."""

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		task_type_fixture.teardown()
		super().tearDownClass()

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")

	def test_a_link_with_no_target_is_refused(self):
		with self.assertRaises(frappe.ValidationError) as caught:
			task_type_fixture.mint_type(
				f"{TYPE_NAME} Blind Link",
				({"label": "ZZ Coach", "fieldname": "zz_coach", "fieldtype": "Link"},))

		self.assertIn("ZZ Coach", str(caught.exception), "the refusal must name the field")

	def test_a_link_to_a_missing_doctype_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			task_type_fixture.mint_type(
				f"{TYPE_NAME} Bad Link",
				({"label": "ZZ Coach", "fieldname": "zz_coach", "fieldtype": "Link",
				  "options": "ZZ No Such DocType"},))

	def test_a_real_link_saves(self):
		name = task_type_fixture.mint_type(
			f"{TYPE_NAME} Real Link",
			({"label": "ZZ Coach", "fieldname": "zz_coach", "fieldtype": "Link", "options": "User"},))

		self.assertTrue(frappe.db.exists("CRM Task Type", name), "a Link naming a real DocType was refused")
