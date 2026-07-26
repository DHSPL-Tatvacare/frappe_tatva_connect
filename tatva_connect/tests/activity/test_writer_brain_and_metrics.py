# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Two activity-brain audit gaps, closed and locked.

GAP 4 (second writer): _pin_review_file poked a HARDCODED 'document' key into the JSON payload,
never validating it was a declared field nor routing it by field_column. Fixed: it writes through
activity.api.set_schema_field, which validates the field is declared and routes it the SAME way
compute_activity does.

GAP 5 (drifted second catalog): tasks.metrics.TASK_TYPE_TO_METRIC is keyed by bare type_name, but after
rekey_task_types_composite the CRM Task's custom_task_type is 'vertical::group::program::type_name', so
every dict lookup missed and the (dormant) rollup counted nothing. Fixed: _metric_field normalises the
composite to its bare type_name before the lookup.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.activity.test_writer_brain_and_metrics
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.activity import api as activity_api
from tatva_connect.tasks import metrics
from tatva_connect.tests.activity import task_type_fixture


class TestActivityWriterBrain(FrappeTestCase):
	"""GAP 4 — a single activity-schema field is written through the brain, not a hardcoded payload key.

	The task is a real (uninserted) CRM Task rather than a stand-in dict: since Phase 2 the brain also
	writes the field's section child row, and only a document can hold one."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# Mint our OWN type carrying a 'document' Attach (no target, so it answers in a section row), so the
		# lock proves the writer regardless of what any operator seed happens to declare.
		cls.doc_type = task_type_fixture.mint_type(
			"ZZ Writer Probe", [{"fieldname": "document", "label": "Document", "fieldtype": "Attach"}]
		)

	@classmethod
	def tearDownClass(cls):
		task_type_fixture.teardown()
		super().tearDownClass()

	def test_document_routes_into_its_section_row_via_brain(self):
		"""Read back through the ONE reader (`_task_values`), so the assertion is about the address
		`field_target` names and not about a home this test happens to know. Phase 7 dropped the JSON
		payload this used to inspect; the answer is a section row now."""
		task = frappe.new_doc("CRM Task")
		changed = activity_api.set_schema_field(task, self.doc_type, "document", "/files/scan.pdf")
		self.assertTrue(changed)
		values = activity_api._task_values(task, activity_api._type_config(self.doc_type))
		self.assertEqual(values["document"], "/files/scan.pdf")

	def test_idempotent_write_returns_false(self):
		task = frappe.new_doc("CRM Task")
		activity_api.set_schema_field(task, self.doc_type, "document", "/files/scan.pdf")
		self.assertFalse(activity_api.set_schema_field(task, self.doc_type, "document", "/files/scan.pdf"))

	def test_undeclared_field_is_rejected(self):
		task = frappe.new_doc("CRM Task")
		with self.assertRaises(frappe.ValidationError):
			activity_api.set_schema_field(task, self.doc_type, "zz_not_declared", "x")


class TestMetricCompositeLookup(FrappeTestCase):
	"""GAP 5 — the metric map resolves the composite custom_task_type, not just a bare type_name."""

	def test_plain_type_extracts_last_component(self):
		self.assertEqual(
			metrics._plain_type("Field-Sales::India::TP::Demo Scheduled Status Field Visit"),
			"Demo Scheduled Status Field Visit",
		)
		# a pre-composite (bare) value has no '::' and is returned unchanged
		self.assertEqual(metrics._plain_type("Demo Scheduled Status Field Visit"), "Demo Scheduled Status Field Visit")

	def test_metric_field_resolves_a_composite_key(self):
		# Before the fix this returned None (the composite never matched a bare dict key).
		bare = next(iter(metrics.TASK_TYPE_TO_METRIC))
		composite = f"AnyVertical::AnyGroup::AnyProgram::{bare}"
		self.assertEqual(metrics._metric_field(composite), metrics.TASK_TYPE_TO_METRIC[bare])

	def test_unknown_type_returns_none(self):
		self.assertIsNone(metrics._metric_field("v::g::p::Not A Real Task Type"))
