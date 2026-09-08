# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""MEDIA THAT EXPIRED OFF THE PROVIDER IS AN ANSWER, NOT A FAILURE.

MEASURED ON PROD AND REPRODUCED LOCALLY, 2026-09-08. `WATI v3 media read failed` was the single largest
WhatsApp error on production — 146 rows in fourteen days — and one manual Refresh on one lead produced
ten more on demand. Every one was a 400 on
`/api/ext/v3/conversations/messages/file/{id}`.

Nothing was broken. The provider expires media after some months, and a rebuild walking a year of
history meets that constantly; the message still files, captioned "Media unavailable". What was wrong is
that we called it a failure: the read treats only 404 as "no file here", and this provider says it with
a 400, so `raise_for_status` turned an ordinary fact into an Error Log row per attachment.

THE STATUS CANNOT TELL THE TWO APART, so the body's own code does. Measured, both against a live tenant:

    {"code": 5004, "message": "Message Not Found"}    an expired June attachment  -> not our problem
    {"code": 400,  "message": "Message ID is invalid"} a wamid where the provider's own id belongs -> OURS

The second must stay loud. It is the shape a caller passing the wrong identity takes, and the only way
anyone would ever notice is the log this change is quietening.

Hermetic: the HTTP layer is stubbed with the exact bodies the provider returned.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.whatsapp.test_media_gone
"""
from unittest import mock

import frappe
import requests
from frappe.tests.utils import FrappeTestCase

from tatva_connect.whatsapp import transport

_ACCOUNT = "Media-gone-probe-account"


def _response(status, body=None, content=b"", content_type="application/pdf"):
	resp = mock.Mock(spec=requests.Response)
	resp.status_code = status
	resp.content = content
	resp.headers = {"content-type": content_type, "content-disposition": 'attachment; filename="x.pdf"'}
	resp.json.side_effect = (lambda: body) if body is not None else ValueError("no json")
	resp.raise_for_status.side_effect = (
		requests.HTTPError(f"{status} Client Error") if status >= 400 else None
	)
	resp.iter_content = lambda chunk_size=None: iter([content])
	return resp


class TestExpiredMediaIsNotAFailure(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if frappe.db.exists("WhatsApp Account", _ACCOUNT):
			frappe.delete_doc("WhatsApp Account", _ACCOUNT, force=True, ignore_permissions=True)
		cls.account = frappe.get_doc({
			"doctype": "WhatsApp Account", "account_name": _ACCOUNT, "status": "Active",
			"url": "https://live-mt-server.wati.io/000662", "token": "media-gone-token",
			"custom_provider": "WATI", "custom_wati_channel_number": "919900000662",
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.delete_doc("WhatsApp Account", _ACCOUNT, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def _read(self, response):
		with mock.patch.object(transport.requests, "get", return_value=response):
			return transport.fetch_message_media(self.account, "6a2bacd1d6055e112c1865f8")

	def test_expired_media_returns_nothing_and_raises_nothing(self):
		"""THE red: this raised, and every expired attachment in a year of history wrote an Error Log row."""
		gone = _response(400, {"code": 5004, "message": "Message Not Found",
		                       "timestamp": "2026-09-08T18:05:35Z"})
		self.assertIsNone(self._read(gone))

	def test_a_bad_message_id_still_raises(self):
		"""The half that must NOT be quietened: the provider is telling us we asked wrongly."""
		invalid = _response(400, {"code": 400, "message": "Message ID is invalid"})
		with self.assertRaises(requests.HTTPError):
			self._read(invalid)

	def test_a_400_with_an_unreadable_body_still_raises(self):
		"""Fail loud on anything unrecognised: silence is earned by a code we have seen, not assumed."""
		with self.assertRaises(requests.HTTPError):
			self._read(_response(400))

	def test_a_404_is_still_nothing_here(self):
		self.assertIsNone(self._read(_response(404)))

	def test_a_server_error_still_raises(self):
		"""A 500 is the provider being down, which the caller retries — never a message with no file."""
		with self.assertRaises(requests.HTTPError):
			self._read(_response(500))

	def test_media_that_IS_there_still_comes_back(self):
		"""The positive half. A read that quietens too much would pass every test above and lose every
		attachment the provider still holds."""
		ok = _response(200, content=b"%PDF-1.4 real bytes")
		content, content_type, filename = self._read(ok)
		self.assertEqual(content, b"%PDF-1.4 real bytes")
		self.assertEqual(content_type, "application/pdf")
		self.assertEqual(filename, "x.pdf")
