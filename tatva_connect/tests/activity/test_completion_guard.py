# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The completion guard asks the FORM's question, and asks it on the transition.

`activity_is_unlogged` is the single definition of *an activity completed empty*, and
`tasks.enforce_activity_logged` is the validate backstop that refuses on it — so the rule holds on the quick
status dropdown, a data import and the API alike, not just on the screen built to complete an activity.

It answered a question of its own. "Logged" meant **any one** declared field non-empty, while the form
demands **every shown-and-required** field. The two diverge exactly where it hurts: a `notes` field homed at
`description` (D-G) reads back non-empty on a task that carries nothing but a description, so a native
`status = Done` — Desk, `frappe.client.set_value`, an import — walked past a payload the form itself refuses.
Measured 2026-07-29 on the live seed: **allowed on 8 of 8** types that home a field at `description`.

The fix is not a second rule, it is the same one. `compute_activity` settles what the form shows and what it
demands through `_settled` (the `_shown_fieldnames` / `_inert` fixpoint) and `_required_here`; the guard reads
the task's stored answers back into the shape a submission has — at the ONE address `field_target` names —
and asks those same functions. What the writer refuses, the guard refuses.

Two things it must NOT do, both asserted here:

  * **It must not fire on every save of a Done task.** The rule in English is *do not MARK it Done empty*.
    3,043 already-Done tasks on this site would otherwise start being refused the next time anything touched
    one. `doc.has_value_changed("status")` scopes it — and answers True on an insert, so a task born Done is
    still judged (`frappe/model/document.py:684`).
  * **It must not weaken.** 58 of 66 live types declare no mandatory field at all, so the form's question
    alone would let a wholly empty Done through on them. The original floor — nothing captured anywhere —
    stays beside it.

Run:
    bench --site wipetest.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.activity.test_completion_guard
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.activity import api as activity_api
from tatva_connect.tests.activity import task_type_fixture

TYPE_NAME = "ZZ Completion Guard Probe"
FLOOR_TYPE_NAME = "ZZ Completion Guard Floor Probe"

# The operator switch the backstop hangs off. Armed for this class, restored OFF — never "what it was":
# restoring the previous value is what propagates a poisoned baseline.
GUARD_SWITCH = "Task::CRM Task::guards"

STATUS = "zz_cg_status"
REASON = "zz_cg_reason"        # static reqd — the form demands it whatever else is answered
FOLLOWUP = "zz_cg_followup"    # made mandatory by a RULE, at one status only
NOTES = "notes"                # homed at `description` by the CRM Task Field catalog (D-G)

CONNECTED, NOT_CONNECTED = "ZZ Connected", "ZZ Not Connected"
DESCRIPTION = "<p>Spoke to the patient</p>"


def _notes_column():
	"""The CRM Task column the operator declared as Notes — asked of `CRM Task Field`, the ONE brain for
	which columns a declaration may write (§8 rule 2). A column name written here would be the second brain
	D-H deleted. Blank if no such column is declared, and the reproduction below says so rather than passing."""
	rows = frappe.get_all(
		"CRM Task Field", filters={"can_set": 1, "label": "Notes"}, pluck="fieldname", limit=1)
	return rows[0] if rows else ""


