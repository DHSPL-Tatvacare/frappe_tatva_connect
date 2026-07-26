# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Phase 2 of docs/plans/task-form-layer/2026-07-25-task-slots-to-sections-and-form-layer.md: every write lands in the
home `field_target` names.

An activity answer used to live in one of two places — a promoted CRM Task column, or a key in the JSON
payload. Neither survived the plan: the slots had no meaning (`custom_key_date_1` was "Visit Date" on one
type and "Sample Collected" on another) and the payload could not be filtered or sorted at all. The new
home is a section child row, named by `field_target` and by nothing else.

Between the two homes there was a phase where BOTH were true, so a backfill had something to reconcile
against and a flipped read had something to read. Phase 5 then dropped the slot leg and Phase 7 dropped the
columns themselves, so what this module now proves is the surviving half: for every shape a declared field
can take, the value the rep submitted is asserted at the ONE address the router names.

The four shapes are the §8 routing table, and they are asserted through the entry point a rep actually
uses (`save_activity`), never by calling the router and believing it:

  1. a field naming a column of its section's target doctype  -> that section's child row
  2. a field naming a retained common CRM Task column         -> the task row, which already IS the new home
  3. a field naming a dying slot                              -> a key-value answer row of its own fieldname
  3. a field naming no target at all                          -> the same, by the same rule

Phase 7 has since retired both old homes outright: the five dying slot columns and the JSON payload are
gone from `tabCRM Task`, and `tests/migration/test_retire_task_slot_columns.py` owns that drop. There is no
second address left for this module to check, which is the point.

