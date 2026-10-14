# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A ticket source can mute Helpdesk's automatic email.

What is asserted (RED before: every ticket whose requester had an email was acknowledged):

  * a ticket from a source with No automatic emails ticked is not acknowledged, though its contact has an email;
  * the same ticket from an unticked source still is, so the box is the gate and not a blanket mute.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.helpdesk.test_source_mutes_email
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.helpdesk.contact import contact_for


class TestSourceMutesEmail(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.db.rollback()

	def _acknowledged(self, mute):
		settings = frappe.get_single("HD Settings")
		settings.send_acknowledgement_email = 1
		settings.save()
		source = frappe.get_doc({"doctype": "HD Ticket Source", "__newname": "ZZ Muted Source", "no_automatic_email": mute}).insert()
		patient = contact_for("+919812397061", email="zz.muted.patient@example.test", create=True)
		with patch("frappe.sendmail") as sendmail:
			ticket = frappe.get_doc({"doctype": "HD Ticket", "subject": "Call from fixture", "contact": patient,
			                         "custom_ticket_source": source.name}).insert()
		self.assertEqual(ticket.raised_by, "zz.muted.patient@example.test")
		return any(c.kwargs.get("recipients") == [ticket.raised_by] for c in sendmail.call_args_list)

	def test_a_muted_source_sends_no_acknowledgement_though_the_contact_has_an_email(self):
		self.assertFalse(self._acknowledged(mute=1))

	def test_an_unmuted_source_still_acknowledges(self):
		self.assertTrue(self._acknowledged(mute=0))
