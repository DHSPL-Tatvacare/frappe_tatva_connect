# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Phase 7 of docs/plans/task-form-layer/2026-07-25-task-slots-to-sections-and-form-layer.md: the dead columns really go
— and never one migrate too early.

§5.5 retires six CRM Task columns: `custom_key_date_1..4`, `custom_reference` and
`custom_activity_payload`. The hazard §0.1 deferred this phase over is an ORDERING one: the pass that
copies answers out of those columns (`activity.backfill.ensure_section_rows`) is an `after_migrate` hook,
and a post-model-sync patch runs BEFORE it. Drop first and the data is gone for ever. So the patch proves
the end state instead of assuming a predecessor ran, and this module is that proof, in the order a real
migrate meets it:

  1. an answer that lives ONLY in an old home  -> the patch REFUSES, loudly, and every column survives;
  2. the backfill runs                          -> the audit says nothing is unhomed any more;
  3. the patch runs again                       -> every column is really gone from `information_schema`,
                                                   every Custom Field doc with it, and the answer is still
                                                   readable through the LIVE reader;
  4. and once more                              -> a free no-op that writes no second refusal.

Read the DDL assertions off `information_schema`, never off `frappe.get_meta` or `has_column`: the claim
is about the TABLE, and frappe's cached column list is exactly the thing that lies after a raw ALTER.

**THIS TEST IS DESTRUCTIVE AND ONE-SHOT.** Step 3 performs the real drop, which no rollback undoes — that
IS the end state Phase 7 ships, so a bench that has run this module is a bench in the shipped state, and
the story self-skips from then on (with a message saying so). Run it LAST.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.migration.test_retire_task_slot_columns
"""
import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import get_datetime

from tatva_connect.activity import api as activity_api
from tatva_connect.activity import backfill
from tatva_connect.patches import retire_task_slot_columns as patch
from tatva_connect.tests.activity import task_type_fixture

TYPE_NAME = "ZZ Retire Slots Probe"

# One dying slot and one payload key — the two old homes, one probe each. Named here because a DROP test
# must name what it expects to be dropped; every other consumer resolves through the declaration.
DYING_SLOT = "custom_key_date_1"
SLOT_FIELD = "zz_rs_sample_collected"
PAYLOAD_FIELD = "zz_rs_remark"
SLOT_ANSWER = "2026-07-02 09:15:00"
PAYLOAD_ANSWER = "ZZ remark that must outlive its column"

_ERROR_TITLE = "retire_task_slot_columns: refused, answers are not homed yet"


def _column_exists(column):
	"""Straight from information_schema: the assertion is about the TABLE, not about anyone's cache of it."""
	return bool(frappe.db.sql(
		"""SELECT 1 FROM information_schema.COLUMNS
		WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tabCRM Task' AND COLUMN_NAME = %s""",
		(column,),
	))


def _key_value_section():
	"""The section a field with no column of its own answers in — the one the operator declared key-value."""
	return frappe.get_all(
		"CRM Task Section", filters={"is_key_value": 1},
		fields=["name", "child_table_field", "row_key_field", "value_field"],
		order_by="display_order",
	)[0]


class TestTheDropListItself(FrappeTestCase):
	"""What the patch removes, checked against §5.5 and against the seam that has to agree with it."""

	def test_the_drop_list_is_exactly_the_six_the_plan_names(self):
		self.assertEqual(
			set(patch._DEAD),
			{"custom_key_date_1", "custom_key_date_2", "custom_key_date_3", "custom_key_date_4",
			 "custom_reference", "custom_activity_payload"},
			"the drop list has drifted from plan §5.5",
		)

	def test_no_retained_common_column_is_on_the_drop_list(self):
		"""D18: the four the task row KEEPS are the live seam — `_validate_asm` reads one of them."""
		self.assertEqual(
			set(patch._DEAD) & set(activity_api.COMMON_COLUMNS), set(),
			"the patch would drop a column the writer still writes and the reader still reads",
		)

	def test_the_audit_and_the_patch_agree_on_which_homes_are_retired(self):
		"""One rule expressed once: the patch names the six, the backfill DERIVES the same six from the two
		lists it already holds, and a refusal computed against a different set would be no guard at all."""
		self.assertEqual(set(backfill.retired_homes()), set(patch._DEAD),
						 "the audit judges a different set of old homes than the patch drops")

	def test_the_fixture_no_longer_declares_any_of_them(self):
		"""A fixture sync never DROPS a field, but it does RE-CREATE one it still declares — so a name left
		in custom_field.json would be rebuilt by the very migrate that dropped it."""
		import json
		import os

		path = os.path.join(frappe.get_app_path("tatva_connect"), "fixtures", "custom_field.json")
		with open(path) as f:
			declared = {r["fieldname"] for r in json.load(f) if r.get("dt") == "CRM Task"}
		self.assertEqual(declared & set(patch._DEAD), set(),
						 "custom_field.json still declares a retired column — it would come back")


