# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A REBUILD READS ONE NUMBER'S THREAD, NOT THE CONTACT'S.

MEASURED AGAINST THE LIVE PROVIDER, 2026-09-08. One contact, one account, two numbers:

    target = the bare phone .................. 41 messages — BOTH numbers' conversations
    target = "918511975757:<contact>" ........ 41 — genuinely all that number's own
    target = "919974306678:<contact>" ........ 16 — genuinely all that number's own

`backfill_lead` asked by the bare phone. On an account whose numbers are its own alone that is
harmless, because the contact holds one conversation. On an account carrying several it is a patient's
Liver Forever thread rebuilt out of a Support conversation — another programme's messages written onto
their record. Filtering the read was tried first and cannot work: `channel`, `channelPhoneNumber` and
`channelNumber` on the query each returned the same 41 items, and a history item carries no channel of
its own to filter on afterwards.

THE PROVIDER SCOPES THROUGH THE TARGET ITSELF — `<channel>:<contact>` — and the same rule addresses
every v3 conversation call, sends included. So there is ONE addressing rule and nothing to store: a
lead with no history yet is still refreshable, which a stored-conversation approach could not manage.

WHAT IS LOCKED HERE: that the rebuild names its own number, that an account with one number keeps
sending the bare contact it always sent, and that the wamid the send cannot know is filled from the
echo without ever overwriting one already held.

