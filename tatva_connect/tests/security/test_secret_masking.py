# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A secret never reaches the Error Log or the browser in plaintext, whoever wrote the line.

The leak this locks was not at our call sites. `make_request` (frappe/integrations/utils.py) calls a
bare `frappe.log_error()` inside its OWN except block, so a request whose URL carries the Facebook App
Secret is written to `tabError Log` before any handler in this app runs. The old `redact_tokens` matched
`EAA...` only, so it scrubbed the token beside it and left the 32-character secret standing.

The fix is the logging seam itself: the Error Log controller is overridden, so every row every app
writes is masked on the way in. These tests drive a REAL failure through frappe's own transport and
read the row back out of the table, rather than asserting anything about our own helper being called.

Nothing here reaches Facebook: the request is aimed at a closed local port.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.security.test_secret_masking
"""
from unittest.mock import patch

import frappe
from frappe.integrations.utils import make_get_request
from frappe.tests.utils import FrappeTestCase

from tatva_connect.lead_sync import graph
from tatva_connect.utils import mask_secrets, mask_value

# Shaped exactly like the real credentials: 32 hex characters, and a token that opens with EAA.
APP_SECRET = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
FB_TOKEN = "EAAGm0PX4ZCpsBAxyz1234567890abcdef"
# A port nothing listens on, so the request fails at connect and never leaves the host.
DEAD_URL = f"http://127.0.0.1:9/v23.0/oauth/access_token?client_secret={APP_SECRET}&fb_exchange_token={FB_TOKEN}"


class TestMaskingHelper(FrappeTestCase):
	def test_the_ends_of_a_secret_stay_readable(self):
		"""An operator has to be able to tell which credential a log is talking about without holding it."""
		masked = mask_value(APP_SECRET)
		self.assertTrue(masked.startswith("a1b2"))
		self.assertTrue(masked.endswith("8f90"))
		self.assertNotIn(APP_SECRET, masked)

	def test_a_value_too_short_to_show_ends_of_is_masked_whole(self):
		self.assertNotIn("hunter2", mask_value("hunter2"))

	def test_any_field_named_like_a_secret_is_masked_whatever_it_holds(self):
		"""This is what makes the rule general: a webhook token or an HMAC secret is covered by being
		named like one, with no code added for it."""
		text = "custom_webhook_hmac_secret=Zt7Qw9Lm2Xy4Bv6Nc8Kp0Rj1 and api_token: Ab12Cd34Ef56Gh78Ij90"
		masked = mask_secrets(text)
		self.assertNotIn("Zt7Qw9Lm2Xy4Bv6Nc8Kp0Rj1", masked)
		self.assertNotIn("Ab12Cd34Ef56Gh78Ij90", masked)

	def test_a_quoted_json_secret_is_masked(self):
		masked = mask_secrets(f'{{"client_secret": "{APP_SECRET}"}}')
		self.assertNotIn(APP_SECRET, masked)
		self.assertIn("a1b2", masked)

	def test_a_bare_facebook_token_is_masked_on_its_shape_alone(self):
		"""It rides an Authorization header and Meta echoes it back under no key at all."""
		self.assertNotIn(FB_TOKEN, mask_secrets(f"Bad token {FB_TOKEN} supplied"))

	def test_a_hash_that_is_not_named_like_a_secret_is_left_alone(self):
		"""The trade-off, stated: masking keys on their NAME rather than 32 hex on its SHAPE is what keeps
		an MD5 checksum readable. A secret written with no name and no shape is what this cannot catch."""
		text = "content_hash=5d41402abc4b2a76b9719d911017c592"
		self.assertEqual(mask_secrets(text), text)

	def test_masking_does_not_alter_the_value_itself(self):
		original = dict(client_secret=APP_SECRET)
		mask_secrets(frappe.as_json(original))
		self.assertEqual(original["client_secret"], APP_SECRET, "masking is for the way out, never in place")


class TestErrorLogSeam(FrappeTestCase):
	"""The row that really lands in tabError Log when FRAPPE's own logger fires, not ours."""

	def tearDown(self):
		frappe.db.delete("Error Log", {"error": ("like", "%oauth/access_token%")})

	def _drive_a_real_failure(self):
		before = frappe.db.count("Error Log")
		with self.assertRaises(Exception):
			make_get_request(DEAD_URL)
		self.assertGreater(frappe.db.count("Error Log"), before, "frappe's own logger must have written a row")
		rows = frappe.get_all("Error Log", fields=["error", "method"], order_by="creation desc", limit=3)
		return "\n".join(f"{row.method}\n{row.error}" for row in rows)

	def test_the_app_secret_does_not_reach_the_error_log(self):
		logged = self._drive_a_real_failure()
		self.assertIn("oauth/access_token", logged, "the failing call must still be identifiable")
		self.assertNotIn(APP_SECRET, logged, "the App Secret reached tabError Log in plaintext")

	def test_the_facebook_token_does_not_reach_the_error_log(self):
		self.assertNotIn(FB_TOKEN, self._drive_a_real_failure())

	def test_the_masked_secret_is_still_recognisable_in_the_row(self):
		logged = self._drive_a_real_failure()
		self.assertIn("a1b2", logged, "a masked secret keeps its leading characters so it can be compared")
		self.assertIn("8f90", logged, "and its trailing characters")


