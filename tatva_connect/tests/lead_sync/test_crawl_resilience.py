# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""One bad lead must not take the batch, and the watermark must not run ahead of what was handled.

Two failures that looked identical from outside — "leads stopped arriving" — and had nothing in common:

  * A malformed payload threw OUTSIDE the per-lead try (`answers()` indexes Graph's own `field_data`),
    escaped the loop, and rolled the whole pass back: every lead already written in that pass, AND the
    failure log meant to say which one broke. The next pass refetched the same batch, hit the same lead,
    rolled back again. The form wedged for good, signalled only by a repeating pair of Error Log rows.
  * The watermark was stamped with `now()` after the loop, so a crawl that died half way still claimed
    every lead up to the clock and the ones it never reached were skipped for ever.

These drive `sync()` itself, which COMMITS per lead by design — so this case cannot use the savepoint
rollback the other suites do. It cleans up explicitly, the way `test_form_drift` does for the drift log.

Graph is never called: `fetch_leads` is handed the payloads it would have returned.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.lead_sync.test_crawl_resilience
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.lead_sync.form import IDENTITY_KEY
from tatva_connect.lead_sync.source import TatvaFacebookSyncSource
from tatva_connect.tests.api import partner_fixture

PHONE_GOOD = "+916100030001"
PHONE_LATER = "+916100030002"


def _graph_lead(lead_id, phone, created_time):
	return {
		"id": lead_id,
		"created_time": created_time,
		"field_data": [{"name": "q_phone", "values": [phone]}],
	}


def _poison(lead_id):
	"""A payload Graph would never send but a partial response can: an answer with no `name`.
	`answers()` reads `a["name"]`, which is the line that used to escape the per-lead guard."""
	return {"id": lead_id, "created_time": "2026-07-20T09:00:00+0530", "field_data": [{"values": ["x"]}]}


class TestCrawlResilience(FrappeTestCase):
	PAGE = "zz-crawl-page"
	FORM = "zz-crawl-form"
	SOURCE = "zz-crawl-src"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._purge_leads()
		partner_fixture.mint_grain()
		cls.contract = frappe.get_doc({
			"doctype": "CRM Lead API Mapping", "contract_name": cls.SOURCE, "enabled": 1,
			"is_internal": 0, "vertical": partner_fixture.VERTICAL, "crm_group": partner_fixture.GROUP,
		}).insert(ignore_permissions=True).name

		if not frappe.db.exists("Facebook Page", cls.PAGE):
			frappe.get_doc({
				"doctype": "Facebook Page", "id": cls.PAGE, "page_name": "ZZ Crawl Page",
				"category": "Test", "access_token": "zz-token", "account_id": "zz-account",
			}).insert(ignore_permissions=True)
		if not frappe.db.exists("Facebook Lead Form", cls.FORM):
			frappe.get_doc({
				"doctype": "Facebook Lead Form", "id": cls.FORM, "page": cls.PAGE,
				"form_name": "ZZ Crawl Form",
				"questions": [{"key": "q_phone", "label": "Phone", "mapped_to_crm_field": IDENTITY_KEY}],
			}).insert(ignore_permissions=True)
		with patch("tatva_connect.lead_sync.source.fetch_and_store_pages", return_value=[]), \
		     patch("tatva_connect.lead_sync.source.refresh_credential", return_value=None):
			if not frappe.db.exists("Lead Sync Source", cls.SOURCE):
				frappe.get_doc({
					"doctype": "Lead Sync Source", "name": cls.SOURCE, "type": "Facebook",
					"access_token": "zz-token", "facebook_lead_form": cls.FORM,
					"api_mapping": cls.contract, "background_sync_frequency": "Daily", "enabled": 0,
				}).insert(ignore_permissions=True)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		cls._purge_leads()
		frappe.db.delete("Failed Lead Sync Log", {"source": cls.SOURCE})
		frappe.delete_doc("Lead Sync Source", cls.SOURCE, force=True, ignore_permissions=True)
		frappe.delete_doc("Facebook Lead Form", cls.FORM, force=True, ignore_permissions=True)
		frappe.delete_doc("Facebook Page", cls.PAGE, force=True, ignore_permissions=True)
		frappe.delete_doc("CRM Lead API Mapping", cls.contract, force=True, ignore_permissions=True)
		partner_fixture.teardown()
		frappe.db.commit()
		super().tearDownClass()

	@classmethod
	def _purge_leads(cls):
		for name in frappe.get_all("CRM Lead", filters={"mobile_no": ["like", "+91610003%"]}, pluck="name"):
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)

	def setUp(self):
		frappe.set_user("Administrator")
		self._reset()

	def tearDown(self):
		frappe.set_user("Administrator")
		self._reset()

	def _reset(self):
		"""`sync()` commits, so nothing here is undone by a rollback — each test clears what it wrote."""
		self._purge_leads()
		frappe.db.delete("Failed Lead Sync Log", {"source": self.SOURCE})
		frappe.db.set_value("Lead Sync Source", self.SOURCE, "last_synced_at", None)
		frappe.db.commit()

	def _fold(self):
		return TatvaFacebookSyncSource("zz-token", self.FORM, source_name=self.SOURCE)

	def _crawl(self, leads):
		fold = self._fold()
		with patch.object(TatvaFacebookSyncSource, "fetch_leads", return_value=leads):
			fold.sync()

	def _lead_of(self, facebook_lead_id):
		return frappe.db.get_value("CRM Lead", {"facebook_lead_id": facebook_lead_id}, "name")

	def _failure_logs(self):
		return frappe.get_all(
			"Failed Lead Sync Log", filters={"source": self.SOURCE}, fields=["type", "lead_data"]
		)

	# -- one bad lead must not take the batch ---------------------------------

	def test_a_lead_after_a_poison_one_still_lands(self):
		"""The poison lead is FIRST, so the good one only survives if the guard let the loop continue."""
		self._crawl([_poison("fb-p1"), _graph_lead("fb-g1", PHONE_GOOD, "2026-07-20T10:00:00+0530")])
		self.assertTrue(self._lead_of("fb-g1"), "the lead after the poison one must still be written")

	def test_a_lead_written_before_a_poison_one_survives_it(self):
		"""The rollback used to reach BACKWARDS: a lead already committed in the pass was discarded too."""
		self._crawl([_graph_lead("fb-g2", PHONE_GOOD, "2026-07-20T10:00:00+0530"), _poison("fb-p2")])
		self.assertTrue(self._lead_of("fb-g2"), "a lead written before the failure must survive it")

	def test_the_failure_log_names_the_lead_without_keeping_the_patient(self):
		"""Upstream stored the ENTIRE Graph payload — name, phone, every screening answer — as JSON in a
		log doctype carrying its default permissions. An operator needs the id to re-fetch the lead from
		Meta and see what happened; the patient's body of answers is not theirs to keep in a log."""
		payload = _poison("fb-p9")
		payload["field_data"] = [{"name": "q_phone", "values": [PHONE_GOOD]}, {"values": ["broken"]}]
		self._crawl([payload])
		logs = self._failure_logs()
		self.assertEqual(len(logs), 1)
		stored = logs[0]["lead_data"]
		self.assertIn("fb-p9", stored, "the log must still name which lead failed")
		self.assertNotIn(PHONE_GOOD, stored, "and must not keep the patient's phone number")
		self.assertNotIn("broken", stored, "nor what they answered")

	def test_the_failure_leaves_a_durable_trace(self):
		"""`create_failure_log` alone is written INSIDE the doomed transaction and dies with it, which is
		why a wedged form left no record of which lead broke it. Roll back, log, then commit."""
		self._crawl([_poison("fb-p3")])
		logs = self._failure_logs()
		self.assertEqual(len(logs), 1, "the failed lead must leave exactly one durable log row")
		self.assertIn("fb-p3", logs[0]["lead_data"], "and the row must name the lead that failed")

	def test_a_poison_lead_does_not_wedge_the_next_pass(self):
		"""The wedge: the pass rolled back, the watermark never moved, so the next pass refetched the same
		batch and died the same way — forever. A second pass must make progress."""
		self._crawl([_poison("fb-p4"), _graph_lead("fb-g4", PHONE_GOOD, "2026-07-20T10:00:00+0530")])
		self._crawl([_poison("fb-p4"), _graph_lead("fb-g5", PHONE_LATER, "2026-07-20T11:00:00+0530")])
		self.assertTrue(self._lead_of("fb-g5"), "the second pass must still ingest")

	# -- the heaviest call is not made on every pass ---------------------------

	def test_the_form_listing_is_asked_for_once_a_day_not_once_a_crawl(self):
		"""The drift check lists the Page's ENTIRE form set with every question expanded. It ran on every
		crawl pass — 288 a day per source at a 5-minute frequency — against a quota Meta prices by cost and
		charges for failed calls too. Marketing publishes a form every few weeks, so once a day is the
		honest cadence; the check itself is untouched, the caller just stops asking it so often."""
		from tatva_connect.lead_sync import source as source_mod

		frappe.cache().delete_value(f"{source_mod.DRIFT_CHECK_CACHE}:{self.SOURCE}")
		doc = frappe.get_doc("Lead Sync Source", self.SOURCE)
		self.assertTrue(doc.drift_check_due(), "the first pass of the day asks Facebook")
		self.assertFalse(doc.drift_check_due(), "every pass after it must not")
		self.assertFalse(doc.drift_check_due(), "and still must not")

	# -- a source cannot be enabled onto a form that cannot produce a lead -----

	def test_a_source_cannot_be_enabled_while_its_phone_question_is_unmapped(self):
		"""`CRMFacebookLeadForm` holds this rule too, but stands itself down for a DISCOVERY write — which
		is exactly how a duplicated form arrives, with a new id and nothing mapped. Without this second
		door the source enables cleanly and every lead it crawls is refused by the upsert and logged."""
		from tatva_connect.lead_sync import discovery

		unmapped = "zz-crawl-form-unmapped"
		discovery.upsert_lead_form(
			{"id": unmapped, "name": "ZZ Unmapped",
			 "questions": [{"id": "q-other", "key": "q_other", "label": "Other", "type": "CUSTOM"}]},
			self.PAGE,
		)
		try:
			with patch("tatva_connect.lead_sync.source.fetch_and_store_pages", return_value=[]), \
			     patch("tatva_connect.lead_sync.source.refresh_credential", return_value=None):
				with self.assertRaises(frappe.ValidationError):
					frappe.get_doc({
						"doctype": "Lead Sync Source", "name": "zz-crawl-src-unmapped", "type": "Facebook",
						"access_token": "zz-token", "facebook_lead_form": unmapped,
						"api_mapping": self.contract, "background_sync_frequency": "Daily", "enabled": 1,
					}).insert(ignore_permissions=True)
		finally:
			frappe.db.rollback()
			if frappe.db.exists("Facebook Lead Form", unmapped):
				frappe.delete_doc("Facebook Lead Form", unmapped, force=True, ignore_permissions=True)
			frappe.db.commit()

	# -- the watermark ---------------------------------------------------------

	def test_the_watermark_stops_at_the_newest_lead_handled(self):
		"""Upstream stamped `now()`, so a crawl claimed leads it had never seen. It must name the newest
		lead this pass actually handled — read back through the same tz conversion that wrote it."""
		self._crawl([
			_graph_lead("fb-g6", PHONE_GOOD, "2026-07-20T10:00:00+0530"),
			_graph_lead("fb-g7", PHONE_LATER, "2026-07-20T11:30:00+0530"),
		])
		stamped = frappe.db.get_value("Lead Sync Source", self.SOURCE, "last_synced_at")
		self.assertEqual(
			frappe.utils.get_datetime(stamped), frappe.utils.get_datetime("2026-07-20 11:30:00"),
			"the watermark must be the newest lead handled, not the wall clock",
		)

	def test_an_empty_pass_leaves_the_watermark_alone(self):
		"""Nothing handled is not progress: a pass that returns no leads must not move the mark forward."""
		frappe.db.set_value("Lead Sync Source", self.SOURCE, "last_synced_at", "2026-07-19 08:00:00")
		frappe.db.commit()
		self._crawl([])
		stamped = frappe.db.get_value("Lead Sync Source", self.SOURCE, "last_synced_at")
		self.assertEqual(frappe.utils.get_datetime(stamped), frappe.utils.get_datetime("2026-07-19 08:00:00"))

	# -- a failed lead stays recoverable, WITH its grain -----------------------

	def test_a_retry_refetches_the_lead_and_stamps_the_contract_grain(self):
		"""Upstream replayed the stored blob through a bare `FacebookSyncSource`: against an id-only reference that raises KeyError, and had it not, the lead would have landed with no vertical and no group. The grain assertions are the point; `Synced` merely proves the button finished."""
		self._crawl([_poison("fb-r1")])
		logs = frappe.get_all("Failed Lead Sync Log", filters={"source": self.SOURCE}, pluck="name")
		self.assertEqual(len(logs), 1, "the poison lead must leave exactly one failure log to retry")
		self.assertFalse(self._lead_of("fb-r1"), "the poison lead must not have landed on the crawl")

		# Meta still holds the lead; only the fetch is stubbed, so the fold and its routing are entirely real.
		payload = _graph_lead("fb-r1", PHONE_GOOD, "2026-07-20T10:00:00+0530")
		with patch.object(TatvaFacebookSyncSource, "fetch_one_lead", return_value=payload):
			frappe.get_doc("Failed Lead Sync Log", logs[0]).retry_sync()

		name = self._lead_of("fb-r1")
		self.assertTrue(name, "a retried lead must land")
		lead = frappe.get_doc("CRM Lead", name)
		self.assertEqual(lead.custom_vertical, partner_fixture.VERTICAL,
		                 "a retried lead must carry the CONTRACT's vertical, exactly as a crawled one does")
		self.assertEqual(lead.custom_group, partner_fixture.GROUP,
		                 "a retried lead must carry the CONTRACT's group, exactly as a crawled one does")
		self.assertEqual(frappe.db.get_value("Failed Lead Sync Log", logs[0], "type"), "Synced")

	def test_a_retry_of_an_already_synced_row_is_refused(self):
		"""Re-pressing the button on a landed lead surfaced a UniqueValidationError, which reads as a fault when the honest answer is that this lead already came in."""
		self._crawl([_poison("fb-r2")])
		log = frappe.get_all("Failed Lead Sync Log", filters={"source": self.SOURCE}, pluck="name")[0]
		frappe.db.set_value("Failed Lead Sync Log", log, "type", "Synced")
		frappe.db.commit()
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc("Failed Lead Sync Log", log).retry_sync()
