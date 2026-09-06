# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Device registration — the rules that keep one rep's patient notifications off another rep's screen.

An FCM token identifies a BROWSER, not a person. The SPA does not unregister on logout, so a second rep
signing in on a shared machine is handed the SAME token, and the row must change hands to them: leave it
with the first rep and their notifications — which carry patient names in the title and body — are pushed
to a screen someone else is now using.

That is the property pinned here. It reads like a hole ("another user can take my device row") and the
next reader will be tempted to close it by scoping the lookup to the caller; that change would leave the
old row in place and re-open the leak, and this test is what stops it. What actually protects the token is
that it is never handed out: CRM Push Subscription is System Manager only and the token is never logged,
so knowing one means already holding the browser.
"""
import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.notifications import api

SUBSCRIPTION = "CRM Push Subscription"
_TOKEN = "fcm-token-shared-browser-probe"
_A = "push-probe-a@example.invalid"
_B = "push-probe-b@example.invalid"


def _user(email):
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": email.split("@")[0], "send_welcome_email": 0}
		).insert(ignore_permissions=True)
	return email


def _rows(token):
	return frappe.get_all(SUBSCRIPTION, filters={"fcm_token": token}, fields=["name", "user"])


class TestPushDeviceRegistration(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		_user(_A)
		_user(_B)

	def setUp(self):
		frappe.db.delete(SUBSCRIPTION, {"fcm_token": _TOKEN})
		self._orig_user = frappe.session.user

	def tearDown(self):
		frappe.set_user(self._orig_user)

	def test_a_shared_browser_hands_the_device_to_whoever_is_signed_in(self):
		"""The whole point: rep A's notifications must never be pushed to a browser rep B is now using."""
		frappe.set_user(_A)
		api.register_token(_TOKEN, device_label="the shared laptop")
		self.assertEqual([r.user for r in _rows(_TOKEN)], [_A])

		frappe.set_user(_B)  # A logs out, B signs in on the same machine — Firebase returns the same token
		api.register_token(_TOKEN, device_label="the shared laptop")

		rows = _rows(_TOKEN)
		self.assertEqual(len(rows), 1, "one browser, one row — never two users pointing at the same screen")
		self.assertEqual(rows[0].user, _B, "the device follows whoever is signed in")

		# and A must no longer be pushed to that browser at all
		from tatva_connect.notifications import presence

		self.assertNotIn(_TOKEN, presence.all_devices(_A), "A's notifications must not reach B's screen")
		self.assertIn(_TOKEN, presence.all_devices(_B))

	def test_a_rep_can_only_unregister_their_own_device(self):
		"""Unregister is scoped to the caller — a crafted token must not silence someone else's phone."""
		frappe.set_user(_A)
		api.register_token(_TOKEN)

		frappe.set_user(_B)
		api.unregister_token(_TOKEN)  # B tries to drop A's device

		rows = _rows(_TOKEN)
		self.assertEqual([r.user for r in rows], [_A], "B must not be able to unregister A's device")

	def test_a_guest_registers_nothing(self):
		frappe.set_user("Guest")
		self.assertEqual(api.register_token(_TOKEN), {"ok": False})
		self.assertEqual(_rows(_TOKEN), [])


if __name__ == "__main__":
	unittest.main()


class TestPushThrottle(unittest.TestCase):
	"""The three push endpoints count per CALLER, not per IP — an office NATs to one address.

	They were on frappe's `@rate_limit`, which keys on the request IP whatever else it is given, so one
	rep on a shared line could exhaust the budget for the whole floor. The refusal class is identical
	either way (`RateLimitExceededError`), so what a browser sees on a refusal does not change.
	"""

	def setUp(self):
		self.counter = f"test-{frappe.generate_hash(length=6)}"
		self.key = f"push-rl:{self.counter}:{frappe.session.user}"
		frappe.cache.delete_value(self.key)
		self.addCleanup(frappe.cache.delete_value, self.key)

	def _with_request(self):
		"""Bind something for `bool(frappe.request)` to see — frappe's own init() tests it the same way."""
		frappe.local.request = object()
		self.addCleanup(lambda: setattr(frappe.local, "request", None))

	def test_it_is_silent_when_there_is_no_http_request(self):
		"""A job or a test calling one of these directly is not a caller with a budget."""
		self.assertFalse(frappe.request)
		for _ in range(50):
			api._throttle(self.counter, 1)

	def test_it_refuses_with_the_same_error_the_decorator_raised(self):
		self._with_request()
		api._throttle(self.counter, 1)
		with self.assertRaises(frappe.RateLimitExceededError):
			api._throttle(self.counter, 1)

	def test_the_budget_belongs_to_the_caller_not_to_their_address(self):
		self._with_request()
		api._throttle(self.counter, 5)
		self.assertEqual(int(frappe.cache.get(frappe.cache.make_key(self.key))), 1)
