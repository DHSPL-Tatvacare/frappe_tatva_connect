# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The trusted posture reaches the WHOLE activity read path, not the two functions someone remembered.

A partner is a role-less user by design: it holds `Partner API User`, which grants nothing, and it is
authorized instead by its enabled `CRM Lead API Mapping` and that mapping's grain. So every partner write
runs inside `_base.trusted_permissions()`, which sets the ONE server-set posture flag, and the shared
engine brains are supposed to honour it.

They did not honour it uniformly. `save_activity` asked the posture; `lead_field_values` — reached from
`compute_activity` the moment a task type declares a `source = Lead` field — did not, and neither did the
lead detail projection it reads through. So a partner creating an activity of such a type met a BARE
`frappe.PermissionError` with an empty message, which the API classifies as a 403 whose body reads "The
request was refused and no reason was recorded." Two of twelve live Anaya types were unusable and the
refusal named nothing.

The defect is not the missing line. It is that "does this caller have to prove permission?" was answered
at nine separate call sites, six of which had never been asked the question — so whichever one the next
403 landed on got the guard, and the next helper anybody adds starts wrong again. The rule now lives once,
in `access.posture`, and `tests/architecture/test_permission_checks_one_seam.py` fails if a bare check
grows back.

What this module proves, in the two directions that matter:

  * the PARTNER path completes — an activity whose type asks a lead field is created, and the lead's own
    value is really snapshotted onto it (an outcome, not a call count);
  * the DESK path is unchanged — a user who may not read the lead is still refused, by the same seam, on
    every entry point that used to refuse them. The bypass is reachable ONLY through the server-set flag.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.activity.test_trusted_posture_one_seam
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access import posture
from tatva_connect.activity import api as activity_api
from tatva_connect.api import _base, partner_activity
from tatva_connect.lead import detail as lead_detail
from tatva_connect.tests.activity import task_type_fixture
from tatva_connect.tests.api import partner_fixture
from tatva_connect.tests.automation import field_allowlist

# A plain, writable, native CRM Lead column — not identity, not routing, not read-only. Asserted as a
# premise below, exactly as tests/activity/test_lead_fields_in_form.py asserts it.
LEAD_FIELD = "job_title"
ACTIVITY_FIELD = "zz_posture_note"
ON_THE_LEAD = "ZZ Snapshot Of The Lead"

PARTNER = "zz-posture-partner@example.invalid"
STRANGER = "zz-posture-stranger@example.invalid"


