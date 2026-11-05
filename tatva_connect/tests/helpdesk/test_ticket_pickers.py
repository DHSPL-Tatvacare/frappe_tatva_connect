# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""An open ticket's pickers, as the server scopes them.

What is asserted:

  * a ticket's sub type picker offers its own business line's rows and the every-line rows, never another line's;
  * a ticket with no business line is offered only the every-line rows;
  * the new-ticket page's picked grain narrows its pickers the same way;
  * a link to another ticket is never scoped, though a ticket carries a grain;
  * the ticket list shows a line's sub type by its label, never its key;
  * a caller who cannot read the ticket is refused.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.helpdesk.test_ticket_pickers
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.helpdesk import SUB_TYPE
from tatva_connect.helpdesk.pickers import grain_filters, new_ticket_pickers, ticket_pickers

FIELD = "custom_ticket_sub_type"
GOODFLIP = ("Goodflip", "India", "Inside-Sales")
ANAYA = ("Goodflip-Care", "Anaya", "Nivolumab")


class TestTicketPickers(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		for axes in (GOODFLIP, ANAYA):
			if not frappe.db.exists("CRM Grain", "::".join(axes)):
				raise cls.skipTest(cls, f"this bench has no grain {'::'.join(axes)}")

	def setUp(self):
		kind = "ZZ Pickers Type"
		if not frappe.db.exists("HD Ticket Type", kind):
			frappe.get_doc({"doctype": "HD Ticket Type", "__newname": kind}).insert()
		self.every_line = self.a_sub_type(kind, ("", "", ""))
		self.goodflip = self.a_sub_type(kind, GOODFLIP)
		self.anaya = self.a_sub_type(kind, ANAYA)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	@staticmethod
	def a_sub_type(kind, axes):
		vertical, group, program = axes
		return frappe.get_doc({"doctype": SUB_TYPE, "label": "ZZ Pickers Sub", "ticket_type": kind,
		                       "vertical": vertical, "group": group, "program": program}).insert().name

	def offered(self, filters):
		return set(frappe.get_all(SUB_TYPE, filters={**filters, "name": ["in", [self.every_line, self.goodflip, self.anaya]]}, pluck="name"))

	def test_a_ticket_s_line_and_every_line_are_offered_never_another_line(self):
		self.assertEqual(self.offered(grain_filters(GOODFLIP)[FIELD]), {self.goodflip, self.every_line})
		self.assertEqual(self.offered(grain_filters(ANAYA)[FIELD]), {self.anaya, self.every_line})

	def test_a_ticket_with_no_line_is_offered_only_every_line_rows(self):
		ticket = frappe.get_doc({"doctype": "HD Ticket", "subject": "Pickers fixture", "raised_by": "pickers.fixture@example.test"}).insert()
		self.assertEqual(self.offered(ticket_pickers(ticket.name)["filters"][FIELD]), {self.every_line})

	def test_the_new_ticket_page_s_picked_grain_narrows_the_same_way(self):
		picked = frappe.as_json({"custom_vertical": GOODFLIP[0], "custom_group": GOODFLIP[1], "custom_current_program": GOODFLIP[2]})
		self.assertEqual(self.offered(new_ticket_pickers(picked)[FIELD]), {self.goodflip, self.every_line})

	def test_the_ticket_list_shows_a_line_s_sub_type_by_its_label(self):
		from tatva_connect.helpdesk.lists import get_list_data

		ticket = frappe.get_doc({"doctype": "HD Ticket", "subject": "Pickers fixture", "raised_by": "pickers.fixture@example.test",
		                         "custom_vertical": GOODFLIP[0], "custom_group": GOODFLIP[1], "custom_current_program": GOODFLIP[2],
		                         "ticket_type": "ZZ Pickers Type", FIELD: self.goodflip}).insert()
		column = {"key": FIELD, "label": "Sub Type", "type": "Link", "options": SUB_TYPE}
		# Through frappe.call with what the list page really sends, `page_length_count` included, which stock does not declare.
		answer = frappe.call(get_list_data, doctype="HD Ticket", filters={"name": ticket.name}, columns=[column], page_length_count=20)
		self.assertNotEqual(self.goodflip, "ZZ Pickers Sub", "the line's row is keyed, so a title is needed at all")
		self.assertEqual(answer["_link_titles"][f"{SUB_TYPE}::{self.goodflip}"], "ZZ Pickers Sub")

	def test_a_link_to_another_ticket_is_never_scoped(self):
		self.assertNotIn("merged_with", grain_filters(GOODFLIP))
		self.assertNotIn("ticket_split_from", grain_filters(("", "", "")))

	def test_a_caller_who_cannot_read_the_ticket_is_refused(self):
		ticket = frappe.get_doc({"doctype": "HD Ticket", "subject": "Pickers fixture", "raised_by": "pickers.fixture@example.test"}).insert()
		frappe.set_user("Guest")
		self.assertRaises(frappe.PermissionError, ticket_pickers, ticket.name)