class TestThrownMessage(FrappeTestCase):
	"""The same text is queued to the browser, so the throw path is masked by the same rule."""

	class _Response:
		status_code = 400

		def __init__(self, message):
			self._message = message
			self.text = message

		def json(self):
			return {"error": {"message": self._message}}

	def _throws(self, message):
		def _raise(*args, **kwargs):
			frappe.flags.integration_request = self._Response(message)
			raise RuntimeError("boom")

		return _raise

	def test_meta_echoing_the_app_secret_back_does_not_reach_the_browser(self):
		echo = f"Invalid client_secret={APP_SECRET} for this app"
		with patch.object(graph, "make_post_request", self._throws(echo)):
			with self.assertRaises(frappe.ValidationError) as caught:
				graph.graph_post("token exchange", "https://graph.facebook.com/v23.0/oauth/access_token", {}, "")
		self.assertNotIn(APP_SECRET, str(caught.exception))

	def test_meta_echoing_the_token_back_does_not_reach_the_browser(self):
		with patch.object(graph, "make_get_request", self._throws(f"Bad token {FB_TOKEN}")):
			with self.assertRaises(frappe.ValidationError) as caught:
				graph.graph_get("probe", "https://graph.facebook.com/v23.0/me", {}, FB_TOKEN)
		self.assertNotIn(FB_TOKEN, str(caught.exception))


class TestCredentialsStayOutOfTheUrl(FrappeTestCase):
	"""Defence in depth beside the masking: Meta's oauth/access_token accepts POST, so the App Secret
	travels in the body rather than in a URL a proxy or a retry can echo."""

	def test_the_exchange_posts_its_credentials_in_the_body(self):
		from tatva_connect.lead_sync import token as token_module

		seen = {}

		def _capture(url, data=None, headers=None):
			seen["url"], seen["data"] = url, data
			return {"access_token": "EAAlonglived000000000000"}

		app = frappe.get_doc({
			"doctype": "CRM Facebook App", "app_id": "700000000000003", "app_name": "Masking Probe",
			"app_secret": APP_SECRET, "graph_api_version": "v23.0", "lead_page_size": 100,
		}).insert(ignore_permissions=True)

		with patch.object(graph, "make_post_request", _capture):
			result = token_module.exchange_for_long_lived("EAAshort0000000000000000", app)

		self.assertEqual(result, "EAAlonglived000000000000")
		self.assertNotIn(APP_SECRET, seen["url"], "the App Secret must not travel in the URL")
		self.assertEqual(seen["data"]["client_secret"], APP_SECRET, "and must still really be sent")
