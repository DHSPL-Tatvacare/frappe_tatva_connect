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
  * THE ESCALATION LOCK: a manager creates and edits accounts on the User form, and Frappe does not
    gate the `roles` child table, so `user_admin.assert_may_grant` is the only thing standing between
    a manager and `System Manager`. `TestManagerAddsUsers` PLANTS that evasion on insert, on edit and
    through a role profile, and fails the build unless each one is refused;
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



class TestManagerRowGrants(FrappeTestCase):
	"""The other half of staffing a team: scoping the person you just brought in, and placing them.

	A manager who may invite but may not set the invitee's grain has handed the job back to an
	administrator halfway through, which is the state this replaced."""

	def test_a_sales_manager_scopes_the_people_they_bring_in(self):
		rows = ledger.rows_for("User Permission")
		self.assertEqual((1, 1, 1, 1), rows[ledger.SALES_MANAGER])

	def test_a_sales_manager_keeps_their_own_org_chart(self):
		"""Write, create and delete, so a manager re-parents their line and removes people from it."""
		rows = ledger.rows_for("CRM Sales Hierarchy")
		self.assertEqual((1, 1, 1, 1), rows[ledger.SALES_MANAGER])

	def test_a_rep_writes_neither(self):
		"""A rep reads the org chart — that is the platform-read bucket and predates this. What the
		manager grants added, and a rep must not have, is everything past the first column."""
		for doctype in ("User Permission", "CRM Sales Hierarchy"):
			rows = ledger.rows_for(doctype)
			self.assertEqual((0, 0, 0), rows.get(ledger.SALES_USER, (0, 0, 0, 0))[1:],
			                 f"{doctype} is writable by a rep")

	def test_every_app_manager_creates_and_edits_users_but_never_deletes(self):
		"""The declaration side: each app's manager holds read, write and create on `User`; delete stays with a System Manager."""
		rows = ledger.rows_for("User")
		for role in ledger.MANAGER_ROLES:
			self.assertEqual((1, 1, 1, 0), rows[role], f"{role} does not hold the manager row on User")
		for role in (ledger.SALES_USER, ledger.AGENT):
			self.assertEqual((1, 0, 0, 0), rows[role], f"{role} may write User")


