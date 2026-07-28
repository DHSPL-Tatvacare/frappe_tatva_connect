# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""W3 — an author picks a predicate value instead of typing it blind.

THE RED. `describe._descriptor` has always produced `options` — a Select's real choices, a Link's target
doctype. `refs.readable_for` kept `ref`, `label` and `type` and DROPPED it, so by the time a field reached
the canvas there was nothing to build a dropdown from. Every predicate on a Select field offered a
free-text box, and an author typed `Qualified` where the field really said `qualified` and the condition
silently never matched.

WHAT MOVES, AND WHAT DOES NOT. Only the field's declared CHOICES cross the seam. The identity still
changes there and only there — describe's bare `key` goes in, a namespaced `ref` comes out (W2.3,
refs.py:145) — and this carries a fact ABOUT a field, never a second word FOR one.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import describe
from tatva_connect.workflow_engine import refs, upstream
from tatva_connect.workflow_engine.tests import fixtures


def _a_select_field(doctype="CRM Lead"):
	"""A real Select field on the subject, taken from the describe brain rather than named here."""
	for field in describe.fields_for_doctype(doctype):
		if field["type"] == "Select" and field.get("options"):
			return field
	raise AssertionError(f"{doctype} declares no Select field with options — the fixture is wrong, not the code")


class TestTheSeamCarriesTheChoices(FrappeTestCase):
	def test_readable_for_keeps_the_options_describe_produced(self):
		field = _a_select_field()

		carried = next(f for f in refs.readable_for("CRM Lead") if f["ref"].endswith(field["key"]))

		self.assertEqual(carried["options"], field["options"])

	def test_the_identity_still_changes_and_the_choices_do_not(self):
		"""The seam's whole job: `key` in, `ref` out, and everything else untouched."""
		field = _a_select_field()

		carried = next(f for f in refs.readable_for("CRM Lead") if f["ref"].endswith(field["key"]))

		self.assertEqual(carried["ref"], refs.of_record("CRM Lead", field["key"]))
		self.assertNotIn("key", carried)


class TestTheCanvasIsOfferedThem(FrappeTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def _variables(self):
		workflow = fixtures.make_workflow(
			f"WF-OPTIONS-{frappe.generate_hash(length=6)}",
			[fixtures.trigger(), fixtures.node("n1", "Terminal")],
		)
		nodes = frappe.get_all(
			"CRM Workflow Node", filters={"workflow": workflow.name},
			fields=["node_id", "node_type", "config_json"],
		)
		return upstream.available_at(nodes, "n1")

	def test_a_select_field_reaches_the_picker_with_its_choices(self):
		field = _a_select_field()

		variable = next(v for v in self._variables() if v["ref"].endswith(f".{field['key']}"))

		self.assertEqual(variable["options"], field["options"])

	def test_a_field_with_no_choices_claims_none(self):
		"""Absent, not `None`: a row that carries the key is a row claiming to have choices."""
		plain = next(
			v for v in self._variables()
			if v["type"] in ("Data", "Small Text") and not v["ref"].endswith(".name")
		)

		self.assertNotIn("options", plain)
