# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Phase 10 of docs/plans/task-form-layer/2026-07-25-task-slots-to-sections-and-form-layer.md — a lead field
asked inside an activity form.

LSQ mixes lead-sourced fields into an activity section (§16.1): the section is layout, the icon is source. A
`CRM Task Type Field` declaring `source = Lead` is therefore read from and written back to the LEAD, and the
task never carries a copy of it (D11). D31 settles the path and names every seam, and this module holds it to
each one:

  * the WRITE goes through the automation set path — `automation.fields.is_settable` at the lead's own grain
    (the same allowlist gate the Set Field action is refused by) and then one load-set-save so the lead's
    field permissions, `validate` and every `doc_event` re-fire. Never a third writer;
  * the PERMISSION is checked on the SERVER: a caller without `write` on the lead is refused, and so is a
    field the lead's grain contract does not tick. The modal is paint and decides neither;
  * the TASK KEEPS NO COPY: no promoted column, no section row, and nothing in `_task_values`;
  * the PREFILL is read through the lead detail brain, and rides the `type_config` answer the form already
    fetches — so a form with lead fields still loads in ONE call.

EXPECTED_RED_TODAY (measured against the pre-batch code):

  * `test_a_permitted_lead_field_lands_on_the_lead` FAILS — old code has no lead write at all, so the lead's
    column is unchanged after the save;
  * `test_the_task_carries_no_copy_of_a_lead_field` FAILS — old `compute_activity` routes EVERY declared
    field through `field_target`, so the lead answer is written into the answers section as a copy;
  * `test_a_field_the_grain_contract_does_not_tick_is_refused` FAILS — old code stores it silently instead of
    refusing;
  * `test_a_caller_without_write_on_the_lead_is_refused` and `test_the_form_opens_prefilled_from_the_lead`
    and `test_the_prefill_rides_the_type_config_answer` raise `AttributeError` — `write_lead_fields` and
    `lead_field_values` do not exist, and `type_config` takes no `lead`.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.activity.test_lead_fields_in_form
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.activity import api as activity_api
from tatva_connect.tests.activity import task_type_fixture
from tatva_connect.tests.automation import field_allowlist

# A plain, writable, native CRM Lead column — not identity, not routing, not read-only, so writing it proves
# the path without touching anything the lead is keyed or scoped by. Asserted as a premise below.
LEAD_FIELD = "job_title"
# Declared on the type but never ticked by any contract: the allowlist half of the gate, in isolation.
UNTICKED_LEAD_FIELD = "website"

ACTIVITY_FIELD = "zz_lf_note"
TYPED = "ZZ typed on the form"
PREFILLED = "ZZ already on the lead"


