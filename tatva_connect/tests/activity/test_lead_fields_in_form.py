# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A lead field asked inside an activity form — §4 of
docs/plans/task-form-layer/2026-07-29-generic-activity-storage.md.

LSQ mixes lead-sourced fields into an activity section: the section is layout, the source is whose record
the value belongs to. A `CRM Task Type Field` declaring `source = Lead` is the CONTEXT the activity was
logged in — shown so the rep can see the patient's details, snapshotted onto the activity as it stood that
day, and never written back to the lead.

D11 IS REVERSED HERE, DELIBERATELY. Until 2026-07-29 a lead-sourced answer was written onto the LEAD and
the task kept no copy, under a fill-once rule. That made *"at this order punch the address was X"*
unanswerable: a live read shows today's address against a two-year-old order, which is a different and
false claim. It also made migrated and live activities two different shapes — LSQ stores the value on the
activity — so the app needed two code paths for one thing. §4.2 of the plan settles it the other way, and
`write_lead_fields` / `lead_field_is_open` / `test_lead_fields_fill_once` are gone with it.

What this module holds:

  * the PREFILL still rides the `type_config` answer the form already fetches, so a form with lead fields
    loads in ONE call, and it is read through the lead detail brain — a field this viewer is not entitled
    to see is not in the answer at all;
  * the descriptor carries `source`, which is how the client knows which fields to prefill;
  * the field is painted READ-ONLY, always.

Where the value LANDS is the router's business and is asserted where every other routed shape is, through
the entry point a rep uses: `tests/activity/test_dual_write.py`.

Run:
    bench --site <site> run-tests --app tatva_connect \\
        --module tatva_connect.tests.activity.test_lead_fields_in_form
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.activity import api as activity_api
from tatva_connect.tests.activity import task_type_fixture
from tatva_connect.tests.automation import field_allowlist

# A plain, writable, native CRM Lead column — not identity, not routing, not read-only. Asserted as a premise below.
LEAD_FIELD = "job_title"

ACTIVITY_FIELD = "zz_lf_note"
PREFILLED = "ZZ already on the lead"


class TestLeadFieldsInForm(FrappeTestCase):
	"""One type declaring a lead field and an ordinary activity field beside it."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.task_type = task_type_fixture.mint_type("ZZ Lead Fields Probe", [
			{"label": "ZZ Lead Job Title", "fieldname": LEAD_FIELD, "fieldtype": "Data", "source": "Lead"},
			{"label": "ZZ Activity Note", "fieldname": ACTIVITY_FIELD, "fieldtype": "Data"},
		])
		# The prefill reads through the lead detail brain, so the field has to BE on the lead's catalog and
		# visible at this grain — which is what this shared helper seeds. Nothing here is about writing.
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
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input

	# ---- the premise ---------------------------------------------------------------------------------

	def test_the_lead_carries_a_plain_column_for_this_test_to_read(self):
		"""Without it every assertion below would be about a field that does not exist, and would pass."""
		df = frappe.get_meta("CRM Lead").get_field(LEAD_FIELD)
		self.assertIsNotNone(df, f"CRM Lead no longer has a `{LEAD_FIELD}` column — pick another plain field")
		self.assertFalse(df.read_only, f"`{LEAD_FIELD}` became read-only; this test would prove nothing")

	# ---- the prefill ----------------------------------------------------------------------------------

	def test_the_form_opens_prefilled_from_the_lead(self):
		"""The rep sees the patient's current details, not blanks — read through the lead detail brain, so a
		field the viewer may not see is not in the answer at all."""
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

	# ---- the paint ------------------------------------------------------------------------------------

	def test_a_lead_field_is_read_only_and_an_activity_field_beside_it_is_not(self):
		"""The form shows context and collects answers. Nothing about the lead's state changes that, so
		there is no state in which the box opens and none in which a rep is refused after typing."""
		for on_file in (None, PREFILLED):
			with self.subTest(on_file=on_file):
				frappe.db.set_value("CRM Lead", self.lead.name, LEAD_FIELD, on_file)
				fields = {f["fieldname"]: f
						  for f in activity_api.type_config(self.task_type, lead=self.lead.name)["fields"]}
				self.assertEqual(fields[LEAD_FIELD]["read_only"], 1, "a lead field opened editable")
				self.assertEqual(fields[ACTIVITY_FIELD]["read_only"], 0,
								 "an ordinary activity field was painted read-only")

	def test_the_descriptor_tells_the_form_which_fields_are_the_leads(self):
		"""The client renders and submits from this one list, so `source` has to be on it — otherwise the
		modal would need a second call to learn which fields to prefill."""
		sources = {f.fieldname: f.source
				   for f in activity_api.compiled_fields(frappe.get_doc("CRM Task Type", self.task_type))}
		self.assertEqual(sources.get(LEAD_FIELD), activity_api.LEAD_SOURCE)
		self.assertNotEqual(sources.get(ACTIVITY_FIELD), activity_api.LEAD_SOURCE)
