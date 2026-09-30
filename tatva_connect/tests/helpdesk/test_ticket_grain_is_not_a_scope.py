# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A ticket's grain columns are partner provenance, never a scope on the people who work the ticket.

The partner spine stamps a ticket with the caller's line and fences its reads on the same columns
(`api/_base.grain_fence`). A ticket that did not arrive through a partner carries none, which is
correct — and under `apply_strict_user_permissions` frappe reads a blank link as a denial, so an
agent scoped to a product line could not open, reply to or close any ordinary ticket.

What is asserted:

  * every grain column HD Ticket carries is declared `ignore_user_permissions`, read off the schema;
  * THE CALL: under strict user permissions a vertical-scoped agent may still write a blank-grain
    ticket, which is every ticket that did not come from a partner;
  * THE EVASION: drop the declaration on one column and that same agent is refused, so this is
    watching the flag and not some unrelated grant;
  * the fields an agent actually works — status, priority, type, team — were never in scope at all;
  * the partner fence still names those columns, so nothing here widened what a partner can read.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.helpdesk.test_ticket_grain_is_not_a_scope
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.api._base import grain_fence
from tatva_connect.helpdesk import TICKET
from tatva_connect.taxonomy import grain
from tatva_connect.tests.authz.base import mk_user

AGENT = "zz-grain-scope-agent@example.com"
STRICT = "apply_strict_user_permissions"


def grain_columns():
	"""The grain columns HD Ticket declares — the same brain the partner fence filters on."""
	return [c for c in grain.columns(TICKET) if c]


class TestTicketGrainIsNotAScope(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

	def setUp(self):
		self.mailbox = frappe.get_all("Email Account", filters={"enable_incoming": 1}, pluck="name")
		if not self.mailbox:
			self.skipTest("no incoming email account on this bench")
		self.vertical = frappe.get_all("CRM Vertical", pluck="name", limit=1)
		if not self.vertical:
			self.skipTest("no CRM Vertical on this bench")
		self.agent = mk_user(AGENT, ["Agent"])
		self.scope_the_agent()
		self.strict(1)

	def tearDown(self):
		self.strict(0)
		frappe.db.rollback()
		frappe.clear_cache()

	def strict(self, on):
		"""The operator's own switch; frappe caches System Settings, so the cache goes with it."""
		frappe.db.set_single_value("System Settings", STRICT, on)
		frappe.clear_cache()

	def scope_the_agent(self):
		"""One User Permission on a grain master is all it takes for the blank link to be judged."""
		frappe.get_doc({"doctype": "User Permission", "user": self.agent,
		                "allow": grain.master("vertical"), "for_value": self.vertical[0]}
		               ).insert(ignore_permissions=True)

	def a_blank_grain_ticket(self):
		"""Through the document API, so it has the shape an arriving mail actually produces."""
		doc = frappe.get_doc({"doctype": TICKET, "subject": "Grain scope fixture",
		                      "via_customer_portal": 1, "email_account": self.mailbox[0]}).insert()
		self.assertFalse(any(doc.get(c) for c in grain_columns()), "fixture must carry no grain")
		return doc

	def may_write(self, doc):
		return frappe.has_permission(TICKET, "write", doc=doc, user=self.agent)

	def test_every_grain_column_is_declared_out_of_scope(self):
		meta = frappe.get_meta(TICKET)
		for column in grain_columns():
			self.assertTrue(meta.get_field(column).ignore_user_permissions,
			                f"{column} is read as a scope on whoever works the ticket")

	def test_a_scoped_agent_may_write_a_blank_grain_ticket(self):
		self.assertTrue(self.may_write(self.a_blank_grain_ticket()))

	def test_dropping_the_declaration_refuses_that_same_agent(self):
		doc = self.a_blank_grain_ticket()
		frappe.db.set_value("Custom Field", f"{TICKET}-{grain_columns()[0]}", "ignore_user_permissions", 0)
		frappe.clear_cache(doctype=TICKET)
		self.assertFalse(self.may_write(doc), "the flag is not what the previous test proved")

	def test_the_fields_an_agent_works_were_never_in_scope(self):
		scoped = {r[0] for r in frappe.db.sql("SELECT DISTINCT allow FROM `tabUser Permission`")}
		worked = [f for f in frappe.get_meta(TICKET).get_link_fields()
		          if f.fieldname in ("status", "priority", "ticket_type", "agent_group")]
		self.assertEqual(len(worked), 4, "a field an agent works went missing")
		for field in worked:
			self.assertNotIn(field.options, scoped, f"{field.fieldname} became a scope")

	def test_the_partner_fence_still_names_those_columns(self):
		mapping = frappe._dict(vertical=self.vertical[0], crm_group="anything")
		self.assertEqual(set(grain_fence(mapping, TICKET)), set(grain_columns()[:2]))