class TestLeadFieldsInForm(FrappeTestCase):
	"""One type declaring a ticked lead field, an unticked one, and an ordinary activity field beside them."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.task_type = task_type_fixture.mint_type("ZZ Lead Fields Probe", [
			{"label": "ZZ Lead Job Title", "fieldname": LEAD_FIELD, "fieldtype": "Data", "source": "Lead"},
			{"label": "ZZ Lead Website", "fieldname": UNTICKED_LEAD_FIELD, "fieldtype": "Data",
			 "source": "Lead"},
			{"label": "ZZ Activity Note", "fieldname": ACTIVITY_FIELD, "fieldtype": "Data"},
		])
		# The write gate: a can_set catalog row for the field PLUS the internal contract of the fixture grain
		# ticking its key. Both are what `is_settable` reads, and the seed helper is the shared one the
		# automation tests already use — no second way to grant a write.
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
			"doctype": "CRM Lead", "first_name": "Lead Fields Probe",
			"mobile_no": f"+9198128{int(frappe.generate_hash(length=8), 16) % 100000:05d}",
			"custom_vertical": task_type_fixture.VERTICAL, "custom_group": task_type_fixture.GROUP,
		}).insert(ignore_permissions=True)

	def _reread(self, fieldname=LEAD_FIELD):
		"""The lead's value straight off the database — never the doc this test holds in memory."""
		return frappe.db.get_value("CRM Lead", self.lead.name, fieldname)

	# ---- the premise ---------------------------------------------------------------------------------

	def test_the_lead_carries_a_plain_writable_column_for_this_test_to_write(self):
		"""Without it every assertion below would be about a field that does not exist, and would pass."""
		df = frappe.get_meta("CRM Lead").get_field(LEAD_FIELD)
		self.assertIsNotNone(df, f"CRM Lead no longer has a `{LEAD_FIELD}` column — pick another plain field")
		self.assertFalse(df.read_only, f"`{LEAD_FIELD}` became read-only; this test would prove nothing")

	def test_the_gate_the_write_asks_agrees_the_field_is_settable_here(self):
		"""The seed's own premise: the ticked field passes `is_settable` at this lead's grain and the unticked
		one does not. If both passed, the refusal test below would be vacuous."""
		from tatva_connect.automation import fields as automation_fields

		axes = (task_type_fixture.VERTICAL, task_type_fixture.GROUP, "")
		self.assertTrue(automation_fields.is_settable("CRM Lead", LEAD_FIELD, axes),
						"the seeded contract tick did not make the field settable")
		self.assertFalse(automation_fields.is_settable("CRM Lead", UNTICKED_LEAD_FIELD, axes),
						 f"`{UNTICKED_LEAD_FIELD}` is settable on this site — pick a field no contract ticks")

	# ---- the write ------------------------------------------------------------------------------------

	def test_a_permitted_lead_field_lands_on_the_lead(self):
		"""D31, end to end: the answer typed into the activity form is on the LEAD after the save."""
		activity_api.save_activity(self.lead.name, self.task_type,
								   {LEAD_FIELD: TYPED, ACTIVITY_FIELD: "note"})
		self.assertEqual(self._reread(), TYPED, "the lead-sourced answer never reached the lead")

	def test_the_task_carries_no_copy_of_a_lead_field(self):
		"""D11. A copy is a second brain: the lead's own panel would edit one value and the activity another,
		and the two would drift the first time either changed."""
		name = activity_api.save_activity(self.lead.name, self.task_type,
										 {LEAD_FIELD: TYPED, ACTIVITY_FIELD: "note"})
		task = frappe.get_doc("CRM Task", name)
		values = activity_api._task_values(task, activity_api._type_config(self.task_type))
		self.assertNotIn(LEAD_FIELD, values, "the task reported a value for a field it must not hold")
		self.assertEqual(values.get(ACTIVITY_FIELD), "note",
						 "the ordinary activity field beside it stopped landing")
		# And nothing was written into a section row addressed by that fieldname, which is where a copy would be.
		for section in activity_api._sections():
			if not section.is_key_value:
				continue
			self.assertFalse(
				frappe.db.exists(section.target_doctype,
								 {"parent": str(name), "parenttype": "CRM Task",
								  section.row_key_field: LEAD_FIELD}),
				"a section row was written for a lead-sourced field")

	def test_an_unanswered_lead_field_does_not_touch_the_lead(self):
		"""A blank is "not sent", never "erase this" — the rule the partner API already carries. Merely SHOWING
		a lead field must not let a save blank the patient record, and the unticked field beside it must not
		refuse a save that never tried to write it."""
		frappe.db.set_value("CRM Lead", self.lead.name, LEAD_FIELD, PREFILLED)
		name = activity_api.save_activity(self.lead.name, self.task_type, {ACTIVITY_FIELD: "note"})
		self.assertTrue(frappe.db.exists("CRM Task", name), "an unanswered lead field blocked the save")
		self.assertEqual(self._reread(), PREFILLED, "an unanswered lead field blanked the lead")

	# ---- the refusals, both on the server -------------------------------------------------------------

	def test_a_field_the_grain_contract_does_not_tick_is_refused(self):
		"""The allowlist half of the gate. The field is declared on the form, so a rep can type into it — and
		the SERVER is what refuses it, because a declaration is not an entitlement."""
		with self.assertRaises(frappe.ValidationError) as caught:
			activity_api.save_activity(self.lead.name, self.task_type,
									   {UNTICKED_LEAD_FIELD: "https://example.invalid"})
		self.assertIn(UNTICKED_LEAD_FIELD, str(caught.exception),
					  "the refusal did not name the field it refused")
		self.assertIsNone(self._reread(UNTICKED_LEAD_FIELD), "the refused value was written anyway")

	def test_a_caller_without_write_on_the_lead_is_refused(self):
		"""The permission half, asked of frappe's own `has_permission` and not of a rule written here. The
		modal cannot be trusted to have asked it, so the write asks it itself."""
		fields = activity_api.compiled_fields(frappe.get_doc("CRM Task Type", self.task_type))
		frappe.set_user("Guest")
		self.addCleanup(frappe.set_user, "Administrator")
		with self.assertRaises(frappe.PermissionError):
			activity_api.write_lead_fields(self.lead.name, fields, {LEAD_FIELD: TYPED}, {LEAD_FIELD})

	def test_a_hidden_lead_field_is_not_written(self):
		"""A lead field is a form field like any other: hidden, it was never asked, so nothing is written to
		the lead on its behalf (D22 reaching the lead write)."""
		activity_api.write_lead_fields(self.lead.name,
									   activity_api.compiled_fields(frappe.get_doc("CRM Task Type", self.task_type)),
									   {LEAD_FIELD: TYPED}, set())
		self.assertIsNone(self._reread(), "a field the form never showed was written to the lead")

	# ---- the prefill ----------------------------------------------------------------------------------

	def test_the_form_opens_prefilled_from_the_lead(self):
		"""§16.2: the rep sees the patient's current details, not blanks — read through the lead detail brain,
		so a field the viewer may not see is not in the answer at all."""
		frappe.db.set_value("CRM Lead", self.lead.name, LEAD_FIELD, PREFILLED)
		values = activity_api.lead_field_values(self.lead.name, self.task_type)
		self.assertEqual(values.get(LEAD_FIELD), PREFILLED, "the form would open blank over a filled lead")

	def test_the_prefill_rides_the_type_config_answer(self):
		"""ONE call loads a form, however many lead fields it declares — the values come back on the config
		the form already fetches, and a config asked without a lead carries none."""
		frappe.db.set_value("CRM Lead", self.lead.name, LEAD_FIELD, PREFILLED)
		with_lead = activity_api.type_config(self.task_type, lead=self.lead.name)
		self.assertEqual(with_lead["lead_values"].get(LEAD_FIELD), PREFILLED,
						 "type_config did not carry the lead's values")
		self.assertEqual(activity_api.type_config(self.task_type)["lead_values"], {},
						 "a lead-less config answered with lead values it could not have scoped")

	def test_the_descriptor_tells_the_form_which_fields_are_the_leads(self):
		"""The client renders and submits from this one list, so `source` has to be on it — otherwise the
		modal would need a second call to learn which fields to prefill."""
		sources = {f.fieldname: f.source
				   for f in activity_api.compiled_fields(frappe.get_doc("CRM Task Type", self.task_type))}
		self.assertEqual(sources.get(LEAD_FIELD), activity_api.LEAD_SOURCE)
		self.assertNotEqual(sources.get(ACTIVITY_FIELD), activity_api.LEAD_SOURCE)
