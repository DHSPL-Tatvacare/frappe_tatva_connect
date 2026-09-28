# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A notification mail opens the record where its people actually work.

What is asserted:

  * a share on a ticket carries the helpdesk link, which helpdesk fills for assignments alone;
  * a lead carries the CRM link, because one mailer serves every app on this site;
  * a link the caller already chose is left alone;
  * a record with no app of its own carries none, so frappe's own Desk fallback still runs.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.notifications.test_deep_link
"""
import frappe
from frappe.tests.utils import FrappeTestCase

LOG = "Notification Log"


class TestNotificationDeepLink(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.db.rollback()

	def a_log(self, **fields):
		"""Through the document API, so `before_insert` runs exactly as it does on a real notification."""
		return frappe.get_doc({"doctype": LOG, "type": "Share", "subject": "test",
		                       "for_user": "Administrator", **fields}).insert(ignore_permissions=True)

	def test_a_ticket_links_to_the_helpdesk_app(self):
		log = self.a_log(document_type="HD Ticket", document_name="0001")
		self.assertEqual(log.link, frappe.utils.get_url("/helpdesk/tickets/0001"))

	def test_a_lead_links_to_the_crm_app(self):
		log = self.a_log(document_type="CRM Lead", document_name="abc123")
		self.assertEqual(log.link, frappe.utils.get_url("/crm/leads/abc123"))

	def test_a_link_the_caller_chose_is_kept(self):
		chosen = "https://example.test/somewhere"
		log = self.a_log(document_type="CRM Lead", document_name="abc123", link=chosen)
		self.assertEqual(log.link, chosen)

	def test_a_record_with_no_app_keeps_frappes_desk_fallback(self):
		log = self.a_log(document_type="ToDo", document_name="whatever")
		self.assertFalse(log.link)
