# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A Route can test a counter that lives in a SINGLETON child row.

LeadSquared reaches `Inactive Doctor` through three thresholds and nothing else — `RNR Count >= 6`,
`Not Reachable Count >= 6`, `Doctor Visit Not Completed Count Introductory >= 6`. Every one of those
counters lives on `CRM Lead Activity Metrics`, a singleton child table. `CRM Lead`'s meta carries the
Table field and never its columns, so the criterion vocabulary had no name for them and `context_for`
built no value: the predicate raised `PredicateError` and those three stages were unreachable forever.

Asserted on the VERDICT a predicate returns, never on a call — a test that checked the field was offered
would have stayed green while the value was still missing.

A multi-row section resolves to its LATEST row and that is asserted too: three lab reports read as the
newest one, which is the row the Data tab and a Smart View already show. `lead.multirow` is the ONE rule
deciding that; what is refused is `rows[0]`, the SCHEMA §3 scar, not multi-row itself.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import context as ctx_build
from tatva_connect.automation import describe, rules
from tatva_connect.workflow_engine import refs
from tatva_connect.workflow_engine.tests import fixtures as fx

_TABLE = "custom_lead_activity_metrics"
_COUNTER = "custom_rnr_count"
_REF = f"crm_lead.{_TABLE}.{_COUNTER}"
_THRESHOLD = {"type": "rule", "field": _REF, "operator": "at least", "value": 6}


class TestChildFieldReadable(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		cls.lead = fx.make_lead()
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.delete_doc("CRM Lead", cls.lead.name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _at(self, count):
		doc = frappe.get_doc("CRM Lead", self.lead.name)
		doc.set(_TABLE, [])
		doc.append(_TABLE, {_COUNTER: count})
		doc.save(ignore_permissions=True)
		return ctx_build.context_for(frappe.get_doc("CRM Lead", self.lead.name), {})

	def test_the_threshold_routes_on_the_counter(self):
		"""The whole point: the LSQ rule that reaches Inactive Doctor, and the value it turns on."""
		types = ctx_build.field_types_for("CRM Lead")
		self.assertTrue(rules.predicate_match(_THRESHOLD, self._at(6), types), "6 did not meet >= 6")
		self.assertFalse(rules.predicate_match(_THRESHOLD, self._at(5), types), "5 met >= 6")

	def test_the_value_really_comes_off_the_child_row(self):
		self.assertEqual(self._at(4).get(_REF), 4)

	def test_an_author_is_offered_the_counter(self):
		offered = {d["key"] for d in describe._criterion_fields("CRM Lead", *fx.AXES)}
		self.assertIn(f"{_TABLE}.{_COUNTER}", offered)

	def test_a_multi_row_section_reads_its_latest_row(self):
		"""Three lab reports, inserted out of order: a rule reads the newest, which is what the rep sees."""
		doc = frappe.get_doc("CRM Lead", self.lead.name)
		doc.set("custom_lab_profile", [])
		for report_date, hba1c in (("2025-08-14", "8.9"), ("2026-02-18", "6.1"), ("2025-11-22", "7.4")):
			doc.append("custom_lab_profile", {"report_date": report_date, "hba1c": hba1c})
		doc.save(ignore_permissions=True)

		values = ctx_build.context_for(frappe.get_doc("CRM Lead", self.lead.name), {})
		self.assertEqual(float(values.get("crm_lead.custom_lab_profile.hba1c")), 6.1)
		self.assertEqual(str(values.get("crm_lead.custom_lab_profile.report_date")), "2026-02-18")

	def test_a_multi_row_column_is_offered_to_an_author(self):
		"""The section is in the vocabulary, not skipped. WHICH of its columns is still the grain's call —
		a Goodflip-Care lab field is rightly absent from a TatvaPractice workflow."""
		offered = {d["key"] for d in describe._criterion_fields("CRM Lead", *fx.AXES)}
		self.assertTrue([k for k in offered if k.startswith("custom_lab_profile.")])

	def test_what_an_author_is_offered_is_a_subset_of_what_runs(self):
		"""THE invariant across the layers. The picker narrows by grain, the gate at publish and run time is
		wider, and the offer must never escape it — a field offered but ungated is a predicate an author can
		build and the evaluator then refuses with PredicateError, on a live journey. Both subjects, because
		a Task's children are activity storage and offering their raw columns leaked 44 such fields once."""
		for doctype in ("CRM Lead", "CRM Task"):
			with self.subTest(doctype=doctype):
				offered = {refs.of_record(doctype, d["key"])
				           for d in describe._criterion_fields(doctype, *fx.AXES)}
				gate = set(ctx_build.field_types_for(doctype))
				self.assertEqual(offered - gate, set(), f"{doctype}: offered but not evaluable")
