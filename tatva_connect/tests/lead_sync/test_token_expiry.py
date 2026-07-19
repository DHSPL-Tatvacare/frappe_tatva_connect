# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A lapsing Facebook token is visible before it stops the crawl.

A Page token dies at about 60 days. The crawl then returns no leads and reports no error — the same
silence as form drift. Graph knows the date, so it is asked once when the token is saved and stamped on
the source; a native Notification (Days Before) reads it. No job is written: Frappe owns that scheduler.

Graph is stubbed — these assert OUR handling of its answer, never Facebook's availability.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.lead_sync.test_token_expiry
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.lead_sync import token
from tatva_connect.lead_sync.notification_seed import NAME as NOTIFICATION_NAME


class TestTokenExpiry(FrappeTestCase):
	def test_graph_expiry_becomes_a_date(self):
		# 2026-09-17T00:00:00Z as a unix timestamp, the shape Graph returns.
		with patch.object(token, "make_get_request", return_value={"data": {"expires_at": 1789603200}}):
			self.assertEqual(str(token.expiry_of("zz-token")), "2026-09-17")

	def test_a_never_expiring_token_stamps_nothing(self):
		"""Graph reports 0 for a System User token — that is not an expiry of 1970."""
		with patch.object(token, "make_get_request", return_value={"data": {"expires_at": 0}}):
			self.assertIsNone(token.expiry_of("zz-token"))

	def test_no_token_is_not_asked_about(self):
		with patch.object(token, "make_get_request", side_effect=AssertionError("Graph must not be called")):
			self.assertIsNone(token.expiry_of(""))

	def test_an_unaskable_token_never_blocks_the_save(self):
		"""The stamp is a convenience; a Graph outage must not stop an operator saving a source."""
		source = frappe._dict(
			name="zz-tok-src", type="Facebook", token_expires_on=None,
			meta=frappe.get_meta("Lead Sync Source"),
			get_password=lambda *a, **k: "zz-token",
		)
		with patch.object(token, "make_get_request", side_effect=RuntimeError("graph down")):
			token.stamp_expiry(source)
		self.assertIsNone(source.token_expires_on)


class TestExpiryNotification(FrappeTestCase):
	def test_the_alert_exists_and_ships_dormant(self):
		self.assertTrue(frappe.db.exists("Notification", NOTIFICATION_NAME))
		alert = frappe.get_doc("Notification", NOTIFICATION_NAME)
		self.assertFalse(alert.enabled, "every automation ships OFF; the operator enables it at go-live")
		self.assertEqual(alert.event, "Days Before")
		self.assertEqual(alert.date_changed, "token_expires_on")
		self.assertEqual(alert.document_type, "Lead Sync Source")

	def test_the_field_it_reads_actually_exists(self):
		"""A Days Before alert pointed at a missing field never fires and never says so."""
		self.assertTrue(frappe.get_meta("Lead Sync Source").has_field("token_expires_on"))
