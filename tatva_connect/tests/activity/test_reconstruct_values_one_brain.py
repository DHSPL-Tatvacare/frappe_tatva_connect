# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A3.1 — `reconstruct_values` is not a second reader any more.

`activity/automation.py:reconstruct_values` is what the fail-closed location backstop
(`tasks/tasks.py:enforce_location` -> `location/api.py:location_required`) asks "what did this activity
form say?". It used to answer that itself: the JSON payload, merged with `doc.get(f.target)` for every
schema field carrying a target. That is a private copy of a rule `activity/api.py:_task_values` already
owns, and the constitution forbids exactly that copy.

The copy also carried a DEFECT the one reader does not have. Its loop was `val = doc.get(f.target)` and
`if val is not None`, so a RETIRED slot column still holding what it held before Phase 5 OVERRODE the
answer the rep actually gave, and the location guard judged the visit on history. Phase 7 has since
DROPPED those columns, so the fossil no longer exists to be read: the stale-column test that was this
module's red proof has been deleted rather than left iterating a column that is not there.

What is asserted:

  * for a type with answers in ALL THREE routed shapes — a retained common CRM Task column (§8 rule 2),
    a real column on a section's own child doctype (rule 1), and a key-value answer row (rule 3) —
    `reconstruct_values` returns exactly what `_task_values` reports, keyed by schema fieldname, which is
    the key criteria and `location_when` match on;
  * the location guard still receives the value it needs, driven through `tasks/tasks.py`'s own path
    (its import, its reconstruction, the argument it hands `location_required`) and not through the
    helper in isolation.

Nothing here names a column or a section as a literal: every one is read off the brain
(`task_columns()`, the `CRM Task Section` rows), so a column that changes sides moves this test with it.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.activity.test_reconstruct_values_one_brain
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.activity import api as activity_api
from tatva_connect.activity.automation import reconstruct_values
from tatva_connect.tasks import tasks as tasks_module
from tatva_connect.tests.activity import task_type_fixture

TYPE_NAME = "ZZ Reconstruct One Brain Probe"

FRESH_COMMON = "ZZ fresh common answer"
FRESH_SECTION = "ZZ fresh section answer"
FRESH_SLOT = "ZZ fresh answer with no column of its own"
NOTES = "ZZ reconstruct notes"


def _data_column(doctype, candidates=None):
	"""A real Data column of `doctype` — the probe writes a string, so the answer compares exactly."""
	meta = frappe.get_meta(doctype)
	pool = candidates if candidates is not None else [f.fieldname for f in meta.fields]
	return next(c for c in pool if (meta.get_field(c) and meta.get_field(c).fieldtype == "Data"))


def _column_section():
	"""A seeded section that is a plain column shape — neither key-value nor multi-row. Read off the
	declaration, never named: D13 says the keys are the operator's to change."""
	return frappe.get_all(
		"CRM Task Section",
		filters={"is_key_value": 0, "is_multi_row": 0},
		fields=["name", "target_doctype"],
		order_by="display_order",
	)[0]


