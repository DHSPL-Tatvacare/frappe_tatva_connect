# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Phase 5 of docs/plans/task-form-layer/2026-07-25-task-slots-to-sections-and-form-layer.md: the slot leg is gone —
and after Phase 7 there is no slot left to write.

Phase 2 made every activity answer land in two homes at once, Phase 3 moved the history into the new one
and Phase 4 flipped every reader onto it. So the slot columns became written by a writer nobody read —
the exact shape that lets two homes drift apart unnoticed. Phase 5 removed that leg; Phase 7 then removed
the columns, which is a stronger guarantee than any assertion about what a writer touches.

What is asserted, and what is deliberately NOT:

  * every retired slot is really GONE — no Custom Field doc, no column — so "never written" is now a fact
    about the schema and not a property of a writer. `tests/migration/test_retire_task_slot_columns.py`
    owns the DROP itself; this is the standing check that it stayed dropped;
  * a field naming no retained column answers in its section row, so nothing was lost with the leg;
  * every retained common column (§8 rule 2, D18) is still written exactly as before — including
    `custom_asm`, which no rep picker offers but `_validate_asm` reads out of the very dict this phase
    re-routed. Lose that write and the ASM validation goes blind, silently.

What was DELETED with Phase 7, and why it now proves nothing: the sentinel tests. They stamped an old
value into a slot column and asserted a re-save left it alone. A column that does not exist cannot hold a
sentinel, so those assertions would iterate an empty set and pass for ever.

Nothing here names a slot or a common column as a literal except `custom_asm`, which D18 singles out by
name and which this module must therefore also single out by name. The retired set is read off the
migration layer that still records the old schema (`activity.backfill.retired_homes`), so a column that
changes sides moves this test with it.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.activity.test_no_slot_writes
"""
import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import get_datetime

from tatva_connect.activity import api as activity_api
from tatva_connect.activity import backfill
from tatva_connect.tests.activity import task_type_fixture

TYPE_NAME = "ZZ No Slot Writes Probe"

# The old homes Phase 7 retired, and the columns the task row keeps — both read off the app, never
# restated: a column moved between the two sets moves this test with it.
RETIRED = backfill.retired_homes()

# A field naming no retained column: the shape that used to be a slot or a payload key and is an answer row
# now. Declared without a target, because the target it once carried names nothing at all any more.
SECTION_FIELD = "zz_ns_answer"
SECTION_ANSWER = "ZZ answer with no column of its own"

# D18 names this one specifically: operational, never rep-facing, and read by `_validate_asm` out of the
# promoted dict this phase re-routed. A Link to User, so its probe value must be a real user.
ASM_COLUMN = "custom_asm"
ASM_USER = "Administrator"
NOT_AN_ASM = "Guest"


def _declared_fieldtype(column_fieldtype):
	"""The declared fieldtype a probe may use for a column — asked of the CRM Task Type Field Select's OWN
	options, because a retained column's native type is not always one a declaration may take (`description`
	is a Text Editor, which the declaration does not offer)."""
	options = frappe.get_meta("CRM Task Type Field").get_field("fieldtype").options or ""
	allowed = {o.strip() for o in options.split("\n") if o.strip()}
	return column_fieldtype if column_fieldtype in allowed else "Small Text"


def _key_value_section():
	"""The section a field with no column of its own answers in — the one the operator declared key-value."""
	rows = frappe.get_all(
		"CRM Task Section",
		filters={"is_key_value": 1},
		fields=["name", "child_table_field", "row_key_field", "value_field"],
		order_by="display_order",
	)
	return rows[0]


def _column_exists(column):
	"""Straight from information_schema: the claim is about the TABLE, not about anyone's cache of it."""
	return bool(frappe.db.sql(
		"""SELECT 1 FROM information_schema.COLUMNS
		WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tabCRM Task' AND COLUMN_NAME = %s""",
		(column,),
	))


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