class TestCompletionGuard(FrappeTestCase):
	"""One type shaped like the live ones that broke: a status, a field the form always demands, a field a
	rule demands at one status, and a `notes` homed at `description`."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.notes_column = _notes_column()
		cls.task_type = task_type_fixture.mint_type(TYPE_NAME, [
			{"label": "ZZ CG Status", "fieldname": STATUS, "fieldtype": "Select",
			 "options": f"{CONNECTED}\n{NOT_CONNECTED}"},
			{"label": "ZZ CG Reason", "fieldname": REASON, "fieldtype": "Data", "reqd": 1},
			{"label": "ZZ CG Followup", "fieldname": FOLLOWUP, "fieldtype": "Data"},
			{"label": "ZZ CG Notes", "fieldname": NOTES, "fieldtype": "Small Text",
			 "target": cls.notes_column or ""},
		], rules=[
			{"action": "Make Mandatory", "targets": FOLLOWUP, "condition_field": STATUS,
			 "operator": "is", "condition_value": NOT_CONNECTED},
		])
		# A type declaring nothing mandatory — 58 of the 66 live types are this shape, and the form's own
		# question cannot answer for them. Without it "the guard did not weaken" is untestable.
		cls.floor_type = task_type_fixture.mint_type(FLOOR_TYPE_NAME, [
			{"label": "ZZ Floor Note", "fieldname": "zz_floor_note", "fieldtype": "Data"},
		])
		frappe.db.commit()
		cls.addClassCleanup(frappe.db.commit)
		cls.addClassCleanup(frappe.db.set_value, "CRM Tatva Automation", GUARD_SWITCH, "enabled", 0)
		frappe.db.set_value("CRM Tatva Automation", GUARD_SWITCH, "enabled", 1)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		task_type_fixture.teardown()
		super().tearDownClass()

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")
		self.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Completion Guard Probe",
			"mobile_no": f"+9198125{int(frappe.generate_hash(length=8), 16) % 100000:05d}",
			"custom_vertical": task_type_fixture.VERTICAL, "custom_group": task_type_fixture.GROUP,
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input

	# ---- the premise --------------------------------------------------------------------------------

	def test_the_probe_reproduces_the_shape_that_got_past_the_guard(self):
		"""Without a field really homed at `description`, "a task carrying only a description" is not the
		live shape and every assertion below is about something else."""
		self.assertTrue(self.notes_column,
						"no CRM Task column is declared as Notes — the 8-of-8 defect cannot be reproduced")
		schema = {f.fieldname: f for f in self._schema()}
		self.assertEqual(activity_api.field_target(schema[NOTES]), (None, self.notes_column),
						 "the probe's notes field is not homed on the task's own column")
		self.assertTrue(schema[REASON].reqd, "the probe demands nothing — the form's question is untestable")

	def test_the_backstop_that_does_the_refusing_is_actually_armed(self):
		"""With the switch off there is no refusal to prevent, and every save assertion below is vacuous."""
		from tatva_connect.automation import settings

		self.assertTrue(settings.is_enabled(GUARD_SWITCH), "the activity-logged backstop is dormant")

	# ---- the defect: a payload the FORM refuses walked past the guard --------------------------------

	def test_a_done_task_carrying_only_a_description_is_refused(self):
		"""THE defect, in the shape it was measured in. `notes` is homed at `description`, so the old rule —
		any one declared field non-empty — read this as logged and let it through on 8 of 8 live types. The
		form would have refused the same payload outright: `ZZ CG Reason` is shown and demanded and blank."""
		task = self._todo_task()
		task.description = DESCRIPTION
		task.status = "Done"

		self.assertTrue(activity_api.activity_is_unlogged(task),
						"a description-only completion still reads as logged — the guard is answering its "
						"own question instead of the form's")

	def test_the_same_payload_is_refused_on_the_save_path_a_rep_can_reach(self):
		"""Asked of the backstop, not of the predicate: the rule has to hold on the quick status dropdown and
		on an import, which is the whole reason it lives in `validate`."""
		task = self._todo_task()
		task.description = DESCRIPTION
		task.status = "Done"

		with self.assertRaises(frappe.ValidationError) as caught:
			task.save(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
		self.assertIn("Log this activity's details", str(caught.exception))

	def test_a_completion_that_answers_what_the_form_demands_goes_through(self):
		"""The other direction, so the refusal above cannot pass by refusing everything."""
		task = self._todo_task()
		activity_api.set_schema_field(task, self.task_type, REASON, "ZZ patient asked to call back")
		task.status = "Done"
		task.save(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input

		self.assertEqual(frappe.db.get_value("CRM Task", task.name, "status"), "Done",
						 "a completion carrying every demanded answer was refused")

	# ---- the fixpoint is REUSED, not restated -------------------------------------------------------

	def test_a_field_a_rule_demands_is_demanded_by_the_guard_too(self):
		"""`FOLLOWUP` carries no static `reqd`; a Make Mandatory rule demands it at one status only. The guard
		reaches that through the SAME `_settled` fixpoint and `_required_here` the writer refuses by — a
		second copy of the shown/required logic would answer this differently the first time a rule changed."""
		task = self._todo_task()
		activity_api.set_schema_field(task, self.task_type, REASON, "ZZ reason")
		activity_api.set_schema_field(task, self.task_type, STATUS, NOT_CONNECTED)
		task.status = "Done"

		self.assertTrue(activity_api.activity_is_unlogged(task),
						"a field a rule made mandatory was not demanded on completion")

	def test_the_same_field_is_not_demanded_at_a_status_that_does_not_demand_it(self):
		"""The condition is real: at the other status the rep was never asked, so requiring it would brick a
		completion the form itself allows."""
		task = self._todo_task()
		activity_api.set_schema_field(task, self.task_type, REASON, "ZZ reason")
		activity_api.set_schema_field(task, self.task_type, STATUS, CONNECTED)
		task.status = "Done"

		self.assertFalse(activity_api.activity_is_unlogged(task),
						 "a field nothing demanded at this status was demanded anyway")

	# ---- it did not weaken --------------------------------------------------------------------------

	def test_a_type_demanding_nothing_still_cannot_be_completed_empty(self):
		"""58 of 66 live types declare no mandatory field, so the form's question alone can never refuse them.
		The original floor — nothing captured anywhere — is what still does."""
		task = frappe.get_doc({
			"doctype": "CRM Task", "title": "ZZ Floor", "custom_task_type": self.floor_type,
			"status": "Todo", "reference_doctype": "CRM Lead", "reference_docname": self.lead.name,
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
		task.status = "Done"

		self.assertTrue(activity_api.activity_is_unlogged(task),
						"a type that demands nothing can now be completed with nothing at all")

	def test_a_type_demanding_nothing_is_logged_once_anything_is_answered(self):
		"""The floor is a floor and not a second requirement rule: one answer is enough for a type that asks
		for none in particular."""
		task = frappe.get_doc({
			"doctype": "CRM Task", "title": "ZZ Floor", "custom_task_type": self.floor_type,
			"status": "Todo", "reference_doctype": "CRM Lead", "reference_docname": self.lead.name,
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
		activity_api.set_schema_field(task, self.floor_type, "zz_floor_note", "ZZ answered")
		task.status = "Done"

		self.assertFalse(activity_api.activity_is_unlogged(task),
						 "an answered activity of an undemanding type was refused")

	# ---- the transition, not every save -------------------------------------------------------------

	def test_an_already_done_task_is_not_refused_when_something_else_is_edited(self):
		"""The rule is *do not MARK it Done empty*. 3,043 tasks on this site are already Done and were logged
		under looser rules; a stricter guard applied on every save would refuse the next edit of every one of
		them — a title change, an assignment, an automation touching a column."""
		task = self._todo_task()
		task.description = DESCRIPTION
		task.db_set("status", "Done", update_modified=False)  # already Done, exactly as a migrated row is

		reopened = frappe.get_doc("CRM Task", task.name)
		reopened.title = "ZZ Renamed Long After"
		reopened.save(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input

		self.assertEqual(frappe.db.get_value("CRM Task", task.name, "title"), "ZZ Renamed Long After",
						 "editing an already-Done activity was refused by the completion guard")

	def test_marking_that_same_task_done_from_todo_is_still_refused(self):
		"""The other half of the scoping, so "not on every save" cannot pass by never firing."""
		task = self._todo_task()
		task.description = DESCRIPTION
		task.status = "Done"

		with self.assertRaises(frappe.ValidationError):
			task.save(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input

	def test_a_task_born_done_and_empty_is_refused(self):
		"""An insert has no before-save doc, so `has_value_changed` answers True (document.py:684-685) and the
		transition scoping cannot become a hole a caller reaches by creating the task Done in one shot."""
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({
				"doctype": "CRM Task", "title": "ZZ Born Done", "custom_task_type": self.task_type,
				"status": "Done", "description": DESCRIPTION,
				"reference_doctype": "CRM Lead", "reference_docname": self.lead.name,
			}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input

	# ---- helpers ------------------------------------------------------------------------------------

	def _schema(self):
		return activity_api.compiled_fields(frappe.get_doc("CRM Task Type", self.task_type))

	def _todo_task(self):
		"""The open task a rep opens from the board — created by hand, NOT by save_activity, because what is
		under test is the completion of a task that already exists and carries no answers yet."""
		return frappe.get_doc({
			"doctype": "CRM Task", "title": "ZZ Completion Guard", "custom_task_type": self.task_type,
			"status": "Todo", "reference_doctype": "CRM Lead", "reference_docname": self.lead.name,
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
