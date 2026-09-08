# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Adding a colleague is management, not administration — and it stops there.

`native_guards._require_user_admin` is the second spelling beside `_require_platform`: a manager may
open the door their own app owns, and nothing wider. This module is the wall on both halves.

WHAT IS ASSERTED

  * each door names its OWN manager — a Sales Manager opens crm's invite, an Agent Manager opens
    helpdesk's Add Agent, and neither opens the other's. The gate takes a role list per call site, so
    a shared "any manager" pass is exactly the drift this catches;
  * a rep and an agent are refused, in OUR words, so the button says what is actually wrong;
  * a System Manager still passes both, because `is_privileged` is the first half of the gate;
  * the platform doors did NOT soften with them — `delete_member` still refuses a manager. That is the
    line between administration and management, and it is the one a later edit is likeliest to blur;
  * THE ESCALATION LOCK: the account `sent_invites` creates on a manager's behalf carries no role the
    manager did not already hold. Granting `User` create in the ledger was the obvious alternative and
    is unsafe — Frappe does not gate the `roles` child table on insert, so a manager could have
    attached `System Manager` to a new account. `test_a_manager_cannot_mint_a_system_manager` PLANTS
    that evasion against the live engine and fails the build if Frappe ever starts allowing it
    silently; `test_the_account_a_manager_creates_holds_no_role` proves our door does not;
  * the row grants that ride with the tier — `User Permission` and `CRM Sales Hierarchy` — are read off
    `ledger.rows_for`, the one declaration, so the intent behind the tuples is stated somewhere a later
    edit has to argue with. Whether the runtime MATCHES that declaration is already `lockdown`'s own
    migrate-time gate, and is not restated here.

Every call goes through `dispatch`, which resolves `override_whitelisted_methods` the way
`frappe.handler.execute_cmd` does — a direct import would prove only that a helper exists.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.access.test_user_admin_tier
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access import ledger
from tatva_connect.tests.authz.base import dispatch, mk_user, set_user

TAG = "user-admin-tier"

# What our gate says when it refuses. The test asserts on OUR refusal, never on a native one, so a
# native rule that changes underneath us cannot turn this suite green or red by accident.
OURS = "may add users"

CRM_DOOR = "crm.api.invite_by_email"
HELPDESK_DOOR = "helpdesk.api.agent.sent_invites"
PLATFORM_DOOR = "frappe.contacts.doctype.contact.contact.invite_user"


