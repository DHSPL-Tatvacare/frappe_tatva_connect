# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The AUTHORING half of the condition contract — that what the builder offers is what the evaluator runs.

`test_operators.py` proves the evaluator by handing `_one_match` a criterion built by hand. That is the
run-time half, and it stayed green through both defects locked here, because a hand-built criterion never
asks how an AUTHOR could have produced it:

  * `changed from…to` reads `c.from_value`, and `operator_shapes()` did not put it in the bucket the
    builder renders a second box for — so the only criterion an author could build had a blank first end,
    which the evaluator reads as "was blank and is now X".
  * the field picker offered fields whose TYPE has no operators at all — a form's layout breaks, and the
    two types `_SCHEMA_TYPES` forgot — so a condition could be started on them and never finished.

Both are differential, per S.6: the verdict is never hardcoded here. The operator side asks the EVALUATOR
which operators read a second value (by mutating it and watching the verdict move) and compares that to
what the builder declares; the field side asks the builder's own operator table whether every field it
offers can be tested at all. A planted control proves each probe is not blind.
"""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import describe, rules


def _criterion(field, operator, value=None, from_value=None):
	"""A minimal duck-typed criterion row — the same shape `test_operators._criterion` builds."""
	return frappe._dict(field=field, operator=operator, value=value, from_value=from_value)


def _reads_from_value(operator):
	"""Does the EVALUATOR read `from_value` for this operator? Asked by mutating it, not by a list.

	One context serves every operator: `f` moved from "A" to "B". With `value="B"` pinned, only an
	operator that also reads the FIRST end can tell "A" from "Z", so a verdict that moves is proof it
	read it."""
	ctx = {"f": "B", "f__before": "A"}
	kept = rules._one_match(_criterion("f", operator, value="B", from_value="A"), ctx, "Data")
	moved = rules._one_match(_criterion("f", operator, value="B", from_value="Z"), ctx, "Data")
	return kept != moved


class TestTwoEndedOperatorsAreAuthorable(FrappeTestCase):
	def test_the_probe_is_not_blind(self):
		"""The planted control: a known two-ended operator is seen, a known one-ended one is not."""
		self.assertTrue(_reads_from_value("is between"))
		self.assertFalse(_reads_from_value("is"))

	def test_every_operator_that_reads_a_second_value_is_offered_a_second_box(self):
		"""THE lock. `range` is the shape the builder renders two value controls for; an operator the
		evaluator reads two ends of and that is missing here can only ever be authored half-filled."""
		two_ended = {op for op in rules.KNOWN_OPERATORS if _reads_from_value(op)}
		self.assertEqual(two_ended, set(describe.operator_shapes()["range"]))

	def test_a_two_ended_operator_is_in_exactly_one_bucket(self):
		"""A shape is how the control is drawn; two answers would draw two different controls."""
		shapes = describe.operator_shapes()
		for operator in shapes["range"]:
			self.assertNotIn(operator, shapes["none"])
			self.assertNotIn(operator, shapes["list"])

	def test_the_ends_are_ordered_before_then_after(self):
		"""What the two boxes MEAN, proven on the evaluator: `from_value` is the first end in both
		families — the low bound of a range, and the value a field moved away from."""
		self.assertTrue(
			rules._one_match(_criterion("n", "is between", value="20", from_value="5"), {"n": "10"}, "Int")
		)
		self.assertTrue(
			rules._one_match(
				_criterion("stage", "changed from…to", value="Qualified", from_value="New"),
				{"stage": "Qualified", "stage__before": "New"},
				"Select",
			)
		)


class TestEveryOfferedFieldCanBeTested(FrappeTestCase):
	"""A field the picker offers and the operator table cannot answer for is a dead end on screen: the
	author picks it, the operator select is empty, and nothing says why."""

	def test_the_sweep_is_not_blind(self):
		"""Planted: a type nothing declares operators for is exactly what this sweep must catch."""
		self.assertFalse(describe.operators_by_type().get("Geolocation"))

	def test_every_field_the_canvas_picker_offers_has_operators(self):
		"""`fields_for_doctype` is what `refs.readable_for` hands the workflow canvas's field picker."""
		operators = describe.operators_by_type()
		for doctype in ("CRM Lead", "CRM Task"):
			dead = {f["key"]: f["type"] for f in describe.fields_for_doctype(doctype) if not operators.get(f["type"])}
			self.assertEqual(dead, {}, f"{doctype} offers fields no operator can test")

	def test_every_field_the_rule_builder_offers_has_operators(self):
		"""The other picker reading the same vocabulary — `builder_schema`'s typed catalog."""
		operators = describe.operators_by_type()
		for doctype in ("CRM Lead", "CRM Task"):
			dead = {f["key"]: f["type"] for f in describe._typed_catalog(doctype) if not operators.get(f["type"])}
			self.assertEqual(dead, {}, f"{doctype} offers fields no operator can test")

	def test_a_layout_break_declared_on_a_form_is_never_offered(self):
		"""Deterministic twin of the sweep: a site with no activity forms yet would pass it vacuously."""
		layout = frappe._dict(fieldname="zzpik_layout", label="Layout", fieldtype="Section Break", options=None)
		real = frappe._dict(fieldname="zzpik_outcome", label="Outcome", fieldtype="Data", options=None)
		original = describe.activity_schema_fields
		describe.activity_schema_fields = lambda: {"zzpik_layout": layout, "zzpik_outcome": real}
		try:
			offered = {f["key"] for f in describe.fields_for_doctype("CRM Task")}
			catalogued = {f["key"] for f in describe._typed_catalog("CRM Task")}
		finally:
			describe.activity_schema_fields = original
		self.assertIn("zzpik_outcome", offered)
		self.assertIn("zzpik_outcome", catalogued)
		self.assertNotIn("zzpik_layout", offered)
		self.assertNotIn("zzpik_layout", catalogued)

	def test_a_declared_option_set_gets_its_picker_whatever_control_declares_it(self):
		"""An Autocomplete declares its choices exactly as a Select does — `_LISTED_FIELDTYPES` already
		says so where they are MERGED, and the pick source is the same answer."""
		self.assertEqual(
			describe._pick_for("Autocomplete", "Male\nFemale", "custom_gender"),
			describe._pick_for("Select", "Male\nFemale", "custom_gender"),
		)


if __name__ == "__main__":
	unittest.main()
