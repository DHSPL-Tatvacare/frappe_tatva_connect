# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Completing an existing activity is ONE write.

It used to be two. The modal called `frappe.client.set_value` with the standard CRM Task fields, and then
`save_activity` with the answers — and that fork was three defects at once:

  1. `status: Done` committed BEFORE any answer existed, so `enforce_activity_logged` (the fail-closed
     backstop, one brain with `activity_is_unlogged`) refused the rep who had just filled the form:
     *"Log this activity's details before marking it Done"*. A typed activity could not be completed
     from the screen built to complete it.
  2. The payload builder appended `notes` OUTSIDE the visibility loop, so a `notes` a rule had hidden was
     still submitted and `compute_activity` correctly refused it. On Document Verification Status, `notes`
     shows for 3 of 7 statuses — the other 4 were unsaveable from the SPA while the same payload went
     through over the API.
  3. Every refusal left the task half-updated: title/status/priority/dates/assignee were already
     committed by the first call when the second one threw.

All three are one root cause, so this module asserts the one property that kills all three: the caller's
own CRM Task columns ride the SAME `save_activity` call, land in the SAME `doc.update` as the computed
answers, and are saved once. A throw rolls back everything.

Symptom 2's client half — the payload builder no longer appending a hidden `notes` — is asserted where it
lives, in `frappe_tatva_crm/frontend/tests/component/TaskModal.test.js`. What is asserted HERE is the
server-side end state the fix has to reach: the rep's description survives a status at which the declared
`notes` field is hidden.

Run:
    bench --site wipetest.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.activity.test_one_write_per_save
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.activity import api as activity_api
from tatva_connect.tests.activity import task_type_fixture

TYPE_NAME = "ZZ One Write Probe"

# The operator switch the `enforce_activity_logged` backstop hangs off. Armed for this class, restored OFF
# — never "what it was": restoring the previous value is what propagates a poisoned baseline.
GUARD_SWITCH = "Task::CRM Task::guards"

STATUS = "zz_dv_status"
NOTES = "notes"
SHOWS_NOTES, HIDES_NOTES = "ZZ Verified", "ZZ Rejected"

# Plain text, not markup: `description` is a Text Editor and a sanitiser rewriting the tags would make
# these assertions about the sanitiser rather than about where the value landed.
DESCRIPTION = "ZZ Aadhaar and prescription checked"