class TestUserAdminTier(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.sales_manager = mk_user(f"{TAG}-salesmgr@example.test", ["Sales Manager"])
		cls.sales_user = mk_user(f"{TAG}-rep@example.test", ["Sales User"])
		cls.agent_manager = mk_user(f"{TAG}-agentmgr@example.test", ["Agent Manager"])
		cls.agent = mk_user(f"{TAG}-agent@example.test", ["Agent"])
		cls.admin = mk_user(f"{TAG}-admin@example.test", ["System Manager"])
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for email in (cls.sales_manager, cls.sales_user, cls.agent_manager, cls.agent, cls.admin):
			frappe.delete_doc("User", email, force=True, ignore_permissions=True, delete_permanently=True)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")

	# ---- helpers -----------------------------------------------------------------------------------

	def _refused_by_us(self, user, door, **kwargs):
		"""True when OUR gate refused. A native refusal (crm's role cap, helpdesk's own check) counts as
		admitted — the caller got past us, which is the only thing this module owns."""
		with set_user(user):
			try:
				dispatch(door, **kwargs)
			except frappe.PermissionError as e:
				return OURS in str(e)
			except Exception:
				return False  # native decided; we admitted
		return False

	def _invite(self, email):
		return {"emails": email, "role": "Sales User"}

	# ---- each door names its own manager -----------------------------------------------------------

	def test_a_sales_manager_opens_the_crm_door(self):
		self.assertFalse(self._refused_by_us(self.sales_manager, CRM_DOOR, **self._invite(f"{TAG}-a@example.test")))

	def test_an_agent_manager_opens_the_helpdesk_door(self):
		self.assertFalse(self._refused_by_us(self.agent_manager, HELPDESK_DOOR, emails=[f"{TAG}-b@example.test"],
		                                     send_welcome_mail_to_user=False))

	def test_a_sales_manager_does_not_open_the_helpdesk_door(self):
		self.assertTrue(self._refused_by_us(self.sales_manager, HELPDESK_DOOR, emails=[f"{TAG}-c@example.test"],
		                                    send_welcome_mail_to_user=False))

	def test_an_agent_manager_does_not_open_the_crm_door(self):
		self.assertTrue(self._refused_by_us(self.agent_manager, CRM_DOOR, **self._invite(f"{TAG}-d@example.test")))

	# ---- the floor ---------------------------------------------------------------------------------

	def test_a_rep_is_refused_the_crm_door(self):
		self.assertTrue(self._refused_by_us(self.sales_user, CRM_DOOR, **self._invite(f"{TAG}-e@example.test")))

	def test_an_agent_is_refused_the_helpdesk_door(self):
		self.assertTrue(self._refused_by_us(self.agent, HELPDESK_DOOR, emails=[f"{TAG}-f@example.test"],
		                                    send_welcome_mail_to_user=False))

	def test_a_system_manager_opens_both_doors(self):
		self.assertFalse(self._refused_by_us(self.admin, CRM_DOOR, **self._invite(f"{TAG}-g@example.test")))
		self.assertFalse(self._refused_by_us(self.admin, HELPDESK_DOOR, emails=[f"{TAG}-h@example.test"],
		                                     send_welcome_mail_to_user=False))

	# ---- the platform doors did not soften with them -----------------------------------------------

	def test_a_manager_still_does_not_administer_accounts(self):
		"""`_require_platform` is the other spelling and it did not move. Deleting an account and minting
		one off a contact stay with an administrator, whatever a manager may now invite."""
		for user in (self.sales_manager, self.agent_manager):
			with set_user(user), self.assertRaises(frappe.PermissionError) as caught:
				dispatch("lms.lms.api.delete_member", user=self.sales_user)
			self.assertIn("System Manager", str(caught.exception))

			with set_user(user), self.assertRaises(frappe.PermissionError) as caught:
				dispatch(PLATFORM_DOOR, contact="nobody")
			self.assertIn("System Manager", str(caught.exception))

	# ---- the escalation lock -----------------------------------------------------------------------

	def test_the_account_a_manager_creates_holds_no_role(self):
		"""The whole reason the account is made in the guard rather than by a ledger grant: nothing here
		reads a role from the caller, so the new account starts with none of its own."""
		email = f"{TAG}-fresh@example.test"
		with set_user(self.agent_manager):
			dispatch(HELPDESK_DOOR, emails=[email], send_welcome_mail_to_user=False)
		self.assertTrue(frappe.db.exists("User", email), "the manager's own door did not create the account")
		granted = frappe.get_all("Has Role", filters={"parent": email}, pluck="role")
		self.assertNotIn("System Manager", granted)
		self.assertNotIn("Agent Manager", granted)

	def test_a_manager_cannot_mint_a_system_manager(self):
		"""THE PLANTED EVASION — what a `User` create grant in the ledger would have opened.

		Frappe does not gate the `roles` child table on insert, so an account carrying `System Manager`
		inserts cleanly once the doctype grant exists. This asserts the grant does NOT exist: the insert
		must be refused at the `User` matrix, as a manager. If this ever goes red, someone gave a manager
		create on `User` and the child table is the way out."""
		for user in (self.sales_manager, self.agent_manager):
			with set_user(user), self.assertRaises(frappe.PermissionError):
				frappe.get_doc({
					"doctype": "User", "email": f"{TAG}-esc@example.test", "first_name": "Escalation",
					"send_welcome_email": 0, "roles": [{"role": "System Manager"}],
				}).insert()


class TestManagerRowGrants(FrappeTestCase):
	"""The other half of staffing a team: scoping the person you just brought in, and placing them.

	A manager who may invite but may not set the invitee's grain has handed the job back to an
	administrator halfway through, which is the state this replaced."""

	def test_a_sales_manager_scopes_the_people_they_bring_in(self):
		rows = ledger.rows_for("User Permission")
		self.assertEqual((1, 1, 1, 1), rows[ledger.SALES_MANAGER])

	def test_a_sales_manager_places_their_own_org_chart_but_does_not_erase_it(self):
		"""Write and create so a manager re-parents their line; delete stays with an administrator,
		because a removed hierarchy row silently widens everyone above it."""
		rows = ledger.rows_for("CRM Sales Hierarchy")
		self.assertEqual((1, 1, 1, 0), rows[ledger.SALES_MANAGER])

	def test_a_rep_writes_neither(self):
		"""A rep reads the org chart — that is the platform-read bucket and predates this. What the
		manager grants added, and a rep must not have, is everything past the first column."""
		for doctype in ("User Permission", "CRM Sales Hierarchy"):
			rows = ledger.rows_for(doctype)
			self.assertEqual((0, 0, 0), rows.get(ledger.SALES_USER, (0, 0, 0, 0))[1:],
			                 f"{doctype} is writable by a rep")

	def test_user_stays_read_only_for_every_manager(self):
		"""The declaration side of the escalation lock: no manager holds create on `User`, so the
		`roles` child table is never reachable on insert."""
		rows = ledger.rows_for("User")
		for role in (ledger.SALES_MANAGER, ledger.AGENT_MANAGER, ledger.SALES_USER, ledger.AGENT):
			self.assertEqual((1, 0, 0, 0), rows[role], f"{role} may write User")
