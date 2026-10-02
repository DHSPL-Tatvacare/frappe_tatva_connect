# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A colleague is not a customer: the Contact list hides contacts whose linked User is a System User.
Customers and portal users stay visible, privileged callers see all, and the rule is dormant until armed."""
import frappe
from frappe.contacts.doctype.contact.contact import get_contact_name
from frappe.tests import IntegrationTestCase

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


class TestContactScope(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		# user_type is derived from roles, so it is forced here to pin each user to one side of the rule.
		cls.staff = _user(STAFF, "ZZ Staff", STAFF_USER_TYPE)
		cls.portal = _user(PORTAL, "ZZ Portal", "Website User")
		# Sales User, because a caller who cannot read Contact at all is refused earlier and proves nothing.
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
		# The rule ships dormant, so every test arms it first.
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
		"""A portal login is an external person, so the rule keys on user TYPE, not on a user being set.
		Filtering on "has a user" would hide every customer who holds a portal login."""
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
		"""User provisioning reads through `get_all`/`db.exists`, which apply no permission conditions.
		Hiding staff from the list must not stop frappe resolving a user's own contact."""
		frappe.set_user(REP)
		self.assertEqual(get_contact_name(STAFF), self.staff_contact,
						 "hiding staff contacts broke frappe's own user-to-contact lookup")

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
	# Set on the row, because save derives `user_type` from roles and the fixture must declare it.
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
