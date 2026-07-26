# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Phase 3 of docs/plans/task-form-layer/2026-07-25-task-slots-to-sections-and-form-layer.md: the history moves too.

Phase 2 made every NEW write land in both homes. Every task saved before it answers only in a slot column
or in the JSON payload, so for the whole existing history the two homes disagree and Phase 4's flipped
read would find nothing there. This asserts the patch closes that gap for a task that predates the dual
write — the pre-state is therefore CONSTRUCTED, by inserting a task straight into the old homes, never by
calling the writer (which would have filled the new one and proved nothing).

The three routed shapes are the §8 table, and each is asserted at the address `field_target` names rather
than at a name written here:

  1. a field naming a column of its section's target doctype  -> that section's child row, that column
  2. a field naming a retained common CRM Task column         -> the task row, which already IS the new home
  3. a field naming a dying slot, or no target at all         -> a key-value answer row of its own fieldname

D17 is asserted on the Datetime answer: `value` is ALWAYS populated and `value_datetime` carries the same
moment, because the section declares `value` as its read column while a range filter needs a real date.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.migration.test_backfill_sections
"""
import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import get_datetime

from tatva_connect.patches import backfill_task_section_rows
from tatva_connect.tests.activity import task_type_fixture

TYPE_NAME = "ZZ Backfill Probe"

# A slot the plan retires, and a common column it keeps. Named here because the fixture must MEAN one of
# each; the patch is never told which is which.
DYING_SLOT = "custom_key_date_1"
RETAINED_COMMON = "custom_outcome"

SLOT_ANSWER = "2026-07-02 09:15:00"
OUTCOME_ANSWER = "ZZ Reached"
PAYLOAD = {"zz_bf_remark": "ZZ backfilled remark", "zz_bf_column_answer": "ZZ Column Answer"}


def _key_value_section():
	"""The section a field with no column of its own answers in — the one the operator declared key-value."""
	return frappe.get_all(
		"CRM Task Section",
		filters={"is_key_value": 1},
		fields=["name", "target_doctype", "child_table_field", "row_key_field", "value_field"],
		order_by="display_order",
	)[0]


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


class TestBackfillSections(FrappeTestCase):
	"""A task that predates the dual write ends up answering at both addresses, and only once."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.key_value = _key_value_section()
		cls.column_section, cls.column = _column_section()
		cls.task_type = task_type_fixture.mint_type(TYPE_NAME, [
			# Rule 3 — a dying slot: promoted today, claimed by no section and no common column tomorrow.
			{"label": "ZZ BF Sample Collected", "fieldname": "zz_bf_sample_collected",
			 "fieldtype": "Datetime", "target": DYING_SLOT},
			# Rule 3 — no target at all: today's JSON payload, tomorrow an answer row, by the same rule.
			{"label": "ZZ BF Remark", "fieldname": "zz_bf_remark", "fieldtype": "Small Text"},
			# Rule 2 — a retained common column: the task row already IS its new home, so nothing moves.
			{"label": "ZZ BF Outcome", "fieldname": "zz_bf_outcome", "fieldtype": "Data",
			 "target": RETAINED_COMMON},
			# Rule 1 — a real column of its section's target doctype, in the payload until this patch.
			{"label": "ZZ BF Column Answer", "fieldname": "zz_bf_column_answer", "fieldtype": "Data",
			 "section": cls.column_section.name, "target": cls.column},
		])
		# Minted here and not inside the test that needs it: mint_type commits, and a commit mid-test would
		# put that test's own lead and task on the far side of the per-test rollback.
		cls.bare_type = task_type_fixture.mint_type("ZZ Backfill No Schema", [])

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		task_type_fixture.teardown()
		super().tearDownClass()

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")
		self.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Backfill Probe",
			"mobile_no": f"+9198126{int(frappe.generate_hash(length=8), 16) % 100000:05d}",
			"custom_vertical": task_type_fixture.VERTICAL, "custom_group": task_type_fixture.GROUP,
		}).insert(ignore_permissions=True)
		self.task = self._pre_state_task()

	def _pre_state_task(self):
		"""A task as it exists BEFORE Phase 2: answers in a slot column and in the payload, no section row."""
		task = frappe.get_doc({
			"doctype": "CRM Task",
			"title": TYPE_NAME,
			"custom_task_type": self.task_type,
			"reference_doctype": "CRM Lead",
			"reference_docname": self.lead.name,
			"status": "Todo",
			DYING_SLOT: SLOT_ANSWER,
			RETAINED_COMMON: OUTCOME_ANSWER,
			"custom_activity_payload": frappe.as_json(PAYLOAD),
		}).insert(ignore_permissions=True)
		return frappe.get_doc("CRM Task", task.name)

	# ---- the premise ------------------------------------------------------------------------------

	def test_the_pre_state_really_has_no_new_home(self):
		"""A pre-state carrying section rows already would let the patch prove nothing at all."""
		self.assertEqual(self._answers(self.task), {}, "the constructed pre-state already answers in the new home")
		self.assertEqual(self.task.get(self.column_section.child_table_field), [],
						 "the constructed pre-state already carries a column section row")
		self.assertEqual(get_datetime(self.task.get(DYING_SLOT)), get_datetime(SLOT_ANSWER))
		self.assertEqual(frappe.parse_json(self.task.custom_activity_payload), PAYLOAD)

	# ---- the property -----------------------------------------------------------------------------

	def test_every_old_answer_lands_at_the_address_field_target_names(self):
		backfill_task_section_rows.execute()
		task = frappe.get_doc("CRM Task", self.task.name)
		answers = self._answers(task)

		# Rule 3, the dying slot: an answer row of the field's own fieldname now carries the slot's value.
		self.assertIn("zz_bf_sample_collected", answers, "the slot's answer never reached its answer row")
		self.assertEqual(get_datetime(answers["zz_bf_sample_collected"].get(self.key_value.value_field)),
						 get_datetime(SLOT_ANSWER), "the answer row disagrees with the slot it came from")

		# Rule 3, the payload key: the same rule, the same shape, no mapping table between them.
		self.assertEqual(answers["zz_bf_remark"].get(self.key_value.value_field), PAYLOAD["zz_bf_remark"],
						 "a payload key and its answer row are the same answer, 1:1")

		# Rule 1: the section's OWN column, by its real name, read off the declaration.
		self.assertEqual(self._column_row(task).get(self.column), PAYLOAD["zz_bf_column_answer"],
						 "a field naming a real column of its section did not land in it")

		# Rule 2: the retained common column IS the new home, so nothing shadows it.
		self.assertEqual(task.get(RETAINED_COMMON), OUTCOME_ANSWER, "the backfill rewrote the task row")
		self.assertNotIn("zz_bf_outcome", answers,
						 "a retained common column was ALSO copied into an answer row — two homes, one field")

		# The old homes are untouched: this phase adds a home, it does not move anything out of one.
		self.assertEqual(get_datetime(task.get(DYING_SLOT)), get_datetime(SLOT_ANSWER))
		self.assertEqual(frappe.parse_json(task.custom_activity_payload), PAYLOAD)

	def test_a_datetime_answer_carries_both_the_read_column_and_the_comparable_one(self):
		"""D17: `value` is always populated and the typed column agrees — a range filter must not be lexical."""
		backfill_task_section_rows.execute()
		row = self._answers(frappe.get_doc("CRM Task", self.task.name))["zz_bf_sample_collected"]
		self.assertTrue(row.get(self.key_value.value_field), "the declared read column was left blank")
		self.assertIsNotNone(row.value_datetime, "a declared Datetime landed with nothing a date can compare")
		self.assertEqual(get_datetime(row.value_datetime),
						 get_datetime(row.get(self.key_value.value_field)),
						 "the read column and the comparable column carry different moments")

	def test_a_second_run_changes_nothing_and_duplicates_no_row(self):
		"""An applied backfill is an end state, not an append: running it twice is running it once."""
		backfill_task_section_rows.execute()
		before = self._snapshot()

		backfill_task_section_rows.execute()

		self.assertEqual(self._snapshot(), before, "the second run moved a value or grew a row")
		task = frappe.get_doc("CRM Task", self.task.name)
		self.assertEqual(len(self._answers(task)), len(task.get(self.key_value.child_table_field)),
						 "two rows answer for one fieldname — the later read would pick between them")

	def test_a_task_type_with_no_declared_schema_is_skipped(self):
		"""There is no answer to move, and a row keyed on nothing would be an address that resolves to nothing."""
		task = frappe.get_doc({
			"doctype": "CRM Task", "title": "ZZ Backfill No Schema", "custom_task_type": self.bare_type,
			"reference_doctype": "CRM Lead", "reference_docname": self.lead.name, "status": "Todo",
			"custom_activity_payload": frappe.as_json({"zz_undeclared": "ZZ nothing declares me"}),
		}).insert(ignore_permissions=True)

		backfill_task_section_rows.execute()

		reread = frappe.get_doc("CRM Task", task.name)
		self.assertEqual(self._answers(reread), {}, "an undeclared payload key was given a home")

	# ---- reading the new home, through the declaration and never a literal --------------------------

	def _answers(self, task):
		"""The task's key-value rows, keyed by the fieldname each one answers."""
		return {r.get(self.key_value.row_key_field): r for r in task.get(self.key_value.child_table_field)}

	def _column_row(self, task):
		"""The single row of the column section — one row per task, not one per field."""
		rows = task.get(self.column_section.child_table_field)
		self.assertEqual(len(rows), 1, "a single-row section carries one row per task, always")
		return rows[0]

	def _snapshot(self):
		"""Every section row this task carries, as (table, address, values) — what a duplicate would move."""
		task = frappe.get_doc("CRM Task", self.task.name)
		out = {}
		for section in frappe.get_all("CRM Task Section", fields=["child_table_field", "row_key_field"]):
			rows = task.get(section.child_table_field) or []
			out[section.child_table_field] = sorted(
				(frappe.utils.cstr(r.get(section.row_key_field)), frappe.as_json(r.as_dict(no_default_fields=True)))
				for r in rows
			)
		return out
