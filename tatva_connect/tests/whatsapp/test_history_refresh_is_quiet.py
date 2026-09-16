# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A history backfill signals the lead once when it ends, never once per filed row; live ingest still signals per message."""
from unittest import mock

import frappe

from tatva_connect.channels import event as channel_event
from tatva_connect.tests.whatsapp.test_history_scope import _NUMBER, _Case
from tatva_connect.whatsapp import backfill, ingest


def _echo(account, i):
	return channel_event.build(
		channel="whatsapp", provider="WATI", account=account, kind="outbound_echo", outcome="sent",
		provider_message_id=f"quiet-refresh-{i}", subject_number=_NUMBER, text=f"history {i}", raw={},
	)


class TestAHistoryRefreshSignalsTheLeadOnce(_Case):
	def _signals(self, call, events):
		"""Run `call` against a stubbed provider; return the whatsapp_message emits and the pauses taken."""
		adapter = mock.Mock()
		adapter.history.return_value = events
		adapter.normalize_history.side_effect = lambda item, **_kw: item
		with mock.patch("tatva_connect.whatsapp.routing.resolve_account_for_lead", return_value=self.solo), \
		     mock.patch("tatva_connect.whatsapp.channel.is_enabled", return_value=True), \
		     mock.patch("tatva_connect.whatsapp.backfill.resolve.adapter_for", return_value=adapter), \
		     mock.patch("tatva_connect.whatsapp.ingest._targets", return_value=[self.lead.name]), \
		     mock.patch("tatva_connect.whatsapp.backfill.time.sleep") as pause, \
		     mock.patch.object(frappe, "publish_realtime") as pub:
			call()
		emits = [c for c in pub.call_args_list if (c.args[0] if c.args else c.kwargs.get("event")) == "whatsapp_message"]
		return emits, pause.call_count

	def _filed(self):
		return frappe.db.count("WhatsApp Message", {"reference_name": self.lead.name})

	def test_a_backfill_of_many_rows_emits_one_signal(self):
		"""THE red: before the fix this emitted two signals per filed row."""
		count = 2 * backfill.HISTORY_CHUNK + 5
		events = [_echo(self.solo, i) for i in range(count)]
		emits, pauses = self._signals(lambda: backfill.backfill_lead(self.lead.name, dry_run=False), events)
		self.assertEqual(self._filed(), count, "every missing row must still be filed")
		self.assertEqual(len(emits), 1, "one signal for the lead, not one per filed row")
		self.assertEqual(emits[0].kwargs.get("docname"), self.lead.name)
		self.assertEqual(pauses, 2, "a pause after each full chunk")

	def test_the_quiet_mode_is_released_when_the_backfill_ends(self):
		"""A quiet flag left set would silence every live message after it in the same worker."""
		self._signals(lambda: backfill.backfill_lead(self.lead.name, dry_run=False), [_echo(self.solo, 0)])
		self.assertFalse(frappe.flags.get("tatva_bulk_history"))

	def test_a_live_message_still_signals(self):
		"""The mutation guard: the quiet mode must never reach ordinary ingest."""
		emits, _pauses = self._signals(lambda: ingest.apply(_echo(self.solo, "live")), [])
		self.assertEqual(self._filed(), 1)
		self.assertGreaterEqual(len(emits), 1, "a live message must still reach the open thread")

	def test_a_backfill_with_nothing_new_emits_nothing(self):
		emits, pauses = self._signals(lambda: backfill.backfill_lead(self.lead.name, dry_run=False), [])
		self.assertEqual((len(emits), pauses), (0, 0))


class TestTheRefreshRunsOnTheLongLane(_Case):
	def test_the_refresh_is_queued_on_long(self):
		from tatva_connect.api import whatsapp as api

		with mock.patch.object(frappe, "enqueue") as enqueue, mock.patch.object(frappe, "publish_realtime"):
			api.refresh_messages_from_wati("CRM Lead", self.lead.name)
		self.assertEqual(enqueue.call_args.kwargs.get("queue"), "long")
