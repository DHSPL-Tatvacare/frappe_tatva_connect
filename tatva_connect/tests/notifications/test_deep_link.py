# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A notification mail opens the record where its people actually work.

What is asserted:

  * a share on a ticket carries the helpdesk link, which helpdesk fills for assignments alone;
  * a lead carries the CRM link, because one mailer serves every app on this site;
  * a link the caller already chose is left alone;
  * a record with no app of its own carries none, so frappe's own Desk fallback still runs;
  * a notice about many records opens its list drilled to the filters, read live when opened;
  * a task opens on its lead's Tasks tab, a mention at its comment and a WhatsApp notice on its tab;
  * with the switch off nothing is written at all, so frappe behaves exactly as it ships.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.notifications.test_deep_link
"""
import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.notifications import deep_link
from tatva_connect.notifications.dispatch import SWITCH as AUTOMATION_KEY

LOG = "Notification Log"
SWITCH = "CRM Tatva Automation"


class TestNotificationDeepLink(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

	def setUp(self):
		if not frappe.db.exists(SWITCH, AUTOMATION_KEY):
			self.skipTest("the automation row is seeded on migrate")
		self.arm(1)

	def tearDown(self):
		frappe.db.rollback()

	def arm(self, enabled):
		"""The operator's own switch, which every automation here rides on."""
		frappe.db.set_value(SWITCH, AUTOMATION_KEY, "enabled", enabled)

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

	def test_a_list_opens_drilled_to_its_filters(self):
		link = deep_link.list_url("CRM Lead", {"_assign": ["like", "%rep@example.com%"]})
		self.assertEqual(link, frappe.utils.get_url("/crm/leads/view/list?filters=%7B%22_assign%22%3A%5B%22like%22%2C%22%25rep%40example.com%25%22%5D%7D"))

	def test_the_switch_off_leaves_frappe_as_it_ships(self):
		self.arm(0)
		log = self.a_log(document_type="CRM Lead", document_name="abc123")
		self.assertFalse(log.link)
		self.assertIsNone(deep_link.list_url("CRM Lead", {"_assign": ["like", "%rep@example.com%"]}))

	def test_a_task_opens_on_its_leads_tasks_tab(self):
		from tatva_connect.workflow_engine.tests.fixtures import make_lead

		lead = make_lead()
		task = frappe.get_doc({"doctype": "CRM Task", "title": "probe", "reference_doctype": "CRM Lead",
		                       "reference_docname": lead.name}).insert(ignore_permissions=True)
		log = self.a_log(document_type="CRM Task", document_name=task.name)
		self.assertEqual(log.link, frappe.utils.get_url(f"/crm/leads/{lead.name}#tasks"))

	def test_a_mention_opens_at_its_comment_and_whatsapp_on_its_tab(self):
		from tatva_connect.workflow_engine.tests.fixtures import make_lead

		lead = make_lead()
		comment = lead.add_comment("Comment", "probe")
		mention = self.a_log(type="Mention", document_type="CRM Lead", document_name=lead.name,
		                     source_doctype="Comment", source_name=comment.name)
		self.assertEqual(mention.link, frappe.utils.get_url(f"/crm/leads/{lead.name}#{comment.name}"))
		whatsapp = self.a_log(type="WhatsApp", document_type="CRM Lead", document_name="abc123")
		self.assertEqual(whatsapp.link, frappe.utils.get_url("/crm/leads/abc123#whatsapp"))
