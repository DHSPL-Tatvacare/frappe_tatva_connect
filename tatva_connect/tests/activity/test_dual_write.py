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
  4. a field NAMING a key-value section                       -> that section, addressed by its own fieldname

Shape 4 is what makes a SECOND key-value section reachable, and the lead snapshot is the first one to
need it: rule 3's fallback always answers with the first key-value section by display order, so before
this a declaration naming any other key-value section landed somewhere it did not name.

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
from tatva_connect.tests.automation import field_allowlist

TYPE_NAME = "ZZ Dual Write Probe"

# A slot the plan retires, and a common column it keeps. Named here because the fixture must MEAN one of
# each; the router is never told which is which.
DYING_SLOT = "custom_key_date_1"
RETAINED_COMMON = "custom_outcome"

# A plain, writable, native CRM Lead column: the lead-sourced field snapshots it, and "the lead was not written" is only a real assertion about a column that COULD have been written. Asserted as a premise below.
LEAD_FIELD = "job_title"

# What the LEAD holds, and what a client SENDS for the same field. They differ on purpose: a `source = Lead`
# value is read on the server and the submitted one is ignored entirely, so the two constants are the only
# way to tell "the lead's context was snapshotted" from "whatever the caller typed was stored".
ON_LEAD = "ZZ Oncologist On File"
FORGED = "ZZ Forged By The Client"

SUBMITTED = {
	"zz_sample_collected": "2026-07-01 10:30:00",
	"zz_remark": "ZZ remark text",
	"zz_outcome": "ZZ Reached",
	"zz_column_answer": "ZZ Column Answer",
	LEAD_FIELD: FORGED,
}

RESUBMITTED = dict(SUBMITTED, zz_remark="ZZ remark text, revised")


