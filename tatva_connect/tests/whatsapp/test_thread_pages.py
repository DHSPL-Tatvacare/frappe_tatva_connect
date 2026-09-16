# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A WhatsApp thread pages newest-first by (creation, name): every message exactly once, replies and reactions resolved across pages."""
from unittest import mock

import frappe
from crm.api.whatsapp import THREAD_PAGE_SIZE, get_whatsapp_messages

from tatva_connect.channels import event as channel_event
from tatva_connect.tests.whatsapp.test_history_scope import _NUMBER, _Case
from tatva_connect.whatsapp import ingest

_EPOCH = 1788000000


class TestTheThreadPages(_Case):
	def _file(self, index, at):
		"""One outbound row through the real ingest path, stamped with the provider's time as history is."""
		event = channel_event.build(
			channel="whatsapp", provider="WATI", account=self.solo, kind="outbound_echo", outcome="sent",
			provider_message_id=f"page-{index:04d}", subject_number=_NUMBER, text=f"m{index}", at=str(at), raw={},
		)
		with mock.patch("tatva_connect.whatsapp.ingest._targets", return_value=[self.lead.name]), \
		     mock.patch.object(frappe, "publish_realtime"):
			ingest.apply(event)

	def _message(self, **fields):
		doc = frappe.get_doc({
			"doctype": "WhatsApp Message", "type": "Outgoing", "message_type": "Manual", "content_type": "text",
			"to": _NUMBER, "whatsapp_account": self.solo, "status": "sent", **fields,
		})
		doc.flags.tatva_ingested = True  # already on the wire — never re-sent
		return doc.insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no user input

	def _walk(self):
		seen, before = [], None
		while page := get_whatsapp_messages("CRM Lead", self.lead.name, paged=1, before=before):
			self.assertLessEqual(len(page), THREAD_PAGE_SIZE)
			seen += [m["name"] for m in page]
			before = min(page, key=lambda m: (m["creation"], m["name"]))["name"]
		return seen

	def test_every_message_arrives_once_even_when_a_page_ends_inside_a_shared_timestamp(self):
		"""THE red for a creation-only cursor: ten rows per second puts a tie across every page boundary."""
		count = 2 * THREAD_PAGE_SIZE + 7
		for i in range(count):
			self._file(i, _EPOCH + i // 10)
		seen = self._walk()
		self.assertEqual(len(seen), count, "no message may be skipped")
		self.assertEqual(len(set(seen)), count, "no message may be repeated")

	def test_the_first_page_is_the_newest(self):
		for i in range(THREAD_PAGE_SIZE + 3):
			self._file(i, _EPOCH + i)
		page = get_whatsapp_messages("CRM Lead", self.lead.name, paged=1)
		self.assertIn(f"{self.lead.name}-page-{THREAD_PAGE_SIZE + 2:04d}", [m["name"] for m in page])
		self.assertNotIn(f"{self.lead.name}-page-0000", [m["name"] for m in page])

	def test_unpaged_is_the_stock_whole_thread(self):
		for i in range(THREAD_PAGE_SIZE + 3):
			self._file(i, _EPOCH + i)
		self.assertEqual(len(get_whatsapp_messages("CRM Lead", self.lead.name)), THREAD_PAGE_SIZE + 3)

	def test_a_reply_quoting_an_older_page_still_shows_its_original(self):
		self._message(message="the original", message_id="quoted-original")
		for i in range(THREAD_PAGE_SIZE + 2):
			self._message(message=f"filler {i}", message_id=f"filler-{i}")
		self._message(message="the reply", message_id="the-reply", is_reply=1, reply_to_message_id="quoted-original")
		page = get_whatsapp_messages("CRM Lead", self.lead.name, paged=1)
		reply = next(m for m in page if m["message_id"] == "the-reply")
		self.assertEqual(reply.get("reply_message"), "the original")

	def test_a_reaction_rides_on_its_message_and_never_takes_a_page_slot(self):
		self._message(message="react to me", message_id="reacted")
		self._message(message="👍", message_id="reaction-1", content_type="reaction", reply_to_message_id="reacted")
		page = get_whatsapp_messages("CRM Lead", self.lead.name, paged=1)
		self.assertNotIn("reaction", [m["content_type"] for m in page])
		self.assertEqual(next(m for m in page if m["message_id"] == "reacted").get("reaction"), "👍")