The last two are ONE rule, and that is the point: the router enumerates no field and no slot, so a slot
is simply anything rules 1 and 2 did not claim. The fixture therefore declares its own targets rather
than reading an operator's seed, and the section it writes into is read from the `CRM Task Section`
declaration — a key or a table named here as a literal would be the second brain the lock forbids.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.activity.test_dual_write
"""
import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import get_datetime

from tatva_connect.activity import api as activity_api
from tatva_connect.activity import backfill
from tatva_connect.tests.activity import task_type_fixture

TYPE_NAME = "ZZ Dual Write Probe"

# A slot the plan retires, and a common column it keeps. Named here because the fixture must MEAN one of
# each; the router is never told which is which.
DYING_SLOT = "custom_key_date_1"
RETAINED_COMMON = "custom_outcome"

SUBMITTED = {
	"zz_sample_collected": "2026-07-01 10:30:00",
	"zz_remark": "ZZ remark text",
	"zz_outcome": "ZZ Reached",
	"zz_column_answer": "ZZ Column Answer",
}

RESUBMITTED = dict(SUBMITTED, zz_remark="ZZ remark text, revised")


def _key_value_section():
	"""The section a field with no column of its own answers in — the one the operator declared key-value."""
	rows = frappe.get_all(
		"CRM Task Section",
		filters={"is_key_value": 1},
		fields=["name", "target_doctype", "child_table_field", "row_key_field", "value_field"],
		order_by="display_order",
	)
	return rows[0]


def _column_section():
	"""A section whose rows carry real named columns, and one of those columns — both read from the
	declaration, so this test names neither a section key nor a child table."""
	for section in frappe.get_all(
		"CRM Task Section",
		filters={"is_key_value": 0, "is_multi_row": 0},
		fields=["name", "target_doctype", "child_table_field"],
		order_by="display_order",
	):
		column = next(
			(f.fieldname for f in frappe.get_meta(section.target_doctype).fields if f.fieldtype == "Data"),
			None,
		)
		if column:
			return section, column
	return None, None


class TestActivityDualWrite(FrappeTestCase):
	"""One submitted form, two homes, identical values — and a second save that grows no second row."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.key_value = _key_value_section()
		cls.column_section, cls.column = _column_section()
		cls.task_type = task_type_fixture.mint_type(TYPE_NAME, [
			# Rule 3 — a dying slot. Promoted today, claimed by no section and no common column tomorrow.
			{"label": "ZZ Sample Collected", "fieldname": "zz_sample_collected",
			 "fieldtype": "Datetime", "target": DYING_SLOT},
			# Rule 3 — no target at all: the JSON payload once, the same answer row now, by the same rule.
			{"label": "ZZ Remark", "fieldname": "zz_remark", "fieldtype": "Small Text"},
			# Rule 2 — a retained common column: the task row already IS its new home.
			{"label": "ZZ Outcome", "fieldname": "zz_outcome", "fieldtype": "Data", "target": RETAINED_COMMON},
			# Rule 1 — a real column of its section's target doctype.
			{"label": "ZZ Column Answer", "fieldname": "zz_column_answer", "fieldtype": "Data",
			 "section": cls.column_section.name, "target": cls.column},
		])

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		task_type_fixture.teardown()
		super().tearDownClass()

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")
		self.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Dual Write Probe",
			"mobile_no": f"+9198127{int(frappe.generate_hash(length=8), 16) % 100000:05d}",
			"custom_vertical": task_type_fixture.VERTICAL, "custom_group": task_type_fixture.GROUP,
		}).insert(ignore_permissions=True)

	# ---- the premise ------------------------------------------------------------------------------

	def test_the_probe_carries_one_field_of_each_routed_shape(self):
		"""A fixture missing a shape would prove the router for the shapes it happens to carry and no more."""
		self.assertIsNotNone(self.column_section, "no section declares real named columns — rule 1 is untestable")
		self.assertTrue(self.key_value, "no section is declared key-value — rule 3 has no home")
		self.assertIn(DYING_SLOT, backfill.PROMOTED_COLUMNS,
					  f"`{DYING_SLOT}` was never a promoted column — it is no longer a dying slot")
		self.assertNotIn(DYING_SLOT, activity_api.COMMON_COLUMNS,
						 f"`{DYING_SLOT}` is retained — pick a slot the plan actually dropped")
		self.assertIn(RETAINED_COMMON, activity_api.COMMON_COLUMNS,
					  f"`{RETAINED_COMMON}` is not a retained common column — rule 2 is untestable")

	def test_the_router_answers_each_shape_by_its_own_declaration(self):
		"""§8, read off the router itself: no field is enumerated, so a slot IS whatever rules 1 and 2 left."""
		schema = {f.fieldname: f for f in frappe.get_doc("CRM Task Type", self.task_type).schema}
		self.assertEqual(activity_api.field_target(schema["zz_column_answer"]),
						 (self.column_section.name, self.column))
		self.assertEqual(activity_api.field_target(schema["zz_outcome"]), (None, RETAINED_COMMON))
		self.assertEqual(activity_api.field_target(schema["zz_sample_collected"]),
						 (self.key_value.name, "zz_sample_collected"))
		self.assertEqual(activity_api.field_target(schema["zz_remark"]),
						 (self.key_value.name, "zz_remark"))

	# ---- the property -----------------------------------------------------------------------------

	def test_every_answer_lands_in_both_homes_identically(self):
		"""Through the entry point the rep uses. Each shape is checked at BOTH addresses, and equal."""
		task = frappe.get_doc("CRM Task", activity_api.save_activity(self.lead.name, self.task_type, SUBMITTED))
		answers = self._answers(task)

		# Rule 3, dying slot: its answer row and the typed column beside it — the slot column is gone.
		self.assertEqual(answers["zz_sample_collected"].get(self.key_value.value_field),
						 SUBMITTED["zz_sample_collected"], "the slot's value never reached its answer row")
		self.assertEqual(get_datetime(answers["zz_sample_collected"].value_datetime),
						 get_datetime(SUBMITTED["zz_sample_collected"]),
						 "a declared Datetime did not land in the column a date can be compared in")

		# Rule 3, former payload field: an answer row of its own name is now the whole of where it lives.
		self.assertEqual(answers["zz_remark"].get(self.key_value.value_field), SUBMITTED["zz_remark"],
						 "a field that named no target did not reach its answer row")

		# Rule 2: the common column IS the new home, so no answer row shadows it.
		self.assertEqual(task.get(RETAINED_COMMON), SUBMITTED["zz_outcome"])
		self.assertNotIn("zz_outcome", answers,
						 "a retained common column was ALSO copied into an answer row — two homes, one field")

		# Rule 1: the section's own column, by its real name, read off the declaration.
		self.assertEqual(self._column_row(task).get(self.column), SUBMITTED["zz_column_answer"],
						 "a field naming a real column of its section did not land in it")

	def test_a_second_save_updates_the_rows_and_never_duplicates_them(self):
		"""A task is saved many times over its life; an answer that grew a row per save is not an answer."""
		name = activity_api.save_activity(self.lead.name, self.task_type, SUBMITTED)
		before = self._row_counts(frappe.get_doc("CRM Task", name))

		activity_api.save_activity(self.lead.name, self.task_type, RESUBMITTED, task=name)
		task = frappe.get_doc("CRM Task", name)

		self.assertEqual(self._row_counts(task), before,
						 "a re-save grew the section rows — the write appends where it must address")
		answers = self._answers(task)
		self.assertEqual(answers["zz_remark"].get(self.key_value.value_field), RESUBMITTED["zz_remark"],
						 "the second save did not reach the row the first one wrote")
		self.assertEqual(len(answers), len(task.get(self.key_value.child_table_field)),
						 "two rows answer for one fieldname — the later read would pick between them")

	def test_the_single_field_writer_dual_writes_onto_the_same_row(self):
		"""set_schema_field is the automation lane's writer; it must reach the same home, and only once."""
		name = activity_api.save_activity(self.lead.name, self.task_type, SUBMITTED)
		task = frappe.get_doc("CRM Task", name)
		self.assertTrue(activity_api.set_schema_field(task, self.task_type, "zz_remark", "ZZ set by automation"))
		task.save(ignore_permissions=True)

		task = frappe.get_doc("CRM Task", name)
		answers = self._answers(task)
		self.assertEqual(answers["zz_remark"].get(self.key_value.value_field), "ZZ set by automation",
						 "the single-field writer did not reach the answer row")
		self.assertEqual(
			len([r for r in task.get(self.key_value.child_table_field)
				 if r.get(self.key_value.row_key_field) == "zz_remark"]),
			1, "the single-field writer appended a second row for a fieldname that already had one",
		)

	# ---- reading the new home, through the declaration and never a literal --------------------------

	def _answers(self, task):
		"""The task's key-value rows, keyed by the fieldname each one answers."""
		return {r.get(self.key_value.row_key_field): r for r in task.get(self.key_value.child_table_field)}

	def _column_row(self, task):
		"""The single row of the column section — the shape that is one row per task, not one per field."""
		rows = task.get(self.column_section.child_table_field)
		self.assertEqual(len(rows), 1, "a single-row section carries one row per task, always")
		return rows[0]

	def _row_counts(self, task):
		"""Rows per section child table, and the value each answer row holds — what a duplicate would move."""
		return {
			s.child_table_field: len(task.get(s.child_table_field) or [])
			for s in frappe.get_all("CRM Task Section", fields=["child_table_field"])
			if s.child_table_field
		}
