# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Each manager adds users only through their own app's door and grants only their own app's roles.
Calls go through `dispatch`, which resolves method overrides the way a real request does."""
import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.tests.authz.base import dispatch, mk_user, set_user

TAG = "user-admin-tier"

# Our gate's refusal text, so a native refusal can never pass or fail these tests.
OURS = "may add users"

CRM_DOOR = "crm.api.invite_by_email"
HELPDESK_DOOR = "helpdesk.api.agent.sent_invites"
PLATFORM_DOOR = "frappe.contacts.doctype.contact.contact.invite_user"


class TestUserAdminTier(IntegrationTestCase):
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
		"""True only when our gate refused; a later native refusal counts as admitted."""
		with set_user(user):
			try:
				dispatch(door, **kwargs)
			except frappe.PermissionError as e:
				return OURS in str(e)
			except frappe.ValidationError:
				return False  # native validation decided after we admitted; any other error is a real failure
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
		"""Deleting an account and creating one from a contact stay with a System Manager."""
		for user in (self.sales_manager, self.agent_manager):
			with set_user(user), self.assertRaises(frappe.PermissionError) as caught:
				dispatch("lms.lms.api.delete_member", user=self.sales_user)
			self.assertIn("System Manager", str(caught.exception))

			with set_user(user), self.assertRaises(frappe.PermissionError) as caught:
				dispatch(PLATFORM_DOOR, contact="nobody")
			self.assertIn("System Manager", str(caught.exception))

	# ---- the escalation lock -----------------------------------------------------------------------

	def test_the_account_a_manager_creates_holds_no_role(self):
		"""An account a manager creates starts with no role copied from the manager."""
		email = f"{TAG}-fresh@example.test"
		with set_user(self.agent_manager):
			dispatch(HELPDESK_DOOR, emails=[email], send_welcome_mail_to_user=False)
		self.assertTrue(frappe.db.exists("User", email), "the manager's own door did not create the account")
		granted = frappe.get_all("Has Role", filters={"parent": email}, pluck="role")
		self.assertNotIn("System Manager", granted)
		self.assertNotIn("Agent Manager", granted)



class TestManagerAddsUsers(IntegrationTestCase):
	"""A manager grants only their own apps' roles and never touches a System Manager's account."""

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
		"""A manager cannot grant `System Manager` or another app's role through the roles table on insert."""
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



class TestNoRoleBelowAdminCanEscalate(IntegrationTestCase):
	"""Asked of Frappe's own engine as real users holding each role, against the permissions lockdown built."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from tatva_connect.access import ledger

		cls.managers = {role: mk_user(f"{TAG}-esc-{i}@example.test", [role]) for i, role in enumerate(ledger.MANAGER_ROLES)}
		cls.rep = mk_user(f"{TAG}-esc-rep@example.test", [ledger.SALES_USER])

	def test_no_manager_writes_a_pending_invitation(self):
		"""Accepting an invitation grants its roles with permissions ignored, so editing one is an escalation."""
		for role, user in self.managers.items():
			for doctype in ("User Invitation", "CRM Invitation"):
				with self.subTest(role=role, doctype=doctype):
					self.assertFalse(frappe.has_permission(doctype, "write", user=user))

	def test_no_manager_deletes_an_account(self):
		for role, user in self.managers.items():
			with self.subTest(role=role):
				self.assertFalse(frappe.has_permission("User", "delete", user=user))

	def test_a_rep_cannot_grant_themselves_a_line_or_a_manager(self):
		for doctype in ("User Permission", "CRM Sales Hierarchy"):
			for ptype in ("write", "create", "delete"):
				with self.subTest(doctype=doctype, ptype=ptype):
					self.assertFalse(frappe.has_permission(doctype, ptype, user=self.rep))