def _key_value_sections():
	"""Every section the operator declared key-value, in the order the untargeted fallback picks from."""
	return frappe.get_all(
		"CRM Task Section",
		filters={"is_key_value": 1},
		fields=["name", "target_doctype", "child_table_field", "row_key_field", "value_field"],
		order_by="display_order",
	)


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
		key_value_sections = _key_value_sections()
		cls.key_value = key_value_sections[0]
		# The one a declaration must NAME to reach, because the fallback above can only ever answer with the first.
		cls.named_key_value = key_value_sections[1] if len(key_value_sections) > 1 else None
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
			# Rule 4 — a lead-sourced field NAMING a key-value section: the lead's context, snapshotted.
			{"label": "ZZ Lead Context", "fieldname": LEAD_FIELD, "fieldtype": "Data", "source": "Lead",
			 "section": cls.named_key_value.name if cls.named_key_value else ""},
		])
		# A type declaring NO lead field, so "nothing routes there ⇒ no row" is asserted and not assumed.
		cls.plain_type = task_type_fixture.mint_type("ZZ No Lead Fields Probe", [
			{"label": "ZZ Plain Note", "fieldname": "zz_plain_note", "fieldtype": "Data"},
		])
		# The lead's value is read through the lead detail brain, so the field has to BE on the lead catalog
		# and visible at this grain — a field this viewer may not see is not in the answer at all, and every
		# snapshot assertion below would be vacuous. Same seed `test_lead_fields_in_form` uses.
		field_allowlist.seed_settable("CRM Lead", LEAD_FIELD,
									  vertical=task_type_fixture.VERTICAL, group=task_type_fixture.GROUP)
		frappe.db.commit()  # class-level seed, same as mint_type: the per-test rollback must not eat it

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		field_allowlist.clear()
		task_type_fixture.teardown()
		super().tearDownClass()

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")
		self.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Dual Write Probe",
			"mobile_no": f"+9198127{int(frappe.generate_hash(length=8), 16) % 100000:05d}",
			"custom_vertical": task_type_fixture.VERTICAL, "custom_group": task_type_fixture.GROUP,
			LEAD_FIELD: ON_LEAD,
		}).insert(ignore_permissions=True)

	# ---- the premise ------------------------------------------------------------------------------

	def test_the_probe_carries_one_field_of_each_routed_shape(self):
		"""A fixture missing a shape would prove the router for the shapes it happens to carry and no more."""
		self.assertIsNotNone(self.column_section, "no section declares real named columns — rule 1 is untestable")
		self.assertTrue(self.key_value, "no section is declared key-value — rule 3 has no home")
		self.assertIn(DYING_SLOT, backfill.PROMOTED_COLUMNS,
					  f"`{DYING_SLOT}` was never a promoted column — it is no longer a dying slot")
		self.assertNotIn(DYING_SLOT, activity_api.task_columns(),
						 f"`{DYING_SLOT}` is retained — pick a slot the plan actually dropped")
		self.assertIn(RETAINED_COMMON, activity_api.task_columns(),
					  f"`{RETAINED_COMMON}` is not a retained common column — rule 2 is untestable")
		self.assertIsNotNone(self.named_key_value,
							 "only one section is key-value — rule 4 has nothing to name and is untestable")
		self.assertNotEqual(self.named_key_value.name, self.key_value.name,
							"the named section IS the fallback, so naming it would prove nothing")
		df = frappe.get_meta("CRM Lead").get_field(LEAD_FIELD)
		self.assertIsNotNone(df, f"CRM Lead no longer has `{LEAD_FIELD}` — pick another plain column")
		self.assertFalse(df.read_only, f"`{LEAD_FIELD}` became read-only; 'the lead was not written' would be vacuous")
		# The snapshot is read through the lead detail brain, so a field this viewer is not entitled to see is
		# not in the answer at all — and every assertion about what got snapshotted would pass on an empty dict.
		self.assertEqual(activity_api.lead_field_values(self.lead.name, self.task_type).get(LEAD_FIELD), ON_LEAD,
						 "the lead's value is not visible at this grain — the snapshot assertions are vacuous")

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
		self.assertEqual(activity_api.field_target(schema[LEAD_FIELD]),
						 (self.named_key_value.name, LEAD_FIELD),
						 "a field naming a key-value section was routed to the fallback one instead")

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

	# ---- an unanswered question is not an answer ---------------------------------------------------

	def test_a_blank_answer_earns_no_key_value_row(self):
		"""A key-value row COSTS a row, so writing one for a question nobody answered is pure weight.

		Measured on the 10-lead Anaya trial before this rule existed: 20,754 answer rows of which 13,442 —
		65% — held an empty string. A type declaring 17 fields wrote 17 rows however few the rep filled in.
		"""
		submitted = {**SUBMITTED, "zz_remark": ""}
		task = frappe.get_doc("CRM Task", activity_api.save_activity(self.lead.name, self.task_type, submitted))

		self.assertNotIn("zz_remark", self._answers(task), "a blank answer was given a row of its own")
		self.assertIn("zz_sample_collected", self._answers(task),
					  "the answered fields must be untouched by the blank rule")

	def test_clearing_an_answer_REMOVES_its_row_rather_than_blanking_it(self):
		"""The other half, and the reason a blank cannot simply be skipped: skipping would leave the row the
		first save wrote, so a cleared field would keep answering with the value the rep just deleted."""
		name = activity_api.save_activity(self.lead.name, self.task_type, SUBMITTED)
		self.assertIn("zz_remark", self._answers(frappe.get_doc("CRM Task", name)))

		activity_api.save_activity(self.lead.name, self.task_type, {**SUBMITTED, "zz_remark": ""}, task=name)

		self.assertNotIn("zz_remark", self._answers(frappe.get_doc("CRM Task", name)),
						 "the cleared answer still has a row — it would read back as the old value")

	def test_a_column_section_row_is_not_born_to_hold_only_blanks(self):
		"""A column row is shared by its whole family, so it exists only once something in it is answered.
		Creating one regardless is what filled Engagement with 3,055 rows carrying nothing at all."""
		task = frappe.get_doc("CRM Task", activity_api.save_activity(
			self.lead.name, self.task_type, {**SUBMITTED, "zz_column_answer": ""}))

		self.assertIsNone(self._column_row(task), "a row was created holding only a blank")

	def test_an_answered_column_field_still_gets_its_row(self):
		"""The other direction, so the rule above cannot pass by never writing a column row at all."""
		task = frappe.get_doc("CRM Task", activity_api.save_activity(self.lead.name, self.task_type, SUBMITTED))

		self.assertEqual(self._column_row(task).get(self.column), SUBMITTED["zz_column_answer"])

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

	# ---- the lead's context, snapshotted onto the activity ------------------------------------------

	def test_a_lead_sourced_value_is_snapshotted_onto_the_task(self):
		"""What the LEAD held when this activity was logged is a fact ABOUT the activity, so the activity
		stores it. Reading the lead now would answer a different question: "what does it hold today".

		The value asserted is the lead's own, read back off the lead — not the constant the client sent, which
		is what this used to assert and which was the defect written down: the submitted value was believed."""
		task = frappe.get_doc("CRM Task", activity_api.save_activity(self.lead.name, self.task_type, SUBMITTED))

		self.assertEqual(self._snapshot(task)[LEAD_FIELD].get(self.named_key_value.value_field),
						 frappe.db.get_value("CRM Lead", self.lead.name, LEAD_FIELD),
						 "the lead's context never reached the activity")
		self.assertNotIn(LEAD_FIELD, self._answers(task),
						 "the lead's context landed among the rep's answers — the two are not the same thing")

	def test_a_forged_lead_value_is_ignored_and_the_lead_wins(self):
		"""THE lock. A `source = Lead` field is context, not an answer, so the payload has no say in it: the
		server reads the lead and drops what the caller sent. Without this, any HTTP client could assert *"at
		this order punch the oncologist was X"* about a patient record that never said so — an audited clinical
		claim, forged, and indistinguishable afterwards from one the lead really carried."""
		self.assertNotEqual(ON_LEAD, SUBMITTED[LEAD_FIELD], "the payload agrees with the lead — nothing is proved")
		task = frappe.get_doc("CRM Task", activity_api.save_activity(self.lead.name, self.task_type, SUBMITTED))

		self.assertEqual(self._snapshot(task)[LEAD_FIELD].get(self.named_key_value.value_field), ON_LEAD,
						 "a value the CLIENT sent was snapshotted as the lead's context")

	def test_a_lead_sourced_value_is_never_written_to_the_lead(self):
		"""A lead is corrected on its own page, where the change is visible and attributable, and never
		sideways through an activity form."""
		activity_api.save_activity(self.lead.name, self.task_type, SUBMITTED)

		self.assertEqual(frappe.db.get_value("CRM Lead", self.lead.name, LEAD_FIELD), ON_LEAD,
						 "an activity wrote to the patient record")

	def test_editing_the_lead_later_changes_no_past_activity(self):
		"""The whole reason the value is snapshotted: a live read would show today's value against a
		two-year-old order punch, which is a different and false claim."""
		name = activity_api.save_activity(self.lead.name, self.task_type, SUBMITTED)
		frappe.db.set_value("CRM Lead", self.lead.name, LEAD_FIELD, "ZZ Corrected Much Later")

		self.assertEqual(activity_api.task_detail(name)["task"]["values"].get(LEAD_FIELD),
						 ON_LEAD, "a past activity moved when the lead was corrected")

	def test_a_type_declaring_no_lead_field_writes_no_snapshot_row(self):
		"""Not every activity asks for lead context, and one that does not must cost nothing."""
		task = frappe.get_doc("CRM Task", activity_api.save_activity(
			self.lead.name, self.plain_type, {"zz_plain_note": "ZZ note"}))

		self.assertEqual(task.get(self.named_key_value.child_table_field) or [], [],
						 "a type with no lead field was given a snapshot row anyway")

	def test_a_lead_field_is_painted_read_only_whatever_the_lead_holds(self):
		"""The form SHOWS the context; it never collects it. So there is nothing to ask and no state in
		which the box opens — which is why the rep can never be refused after typing."""
		for on_file in (None, "ZZ Already On File"):
			with self.subTest(on_file=on_file):
				frappe.db.set_value("CRM Lead", self.lead.name, LEAD_FIELD, on_file)
				cfg = activity_api.type_config(self.task_type, lead=self.lead.name)
				descriptor = next(f for f in cfg["fields"] if f["fieldname"] == LEAD_FIELD)

				self.assertEqual(descriptor["read_only"], 1, "a lead field opened editable")
				self.assertEqual(next(f for f in cfg["fields"] if f["fieldname"] == "zz_remark")["read_only"], 0,
								 "an ordinary activity field was painted read-only beside it")

	# ---- reading the new home, through the declaration and never a literal --------------------------

	def _snapshot(self, task):
		"""The task's rows in the NAMED key-value section, keyed by the fieldname each one snapshots."""
		return {r.get(self.named_key_value.row_key_field): r
				for r in task.get(self.named_key_value.child_table_field)}

	def _answers(self, task):
		"""The task's key-value rows, keyed by the fieldname each one answers."""
		return {r.get(self.key_value.row_key_field): r for r in task.get(self.key_value.child_table_field)}

	def _column_row(self, task):
		"""The column section's row, or None when nothing in that family was answered. Never more than one:
		it is one row per task, not one per field."""
		rows = task.get(self.column_section.child_table_field) or []
		self.assertLessEqual(len(rows), 1, "a single-row section grew a second row")
		return rows[0] if rows else None

	def _row_counts(self, task):
		"""Rows per section child table, and the value each answer row holds — what a duplicate would move."""
		return {
			s.child_table_field: len(task.get(s.child_table_field) or [])
			for s in frappe.get_all("CRM Task Section", fields=["child_table_field"])
			if s.child_table_field
		}