class TestManagerAddsUsers(FrappeTestCase):
	"""A manager grants only their own app's roles, the union when they manage several, and never touches a System Manager."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.sales_manager = mk_user(f"{TAG}-sm2@example.test", ["Sales Manager"])
		cls.agent_manager = mk_user(f"{TAG}-am2@example.test", ["Agent Manager"])
		cls.two_apps = mk_user(f"{TAG}-two@example.test", ["Sales Manager", "Insights Admin"])
		cls.rep = mk_user(f"{TAG}-rep2@example.test", ["Sales User"])
		cls.admin = mk_user(f"{TAG}-admin2@example.test", ["System Manager"])
		cls.moderator = mk_user(f"{TAG}-mod2@example.test", ["Moderator"])
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for email in (cls.sales_manager, cls.agent_manager, cls.two_apps, cls.rep, cls.admin, cls.moderator):
			frappe.delete_doc("User", email, force=True, ignore_permissions=True, delete_permanently=True)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.set_user("Administrator")

	def _insert(self, caller, email, roles=(), **extra):
		with set_user(caller):
			return frappe.get_doc({
				"doctype": "User", "email": email, "first_name": email.split("@")[0], "send_welcome_email": 0,
				"roles": [{"role": r} for r in roles], **extra,
			}).insert()

	def _edit(self, caller, email, change):
		with set_user(caller):
			doc = frappe.get_doc("User", email)
			change(doc)
			doc.save()

	def test_a_sales_manager_creates_a_sales_user(self):
		doc = self._insert(self.sales_manager, f"{TAG}-new@example.test", ["Sales User"])
		self.assertIn("Sales User", frappe.get_roles(doc.name))

	def test_a_manager_of_two_apps_grants_both(self):
		doc = self._insert(self.two_apps, f"{TAG}-union@example.test", ["Sales User", "Insights User"])
		self.assertTrue({"Sales User", "Insights User"} <= set(frappe.get_roles(doc.name)))

	def test_a_manager_is_refused_a_role_outside_their_app(self):
		"""THE PLANTED EVASION: `System Manager`, and another app's role, through the roles table on insert."""
		for caller, role in ((self.sales_manager, "System Manager"), (self.sales_manager, "Agent"),
		                     (self.agent_manager, "Sales User"), (self.two_apps, "Moderator")):
			with self.assertRaises(frappe.PermissionError) as caught:
				self._insert(caller, f"{TAG}-esc@example.test", [role])
			self.assertIn("may not grant", str(caught.exception))

	def test_a_role_profile_is_judged_by_the_roles_it_carries(self):
		profile = frappe.get_doc({
			"doctype": "Role Profile", "role_profile": f"{TAG}-admin-profile", "roles": [{"role": "System Manager"}],
		}).insert()
		with self.assertRaises(frappe.PermissionError):
			self._insert(self.sales_manager, f"{TAG}-prof@example.test", role_profiles=[{"role_profile": profile.name}])

	def test_an_edit_is_capped_the_same_way(self):
		with self.assertRaises(frappe.PermissionError):
			self._edit(self.sales_manager, self.rep, lambda d: d.append("roles", {"role": "System Manager"}))
		self._edit(self.sales_manager, self.rep, lambda d: d.append("roles", {"role": "Sales Manager"}))
		self.assertIn("Sales Manager", frappe.get_roles(self.rep))

	def test_a_manager_disables_a_rep(self):
		self._edit(self.sales_manager, self.rep, lambda d: setattr(d, "enabled", 0))
		self.assertEqual(0, frappe.db.get_value("User", self.rep, "enabled"))

	def test_a_manager_never_changes_a_system_manager(self):
		with self.assertRaises(frappe.PermissionError) as caught:
			self._edit(self.sales_manager, self.admin, lambda d: setattr(d, "enabled", 0))
		self.assertIn("System Manager's account", str(caught.exception))

	def test_a_manager_never_deletes_an_account(self):
		with set_user(self.sales_manager), self.assertRaises(frappe.PermissionError):
			frappe.delete_doc("User", self.rep)

	def test_keys_and_passwords_stay_with_a_system_manager(self):
		"""Level 1 carries roles and profiles, which a manager edits; level 2 carries keys, passwords and sessions."""
		with set_user(self.sales_manager):
			doc = frappe.get_doc("User", self.rep)
			self.assertIn(1, doc.get_permlevel_access("write"))
			self.assertNotIn(2, doc.get_permlevel_access("read"))
		self.assertEqual(2, frappe.get_meta("User").get_field("api_key").permlevel)

	def test_a_manager_never_renames_an_account(self):
		"""A rename moves the address a password reset goes to, so it would hand over the account."""
		with set_user(self.sales_manager), self.assertRaises(frappe.PermissionError) as caught:
			frappe.rename_doc("User", self.admin, f"{TAG}-taken@example.test")
		self.assertIn("rename an account", str(caught.exception))

	def test_lms_save_role_is_capped_the_same_way(self):
		"""lms writes Has Role directly, so the LMS door carries the same cap as the User form."""
		with set_user(self.moderator), self.assertRaises(frappe.PermissionError) as caught:
			dispatch("lms.lms.api.save_role", user=self.rep, role="Sales User", value=1)
		self.assertIn("may not grant", str(caught.exception))

	def test_no_manager_edits_an_invitation(self):
		"""frappe checks an invitation's roles only at insert, and accepting it grants them with permissions ignored."""
		for doctype in ("User Invitation", "CRM Invitation"):
			rows = ledger.rows_for(doctype)
			for role in ledger.MANAGER_ROLES:
				self.assertEqual((0, 0), tuple(rows.get(role, (0, 0, 0, 0))[1:3]), f"{role} may write or create {doctype}")
