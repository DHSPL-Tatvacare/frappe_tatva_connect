# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A call reconcile signals the lead once when it ends, never once per filed row, and opens no journey; live capture still signals per call."""
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.channels import refresh
from tatva_connect.telephony import reconcile, writer
from tatva_connect.tests.telephony.fixtures import config

LEAD_PHONE = "9000054321"
DID = "9000007179"
PREFIX = "_test-quiet-reconcile"


def _record(call_id):
	"""One Acefone Call Detail Record, in the shape the live API returns."""
	return {
		"call_id": f"{PREFIX}-{call_id}",
		"uuid": f"uuid-{call_id}",
		"direction": "outbound",
		"call_hint": "dialer",
		"status": "answered",
		"client_number": f"+91{LEAD_PHONE}",
		"did_number": f"+91{DID}",
		"call_duration": 52,
		"date": "2026-07-12",
		"time": "17:34:58",
		"end_stamp": "2026-07-12 17:35:49",
		"hangup_cause": "disconnected_by_caller",
		"call_flow": [],
	}


class _Case(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		"""Telephony ships INERT: without an account, a mapped DID and a capture rule every record is declined."""
		super().setUpClass()
		config.ensure_account()
		cls.saved_rules = config.current_rules()
		cls.addClassCleanup(config.set_rules, cls.saved_rules)
		cls.addClassCleanup(config.clear_dids)
		config.set_rules([config.rule("Outbound", "Dialer"), config.rule("Inbound", "Dialer")])
		config.clear_dids()
		config.map_did(DID)

	def setUp(self):
		self._drop_calls()
		lead = frappe.get_doc({
			"doctype": "CRM Lead", "lead_name": "_Test Quiet Reconcile",
			"first_name": "_Test Quiet Reconcile", "mobile_no": LEAD_PHONE,
			"custom_vertical": config.GRAIN["vertical"],
		}).insert(ignore_permissions=True)
		frappe.db.commit()
		self.lead = lead.name
		# The DID's grain plus the phone is what attributes a call; without it every record files unattributed and the per-row signal this suite is about never fires.
		self.assertEqual(lead.custom_vertical, config.GRAIN["vertical"], "the lead must sit in the DID's grain")
		self.addCleanup(self._drop_calls)
		self.addCleanup(self._drop_lead)

	def _drop_lead(self):
		frappe.delete_doc("CRM Lead", self.lead, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _drop_calls(self):
		for row in frappe.get_all("CRM Call Log", filters={"id": ["like", f"{PREFIX}%"]}, pluck="name"):
			frappe.delete_doc("CRM Call Log", row, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _walk(self, rows):
		"""Run a real reconcile against a stubbed provider; return its summary, the telephony_call emits and the flag each write saw."""
		seen = []
		filed = reconcile.adapter.process

		def spy(payload, **kwargs):
			seen.append(frappe.flags.get("in_workflow"))
			return filed(payload, **kwargs)

		with mock.patch("tatva_connect.telephony.api.is_enabled", return_value=True), \
		     mock.patch("tatva_connect.telephony.routing.resolve_account_for_lead", return_value=config.ACCOUNT), \
		     mock.patch.object(reconcile, "_records_for_number", return_value=(rows, False)), \
		     mock.patch.object(reconcile.adapter, "process", side_effect=spy), \
		     mock.patch.object(frappe, "publish_realtime") as pub:
			summary = reconcile.reconcile_lead(self.lead, dry_run=False)
		emits = [c for c in pub.call_args_list if (c.args[0] if c.args else c.kwargs.get("event")) == "telephony_call"]
		return summary, emits, seen


class TestACallReconcileSignalsTheLeadOnce(_Case):
	def test_a_reconcile_of_many_calls_emits_one_signal(self):
		"""THE red: before the fix this emitted one signal per filed row, and every one refetched the whole tab."""
		summary, emits, _seen = self._walk([_record(i) for i in range(12)])
		self.assertEqual(summary["new"], 12, "every missing call must still be filed")
		self.assertEqual(len(emits), 1, "one signal for the lead, not one per filed row")
		self.assertEqual(emits[0].kwargs.get("docname"), self.lead)
		self.assertEqual(emits[0].kwargs.get("doctype"), "CRM Lead")

	def test_a_reconcile_with_nothing_new_emits_nothing(self):
		_summary, emits, _seen = self._walk([])
		self.assertEqual(len(emits), 0)

	def test_the_quiet_mode_is_released_when_the_reconcile_ends(self):
		"""A flag left set would silence every live call after it in the same worker."""
		self._walk([_record("release")])
		self.assertFalse(frappe.flags.get("tatva_bulk_history"))
		self.assertFalse(frappe.flags.get("in_workflow"))


class TestAPulledCallStartsNothing(_Case):
	def test_a_past_call_is_filed_with_the_entry_trigger_suppressed(self):
		"""`in_workflow` is the codebase's own entry-trigger suppressor, so a refresh cannot fire a journey built on CRM Call Log."""
		_summary, _emits, seen = self._walk([_record(i) for i in range(3)])
		self.assertEqual(len(seen), 3, "every record must reach the writer")
		self.assertTrue(all(seen), "a pulled call was filed as a live event")

	def test_a_live_call_still_signals(self):
		"""The mutation guard: the quiet mode must never reach the webhook's own path."""
		row = frappe.new_doc("CRM Call Log")
		row.update({"type": "Incoming", "status": "Completed",
		            "reference_doctype": "CRM Lead", "reference_docname": self.lead})
		with mock.patch.object(frappe, "publish_realtime") as pub:
			writer._publish(row)
		self.assertEqual(pub.call_count, 1, "a live call must still reach the open tab")
		self.assertEqual(pub.call_args.kwargs.get("docname"), self.lead)


class TestTheCallRefreshRunsLikeWhatsApp(_Case):
	def test_the_refresh_is_queued_on_long_under_the_record_lock(self):
		"""Same brain as WhatsApp: the long lane, and the job id that is the cross-user lock."""
		with mock.patch.object(frappe, "enqueue") as enqueue, mock.patch.object(frappe, "publish_realtime"):
			refresh.start("calls", "CRM Lead", self.lead)
		self.assertEqual(enqueue.call_args.kwargs.get("queue"), "long")
		self.assertTrue(enqueue.call_args.kwargs.get("deduplicate"))
		self.assertEqual(enqueue.call_args.kwargs.get("job_id"), f"tatva-refresh:calls:{self.lead}")

	def test_the_outcome_is_addressed_to_the_person_who_asked(self):
		"""A refresh is an ACTION: it goes to that user's own room, never the record's and never the site's."""
		with mock.patch.object(frappe, "enqueue"), mock.patch.object(frappe, "publish_realtime") as pub:
			refresh.start("calls", "CRM Lead", self.lead)
		self.assertEqual(pub.call_args.kwargs.get("user"), frappe.session.user)
		self.assertIsNone(pub.call_args.kwargs.get("doctype"))


class TestAReconcileAsksForTheLeadByNumber(_Case):
	def _asks(self):
		"""Every call_records request a reconcile makes, as kwargs."""
		with mock.patch("tatva_connect.telephony.api.is_enabled", return_value=True), \
		     mock.patch("tatva_connect.telephony.routing.resolve_account_for_lead", return_value=config.ACCOUNT), \
		     mock.patch("tatva_connect.telephony.api.get_call_records", return_value={"results": []}) as asked, \
		     mock.patch.object(frappe, "publish_realtime"):
			reconcile.reconcile_lead(self.lead, dry_run=True)
		return [c.kwargs for c in asked.call_args_list]

	def test_the_customer_number_goes_to_the_provider_both_ways(self):
		"""THE red: unfiltered, this walked the account's whole window and never reached a busy account's older calls."""
		asks = self._asks()
		self.assertEqual(len(asks), 2, "one ask per direction, not a paged sweep")
		self.assertEqual(
			sorted(k for ask in asks for k in ask if k in reconcile.CUSTOMER_FILTERS),
			["callerid", "destination"],
		)
		for ask in asks:
			self.assertIn(LEAD_PHONE, ask.values(), "the ask must name the lead's own number")