class TestReconstructValuesOneBrain(FrappeTestCase):
	"""One saved activity, read two ways — and after A3.1 there is only one way."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		# The three routes, each resolved off the brain rather than named.
		cls.common_column = _data_column("CRM Task", list(activity_api.task_columns()))
		cls.section = _column_section()
		cls.section_column = _data_column(cls.section.target_doctype)

		cls.f_common, cls.f_section, cls.f_slot = "zz_rv_common", "zz_rv_section", "zz_rv_slot"
		cls.task_type = task_type_fixture.mint_type(TYPE_NAME, [
			{"label": "ZZ RV Common", "fieldname": cls.f_common, "fieldtype": "Data",
			 "target": cls.common_column},
			{"label": "ZZ RV Section", "fieldname": cls.f_section, "fieldtype": "Data",
			 "section": cls.section.name, "target": cls.section_column},
			# Rule 3: a field naming no column at all, which is where a retired slot's answers landed.
			{"label": "ZZ RV Slot", "fieldname": cls.f_slot, "fieldtype": "Data"},
		])
		cls.declared = {
			cls.f_common: FRESH_COMMON,
			cls.f_section: FRESH_SECTION,
			cls.f_slot: FRESH_SLOT,
		}
		# Notes ride along so the equality below also covers the description the reader merges in. It is
		# NOT asserted value-by-value: `description` is a Text Editor, so what comes back is whatever
		# Frappe's HTML sanitiser made of it — the same for both readers, which is all this needs.
		cls.submitted = dict(cls.declared, notes=NOTES)

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		task_type_fixture.teardown()
		super().tearDownClass()

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")
		self.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Reconstruct One Brain Probe",
			"mobile_no": f"+9198127{int(frappe.generate_hash(length=8), 16) % 100000:05d}",
			"custom_vertical": task_type_fixture.VERTICAL, "custom_group": task_type_fixture.GROUP,
		}).insert(ignore_permissions=True)

	# ---- the premise ------------------------------------------------------------------------------

	def test_the_probe_declares_one_field_in_each_of_the_three_routed_shapes(self):
		"""A fixture whose three fields routed the same way would prove the reader for one shape only."""
		schema = {f.fieldname: f for f in frappe.get_doc("CRM Task Type", self.task_type).schema}
		kv = activity_api._key_value_section()
		self.assertEqual(activity_api.field_target(schema[self.f_common]), (None, self.common_column),
						 "the rule-2 probe no longer stays on the task row")
		self.assertEqual(activity_api.field_target(schema[self.f_section]),
						 (self.section.name, self.section_column),
						 "the rule-1 probe no longer lands on its section's own column")
		self.assertEqual(activity_api.field_target(schema[self.f_slot]), (kv, self.f_slot),
						 "a field naming no column no longer falls to the key-value default")

	# ---- the property: one reader -------------------------------------------------------------------

	def test_reconstruct_agrees_with_the_one_reader_in_every_routed_shape(self):
		"""Same task, same keys, same values. `_task_values` is the brain; this must BE it, not resemble it."""
		name = activity_api.save_activity(self.lead.name, self.task_type, self.submitted)
		doc = frappe.get_doc("CRM Task", name)
		cfg = activity_api._type_config(self.task_type)

		self.assertEqual(reconstruct_values(doc), activity_api._task_values(doc, cfg),
						 "reconstruct_values still answers from a reader of its own")

	def test_every_submitted_answer_comes_back_under_its_schema_fieldname(self):
		"""Criteria and `location_when` are authored against the SCHEMA fieldname, so the key is the contract."""
		name = activity_api.save_activity(self.lead.name, self.task_type, self.submitted)
		values = reconstruct_values(frappe.get_doc("CRM Task", name))
		for fieldname, submitted in self.declared.items():
			self.assertEqual(values.get(fieldname), submitted,
							 f"`{fieldname}` did not come back as the rep answered it")

	# ---- ...and the guard that reads it gets what it needs -------------------------------------------

	def test_the_location_guard_receives_the_fresh_answers(self):
		"""Driven through `tasks.enforce_location` itself — its import, its reconstruction, the dict it
		hands the gate — because that is the only caller and the only thing this change can break. The gate
		is spied, not stubbed away: it returns None (no location needed) so nothing throws, and the values
		it was asked about are the assertion."""
		name = activity_api.save_activity(self.lead.name, self.task_type, self.submitted)
		doc = frappe.get_doc("CRM Task", name)
		doc.status = tasks_module.DONE_STATUS

		seen = {}

		def spy(task_type, lead, values):
			seen.update(values or {})
			return None  # no location required, so the backstop returns without touching the doc

		# Phase 11 deleted the workflow stand-down: the backstop now always runs, so there is nothing to
		# patch off. What this test is about is unchanged — WHICH values the guard is handed.
		with patch("tatva_connect.tasks.tasks.automation.is_enabled", return_value=True), \
			 patch("tatva_connect.location.api.location_required", side_effect=spy):
			tasks_module.enforce_location(doc)

		self.assertEqual(seen.get(self.f_common), FRESH_COMMON,
						 "the guard no longer sees a retained common column's answer")
		self.assertEqual(seen.get(self.f_section), FRESH_SECTION,
						 "the guard no longer sees an answer that lives on its section's column")
		self.assertEqual(seen.get(self.f_slot), FRESH_SLOT,
						 "the guard no longer sees an answer that lives in a key-value row")
