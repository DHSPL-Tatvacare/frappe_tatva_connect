# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A colleague is not a customer — the Contact list stops showing the team its own names.

What is asserted:

  * a contact whose linked User is a System User is absent from an ordinary caller's contact list;
  * a contact with no user at all — the ordinary customer — is untouched;
  * a contact whose linked User is a WEBSITE User is untouched, because a portal login is an external
    person and the naive "has a user" filter would have hidden a real customer;
  * a privileged caller still sees every contact, staff included;
  * the rule is DORMANT until its switch is armed — off, the list is stock frappe's;
  * `get_contact_name()` still resolves a staff member's own contact for an unprivileged session, so
    frappe's user provisioning is unaffected by the condition;
  * the condition is ONE correlated predicate and never a list of staff names inlined from Python.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.access.test_contact_scope
"""
import frappe
from frappe.contacts.doctype.contact.contact import get_contact_name
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access.contact_scope import (
	STAFF_USER_TYPE,
	SWITCH,
	get_contact_permission_query_conditions,
)

STAFF = "zz-contact-staff@example.test"
PORTAL = "zz-contact-portal@example.test"
REP = "zz-contact-rep@example.test"
MANAGER = "zz-contact-manager@example.test"
CUSTOMER = "ZZ Contact Scope Customer"


class TestContactScope(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		# user_type is DERIVED from roles, so it is forced here: the fixture must state which side of the
		# rule each user is on, not depend on which roles happen to carry desk access.
		cls.staff = _user(STAFF, "ZZ Staff", STAFF_USER_TYPE)
		cls.portal = _user(PORTAL, "ZZ Portal", "Website User")
		# The caller is a rep, so a System User themselves — staff is hidden from staff, not only from portals.
		# Sales User because the condition only bites on a caller who can read Contact at all; a role-less user is refused earlier and proves nothing.
		cls.rep = _user(REP, "ZZ Rep", STAFF_USER_TYPE, roles=["Sales User"])
		cls.manager = _user(MANAGER, "ZZ Manager", STAFF_USER_TYPE, roles=["System Manager"])
		cls.staff_contact = _contact_for(STAFF, "ZZ Staff")
		cls.portal_contact = _contact_for(PORTAL, "ZZ Portal")
		cls.customer_contact = _plain_contact(CUSTOMER)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for contact in (cls.staff_contact, cls.portal_contact, cls.customer_contact):
			frappe.db.delete("Contact Email", {"parent": contact})
			frappe.db.delete("Contact", {"name": contact})
		for email in (STAFF, PORTAL, REP, MANAGER):
			frappe.db.delete("Contact Email", {"email_id": email})
			frappe.db.delete("Contact", {"user": email})
			frappe.db.delete("Has Role", {"parent": email, "parenttype": "User"})
			frappe.db.delete("User", {"name": email})
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.set_user("Administrator")
		# The rule ships dormant, so every assertion about it arms it first — as the sibling visibility suites do.
		self._arm(1)

	def _arm(self, on):
		frappe.db.set_value("CRM Tatva Automation", SWITCH, "enabled", on)

	# ---- fixtures ----------------------------------------------------------------------------------

	def _visible_to(self, user):
		"""The contact names this user's own list read returns — the real entry point, not the clause."""
		frappe.set_user(user)
		return set(frappe.get_list("Contact", pluck="name", limit_page_length=0))

	# ---- the rule ----------------------------------------------------------------------------------

	def test_a_staff_contact_is_hidden_from_a_rep(self):
		"""Frappe makes one Contact per User; a rep searching for a patient must not find their manager."""
		self.assertEqual(frappe.db.get_value("User", STAFF, "user_type"), STAFF_USER_TYPE,
						 "fixture: the staff user is not a System User, so nothing was being filtered")
		self.assertNotIn(self.staff_contact, self._visible_to(REP),
						 "a colleague's contact was offered to a rep as though it were a customer")

	def test_a_contact_with_no_user_is_visible(self):
		"""The ordinary customer — no login, nothing to link — is untouched by any of this."""
		self.assertIn(self.customer_contact, self._visible_to(REP),
					  "an ordinary customer disappeared from the contact list")

	def test_a_website_user_contact_is_visible(self):
		"""THE reason the rule is "the linked User is a System User" and not "has a user set".

		A Website User is a portal login held by an external person — a patient, a partner, a customer.
		Filtering on `user` being set at all would have hidden every one of them, which is the defect this
		test exists to catch. Staff is a user TYPE, never the presence of a user.
		"""
		self.assertEqual(frappe.db.get_value("User", PORTAL, "user_type"), "Website User",
						 "fixture: the portal user is not a Website User, so this proved nothing")
		self.assertIn(self.portal_contact, self._visible_to(REP),
					  "a customer holding a portal login was hidden as though they were staff")

	def test_privileged_sees_every_contact(self):
		"""An operator administers the team, so the team's own contacts are theirs to see."""
		visible = self._visible_to(MANAGER)
		self.assertIn(self.staff_contact, visible, "a System Manager lost sight of a staff contact")
		self.assertIn(self.portal_contact, visible, "a System Manager lost sight of a customer")
		self.assertIn(self.customer_contact, visible, "a System Manager lost sight of a customer")

	def test_frappe_can_still_find_a_users_contact(self):
		"""User provisioning reads through `get_all`/`db.exists`, which apply no permission conditions —
		so hiding staff from the LIST can never stop frappe resolving a user's own contact."""
		frappe.set_user(REP)
		self.assertEqual(get_contact_name(STAFF), self.staff_contact,
						 "hiding staff contacts broke frappe's own user-to-contact lookup")

	def test_the_condition_is_one_predicate(self):
		"""One correlated NOT EXISTS the optimiser can drive — never a name list that grows with headcount
		and is stale the moment a login is created."""
		frappe.set_user(REP)
		condition = get_contact_permission_query_conditions()
		self.assertIn("not exists", condition.lower(), "the condition stopped being a correlated predicate")
		self.assertIn("`tabUser`", condition, "the condition no longer asks the User table anything")
		for email in (STAFF, MANAGER):
			self.assertNotIn(email, condition,
							 f"{email} was inlined into the clause, so it grows with every new login")

	def test_the_rule_is_dormant_until_armed(self):
		"""Off is stock frappe — no clause, and the staff contact the armed rule hides is back in the list."""
		self._arm(0)
		frappe.set_user(REP)
		self.assertIsNone(get_contact_permission_query_conditions(),
						  "the dormant rule still handed the caller a restriction to satisfy")
		self.assertIn(self.staff_contact, self._visible_to(REP),
					  "the switch was off and the contact list was still being filtered")

	def test_a_privileged_caller_is_not_restricted_at_all(self):
		"""None, never "" — an empty string is a condition frappe would still AND in, and it says nothing."""
		frappe.set_user(MANAGER)
		self.assertIsNone(get_contact_permission_query_conditions(),
						  "a privileged caller was handed a restriction to satisfy")


