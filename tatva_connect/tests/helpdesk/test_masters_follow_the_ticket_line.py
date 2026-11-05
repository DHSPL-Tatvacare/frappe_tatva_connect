# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A ticket's masters and status rules follow its business line.

What is asserted:

  * a person cannot save a sub type set up for another line, and can save their own line's or an every-line one;
  * a line's own status rule holds on its tickets, and another line's tickets meet the every-line rule;
  * a line's rule replaces the every-line rule for that move, and a second rule for one move on one line is refused;
  * the type every new ticket is born with cannot be narrowed to one line, from its own form or from HD Settings;
  * a sub type cannot be set up under a ticket type of another line;
  * a list filter offers a label once and matches every line's row of that label;
  * the timeline names a line's value by its label.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.helpdesk.test_masters_follow_the_ticket_line
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.helpdesk import SETTINGS, SUB_TYPE, TICKET, TRANSITION

GOODFLIP = ("Goodflip", "India", "Inside-Sales")
ANAYA = ("Goodflip-Care", "Anaya", "Nivolumab")
KIND = "ZZ Line Type"


def _grain(axes):
	return dict(zip(("vertical", "group", "program"), axes, strict=True))


class TestMastersFollowTheTicketLine(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		for axes in (GOODFLIP, ANAYA):
			if not frappe.db.exists("CRM Grain", "::".join(axes)):
				raise cls.skipTest(cls, f"this bench has no grain {'::'.join(axes)}")

	def setUp(self):
		if not frappe.db.exists("HD Ticket Type", KIND):
			frappe.get_doc({"doctype": "HD Ticket Type", "__newname": KIND}).insert()
		frappe.db.delete(TRANSITION)  # the rulebook under test is the one each case writes
		frappe.clear_cache(doctype=TRANSITION)

	def tearDown(self):
		frappe.db.rollback()
		frappe.clear_cache(doctype=TRANSITION)

	def a_ticket(self, axes):
		ticket = frappe.get_doc({"doctype": TICKET, "subject": "Line probe", "raised_by": "line.probe@example.test",
		                         "custom_vertical": axes[0], "custom_group": axes[1], "custom_current_program": axes[2]}).insert()
		self.assertEqual((ticket.custom_vertical, ticket.custom_group, ticket.custom_current_program), axes)
		return frappe.get_doc(TICKET, ticket.name)

	def a_sub_type(self, label, axes):
		return frappe.get_doc({"doctype": SUB_TYPE, "label": label, "ticket_type": KIND, **_grain(axes)}).insert().name

	def a_rule(self, to_status, axes=("", "", ""), demands=()):
		rule = frappe.get_doc({"doctype": TRANSITION, "from_status": "", "to_status": to_status, **_grain(axes),
		                       "required_fields": [{"fieldname": f} for f in demands]}).insert()
		frappe.clear_cache(doctype=TRANSITION)
		return rule

	def classify(self, ticket, sub_type):
		ticket.ticket_type = KIND
		ticket.custom_ticket_sub_type = sub_type
		ticket.save()

	def test_a_person_cannot_save_another_line_s_sub_type(self):
		anaya_only = self.a_sub_type("ZZ Infusion Delay", ANAYA)
		with self.assertRaises(frappe.ValidationError):
			self.classify(self.a_ticket(GOODFLIP), anaya_only)
		self.classify(self.a_ticket(GOODFLIP), self.a_sub_type("ZZ Reconnection", GOODFLIP))
		self.classify(self.a_ticket(GOODFLIP), self.a_sub_type("ZZ Other", ("", "", "")))

	def test_a_line_s_own_rule_holds_on_its_tickets_and_others_meet_the_every_line_rule(self):
		self.a_rule("Resolved", demands=["ticket_type"])
		self.a_rule("Resolved", ANAYA, demands=["custom_internal_team"])
		goodflip, anaya = self.a_ticket(GOODFLIP), self.a_ticket(ANAYA)

		goodflip.status = "Resolved"
		with self.assertRaises(frappe.ValidationError) as caught:
			goodflip.save()
		self.assertIn(frappe.get_meta(TICKET).get_label("ticket_type"), str(caught.exception))

		anaya.status = "Resolved"
		with self.assertRaises(frappe.ValidationError) as caught:
			anaya.save()
		self.assertIn(frappe.get_meta(TICKET).get_label("custom_internal_team"), str(caught.exception))
		self.assertNotIn(frappe.get_meta(TICKET).get_label("ticket_type"), str(caught.exception), "a line's rule replaces the every-line rule")

	def test_a_second_rule_for_one_move_on_one_line_is_refused(self):
		self.a_rule("Resolved", ANAYA)
		with self.assertRaises((frappe.ValidationError, frappe.DuplicateEntryError)):
			self.a_rule("Resolved", ANAYA)
		with self.assertRaises(frappe.ValidationError, msg="an older plain-named rule for the move is a twin too"):
			frappe.get_doc({"doctype": TRANSITION, "name": "::Resolved", "from_status": "", "to_status": "Resolved"}).db_insert()  # the shape every rule had before lines
			self.a_rule("Resolved")
		self.a_rule("Resolved", GOODFLIP)

	def test_the_default_type_cannot_be_narrowed_to_one_line(self):
		default = frappe.db.get_single_value(SETTINGS, "default_ticket_type")
		if not default:
			self.skipTest("HD Settings names no default ticket type")
		kind = frappe.get_doc("HD Ticket Type", default)
		kind.custom_vertical = GOODFLIP[0]
		self.assertRaises(frappe.ValidationError, kind.save)

	def test_the_default_type_cannot_become_a_line_specific_one_from_hd_settings(self):
		narrowed = frappe.get_doc({"doctype": "HD Ticket Type", "__newname": "ZZ Line Only Type",
		                           "custom_vertical": ANAYA[0], "custom_group": ANAYA[1], "custom_program": ANAYA[2]}).insert()
		settings = frappe.get_single(SETTINGS)
		settings.default_ticket_type = narrowed.name
		self.assertRaises(frappe.ValidationError, settings.save)

	def test_a_sub_type_cannot_sit_under_a_type_of_another_line(self):
		goodflip_type = frappe.get_doc({"doctype": "HD Ticket Type", "__newname": "ZZ Goodflip Only Type",
		                                "custom_vertical": GOODFLIP[0], "custom_group": GOODFLIP[1], "custom_program": GOODFLIP[2]}).insert()
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({"doctype": SUB_TYPE, "label": "ZZ Stranded", "ticket_type": goodflip_type.name, **_grain(ANAYA)}).insert()
		frappe.get_doc({"doctype": SUB_TYPE, "label": "ZZ Placed", "ticket_type": goodflip_type.name, **_grain(GOODFLIP)}).insert()
		frappe.get_doc({"doctype": SUB_TYPE, "label": "ZZ Placed", "ticket_type": KIND, **_grain(ANAYA)}).insert()

	def test_a_sub_type_for_a_whole_vertical_sits_under_a_type_for_one_group_of_it(self):
		group_type = frappe.get_doc({"doctype": "HD Ticket Type", "__newname": "ZZ Goodflip India Type",
		                             "custom_vertical": GOODFLIP[0], "custom_group": GOODFLIP[1]}).insert()
		frappe.get_doc({"doctype": SUB_TYPE, "label": "ZZ Whole Vertical", "ticket_type": group_type.name,
		                **_grain((GOODFLIP[0], "", ""))}).insert()

	def test_a_list_filter_offers_a_label_once_and_matches_every_line(self):
		import json

		from tatva_connect.helpdesk.contact import get_quick_filters
		from tatva_connect.helpdesk.lists import get_filterable_fields, get_list_data
		from tatva_connect.taxonomy import labels

		goodflip, anaya = self.a_sub_type("ZZ Shared Label", GOODFLIP), self.a_sub_type("ZZ Shared Label", ANAYA)
		tickets = []
		for axes, sub_type in ((GOODFLIP, goodflip), (ANAYA, anaya)):
			ticket = self.a_ticket(axes)
			self.classify(ticket, sub_type)
			tickets.append(ticket.name)
		field = "custom_ticket_sub_type"
		# The list filter bar and the settings condition builder both get a Link; only the filter control reads the hint, as in the CRM.
		for caller in ({}, {"ignore_team_restrictions": 1}):
			offered = next(f for f in frappe.call(get_filterable_fields, doctype=TICKET, **caller) if f["fieldname"] == field)
			self.assertEqual((offered["fieldtype"], offered["link_query"]), ("Link", labels.LABEL_QUERY))
		quick = [f for f in get_quick_filters(TICKET) if f.get("name") == field]
		self.assertTrue(all(f["type"] == "Link" and f["link_query"] == labels.LABEL_QUERY for f in quick))
		answer = labels.label_query(SUB_TYPE, "ZZ Shared", "name", 0, 20, {})
		self.assertEqual([value for value, _label in answer].count("ZZ Shared Label"), 1)
		# The list page sends filters as JSON rows of [field, operator, value].
		rows = frappe.call(get_list_data, doctype=TICKET, filters=json.dumps([[field, "=", "ZZ Shared Label"]]), page_length_count=20)["data"]
		self.assertEqual(sorted(r["name"] for r in rows), sorted(tickets))

	def test_the_timeline_names_a_line_s_value_by_its_label(self):
		ticket = self.a_ticket(GOODFLIP)
		ticket.ticket_type = KIND
		ticket.save()
		ticket.reload()
		ticket.custom_ticket_sub_type = self.a_sub_type("ZZ Timeline Label", GOODFLIP)
		ticket.save()
		said = frappe.get_all("Comment", filters={"reference_doctype": TICKET, "reference_name": ticket.name, "comment_type": "Info"}, pluck="content")
		self.assertTrue(any("ZZ Timeline Label" in c and "::" not in c for c in said), said)
