# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""An open ticket receives every Field Dependency script, which stock helpdesk sends only to the new-ticket page.

Run:
    bench --site dev.localhost run-tests --app tatva_connect --module tatva_connect.tests.helpdesk.test_ticket_customizations
"""
import frappe
from frappe.tests import IntegrationTestCase
from helpdesk.api.settings.field_dependency import create_update_field_dependency
from helpdesk.helpdesk.doctype.hd_ticket.api import get_ticket_customizations as stock
from helpdesk.test_utils import make_agent

from tatva_connect.helpdesk.customizations import get_ticket_customizations

# A pair no real dependency uses, declared through helpdesk's own Field Dependency API, so any declared pair is covered, not one field.
PAIR = 'applyFilters("custom_ticket_bucket"'


class TestOpenTicketGetsFieldDependencies(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		create_update_field_dependency("priority", "custom_ticket_bucket", frappe.as_json({"High": ["zz-bucket"]}), 1, frappe.as_json({}))
		frappe.set_user(make_agent("zz-dependency-agent@example.com"))

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def test_stock_leaves_the_dependency_off_an_open_ticket(self):
		self.assertFalse(any(PAIR in s for s in stock()["_form_script"] or []))

	def test_the_override_hands_an_agent_every_declared_dependency(self):
		self.assertTrue(any(PAIR in s for s in get_ticket_customizations()["_form_script"]))

	def test_the_open_ticket_endpoint_resolves_to_the_override(self):
		hooked = frappe.get_hooks("override_whitelisted_methods")["helpdesk.helpdesk.doctype.hd_ticket.api.get_ticket_customizations"][-1]
		self.assertEqual(hooked, "tatva_connect.helpdesk.customizations.get_ticket_customizations")
