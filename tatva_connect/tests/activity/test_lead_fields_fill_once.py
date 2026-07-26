# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A lead field is FILL-ONCE: writable while the patient record is blank, reference-only once it holds a value.

The owner's rule, 2026-07-26. A rep logging an activity may COMPLETE a patient record — fill the oncologist,
the hospital, the cycle number — but may never overwrite what is already there. A prescription on file is not
something a follow-up call gets to replace; a wrong value is corrected on the lead's own page, where the
change is visible and attributable, not sideways through an activity form.

This replaces LeadSquared's `Make Read-Only` rule action, which listed every locked field by hand on every
form (eight targets across two Anaya forms) and silently let a rep overwrite anything the author forgot to
list. Here the ENGINE decides from the data, so there is nothing to maintain and nothing to forget.

ONE predicate — `activity.api.lead_field_is_open` — answers it, and both sides ask it:

  * the form: `type_config` stamps `read_only` on the descriptor, which is the key the fork's controls
    already bind `disabled` to, so the box is greyed BEFORE the rep types;
  * the save: `write_lead_fields` refuses with the same predicate.

That pairing is the property under test. A rep must never be shown a box the save would reject, and must
never be refused a box the form left open.

Run:
    bench --site <site> run-tests --app tatva_connect \\
        --module tatva_connect.tests.activity.test_lead_fields_fill_once
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.activity import api as activity_api
from tatva_connect.tests.activity import task_type_fixture
from tatva_connect.tests.automation import field_allowlist

# A plain, writable, native CRM Lead column — not identity, not routing. Asserted as a premise below.
LEAD_FIELD = "job_title"
ACTIVITY_FIELD = "zz_fo_note"

ALREADY_ON_FILE = "ZZ already on the patient record"
REP_TYPED = "ZZ what the rep typed"


class TestLeadFieldsAreFillOnce(FrappeTestCase):
	"""One type declaring one lead field and one activity field, against two leads: blank and filled."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.task_type = task_type_fixture.mint_type("ZZ Fill Once Probe", [
			{"label": "ZZ Lead Job Title", "fieldname": LEAD_FIELD, "fieldtype": "Data", "source": "Lead"},
			{"label": "ZZ Activity Note", "fieldname": ACTIVITY_FIELD, "fieldtype": "Data"},
		])
		field_allowlist.seed_settable("CRM Lead", LEAD_FIELD,
									  vertical=task_type_fixture.VERTICAL, group=task_type_fixture.GROUP)
		frappe.db.commit()  # class-level seed: the per-test rollback must not eat it

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		field_allowlist.clear()
		task_type_fixture.teardown()
		super().tearDownClass()

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")

	def _lead(self, **extra):
		return frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Fill Once Probe",
			"mobile_no": f"+9198126{int(frappe.generate_hash(length=8), 16) % 100000:05d}",
			"custom_vertical": task_type_fixture.VERTICAL, "custom_group": task_type_fixture.GROUP,
			**extra,
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input

	def _descriptor(self, lead, fieldname):
		cfg = activity_api.type_config(self.task_type, lead=lead.name)
		return next(f for f in cfg["fields"] if f["fieldname"] == fieldname)

	# ---- the premise ---------------------------------------------------------------------------------

	def test_the_probe_field_is_a_plain_writable_lead_column(self):
		"""Without this every assertion below would be about a field that cannot be written anyway."""
		df = frappe.get_meta("CRM Lead").get_field(LEAD_FIELD)
		self.assertIsNotNone(df, f"CRM Lead no longer has `{LEAD_FIELD}` — pick another plain column")
		self.assertFalse(df.read_only, f"`{LEAD_FIELD}` became read-only; this test would prove nothing")

	# ---- blank record: the rep may fill it -----------------------------------------------------------

	def test_a_blank_lead_field_opens_writable_and_the_rep_can_fill_it(self):
		"""The whole point of allowing the write at all: an activity COMPLETES a patient record."""
		lead = self._lead()
		self.assertEqual(self._descriptor(lead, LEAD_FIELD)["read_only"], 0,
						 "a blank patient field opened locked — the rep cannot complete the record")

		activity_api.save_activity(lead.name, self.task_type, {LEAD_FIELD: REP_TYPED})
		self.assertEqual(frappe.db.get_value("CRM Lead", lead.name, LEAD_FIELD), REP_TYPED,
						 "the rep's answer never reached the patient record")

	# ---- filled record: reference only ---------------------------------------------------------------

	def test_a_filled_lead_field_opens_read_only(self):
		"""The form must grey it BEFORE the rep types — being refused after typing is not the rule working."""
		lead = self._lead(**{LEAD_FIELD: ALREADY_ON_FILE})
		descriptor = self._descriptor(lead, LEAD_FIELD)
		self.assertEqual(descriptor["read_only"], 1,
						 "a patient field that already holds a value rendered editable")
		self.assertEqual(activity_api.type_config(self.task_type, lead=lead.name)["lead_values"][LEAD_FIELD],
						 ALREADY_ON_FILE, "the rep cannot even SEE what is on file")

	def test_the_save_refuses_to_overwrite_what_is_already_on_file(self):
		"""The half that actually protects the record: paint is not enforcement."""
		lead = self._lead(**{LEAD_FIELD: ALREADY_ON_FILE})
		with self.assertRaises(frappe.ValidationError):
			activity_api.save_activity(lead.name, self.task_type, {LEAD_FIELD: REP_TYPED})
		self.assertEqual(frappe.db.get_value("CRM Lead", lead.name, LEAD_FIELD), ALREADY_ON_FILE,
						 "an activity overwrote a value already on the patient record")

	# ---- the pairing: paint and enforcement are the SAME answer ---------------------------------------

	def test_the_form_never_opens_a_box_the_save_would_refuse(self):
		"""Across both states, `read_only` and the writer's refusal must agree — one predicate, asked twice.
		If these could disagree the rep would either be blocked on a box that looked open, or shown a locked
		box they were in fact allowed to fill."""
		for on_file, expect_open in ((None, True), (ALREADY_ON_FILE, False)):
			with self.subTest(on_file=on_file):
				lead = self._lead(**({LEAD_FIELD: on_file} if on_file else {}))
				painted_open = self._descriptor(lead, LEAD_FIELD)["read_only"] == 0
				self.assertEqual(painted_open, expect_open, "the form painted the wrong state")

				refused = False
				try:
					activity_api.save_activity(lead.name, self.task_type, {LEAD_FIELD: REP_TYPED})
				except frappe.ValidationError:
					refused = True
				self.assertEqual(painted_open, not refused,
								 "the form and the save disagree about this field — two brains")

	def test_an_activity_field_is_always_writable_however_often_it_is_answered(self):
		"""Fill-once is a rule about the PATIENT RECORD. An activity's own answers are the activity's."""
		lead = self._lead()
		first = activity_api.save_activity(lead.name, self.task_type, {ACTIVITY_FIELD: "ZZ first"})
		second = activity_api.save_activity(lead.name, self.task_type, {ACTIVITY_FIELD: "ZZ second"})
		self.assertNotEqual(first, second, "the probe did not create two tasks")
		values = activity_api.task_detail(second)["task"]["values"]
		self.assertEqual(values.get(ACTIVITY_FIELD), "ZZ second",
						 "an activity field was treated as fill-once")

		descriptor = self._descriptor(lead, ACTIVITY_FIELD)
		self.assertEqual(descriptor["read_only"], 0, "an activity field was painted read-only")