Hermetic: the transport walk is intercepted, so the assertion is the TARGET that would have gone to the
provider. No network.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.whatsapp.test_history_scope
"""
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.channels import event as channel_event
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist
from tatva_connect.whatsapp import backfill, ingest

_GRAIN = GRAINS[3]
_TENANT = "https://live-mt-server.wati.io/000555"
_SOLO = "Histscope-solo-account"
_SHARED_A = "Histscope-shared-a"
_SHARED_B = "Histscope-shared-b"
_NUMBER = "919900000551"
_CONVERSATION = "69120fc87a6c3112f91a721f"
_CORRELATION = "histscope-correlation"


def _account(name, url, channel_number, multi=False):
	if frappe.db.exists("WhatsApp Account", name):
		frappe.delete_doc("WhatsApp Account", name, force=True, ignore_permissions=True)
	return frappe.get_doc({
		"doctype": "WhatsApp Account", "account_name": name, "status": "Active",
		"url": url, "token": "histscope-token", "custom_provider": "WATI",
		"custom_wati_channel_number": channel_number, "custom_wati_multi_number": 1 if multi else 0,
	}).insert(ignore_permissions=True).name  # authz-ok: tier-c — test fixture, no user input


class _Case(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		assert_masters_exist()
		# Two accounts on ONE url are two numbers of one provider account; the third stands alone.
		cls.shared_a = _account(_SHARED_A, _TENANT, "919900000552", multi=True)
		cls.shared_b = _account(_SHARED_B, _TENANT, "919900000553", multi=True)
		cls.solo = _account(_SOLO, "https://live-mt-server.wati.io/000556", "919900000554")
		# ONE lead for the class: `_update_status` commits, so a per-test lead outlives the rollback and
		# the next test collides with it on the dedup index — the fixture failing to describe itself.
		for stale in frappe.get_all("CRM Lead", filters={"mobile_no": "+" + _NUMBER}, pluck="name"):
			frappe.db.delete("WhatsApp Message", {"reference_name": stale})
			frappe.delete_doc("CRM Lead", stale, force=True, ignore_permissions=True)
		cls.lead = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Histscope", "status": "New", "mobile_no": "+" + _NUMBER,
			"custom_vertical": _GRAIN["vertical"], "custom_group": _GRAIN["group"],
			"custom_current_program": _GRAIN["program"],
		}).insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.db.delete("WhatsApp Message", {"reference_name": cls.lead.name})
		frappe.delete_doc("CRM Lead", cls.lead.name, force=True, ignore_permissions=True)
		for name in (_SHARED_A, _SHARED_B, _SOLO):
			if frappe.db.exists("WhatsApp Account", name):
				frappe.delete_doc("WhatsApp Account", name, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		"""The rows are what each test builds, and a committed one must not reach the next test."""
		self.addCleanup(self._clear_rows)
		self._clear_rows()

	def _clear_rows(self):
		frappe.db.delete("WhatsApp Message", {"reference_name": self.lead.name})
		frappe.db.commit()

	def _row(self, account, conversation=None, wamid=None, correlation=_CORRELATION):
		"""One outbound row. The correlation id is per-row by default because `(message_id,
		reference_name)` is UNIQUE — two rows on one lead must not claim the same provider id."""
		doc = frappe.get_doc({
			"doctype": "WhatsApp Message", "type": "Outgoing", "message_type": "Manual",
			"message": "probe", "content_type": "text", "to": _NUMBER,
			"message_id": correlation, "whatsapp_account": account, "status": "sent",
			"conversation_id": conversation, "custom_outbound_wamid": wamid,
		})
		doc.flags.tatva_ingested = True  # already on the wire — never re-sent
		return doc.insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input

	def _target_used(self, account):
		"""The target the provider would really be asked for — read off the transport walk, below the
		adapter, so the scoping the adapter does is what is being asserted rather than mocked away."""
		seen = {}

		def _walk(_account_doc, target, **_kwargs):
			seen["target"] = target
			return iter(())

		with mock.patch("tatva_connect.whatsapp.transport.iter_conversation_messages", _walk), \
		     mock.patch("tatva_connect.whatsapp.routing.resolve_account_for_lead", return_value=account), \
		     mock.patch("tatva_connect.whatsapp.channel.is_enabled", return_value=True):
			result = backfill.backfill_lead(self.lead.name, dry_run=True)
		return seen.get("target"), result


class TestTheRebuildAimsAtOneNumber(_Case):
	def test_a_shared_account_names_its_own_number_in_the_target(self):
		"""THE red. Asked by the bare contact this returned the sibling number's messages too, and filed
		them onto this patient."""
		target, result = self._target_used(self.shared_a)
		self.assertEqual(target, f"919900000552:{_NUMBER}")
		self.assertTrue(result["ok"])

	def test_each_sibling_names_ITS_OWN_number_and_not_the_others(self):
		"""Sibling numbers are exactly what must not be mixed, so the target is the account's own."""
		self.assertEqual(self._target_used(self.shared_b)[0], f"919900000553:{_NUMBER}")

	def test_an_account_that_shares_no_numbers_asks_by_the_bare_contact(self):
		"""The no-regression half, and the shape production sends today: one number means the contact
		holds one conversation, so the bare contact is a true target and nothing is added to the wire."""
		target, result = self._target_used(self.solo)
		self.assertEqual(target, _NUMBER)
		self.assertTrue(result["ok"])

	def test_a_lead_with_no_history_yet_is_still_refreshable(self):
		"""Nothing is looked up before asking, so a patient nobody has messaged still rebuilds — the
		thing a stored-conversation approach could not do."""
		self.assertTrue(self._target_used(self.shared_a)[1]["ok"])


class TestTheEchoFillsWhatTheSendCouldNotKnow(_Case):
	"""A send knows neither the conversation nor the wamid; the provider hands both back afterwards."""

	def _echo(self, account, conversation="conv-from-echo", wamid="wamid-from-echo"):
		return channel_event.build(
			channel="whatsapp", provider="WATI", account=account, kind="status", outcome="delivered",
			correlation_id=_CORRELATION, subject_number=_NUMBER,
			conversation_id=conversation, wamid=wamid, raw={},
		)

	def test_the_echo_fills_a_blank_wamid(self):
		row = self._row(self.solo)
		ingest._update_status(self._echo(self.solo))
		stored = frappe.db.get_value(
			"WhatsApp Message", row.name, ["custom_outbound_wamid", "status"], as_dict=True
		)
		self.assertEqual(stored.custom_outbound_wamid, "wamid-from-echo")
		self.assertEqual(stored.status, "delivered")

	def test_the_echo_never_overwrites_a_wamid_the_row_already_holds(self):
		"""A later, weaker event must not replace an identity the send already captured."""
		row = self._row(self.solo, wamid="wamid-at-send")
		ingest._update_status(self._echo(self.solo))
		self.assertEqual(
			frappe.db.get_value("WhatsApp Message", row.name, "custom_outbound_wamid"), "wamid-at-send"
		)