# ---- fixture builders ------------------------------------------------------------------------------


def _user(email, first_name, user_type, roles=None):
	"""A user pinned to one side of the rule. Inserting one also makes its Contact (frappe's own hook)."""
	if not frappe.db.exists("User", email):
		doc = frappe.get_doc({
			"doctype": "User", "email": email, "first_name": first_name, "send_welcome_email": 0,
			"roles": [{"role": role} for role in (roles or [])],
		})
		doc.insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no session user
	# Set straight on the row: `user_type` is derived from role desk access on save, and the fixture must
	# declare it rather than inherit whatever the roles happened to imply.
	frappe.db.set_value("User", email, "user_type", user_type)
	return email


def _contact_for(email, first_name):
	"""The Contact frappe makes for a User — reused if the hook already made it, created if it did not."""
	existing = frappe.get_all("Contact", filters={"user": email}, pluck="name", limit=1)  # authz-ok: tier-c — test fixture
	if existing:
		if not frappe.db.exists("Contact Email", {"parent": existing[0], "email_id": email}):
			contact = frappe.get_doc("Contact", existing[0])
			contact.add_email(email, is_primary=True)
			contact.save(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no session user
		return existing[0]
	contact = frappe.get_doc({"doctype": "Contact", "first_name": first_name, "user": email})
	contact.add_email(email, is_primary=True)
	contact.insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no session user
	return contact.name


def _plain_contact(first_name):
	"""An ordinary customer: a name and no login behind it."""
	existing = frappe.get_all(  # authz-ok: tier-c — test fixture
		"Contact", filters={"first_name": first_name, "user": ["in", ["", None]]}, pluck="name", limit=1,
	)
	if existing:
		return existing[0]
	contact = frappe.get_doc({"doctype": "Contact", "first_name": first_name})
	contact.insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no session user
	return contact.name
