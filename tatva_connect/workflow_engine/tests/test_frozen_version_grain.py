# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""W4.6 — a frozen version must record what it froze, and the SILENCE is the actual defect.

`versions.build_payload` read `workflow.vertical/group/program` while `CRM Workflow` declares
`trigger_vertical/trigger_group/trigger_program`. It did not raise: `Document.get` on an unknown field
returns `None`, `or ""` swallowed it, and EVERY frozen version recorded blank grain. A version that
misrecords its own grain cannot be trusted to replay.

THE READ WAS ALREADY FIXED (dff2f5d, 2026-07-26) — `build_payload` now maps axis to column through the
controller's own `TRIGGER_INDEX`, the one brain that decides which column carries which axis. WHAT WAS
STILL MISSING IS THE LOCK, and the lock is the point: a rename that leaves TRIGGER_INDEX behind would go
green and blank the grain again in exactly the same silence. So this asserts the columns REALLY EXIST on
the doctype, and that a real freeze carries the grain its trigger declared.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.tatva_connect.doctype.crm_workflow.crm_workflow import TRIGGER_INDEX
from tatva_connect.workflow_engine import versions
from tatva_connect.workflow_engine.tests import fixtures

_AXES = ("vertical", "group", "program")


class TestTheGrainColumnsReallyExist(FrappeTestCase):
	"""The rename lock. `Document.get` answers None for a column that is gone, and None reads as blank."""

	def test_every_indexed_column_is_a_real_field_on_the_workflow(self):
		meta = frappe.get_meta("CRM Workflow")
		missing = [column for column in TRIGGER_INDEX if not meta.get_field(column)]

		self.assertEqual(missing, [], "TRIGGER_INDEX names a column CRM Workflow no longer has")

	def test_the_payload_asks_for_every_grain_axis_by_name(self):
		"""A renamed AXIS raises loudly; a renamed COLUMN is the silent case above. Both must be covered."""
		column_for = {axis: column for column, axis in TRIGGER_INDEX.items()}

		for axis in _AXES:
			self.assertIn(axis, column_for, f"no column carries the {axis} axis")


class TestAFrozenVersionCarriesItsGrain(FrappeTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_the_freeze_records_the_grain_the_trigger_declared(self):
		"""THE red: this returned '' for all three axes on every workflow ever frozen."""
		workflow = fixtures.make_workflow(
			f"WF-GRAIN-{frappe.generate_hash(length=6)}",
			[fixtures.trigger(), fixtures.node("n1", "Terminal")],
		)

		payload = versions.build_payload(workflow)

		self.assertEqual(
			[payload[axis] for axis in _AXES],
			list(fixtures.AXES),
			"the frozen version must carry the grain its trigger declared, not blanks",
		)

	def test_a_workflow_with_no_grain_freezes_blanks_and_that_is_correct(self):
		"""A blank axis is the wildcard, not an error — the freeze must record it as declared."""
		workflow = fixtures.make_workflow(
			f"WF-NOGRAIN-{frappe.generate_hash(length=6)}",
			[fixtures.trigger(grain=False), fixtures.node("n1", "Terminal")],
		)

		payload = versions.build_payload(workflow)

		self.assertEqual([payload[axis] for axis in _AXES], ["", "", ""])
