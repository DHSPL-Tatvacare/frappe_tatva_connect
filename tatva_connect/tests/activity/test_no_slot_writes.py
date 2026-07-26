# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Phase 5 of docs/plans/task-form-layer/2026-07-25-task-slots-to-sections-and-form-layer.md: the slot leg is gone.

Phase 2 made every activity answer land in two homes at once, Phase 3 moved the history into the new one
and Phase 4 flipped every reader onto it. So the slot columns are now written by a writer nobody reads —
and a write nobody reads is the exact shape that lets two homes drift apart unnoticed. This phase removes
it, and this module is its proof.

What is asserted, and what is deliberately NOT:

  * a slot column the plan retires (`PROMOTED_COLUMNS` minus `COMMON_COLUMNS`) is never written again —
    not merely blank, but STILL CARRYING what it carried, so a writer that re-writes it by name is caught
    even on a task that already had a value there. The columns keep their history; Phase 7 drops them;
  * the same save still fills each of those fields' section rows, so nothing was lost with the leg;
  * every RETAINED common column (§8 rule 2, D18) is still written exactly as before — including
    `custom_asm`, which no rep picker offers but `_validate_asm` reads out of the very dict this phase
    re-routed. Lose that write and the ASM validation goes blind, silently.

Nothing here names a slot or a common column as a literal except `custom_asm`, which D18 singles out by
name and which this module must therefore also single out by name. The two sets are read off the brain,
so retiring or retaining a column moves this test with it.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.activity.test_no_slot_writes
"""
import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import get_datetime

from tatva_connect.activity import api as activity_api
from tatva_connect.tests.activity import task_type_fixture

TYPE_NAME = "ZZ No Slot Writes Probe"

# The columns Phase 5 stops writing, and the ones it must keep writing — both read off the brain, never
# restated: a column moved between the two sets moves this test with it.
DYING_SLOTS = tuple(c for c in activity_api.PROMOTED_COLUMNS if c not in activity_api.COMMON_COLUMNS)
RETAINED = activity_api.COMMON_COLUMNS

# D18 names this one specifically: operational, never rep-facing, and read by `_validate_asm` out of the
# promoted dict this phase re-routed. A Link to User, so its probe value must be a real user.
ASM_COLUMN = "custom_asm"
ASM_USER = "Administrator"
NOT_AN_ASM = "Guest"


def _key_value_section():
	"""The section a field with no column of its own answers in — the one the operator declared key-value."""
	rows = frappe.get_all(
		"CRM Task Section",
		filters={"is_key_value": 1},
		fields=["name", "child_table_field", "row_key_field", "value_field"],
		order_by="display_order",
	)
	return rows[0]


def _fieldname_for(column):
	"""One declared field per probed column, named after the column it targets so a failure names itself."""
	return f"zz_{column}"


def _probe_value(column, fieldtype, seed):
	"""A value the CRM Task column would accept, so a writer that DID write it leaves something visible."""
	if column == ASM_COLUMN:
		return ASM_USER
	if fieldtype in ("Datetime", "Date"):
		return f"2026-07-{seed:02d} 10:30:00" if fieldtype == "Datetime" else f"2026-07-{seed:02d}"
	return f"ZZ answer for {column}"


def _sentinel(fieldtype, seed):
	"""What a slot column already holds before the save — a value no submitted answer could produce, so
	an assertion failure means the writer reached that column BY NAME and not that it merely stayed blank."""
	if fieldtype in ("Datetime", "Date"):
		return f"2001-01-{seed:02d} 00:00:00" if fieldtype == "Datetime" else f"2001-01-{seed:02d}"
	return f"ZZ SENTINEL {seed}"


class TestNoSlotWrites(FrappeTestCase):
	"""One saved activity: the retired columns keep what they held, and every answer is in its new home."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.key_value = _key_value_section()
		meta = frappe.get_meta("CRM Task")
		# The declared fieldtype is the COLUMN's own, so the probe writes something each column accepts and
		# the key-value row's typed mirror (D17) is the one that column's answers really compare in.
		cls.fieldtypes = {c: meta.get_field(c).fieldtype for c in (*DYING_SLOTS, *RETAINED)}
		cls.task_type = task_type_fixture.mint_type(TYPE_NAME, [
			{"label": f"ZZ {column}", "fieldname": _fieldname_for(column),
			 "fieldtype": cls.fieldtypes[column],
			 "options": meta.get_field(column).options or "", "target": column}
			for column in (*DYING_SLOTS, *RETAINED)
		])
		cls.submitted = {
			_fieldname_for(column): _probe_value(column, cls.fieldtypes[column], i + 1)
			for i, column in enumerate((*DYING_SLOTS, *RETAINED))
		}
		cls.sentinels = {
			column: _sentinel(cls.fieldtypes[column], i + 1) for i, column in enumerate(DYING_SLOTS)
		}

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		task_type_fixture.teardown()
		super().tearDownClass()

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")
		self.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "No Slot Writes Probe",
			"mobile_no": f"+9198131{int(frappe.generate_hash(length=8), 16) % 100000:05d}",
			"custom_vertical": task_type_fixture.VERTICAL, "custom_group": task_type_fixture.GROUP,
		}).insert(ignore_permissions=True)

	# ---- the premise --------------------------------------------------------------------------------

	def test_the_probe_carries_a_field_of_each_retired_and_each_retained_column(self):
		"""A fixture missing either set would prove the phase for the columns it happens to carry, no more."""
		self.assertTrue(DYING_SLOTS, "no promoted column is retired — this phase has nothing to remove")
		self.assertTrue(RETAINED, "no promoted column is retained — §8 rule 2 is untestable")
		self.assertEqual(set(DYING_SLOTS) & set(RETAINED), set(),
						 "a column is both retired and retained — the two sets must partition")
		self.assertIn(ASM_COLUMN, RETAINED, "D18: custom_asm is retained, or _validate_asm goes blind")
		self.assertIn("Sales Manager", frappe.get_roles(ASM_USER),
					  f"`{ASM_USER}` cannot stand in for an ASM — _validate_asm would refuse the probe")
		self.assertNotIn("Sales Manager", frappe.get_roles(NOT_AN_ASM),
						 f"`{NOT_AN_ASM}` holds the ASM role — the negative case would not throw")

	def test_the_router_sends_a_retired_column_off_the_task_row_and_keeps_a_retained_one_on_it(self):
		"""§8 read off the router itself: a slot is simply a target rules 1 and 2 did not claim."""
		schema = {f.fieldname: f for f in frappe.get_doc("CRM Task Type", self.task_type).schema}
		for column in DYING_SLOTS:
			section_key, address = activity_api.field_target(schema[_fieldname_for(column)])
			self.assertEqual((section_key, address), (self.key_value.name, _fieldname_for(column)),
							 f"`{column}` still routes to the task row — it is not a retired slot")
		for column in RETAINED:
			self.assertEqual(activity_api.field_target(schema[_fieldname_for(column)]), (None, column),
							 f"`{column}` no longer routes to the task row — §8 rule 2 has moved")

	# ---- the property: a retired column is never written ----------------------------------------------

	def test_a_first_save_writes_no_retired_column(self):
		"""A brand-new task: every retired column comes out of the save exactly as empty as it went in."""
		name = activity_api.save_activity(self.lead.name, self.task_type, self.submitted)
		row = frappe.db.get_value("CRM Task", name, list(DYING_SLOTS), as_dict=True)
		written = {c: row.get(c) for c in DYING_SLOTS if row.get(c) not in (None, "")}
		self.assertEqual(written, {}, f"the save still wrote retired slot columns: {written}")

	def test_a_re_save_leaves_a_retired_column_carrying_exactly_what_it_held(self):
		"""The real proof. The columns keep their history, so 'untouched' is a VALUE that must survive a
		save — not a blank that a writer clearing the column would also produce."""
		name = activity_api.save_activity(self.lead.name, self.task_type, self.submitted)
		frappe.db.set_value("CRM Task", name, self.sentinels, update_modified=False)

		activity_api.save_activity(self.lead.name, self.task_type, self.submitted, task=name)

		row = frappe.db.get_value("CRM Task", name, list(DYING_SLOTS), as_dict=True)
		for column, sentinel in self.sentinels.items():
			self.assertEqual(self._comparable(column, row.get(column)), self._comparable(column, sentinel),
							 f"the writer reached `{column}` by name — a retired slot must keep its history")

	def test_the_single_field_writer_writes_no_retired_column_either(self):
		"""set_schema_field is the automation lane's writer, and it routed by the same dead rule."""
		name = activity_api.save_activity(self.lead.name, self.task_type, self.submitted)
		frappe.db.set_value("CRM Task", name, self.sentinels, update_modified=False)

		task = frappe.get_doc("CRM Task", name)
		column = DYING_SLOTS[0]
		activity_api.set_schema_field(task, self.task_type, _fieldname_for(column),
									  self.submitted[_fieldname_for(column)])
		task.save(ignore_permissions=True)

		held = frappe.db.get_value("CRM Task", name, column)
		self.assertEqual(self._comparable(column, held), self._comparable(column, self.sentinels[column]),
						 f"the single-field writer still writes the retired slot `{column}`")

	# ---- ...and the answer is in its new home ----------------------------------------------------------

	def test_every_retired_field_answers_in_its_section_row(self):
		"""Removing the leg removed a duplicate, never an answer: each one is at the address it now lives at."""
		task = frappe.get_doc("CRM Task", activity_api.save_activity(
			self.lead.name, self.task_type, self.submitted))
		answers = {r.get(self.key_value.row_key_field): r
				   for r in task.get(self.key_value.child_table_field)}
		for column in DYING_SLOTS:
			fieldname = _fieldname_for(column)
			self.assertIn(fieldname, answers, f"`{column}`'s answer reached neither its column nor its row")
			self.assertEqual(answers[fieldname].get(self.key_value.value_field), self.submitted[fieldname],
							 f"`{fieldname}` answers something other than what the rep submitted")

	# ---- ...and every retained column is still written -------------------------------------------------

	def test_every_retained_common_column_is_still_written(self):
		"""D18 regression. These four are the whole of what the task row keeps; losing one is silent."""
		name = activity_api.save_activity(self.lead.name, self.task_type, self.submitted)
		row = frappe.db.get_value("CRM Task", name, list(RETAINED), as_dict=True)
		for column in RETAINED:
			self.assertEqual(self._comparable(column, row.get(column)),
							 self._comparable(column, self.submitted[_fieldname_for(column)]),
							 f"a retained common column stopped being written: `{column}`")

	def test_the_asm_column_is_written_and_the_asm_validation_still_reads_it(self):
		"""D18, specifically. Written is not enough — `_validate_asm` reads it out of the promoted dict this
		phase re-routed, so the proof is that a NON-manager still gets refused: only a value that reached
		that dict can be judged. A silent stop would make this save succeed."""
		name = activity_api.save_activity(self.lead.name, self.task_type, self.submitted)
		self.assertEqual(frappe.db.get_value("CRM Task", name, ASM_COLUMN), ASM_USER,
						 "custom_asm stopped being written — the audited ASM data is now unvalidated")

		refused = dict(self.submitted, **{_fieldname_for(ASM_COLUMN): NOT_AN_ASM})
		with self.assertRaises(frappe.ValidationError) as caught:
			activity_api.save_activity(self.lead.name, self.task_type, refused)
		self.assertIn("Sales Manager", str(caught.exception),
					  "the save was refused by something other than the ASM validation")

	# ---- helpers ---------------------------------------------------------------------------------------

	def _comparable(self, column, value):
		"""A Datetime read back from the DB is a datetime and a submitted one is a string; compare as dates."""
		if self.fieldtypes[column] in ("Datetime", "Date") and value not in (None, ""):
			return get_datetime(value)
		return value