class TestTrustedPostureOneSeam(FrappeTestCase):
	"""One activity type declaring a lead-sourced field, on a grain one minted partner is contracted to."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		# The partner and the task type must sit on the SAME grain, or resolve_lead refuses before the
		# posture is ever asked and this suite would go green on the wrong gate.
		partner_fixture.mint_partner(PARTNER)
		cls.task_type = task_type_fixture.mint_type(
			"ZZ Posture Lead Field",
			[
				{"label": "ZZ Lead Job Title", "fieldname": LEAD_FIELD, "fieldtype": "Data", "source": "Lead"},
				{"label": "ZZ Posture Note", "fieldname": ACTIVITY_FIELD, "fieldtype": "Data"},
			],
			vertical=partner_fixture.VERTICAL, group=partner_fixture.GROUP,
		)
		# The prefill reads through the lead detail brain, so the field has to BE on the catalog and
		# entitled at this grain — the same contract tick the partner's own entitlement resolves through.
		field_allowlist.seed_settable("CRM Lead", LEAD_FIELD,
									  vertical=partner_fixture.VERTICAL, group=partner_fixture.GROUP)
		cls._stranger()
		frappe.db.commit()  # class-level seed: the per-test rollback must not eat it

	@classmethod
	def _stranger(cls):
		"""A real logged-in Desk user holding NO CRM role — the unauthorised reader the security leg needs.

		Not `Guest` and not a mock: the anti-regression has to prove the permission engine still refuses a
		principal that can actually reach a whitelisted method, which is what a role-less System User is."""
		if not frappe.db.exists("User", STRANGER):
			frappe.get_doc({
				"doctype": "User", "email": STRANGER, "first_name": "ZZ Posture Stranger", "enabled": 1,
				"send_welcome_email": 0, "user_type": "System User",
			}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input

	@classmethod
	def tearDownClass(cls):
		"""Nothing this suite minted may outlive it — a contract or a partner left behind widens another
		suite's entitlement silently."""
		frappe.set_user("Administrator")
		for lead in frappe.get_all("CRM Lead", filters={"custom_vertical": partner_fixture.VERTICAL},
								   pluck="name"):
			frappe.delete_doc("CRM Lead", lead, force=True, ignore_permissions=True)
		if frappe.db.exists("User", STRANGER):
			frappe.delete_doc("User", STRANGER, force=True, ignore_permissions=True)
		field_allowlist.clear()
		task_type_fixture.teardown()
		partner_fixture.teardown()
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.set_user("Administrator")
		self.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Posture Probe",
			"mobile_no": f"+9198127{int(frappe.generate_hash(length=8), 16) % 100000:05d}",
			"custom_vertical": partner_fixture.VERTICAL, "custom_group": partner_fixture.GROUP,
			LEAD_FIELD: ON_THE_LEAD,
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input

	# -- premises ---------------------------------------------------------------------------------

	def test_the_lead_carries_a_plain_column_for_this_test_to_snapshot(self):
		"""Without it every assertion below would be about a field that does not exist, and would pass."""
		df = frappe.get_meta("CRM Lead").get_field(LEAD_FIELD)
		self.assertIsNotNone(df, f"CRM Lead no longer has a `{LEAD_FIELD}` column — pick another plain field")
		self.assertFalse(df.read_only, f"`{LEAD_FIELD}` became read-only; this test would prove nothing")

	def test_the_partner_really_is_role_less(self):
		"""The premise the whole posture exists for. If the partner ever gained a role granting CRM Lead
		read, the partner leg below would pass without the seam and prove nothing."""
		frappe.set_user(PARTNER)
		self.assertFalse(
			frappe.has_permission("CRM Lead", "read", doc=self.lead.name),
			"the fixture partner can read leads natively — it is no longer the role-less caller under test",
		)

	# -- the partner path -------------------------------------------------------------------------

	def _create_as_partner(self):
		"""One activity created down the REAL partner path: the caller gate, the grain-scoped lead
		resolution and `trusted_permissions()` all run exactly as `activity_create` runs them."""
		frappe.set_user(PARTNER)
		_user, mp, is_sysmgr = _base._resolve_caller()
		return partner_activity._create_one(
			{"lead": self.lead.name, "task_type": self.task_type, "values": {ACTIVITY_FIELD: "punched"}},
			mp, is_sysmgr,
		)

	def test_a_partner_can_create_an_activity_whose_type_asks_a_lead_field(self):
		"""The measured defect: a bare PermissionError with an empty message, surfaced as a 403 reading
		"The request was refused and no reason was recorded." Every type WITHOUT a lead-sourced field
		worked, which is why it looked like a per-type problem and was not."""
		payload = self._create_as_partner()
		self.assertTrue(payload.get("name"), "the partner path returned no activity id")
		self.assertTrue(frappe.db.exists("CRM Task", payload["name"]),
						"the partner path claimed an activity that is not in the table")

	def test_the_snapshot_is_the_leads_own_value_and_not_the_callers(self):
		"""A `source = Lead` field is CONTEXT: the submitted value is ignored and the lead's own value is
		read on the server. Asserting the value — not that a function was called — is what proves the
		prefill really ran under the trusted posture instead of silently answering blank."""
		frappe.set_user(PARTNER)
		_user, mp, is_sysmgr = _base._resolve_caller()
		payload = partner_activity._create_one(
			{"lead": self.lead.name, "task_type": self.task_type,
			 "values": {ACTIVITY_FIELD: "punched", LEAD_FIELD: "forged by the caller"}},
			mp, is_sysmgr,
		)
		self.assertEqual(payload["values"].get(LEAD_FIELD), ON_THE_LEAD,
						 "the activity did not snapshot the lead's own value")

	# -- the desk path, unchanged -----------------------------------------------------------------

	def test_an_unauthorised_desk_user_is_still_refused_the_prefill(self):
		"""The anti-regression. `lead_field_values` bypasses ONLY on the server-set flag; a real logged-in
		user who may not read the lead is refused exactly as before."""
		frappe.set_user(STRANGER)
		self.assertFalse(posture.is_trusted(), "an ordinary Desk request is not a trusted posture")
		with self.assertRaises(frappe.PermissionError):
			activity_api.lead_field_values(self.lead.name, self.task_type)

	def test_an_unauthorised_desk_user_is_still_refused_the_lead_detail_projection(self):
		"""The second wall the partner met, and the one a fix stopping at the activity module would have
		left standing. It must still refuse a Desk stranger."""
		frappe.set_user(STRANGER)
		with self.assertRaises(frappe.PermissionError):
			lead_detail.lead_detail(self.lead.name)

	def test_an_unauthorised_desk_user_is_still_refused_the_writer(self):
		"""The write leg. `save_activity` already asked the posture; it must keep refusing without it."""
		frappe.set_user(STRANGER)
		with self.assertRaises(frappe.PermissionError):
			activity_api.save_activity(self.lead.name, self.task_type, {ACTIVITY_FIELD: "punched"})

	def test_every_activity_read_entry_point_refuses_the_stranger(self):
		"""The sweep. Each whitelisted read the module publishes must refuse an unauthorised principal —
		routing them all through one seam may not quietly open one of them."""
		frappe.set_user(STRANGER)
		for label, call in (
			("open_activity_tasks", lambda: activity_api.open_activity_tasks(self.lead.name)),
			("list_types_for_lead", lambda: activity_api.list_types_for_lead(self.lead.name)),
			("lead_timeline", lambda: activity_api.lead_timeline(self.lead.name)),
			("get_schema", lambda: activity_api.get_schema(self.task_type)),
			("type_config", lambda: activity_api.type_config(self.task_type, lead=self.lead.name)),
			("compute_activity_fields", lambda: activity_api.compute_activity_fields(
				self.lead.name, self.task_type, {})),
		):
			with self.subTest(entry_point=label):
				with self.assertRaises(frappe.PermissionError):
					call()

	# -- the posture itself -----------------------------------------------------------------------

	def test_the_setter_and_the_reader_agree(self):
		"""`_base.trusted_permissions()` SETS the posture and `access.posture` READS it. They live in two
		modules, so a drift lock keeps them naming the same flag — otherwise the bypass silently stops
		applying and every partner activity 403s again with no reason recorded."""
		frappe.set_user(PARTNER)
		self.assertFalse(posture.is_trusted(), "the posture leaked in from outside a partner block")
		with _base.trusted_permissions():
			self.assertTrue(posture.is_trusted(),
							"trusted_permissions() no longer sets the flag access.posture reads")
		self.assertFalse(posture.is_trusted(), "the posture outlived the block that set it")
