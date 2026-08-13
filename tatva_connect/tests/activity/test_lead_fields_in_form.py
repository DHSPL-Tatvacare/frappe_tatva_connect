# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A lead field asked inside an activity form — §4 of
docs/plans/task-form-layer/2026-07-29-generic-activity-storage.md.

A `CRM Task Type Field` declaring `source = Lead` is prefilled from the lead, routed onto the task like any
other answer, and — when the rep changes it — written back to the lead through `lead.detail.write_lead_fields`.

Both earlier answers are recorded because this file has held each. Until 2026-07-29 the value went onto the
LEAD only, which left "at this punch the address was X" unanswerable; §4.2 moved it onto the task and painted
the field read-only, which left the lead stale because a rep logs a task and does not re-type the same facts
into the Data tab. What ships now keeps §4.2's routing (so history still answers) and adds the lead leg.

Where the value LANDS is asserted through the entry point a rep uses: `tests/activity/test_dual_write.py`.

Where the value LANDS is the router's business and is asserted where every other routed shape is, through
the entry point a rep uses: `tests/activity/test_dual_write.py`.

Run:
    bench --site <site> run-tests --app tatva_connect \\
        --module tatva_connect.tests.activity.test_lead_fields_in_form
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.activity import api as activity_api
from tatva_connect.api._base import trusted_permissions
from tatva_connect.lead import detail as lead_detail
from tatva_connect.tests.activity import task_type_fixture
from tatva_connect.tests.automation import field_allowlist

# A plain, writable, native CRM Lead column — not identity, not routing, not read-only. Asserted as a premise below.
LEAD_FIELD = "job_title"

ACTIVITY_FIELD = "zz_lf_note"
PREFILLED = "ZZ already on the lead"
CHANGED = "ZZ what the rep learnt on the call"


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

	def test_a_lead_field_opens_editable_whatever_the_lead_holds(self):
		"""A rep who learns the new value on the call has to be able to type it. Nothing about the lead's
		state changes that, so there is no state in which the box is locked."""
		for on_file in (None, PREFILLED):
			with self.subTest(on_file=on_file):
				frappe.db.set_value("CRM Lead", self.lead.name, LEAD_FIELD, on_file)
				fields = {f["fieldname"]: f
						  for f in activity_api.type_config(self.task_type, lead=self.lead.name)["fields"]}
				self.assertEqual(fields[LEAD_FIELD]["read_only"], 0, "a lead field opened locked")
				self.assertEqual(fields[ACTIVITY_FIELD]["read_only"], 0,
								 "an ordinary activity field was painted read-only")

	# ---- the write-back -------------------------------------------------------------------------------

	def test_an_answer_the_rep_changes_goes_back_to_the_lead(self):
		"""The point of the leg: one punch logs the task and updates the lead."""
		frappe.db.set_value("CRM Lead", self.lead.name, LEAD_FIELD, PREFILLED)
		activity_api.compute_activity(self.lead.name, self.task_type,
									  {LEAD_FIELD: CHANGED, ACTIVITY_FIELD: "note"})
		self.assertEqual(frappe.db.get_value("CRM Lead", self.lead.name, LEAD_FIELD), CHANGED,
						 "the rep's answer never reached the lead")

	def test_the_task_takes_the_answer_the_rep_gave(self):
		"""§4.2's snapshot survives the new leg — the task stores the punch's value, not a live lead read."""
		frappe.db.set_value("CRM Lead", self.lead.name, LEAD_FIELD, PREFILLED)
		fields = activity_api.compute_activity(self.lead.name, self.task_type,
											   {LEAD_FIELD: CHANGED, ACTIVITY_FIELD: "note"})
		self.assertIn(CHANGED, frappe.as_json(fields), "the task kept no copy of the answer")

	def test_a_field_the_rep_leaves_alone_writes_nothing(self):
		"""An untouched form is not an edit, so every existing punch writes exactly what it wrote before."""
		frappe.db.set_value("CRM Lead", self.lead.name, LEAD_FIELD, PREFILLED)
		before = frappe.db.get_value("CRM Lead", self.lead.name, "modified")
		activity_api.compute_activity(self.lead.name, self.task_type, {ACTIVITY_FIELD: "note"})
		self.assertEqual(frappe.db.get_value("CRM Lead", self.lead.name, LEAD_FIELD), PREFILLED,
						 "an untouched lead field was overwritten")
		self.assertEqual(frappe.db.get_value("CRM Lead", self.lead.name, "modified"), before,
						 "the lead was saved for a punch that changed nothing on it")

	def test_a_trusted_caller_never_moves_the_lead(self):
		"""The migration replays 856k historic punches through this same brain, and the partner API holds its
		own lead endpoint. Either one writing here would rewrite the patient record from history."""
		frappe.db.set_value("CRM Lead", self.lead.name, LEAD_FIELD, PREFILLED)
		with trusted_permissions():
			activity_api.compute_activity(self.lead.name, self.task_type,
										  {LEAD_FIELD: CHANGED, ACTIVITY_FIELD: "note"})
		self.assertEqual(frappe.db.get_value("CRM Lead", self.lead.name, LEAD_FIELD), PREFILLED,
						 "a replayed activity rewrote the lead")

	def test_a_read_only_lead_field_is_shown_but_never_written(self):
		"""The declaration is the switch: `read_only` shows the answer for reference and stops the write leg."""
		frappe.db.set_value("CRM Task Type Field", {"parent": self.task_type, "fieldname": LEAD_FIELD},
							"read_only", 1)
		frappe.db.set_value("CRM Lead", self.lead.name, LEAD_FIELD, PREFILLED)
		fields = {f["fieldname"]: f
				  for f in activity_api.type_config(self.task_type, lead=self.lead.name)["fields"]}
		self.assertEqual(fields[LEAD_FIELD]["read_only"], 1, "the declaration was ignored")
		activity_api.compute_activity(self.lead.name, self.task_type,
									  {LEAD_FIELD: CHANGED, ACTIVITY_FIELD: "note"})
		self.assertEqual(frappe.db.get_value("CRM Lead", self.lead.name, LEAD_FIELD), PREFILLED,
						 "a read-only lead field still wrote to the lead")

	def test_a_lead_field_the_catalog_does_not_make_writable_is_refused(self):
		"""The Data tab's gate is the only gate — a form cannot write what `update_lead_detail` would refuse."""
		with self.assertRaises(frappe.ValidationError):
			lead_detail.write_lead_fields(self.lead.name, {"lead_name": "Forged"})

	def test_the_descriptor_tells_the_form_which_fields_are_the_leads(self):
		"""The client renders and submits from this one list, so `source` has to be on it — otherwise the
		modal would need a second call to learn which fields to prefill."""
		sources = {f.fieldname: f.source
				   for f in activity_api.compiled_fields(frappe.get_doc("CRM Task Type", self.task_type))}
		self.assertEqual(sources.get(LEAD_FIELD), activity_api.LEAD_SOURCE)
		self.assertNotEqual(sources.get(ACTIVITY_FIELD), activity_api.LEAD_SOURCE)
