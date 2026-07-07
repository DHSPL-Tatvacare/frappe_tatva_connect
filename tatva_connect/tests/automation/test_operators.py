# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Task 3 sign-off - rules._one_match on the full v2 word-operator set (plan Part A).

Table-driven, real Frappe types (frappe.utils.getdate/get_datetime/flt/cint cast the same way the
evaluator does), no hardcoded verdict beyond the operator's own definition. Every operator carries
ONE planted known-bad value that must NOT match, so the suite proves it isn't blind (S.6, recall==1.0).
"""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import rules


def _criterion(field, operator, value=None, from_value=None):
	"""A minimal duck-typed criterion row - rules._one_match only reads .field/.operator/.value/.from_value."""
	class _C:
		pass
	c = _C()
	c.field = field
	c.operator = operator
	c.value = value
	c.from_value = from_value
	return c


class TestOperators(FrappeTestCase):
	"""One matching case + one planted-bad case per operator in Part A's frozen set."""

	# --- is / is not (Select, type-aware) ---------------------------------------------------
	def test_is_matches(self):
		ctx = {"stage": "Won"}
		self.assertTrue(rules._one_match(_criterion("stage", "is", "Won"), ctx, "Select"))

	def test_is_planted_bad(self):
		ctx = {"stage": "Won"}
		self.assertFalse(rules._one_match(_criterion("stage", "is", "Lost"), ctx, "Select"))

	def test_is_not_matches(self):
		ctx = {"stage": "Won"}
		self.assertTrue(rules._one_match(_criterion("stage", "is not", "Lost"), ctx, "Select"))

	def test_is_not_planted_bad(self):
		ctx = {"stage": "Won"}
		self.assertFalse(rules._one_match(_criterion("stage", "is not", "Won"), ctx, "Select"))

	# --- greater than / less than (Float, typed ordering) -----------------------------------
	def test_greater_than_matches(self):
		ctx = {"amount": 500.0}
		self.assertTrue(rules._one_match(_criterion("amount", "greater than", "100"), ctx, "Float"))

	def test_greater_than_planted_bad(self):
		ctx = {"amount": 50.0}
		self.assertFalse(rules._one_match(_criterion("amount", "greater than", "100"), ctx, "Float"))

	def test_less_than_matches(self):
		ctx = {"amount": 50.0}
		self.assertTrue(rules._one_match(_criterion("amount", "less than", "100"), ctx, "Float"))

	def test_less_than_planted_bad(self):
		ctx = {"amount": 500.0}
		self.assertFalse(rules._one_match(_criterion("amount", "less than", "100"), ctx, "Float"))

	# --- at least / at most (Int, typed >= / <=) ---------------------------------------------
	def test_at_least_matches(self):
		ctx = {"age": 18}
		self.assertTrue(rules._one_match(_criterion("age", "at least", "18"), ctx, "Int"))

	def test_at_least_planted_bad(self):
		ctx = {"age": 17}
		self.assertFalse(rules._one_match(_criterion("age", "at least", "18"), ctx, "Int"))

	def test_at_most_matches(self):
		ctx = {"age": 18}
		self.assertTrue(rules._one_match(_criterion("age", "at most", "18"), ctx, "Int"))

	def test_at_most_planted_bad(self):
		ctx = {"age": 19}
		self.assertFalse(rules._one_match(_criterion("age", "at most", "18"), ctx, "Int"))

	# --- is one of / is not one of (comma/newline membership list) --------------------------
	def test_is_one_of_matches(self):
		ctx = {"source": "Referral"}
		c = _criterion("source", "is one of", "Meta Form,Referral,Walk-in")
		self.assertTrue(rules._one_match(c, ctx, "Select"))

	def test_is_one_of_planted_bad(self):
		ctx = {"source": "Cold Call"}
		c = _criterion("source", "is one of", "Meta Form,Referral,Walk-in")
		self.assertFalse(rules._one_match(c, ctx, "Select"))

	def test_is_not_one_of_matches(self):
		ctx = {"source": "Cold Call"}
		c = _criterion("source", "is not one of", "Meta Form\nReferral")
		self.assertTrue(rules._one_match(c, ctx, "Select"))

	def test_is_not_one_of_planted_bad(self):
		ctx = {"source": "Referral"}
		c = _criterion("source", "is not one of", "Meta Form\nReferral")
		self.assertFalse(rules._one_match(c, ctx, "Select"))

	# --- contains / does not contain (case-insensitive substring, text) ---------------------
	def test_contains_matches(self):
		ctx = {"notes": "Patient requested a Callback tomorrow"}
		self.assertTrue(rules._one_match(_criterion("notes", "contains", "callback"), ctx, "Data"))

	def test_contains_planted_bad(self):
		ctx = {"notes": "Patient requested a callback tomorrow"}
		self.assertFalse(rules._one_match(_criterion("notes", "contains", "webhook"), ctx, "Data"))

	def test_does_not_contain_matches(self):
		ctx = {"notes": "No issues reported"}
		self.assertTrue(rules._one_match(_criterion("notes", "does not contain", "escalate"), ctx, "Data"))

	def test_does_not_contain_planted_bad(self):
		ctx = {"notes": "Please ESCALATE this lead"}
		self.assertFalse(rules._one_match(_criterion("notes", "does not contain", "escalate"), ctx, "Data"))

	# --- is set / is not set (non-blank / blank) ---------------------------------------------
	def test_is_set_matches(self):
		ctx = {"phone": "9999999999"}
		self.assertTrue(rules._one_match(_criterion("phone", "is set"), ctx, "Data"))

	def test_is_set_planted_bad(self):
		ctx = {"phone": ""}
		self.assertFalse(rules._one_match(_criterion("phone", "is set"), ctx, "Data"))

	def test_is_not_set_matches(self):
		ctx = {"phone": None}
		self.assertTrue(rules._one_match(_criterion("phone", "is not set"), ctx, "Data"))

	def test_is_not_set_planted_bad(self):
		ctx = {"phone": "9999999999"}
		self.assertFalse(rules._one_match(_criterion("phone", "is not set"), ctx, "Data"))

	# --- is between (inclusive range, Date) ---------------------------------------------------
	def test_is_between_matches(self):
		ctx = {"visit_date": "2026-07-10"}
		c = _criterion("visit_date", "is between", value="2026-07-15", from_value="2026-07-01")
		self.assertTrue(rules._one_match(c, ctx, "Date"))

	def test_is_between_planted_bad(self):
		ctx = {"visit_date": "2026-08-01"}
		c = _criterion("visit_date", "is between", value="2026-07-15", from_value="2026-07-01")
		self.assertFalse(rules._one_match(c, ctx, "Date"))

	# --- changed to (new value + field actually changed; needs __before) --------------------
	def test_changed_to_matches(self):
		ctx = {"stage": "Won", "stage__before": "Negotiation"}
		self.assertTrue(rules._one_match(_criterion("stage", "changed to", "Won"), ctx, "Select"))

	def test_changed_to_planted_bad_same_value(self):
		# field present but unchanged (before == after) must NOT match even though value == new value
		ctx = {"stage": "Won", "stage__before": "Won"}
		self.assertFalse(rules._one_match(_criterion("stage", "changed to", "Won"), ctx, "Select"))

	def test_changed_to_missing_before_is_non_match_not_raise(self):
		ctx = {"stage": "Won"}  # no stage__before -> Created event, fail-soft non-match
		self.assertFalse(rules._one_match(_criterion("stage", "changed to", "Won"), ctx, "Select"))

	# --- changed from…to (old value + new value both pinned) --------------------------------
	def test_changed_from_to_matches(self):
		ctx = {"stage": "Won", "stage__before": "Negotiation"}
		c = _criterion("stage", "changed from…to", value="Won", from_value="Negotiation")
		self.assertTrue(rules._one_match(c, ctx, "Select"))

	def test_changed_from_to_planted_bad_wrong_from(self):
		ctx = {"stage": "Won", "stage__before": "Qualified"}
		c = _criterion("stage", "changed from…to", value="Won", from_value="Negotiation")
		self.assertFalse(rules._one_match(c, ctx, "Select"))

	def test_changed_from_to_missing_before_is_non_match_not_raise(self):
		ctx = {"stage": "Won"}  # no stage__before
		c = _criterion("stage", "changed from…to", value="Won", from_value="Negotiation")
		self.assertFalse(rules._one_match(c, ctx, "Select"))

	# --- shared comparator: one bad cast never raises (fail-soft parity, S.6-adjacent) -------
	def test_uncastable_value_is_non_match_not_raise(self):
		ctx = {"amount": "not-a-number"}
		self.assertFalse(rules._one_match(_criterion("amount", "greater than", "100"), ctx, "Float"))


if __name__ == "__main__":
	unittest.main()