class TestTheDropRefusesUntilEveryAnswerIsHomed(FrappeTestCase):
	"""The whole story, in the order a migrate meets it. Destructive and one-shot — see the module header."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.key_value = _key_value_section()
		cls.task_type = task_type_fixture.mint_type(TYPE_NAME, [
			# A dying slot: promoted before Phase 5, claimed by no section and no common column now.
			{"label": "ZZ RS Sample Collected", "fieldname": SLOT_FIELD,
			 "fieldtype": "Datetime", "target": DYING_SLOT},
			# No target at all: the JSON payload before Phase 5, an answer row of its own name now.
			{"label": "ZZ RS Remark", "fieldname": PAYLOAD_FIELD, "fieldtype": "Small Text"},
		])

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		task_type_fixture.teardown()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		if not all(_column_exists(c) for c in patch._DEAD):
			self.skipTest(
				"the retired columns are already gone from tabCRM Task, so there is no un-dropped state to "
				"drive. This module is one-shot by construction — the drop it proves is permanent."
			)
		# The DDL in step 3 implicitly commits, so the per-test rollback cannot remove these two rows: they
		# are deleted by name instead. Registered BEFORE they exist so a mid-way failure still cleans up.
		self.addCleanup(self._destroy_probe)
		self.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Retire Slots Probe",
			"mobile_no": f"+9198132{int(frappe.generate_hash(length=8), 16) % 100000:05d}",
			"custom_vertical": task_type_fixture.VERTICAL, "custom_group": task_type_fixture.GROUP,
		}).insert(ignore_permissions=True)
		# The pre-state: a task as it existed before Phase 2 — answers in the old homes, no section row.
		self.task = frappe.get_doc({
			"doctype": "CRM Task", "title": TYPE_NAME, "custom_task_type": self.task_type,
			"reference_doctype": "CRM Lead", "reference_docname": self.lead.name, "status": "Todo",
			DYING_SLOT: SLOT_ANSWER,
			"custom_activity_payload": frappe.as_json({PAYLOAD_FIELD: PAYLOAD_ANSWER}),
		}).insert(ignore_permissions=True).name

	def _destroy_probe(self):
		for doctype, name in (("CRM Task", getattr(self, "task", None)),
							  ("CRM Lead", getattr(self, "lead", None) and self.lead.name)):
			if name and frappe.db.exists(doctype, name):
				frappe.delete_doc(doctype, name, ignore_permissions=True, force=True, delete_permanently=True)
		frappe.db.commit()

	def test_it_refuses_while_an_answer_is_unhomed_and_drops_once_it_is_not(self):
		# ---- 1. the premise: the answers really are in the old homes and nowhere else -------------------
		self.assertEqual(self._answers(), {}, "the constructed pre-state already answers in the new home")
		# A FULL scan, no limit: the probe's own answer must be in the result, and a capped scan could stop
		# before reaching it on a bench that carries other unhomed answers.
		_routed, unhomed = backfill.audit()
		self.assertIn((str(self.task), SLOT_FIELD), [(str(t), f) for t, f in unhomed],
					  "the audit cannot see the unhomed answer — the refusal below would prove nothing")

		# ---- 2. ...so the patch refuses, and every column survives --------------------------------------
		before = frappe.db.count("Error Log", {"method": ["like", f"%{_ERROR_TITLE}%"]})
		patch.execute()
		for column in patch._DEAD:
			self.assertTrue(_column_exists(column),
							f"`{column}` was dropped while an answer still lived only in an old home")
			self.assertTrue(frappe.db.exists("Custom Field", f"CRM Task-{column}"),
							f"the Custom Field doc for `{column}` was deleted before the copy finished")
		self.assertEqual(get_datetime(frappe.db.get_value("CRM Task", self.task, DYING_SLOT)),
						 get_datetime(SLOT_ANSWER), "the refused run still lost the slot's value")
		self.assertGreater(frappe.db.count("Error Log", {"method": ["like", f"%{_ERROR_TITLE}%"]}), before,
						   "the refusal was silent — a no-op nobody is told about is a lost deploy")

		# ---- 3. the backfill completes the copy, which is the condition the patch gates on --------------
		backfill.ensure_section_rows()
		_routed, unhomed = backfill.audit(limit=10)
		self.assertEqual(unhomed, [],
						 f"answers are still unhomed after the backfill, so the drop cannot be proven: {unhomed}")

		# ---- 4. ...and now the columns really go -------------------------------------------------------
		patch.execute()
		for column in patch._DEAD:
			self.assertFalse(_column_exists(column), f"`{column}` is still a column of tabCRM Task")
			self.assertFalse(frappe.db.exists("Custom Field", f"CRM Task-{column}"),
							 f"the Custom Field doc for `{column}` survived the drop")

		# ---- 5. ...with both answers still readable, at the address field_target names ------------------
		values = activity_api.task_detail(self.task)["task"]["values"]
		self.assertEqual(get_datetime(values[SLOT_FIELD]), get_datetime(SLOT_ANSWER),
						 "the slot's answer died with its column instead of being read from its section row")
		self.assertEqual(values[PAYLOAD_FIELD], PAYLOAD_ANSWER,
						 "the payload key's answer died with the payload")

		# ---- 6. a second run is a free no-op, and writes no second refusal -----------------------------
		after = frappe.db.count("Error Log", {"method": ["like", f"%{_ERROR_TITLE}%"]})
		patch.execute()
		self.assertEqual(frappe.db.count("Error Log", {"method": ["like", f"%{_ERROR_TITLE}%"]}), after,
						 "the applied patch refused on a re-run — the end state is not being read as true")
		for column in patch._DEAD:
			self.assertFalse(_column_exists(column), f"a second run brought `{column}` back")

	def _answers(self):
		"""The task's key-value rows, keyed by the fieldname each one answers."""
		return {r.get(self.key_value.row_key_field): r
				for r in frappe.get_doc("CRM Task", self.task).get(self.key_value.child_table_field)}
