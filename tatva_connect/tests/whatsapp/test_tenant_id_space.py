# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""ONE MESSAGE, ONE ROW — a receipt for a sibling number's message is not a new message.

MEASURED ON LIVE TRAFFIC, 2026-09-08. A free-text message was sent from a Liver Forever lead on the
Lipaglyn number. Seconds later the SAME message appeared a second time on a GoodFlip Support lead,
under the same provider id. The rep saw one programme's conversation inside another's inbox.

WHY IT HAPPENED. A provider mints message ids per TENANT, and one tenant carries many numbers, which
this app models as one account row per number. The sent and status events carry NO channel field — only
an inbound `message` names one — so the URL token is the only thing that says which row received an
echo, and WATI delivered it to the sibling's webhook. `rows_for_correlation` asked "is this id one of
MY account's?", the answer was no, the event fell through to attribution-by-number, and the message was
written again onto whichever lead the sibling routes to.

The lookup's own docstring already said ids are "minted per tenant". The scope applied was one level
narrower than the sentence describing it, and that gap is the defect.

WHAT THIS LOCKS. Two accounts on one tenant share an id space; two accounts on DIFFERENT tenants do
not, because an id from a foreign tenant means nothing here and treating it as ours would tick a row
some other tenant's traffic named. Both halves matter, so both are asserted.

Hermetic: no network, no provider call — this is a persistence-scope question and it is asked of the
functions that decide it.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.whatsapp.test_tenant_id_space
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.channels import event as channel_event
from tatva_connect.whatsapp import channel, ingest

_TENANT = "https://live-mt-server.wati.io/000446"
_OTHER_TENANT = "https://live-mt-server.wati.io/000999"
_SUPPORT = "Idspace-support"
_SIBLING = "Idspace-sibling"
_STRANGER = "Idspace-other-tenant"
_CORRELATION = "fdcfe673-f106-4650-idspace"
_PATIENT = "919900000441"


def _account(name, url, number, trailing_slash=False):
	if frappe.db.exists("WhatsApp Account", name):
		frappe.delete_doc("WhatsApp Account", name, force=True, ignore_permissions=True)
	return frappe.get_doc({
		"doctype": "WhatsApp Account", "account_name": name, "status": "Active",
		"url": url + ("/" if trailing_slash else ""), "token": "idspace-token",
		"custom_provider": "WATI", "custom_wati_channel_number": number,
		"custom_wati_multi_number": 1,
	}).insert(ignore_permissions=True).name  # authz-ok: tier-c — test fixture, no user input


class TestTheIdSpaceIsTheTenant(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# The sibling carries a TRAILING SLASH on purpose: an operator pasting one must not split the
		# tenant in two, which is the same paste that once 404'd every call (`transport.base_url`).
		cls.support = _account(_SUPPORT, _TENANT, "919900000771")
		cls.sibling = _account(_SIBLING, _TENANT, "919900000772", trailing_slash=True)
		cls.stranger = _account(_STRANGER, _OTHER_TENANT, "919900000773")
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		for name in (_SUPPORT, _SIBLING, _STRANGER):
			if frappe.db.exists("WhatsApp Account", name):
				frappe.delete_doc("WhatsApp Account", name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		self.addCleanup(frappe.db.rollback)

	def _sent_row(self, account):
		"""A row this account really sent, carrying the provider's correlation id."""
		doc = frappe.get_doc({
			"doctype": "WhatsApp Message", "type": "Outgoing", "message_type": "Manual",
			"message": "Test Liver OK", "content_type": "text", "to": _PATIENT,
			"message_id": _CORRELATION, "whatsapp_account": account, "status": "sent",
		})
		doc.flags.tatva_ingested = True  # already on the wire — the controller must not send it again
		return doc.insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input

	def _receipt(self, account):
		"""The echo/status event, delivered on `account`'s webhook. It names no channel, because the
		live sent and status payloads carry none."""
		return channel_event.build(
			channel="whatsapp", provider="WATI", account=account, kind="status",
			outcome="delivered", correlation_id=_CORRELATION, subject_number=_PATIENT,
			raw={"eventType": "sentMessageDELIVERED_v2", "localMessageId": _CORRELATION},
		)

	# ===================================================================== The defect =====================================================================
	def test_a_receipt_on_a_sibling_number_finds_the_message_we_sent(self):
		"""THE red. Asked per account this returned nothing, the event was read as a message we had never
		sent, and it was written a second time onto the sibling's lead."""
		row = self._sent_row(self.support)
		found = ingest.rows_for_correlation(self._receipt(self.sibling))
		self.assertEqual(found, [row.name], "a receipt for a sibling's message did not find it")

	def test_the_account_that_sent_it_still_finds_its_own(self):
		"""The unchanged half: widening the scope must not lose the ordinary case."""
		row = self._sent_row(self.support)
		self.assertEqual(ingest.rows_for_correlation(self._receipt(self.support)), [row.name])

	def test_a_receipt_from_a_DIFFERENT_tenant_finds_nothing(self):
		"""The other half of the boundary. An id minted by another tenant means nothing here, and
		matching it would tick this row from traffic that never named it."""
		self._sent_row(self.support)
		self.assertEqual(ingest.rows_for_correlation(self._receipt(self.stranger)), [])

	# ===================================================================== The scope itself =====================================================================
	def test_the_id_space_is_every_account_on_the_tenant(self):
		space = set(channel.id_space(self.support))
		self.assertIn(self.support, space)
		self.assertIn(self.sibling, space, "a trailing slash split one tenant into two id spaces")
		self.assertNotIn(self.stranger, space, "a foreign tenant was admitted into the id space")

	def test_an_account_with_no_url_answers_with_itself(self):
		"""Fail-safe: nothing to share means the old, narrowest behaviour."""
		name = "Idspace-urlless"
		if frappe.db.exists("WhatsApp Account", name):
			frappe.delete_doc("WhatsApp Account", name, force=True, ignore_permissions=True)
		doc = frappe.get_doc({
			"doctype": "WhatsApp Account", "account_name": name, "status": "Active",
			"custom_provider": "WATI", "custom_wati_channel_number": "919900000774",
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
		self.assertEqual(channel.id_space(doc.name), [doc.name])
