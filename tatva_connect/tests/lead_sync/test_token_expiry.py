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

from tatva_connect.lead_sync import notification_seed, token


def _epoch_in_days(days):
	"""The unix expiry Graph reports for a token lapsing `days` from today."""
	return int(frappe.utils.get_datetime(frappe.utils.add_days(frappe.utils.nowdate(), days)).timestamp())


APP = "700000000000001"


def _app(app_id=APP, secret="zz-app-secret"):
	"""A real CRM Facebook App row, because token.py asks it for the Graph URL and the inspector — a
	stand-in would be a second copy of both. FrappeTestCase rolls it back."""
	if not frappe.db.exists("CRM Facebook App", app_id):
		frappe.get_doc({
			"doctype": "CRM Facebook App", "app_id": app_id, "app_name": f"Probe {app_id}",
			"app_secret": secret, "graph_api_version": "v23.0", "lead_page_size": 100,
		}).insert(ignore_permissions=True)
	return frappe.get_doc("CRM Facebook App", app_id)


def _source(access_token):
	"""A stand-in source carrying only what refresh_credential reads and writes."""
	_app()
	return frappe._dict(
		name="zz-tok-src", type="Facebook", access_token=access_token, token_expires_on=None,
		facebook_app=APP, meta=frappe.get_meta("Lead Sync Source"),
		get_password=lambda *args, **kwargs: access_token,
	)


class TestTokenExpiry(FrappeTestCase):
	def test_graph_expiry_becomes_a_date(self):
		# 2026-09-17T00:00:00Z as a unix timestamp, the shape Graph returns.
		with patch.object(token, "graph_get", return_value={"data": {"expires_at": 1789603200}}):
			self.assertEqual(str(token.expiry_date(token.token_info("zz-token", _app()))), "2026-09-17")

	def test_a_never_expiring_token_stamps_nothing(self):
		"""Graph reports 0 for a derived Page token or a System User token, not an expiry of 1970."""
		with patch.object(token, "graph_get", return_value={"data": {"expires_at": 0}}):
			self.assertIsNone(token.expiry_date(token.token_info("zz-token", _app())))

	def test_no_token_is_not_asked_about(self):
		with patch.object(token, "graph_get", side_effect=AssertionError("Graph must not be called")):
			self.assertEqual(token.token_info("", _app()), {})

	def test_an_unaskable_token_never_blocks_the_save(self):
		"""The stamp is a convenience; a Graph outage must not stop an operator saving a source."""
		source = _source("zz-token")
		with patch.object(token, "graph_get", side_effect=RuntimeError("graph down")):
			token.refresh_credential(source)
		self.assertIsNone(source.token_expires_on)
		self.assertEqual(source.access_token, "zz-token", "a failed inspection must not blank the token")


class TestLongLivedExchange(FrappeTestCase):
	"""The operator pastes the short token; what gets stored has to be the durable one."""

	def test_a_short_token_is_recognised_as_short(self):
		self.assertTrue(token.is_short({"expires_at": _epoch_in_days(0)}))

	def test_a_sixty_day_token_is_not_short(self):
		self.assertFalse(token.is_short({"expires_at": _epoch_in_days(60)}))

	def test_a_non_expiring_token_is_not_short(self):
		"""A derived Page token reports 0, which is the opposite of expiring imminently."""
		self.assertFalse(token.is_short({"expires_at": 0}))

	def test_a_short_token_is_replaced_in_place(self):
		source = _source("zz-short")
		with patch.object(token, "token_info", side_effect=[
			{"expires_at": _epoch_in_days(0)}, {"expires_at": _epoch_in_days(60)}
		]), patch.object(token, "exchange_for_long_lived", return_value="zz-long-lived"):
			token.refresh_credential(source)
		self.assertEqual(source.access_token, "zz-long-lived")
		self.assertEqual(str(source.token_expires_on), frappe.utils.add_days(frappe.utils.nowdate(), 60))

	def test_a_long_token_is_left_alone(self):
		source = _source("zz-long")
		with patch.object(token, "token_info", return_value={"expires_at": _epoch_in_days(60)}), \
			patch.object(token, "exchange_for_long_lived",
				side_effect=AssertionError("a long token must not be exchanged")):
			token.refresh_credential(source)
		self.assertEqual(source.access_token, "zz-long")

	def test_a_failed_exchange_never_blocks_the_save(self):
		source = _source("zz-short")
		with patch.object(token, "token_info", return_value={"expires_at": _epoch_in_days(0)}), \
			patch.object(token, "exchange_for_long_lived", side_effect=RuntimeError("graph down")):
			token.refresh_credential(source)
		self.assertEqual(source.access_token, "zz-short")

	def test_graph_is_inspected_once_when_nothing_is_exchanged(self):
		"""A save costs one Graph call, not one per question asked of the token."""
		source = _source("zz-long")
		with patch.object(token, "token_info", return_value={"expires_at": _epoch_in_days(60)}) as info:
			token.refresh_credential(source)
		self.assertEqual(info.call_count, 1)

	def test_no_app_secret_means_no_exchange_attempt(self):
		"""Without the app secret the exchange cannot be made, and "" is returned rather than a throw."""
		self.assertEqual(token.exchange_for_long_lived("zz-short", _app("700000000000002", secret="")), "")


class TestExpiryNotification(FrappeTestCase):
	def test_every_declared_alert_exists_and_ships_dormant(self):
		"""The declaration is `_ALERTS`; this is what makes it true of the running site (B3)."""
		for declared in notification_seed._ALERTS:
			with self.subTest(alert=declared["name"]):
				self.assertTrue(frappe.db.exists("Notification", declared["name"]))
				alert = frappe.get_doc("Notification", declared["name"])
				self.assertFalse(alert.enabled, "every automation ships OFF; the operator enables it")
				self.assertEqual(alert.event, declared["event"])
				self.assertEqual(alert.date_changed, declared["date_changed"])
				self.assertEqual(alert.document_type, notification_seed.DOCTYPE)
				self.assertEqual(alert.condition, "doc.enabled", "a disabled source must not alert")

	def test_every_field_an_alert_reads_actually_exists(self):
		"""A date-based alert pointed at a missing field never fires and never says so."""
		meta = frappe.get_meta(notification_seed.DOCTYPE)
		for declared in notification_seed._ALERTS:
			with self.subTest(alert=declared["name"]):
				self.assertTrue(meta.has_field(declared["date_changed"]))

	def test_silence_is_measured_from_the_last_lead_not_the_last_run(self):
		"""`last_synced_at` only moves when a lead actually lands, which is what makes a Days After alert
		on it mean "no leads" rather than "the job stopped running"."""
		silence = next(a for a in notification_seed._ALERTS if a["event"] == "Days After")
		self.assertEqual(silence["date_changed"], "last_synced_at")