class TestNoSlotWrites(FrappeTestCase):
	"""One saved activity: there is no retired column to write, and every retained one is still written."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.key_value = _key_value_section()
		meta = frappe.get_meta("CRM Task")
		# The declared fieldtype is the COLUMN's own, so the probe writes something each column accepts and
		# the key-value row's typed mirror (D17) is the one that column's answers really compare in.
		cls.retained = activity_api.task_columns()
		cls.fieldtypes = {c: _declared_fieldtype(meta.get_field(c).fieldtype) for c in cls.retained}
		cls.task_type = task_type_fixture.mint_type(TYPE_NAME, [
			*({"label": f"ZZ {column}", "fieldname": _fieldname_for(column),
			   "fieldtype": cls.fieldtypes[column],
			   "options": meta.get_field(column).options or "", "target": column}
			  for column in cls.retained),
			{"label": "ZZ NS Answer", "fieldname": SECTION_FIELD, "fieldtype": "Data"},
		])
		cls.submitted = {
			**{_fieldname_for(column): _probe_value(column, cls.fieldtypes[column], i + 1)
			   for i, column in enumerate(cls.retained)},
			SECTION_FIELD: SECTION_ANSWER,
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

	def test_the_probe_carries_a_field_of_each_retained_column_and_one_of_neither(self):
		"""A fixture missing either shape would prove the phase for the shape it happens to carry, no more."""
		self.assertTrue(RETIRED, "no old home is retired — this phase has nothing to remove")
		self.assertTrue(self.retained, "no promoted column is retained — §8 rule 2 is untestable")
		self.assertEqual(set(RETIRED) & set(self.retained), set(),
						 "a column is both retired and retained — the two sets must partition")
		self.assertIn(ASM_COLUMN, self.retained, "D18: custom_asm is retained, or _validate_asm goes blind")
		self.assertIn("Sales Manager", frappe.get_roles(ASM_USER),
					  f"`{ASM_USER}` cannot stand in for an ASM — _validate_asm would refuse the probe")
		self.assertNotIn("Sales Manager", frappe.get_roles(NOT_AN_ASM),
						 f"`{NOT_AN_ASM}` holds the ASM role — the negative case would not throw")

	def test_the_router_keeps_a_retained_column_on_the_task_row_and_sends_everything_else_off_it(self):
		"""§8 read off the router itself: a section row is simply where rules 1 and 2 did not claim a field."""
		schema = {f.fieldname: f for f in frappe.get_doc("CRM Task Type", self.task_type).schema}
		for column in self.retained:
			self.assertEqual(activity_api.field_target(schema[_fieldname_for(column)]), (None, column),
							 f"`{column}` no longer routes to the task row — §8 rule 2 has moved")
		self.assertEqual(activity_api.field_target(schema[SECTION_FIELD]),
						 (self.key_value.name, SECTION_FIELD),
						 "a field naming no column stopped falling to the key-value default")

	# ---- the property: a retired column cannot be written because it is not there -----------------------

	def test_every_retired_old_home_is_really_gone_from_the_task(self):
		"""Phase 7. Not "the writer leaves it alone" — there is nothing left to leave alone, which is the
		only version of this guarantee a future writer cannot break by accident."""
		for column in RETIRED:
			self.assertFalse(_column_exists(column),
							 f"`{column}` is still a column of tabCRM Task — Phase 7 has not landed here")
			self.assertFalse(frappe.db.exists("Custom Field", f"CRM Task-{column}"),
							 f"the Custom Field doc for `{column}` survives, so a fixture sync can rebuild it")
			self.assertIsNone(frappe.get_meta("CRM Task").get_field(column),
							  f"CRM Task's meta still describes `{column}` — every read would SELECT it")

	# ---- ...and the answer is in its new home ----------------------------------------------------------

	def test_a_field_naming_no_retained_column_answers_in_its_section_row(self):
		"""Removing the leg removed a duplicate, never an answer: it is at the address it now lives at."""
		task = frappe.get_doc("CRM Task", activity_api.save_activity(
			self.lead.name, self.task_type, self.submitted))
		answers = {r.get(self.key_value.row_key_field): r
				   for r in task.get(self.key_value.child_table_field)}
		self.assertIn(SECTION_FIELD, answers, "the answer reached neither a column nor its row")
		self.assertEqual(answers[SECTION_FIELD].get(self.key_value.value_field), SECTION_ANSWER,
						 f"`{SECTION_FIELD}` answers something other than what the rep submitted")

	# ---- ...and every retained column is still written -------------------------------------------------

	def test_every_retained_common_column_is_still_written(self):
		"""D18 regression. These four are the whole of what the task row keeps; losing one is silent."""
		name = activity_api.save_activity(self.lead.name, self.task_type, self.submitted)
		row = frappe.db.get_value("CRM Task", name, list(self.retained), as_dict=True)
		for column in self.retained:
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