class TestOneWritePerSave(FrappeTestCase):
	"""A rep completing a typed activity: one call, one save, and nothing committed when it is refused."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		# The Document Verification shape: a status, and a `notes` homed at `description` that only ONE of
		# the two statuses shows. `notes` is an ordinary declared field (D-G) — its target is read off the
		# `CRM Task Field` catalog by `field_target`, never named here.
		cls.notes_column = _notes_column()
		cls.task_type = task_type_fixture.mint_type(TYPE_NAME, [
			{"label": "ZZ DV Status", "fieldname": STATUS, "fieldtype": "Select",
			 "options": f"{SHOWS_NOTES}\n{HIDES_NOTES}"},
			{"label": "ZZ Notes", "fieldname": NOTES, "fieldtype": "Small Text",
			 "target": cls.notes_column or ""},
		], rules=[
			{"action": "Show", "targets": NOTES, "condition_field": STATUS,
			 "operator": "is", "condition_value": SHOWS_NOTES},
		])
		# is_logged_complete: completing it is what stamps Done, which is the state symptom 1 lived in.
		frappe.db.set_value("CRM Task Type", cls.task_type, "is_logged_complete", 1)
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
			"doctype": "CRM Lead", "first_name": "One Write Probe",
			"mobile_no": f"+9198126{int(frappe.generate_hash(length=8), 16) % 100000:05d}",
			"custom_vertical": task_type_fixture.VERTICAL, "custom_group": task_type_fixture.GROUP,
		}).insert(ignore_permissions=True)
		# The open task a rep opens from the board — created by hand, NOT by save_activity, because the
		# defect lives on the second save of a task that already exists and carries no answers yet.
		self.task = frappe.get_doc({
			"doctype": "CRM Task", "title": "ZZ Document Verification",
			"custom_task_type": self.task_type, "status": "Todo", "priority": "Low",
			"reference_doctype": "CRM Lead", "reference_docname": self.lead.name,
		}).insert(ignore_permissions=True)

	# ---- the premise ------------------------------------------------------------------------------

	def test_the_guard_that_used_to_refuse_the_rep_is_actually_armed(self):
		"""With the switch off there is no refusal to prevent, and every assertion below is vacuous."""
		from tatva_connect.automation import settings

		self.assertTrue(settings.is_enabled(GUARD_SWITCH), "the activity-logged backstop is dormant")
		unlogged = frappe.get_doc("CRM Task", self.task.name)
		unlogged.status = "Done"
		self.assertTrue(activity_api.activity_is_unlogged(unlogged),
						"a fresh typed task with no answers does not read as unlogged — "
						"symptom 1 cannot be reproduced")

	def test_the_declared_notes_field_is_hidden_at_one_status_and_shown_at_the_other(self):
		"""Symptom 2 is about a field a RULE hid; a fixture that shows it always would prove nothing."""
		schema = activity_api.compiled_fields(frappe.get_doc("CRM Task Type", self.task_type))
		self.assertIn(NOTES, activity_api._shown_fieldnames(schema, {STATUS: SHOWS_NOTES}))
		self.assertNotIn(NOTES, activity_api._shown_fieldnames(schema, {STATUS: HIDES_NOTES}))

	# ---- symptom 1: a rep can complete an existing typed activity -----------------------------------

	def test_the_standard_fields_and_the_answers_land_in_one_save(self):
		"""The rep marks it Done and fills the form in the same action, so the backstop must see the
		answers on the save that sets Done — not on the one after it."""
		activity_api.save_activity(
			self.lead.name, self.task_type, {STATUS: HIDES_NOTES}, task=self.task.name,
			task_fields={"title": "ZZ Document Verification", "description": DESCRIPTION,
						 "status": "Done", "priority": "High", "assigned_to": None},
		)

		task = frappe.get_doc("CRM Task", self.task.name)
		self.assertEqual(task.status, "Done", "the completion never landed")
		self.assertEqual(task.priority, "High", "the standard fields the form edited were dropped")
		self.assertEqual(
			activity_api.task_detail(self.task.name)["task"]["values"].get(STATUS), HIDES_NOTES,
			"the answers did not land on the same save")

	# ---- symptom 2: a description survives a status that hides `notes` ------------------------------

	def test_a_hidden_notes_field_refuses_the_value_that_used_to_be_appended(self):
		"""The refusal is CORRECT and stays — which is why the client had to stop sending it. Asserted so
		the fix cannot be mistaken for the server having been loosened."""
		with self.assertRaises(frappe.ValidationError) as caught:
			activity_api.save_activity(
				self.lead.name, self.task_type, {STATUS: HIDES_NOTES, NOTES: "typed anyway"},
				task=self.task.name,
			)
		self.assertIn("was not shown on this form", str(caught.exception))

	def test_the_description_survives_a_status_that_hides_notes(self):
		"""What the rep actually loses if the fix is wrong: the description they wrote. It rides the
		standard fields, so hiding the declared `notes` field costs nothing."""
		activity_api.save_activity(
			self.lead.name, self.task_type, {STATUS: HIDES_NOTES}, task=self.task.name,
			task_fields={"description": DESCRIPTION, "status": "Done"},
		)

		self.assertEqual(frappe.db.get_value("CRM Task", self.task.name, "description"), DESCRIPTION,
						 "the rep's description was lost at a status that hides the notes field")

	def test_a_shown_notes_answer_still_reaches_its_declared_home(self):
		"""The other direction, so "hidden notes is dropped" can never pass by dropping notes always."""
		activity_api.save_activity(
			self.lead.name, self.task_type, {STATUS: SHOWS_NOTES, NOTES: "ZZ verified by hand"},
			task=self.task.name, task_fields={"status": "Done"},
		)

		self.assertEqual(
			activity_api.task_detail(self.task.name)["task"]["values"].get(NOTES), "ZZ verified by hand",
			"a shown notes answer never reached the home its declaration names")

	# ---- symptom 3: a refused save commits NOTHING --------------------------------------------------

	def test_a_refused_save_leaves_the_task_exactly_as_it_was(self):
		"""The whole point of one write. The standard edits used to be committed by the first call, so a
		task refused for its answers was already carrying the new title, status and priority.

		This is a LOCK, not the reproduction: the fork lived in the client, so what went red on the old
		code is `TaskModal.test.js`'s `sv.calls` assertion, not this. What this forbids is a future writer
		putting the caller's columns in before the compute has agreed to them."""
		before = frappe.db.get_value(
			"CRM Task", self.task.name, ["title", "status", "priority", "description"], as_dict=True)

		with self.assertRaises(frappe.ValidationError):
			activity_api.save_activity(
				self.lead.name, self.task_type, {STATUS: HIDES_NOTES, NOTES: "typed anyway"},
				task=self.task.name,
				task_fields={"title": "ZZ Renamed", "description": DESCRIPTION,
							 "status": "Done", "priority": "High"},
			)

		self.assertEqual(
			frappe.db.get_value(
				"CRM Task", self.task.name, ["title", "status", "priority", "description"], as_dict=True),
			before, "a refused save left the task half-updated")

	# ---- every existing caller is unchanged ---------------------------------------------------------

	def test_omitting_the_argument_is_byte_identical_to_what_shipped(self):
		"""Every trusted caller — the partner API, the migration harness — sends answers only. G4: a new
		argument defaults to today's behaviour."""
		before = frappe.db.get_value(
			"CRM Task", self.task.name, ["title", "priority"], as_dict=True)

		activity_api.save_activity(
			self.lead.name, self.task_type, {STATUS: HIDES_NOTES}, task=self.task.name)

		task = frappe.db.get_value(
			"CRM Task", self.task.name, ["title", "priority", "status"], as_dict=True)
		self.assertEqual(task.title, before.title, "an omitted argument moved a standard field")
		self.assertEqual(task.priority, before.priority, "an omitted argument moved a standard field")
		self.assertEqual(task.status, "Done", "the type's declaration no longer decides the status")

	def test_the_new_punch_path_still_inserts_one_task(self):
		"""`save_activity` with no `task` is the ad-hoc punch the partner API uses; it must be untouched."""
		name = activity_api.save_activity(self.lead.name, self.task_type, {STATUS: HIDES_NOTES})

		self.assertEqual(frappe.db.get_value("CRM Task", name, "status"), "Done")
		self.assertEqual(
			activity_api.task_detail(name)["task"]["values"].get(STATUS), HIDES_NOTES)

	# ---- the caller's columns never outrank the declaration -----------------------------------------

	def test_the_computed_fields_are_applied_last(self):
		"""Compute ran SECOND when this was two calls, so the type's declaration decided `status` and every
		routed column. Applying the caller's columns first keeps that exactly as it was."""
		activity_api.save_activity(
			self.lead.name, self.task_type, {STATUS: HIDES_NOTES}, task=self.task.name,
			task_fields={"status": "In Progress"},
		)

		self.assertEqual(frappe.db.get_value("CRM Task", self.task.name, "status"), "Done",
						 "a caller's `status` overrode the type's is_logged_complete declaration")


def _notes_column():
	"""The CRM Task column the operator declared as Notes — asked of `CRM Task Field`, the ONE brain for
	which columns a declaration may write (§8 rule 2). A column name written here would be the second brain
	D-H deleted. Blank if the operator declares no such column, and the field then falls to its key-value
	home like any other — which changes nothing this module asserts."""
	rows = frappe.get_all(
		"CRM Task Field", filters={"can_set": 1, "label": "Notes"}, pluck="fieldname", limit=1)
	return rows[0] if rows else ""
