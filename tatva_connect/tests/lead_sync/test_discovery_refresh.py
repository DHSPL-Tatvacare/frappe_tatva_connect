# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Discovery refreshes rather than skips, and a refresh never costs the operator their mapping.

Discovery used to run once, at source creation, and to skip any Page or form it had already stored. A
re-pasted token therefore never reached the Page the crawl reads, and a form edited by marketing was
never re-read, so a reworded question arrived as an answer with nowhere to land.

Graph is stubbed throughout: these assert OUR handling of its answer, never Facebook's availability.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.lead_sync.test_discovery_refresh
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.lead_sync import discovery, drift
from tatva_connect.lead_sync.form import IDENTITY_KEY

PAGE = "zz-refresh-page"
FORM = "zz-refresh-form"


def _question(key, label, options=None):
	return {"id": f"q-{key}", "key": key, "label": label, "type": "CUSTOM", "options": options or []}


class TestFormRefresh(FrappeTestCase):
	def setUp(self):
		for doctype, name in (("Facebook Lead Form", FORM), ("Facebook Page", PAGE)):
			if frappe.db.exists(doctype, name):
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
		frappe.get_doc({
			"doctype": "Facebook Page", "id": PAGE, "page_name": "ZZ Refresh Page",
			"category": "Health", "access_token": "zz-old-page-token", "account_id": "zz-account",
		}).insert(ignore_permissions=True)

	def tearDown(self):
		for doctype, name in (("Facebook Lead Form", FORM), ("Facebook Page", PAGE)):
			if frappe.db.exists(doctype, name):
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)

	def _store(self, questions, form_name="ZZ Form"):
		discovery.upsert_lead_form({"id": FORM, "name": form_name, "questions": questions}, PAGE)

	def _rows(self):
		return {
			r.key: r for r in frappe.get_doc("Facebook Lead Form", FORM).questions
		}

	def test_a_duplicated_form_inherits_the_mappings_its_sibling_already_carries(self):
		"""Duplicating is how a published form gets edited: the copy has a NEW id and arrives with every
		question unmapped — including the phone question, without which every lead from it is refused by
		the upsert and logged instead of stored. The copy carries the same question KEYS, so its sibling's
		mapping is what makes it work on arrival rather than after someone remembers to remap it."""
		self._store([_question("q_phone", "Phone")])
		form = frappe.get_doc("Facebook Lead Form", FORM)
		form.questions[0].mapped_to_crm_field = IDENTITY_KEY
		form.save(ignore_permissions=True)

		duplicate = "zz-refresh-form-copy"
		discovery.upsert_lead_form(
			{"id": duplicate, "name": "ZZ Form Copy", "questions": [_question("q_phone", "Phone")]}, PAGE
		)
		try:
			copied = frappe.get_doc("Facebook Lead Form", duplicate)
			self.assertEqual(
				copied.questions[0].mapped_to_crm_field, IDENTITY_KEY,
				"the duplicate must arrive with its phone question already mapped",
			)
		finally:
			frappe.delete_doc("Facebook Lead Form", duplicate, force=True, ignore_permissions=True)

	def test_a_key_mapped_differently_on_two_siblings_carries_nothing(self):
		"""Two sibling forms disagreeing about a key identifies nothing, so the copy shows it unmapped —
		a state the operator can see and correct — rather than inheriting one of the two decisions."""
		self._store([_question("q_phone", "Phone"), _question("q_extra", "Extra")])
		form = frappe.get_doc("Facebook Lead Form", FORM)
		{q.key: q for q in form.questions}["q_phone"].mapped_to_crm_field = IDENTITY_KEY
		{q.key: q for q in form.questions}["q_extra"].mapped_to_crm_field = "lead:first_name"
		form.save(ignore_permissions=True)

		rival = "zz-refresh-form-rival"
		discovery.upsert_lead_form(
			{"id": rival, "name": "ZZ Rival", "questions": [_question("q_phone", "Phone"), _question("q_extra", "Extra")]},
			PAGE,
		)
		try:
			doc = frappe.get_doc("Facebook Lead Form", rival)
			{q.key: q for q in doc.questions}["q_extra"].mapped_to_crm_field = "lead:last_name"
			doc.save(ignore_permissions=True)
			copy = "zz-refresh-form-third"
			discovery.upsert_lead_form(
				{"id": copy, "name": "ZZ Third", "questions": [_question("q_extra", "Extra")]}, PAGE
			)
			try:
				third = frappe.get_doc("Facebook Lead Form", copy)
				self.assertFalse(
					third.questions[0].mapped_to_crm_field,
					"a key its siblings map differently must carry nothing",
				)
			finally:
				frappe.delete_doc("Facebook Lead Form", copy, force=True, ignore_permissions=True)
		finally:
			frappe.delete_doc("Facebook Lead Form", rival, force=True, ignore_permissions=True)

	def test_a_new_form_is_stored_with_its_options(self):
		self._store([_question("age_group", "Age group", [{"key": "25_30", "value": "25-30"}])])
		row = self._rows()["age_group"]
		self.assertEqual(row.label, "Age group")
		self.assertEqual(frappe.parse_json(row.options)[0]["value"], "25-30")

	def test_an_existing_form_is_refreshed_not_skipped(self):
		"""The old code returned early on an existing form, so an added question was never seen."""
		self._store([_question("age_group", "Age group")])
		self._store([_question("age_group", "Age group"), _question("hba1c", "Latest HbA1c")])
		self.assertIn("hba1c", self._rows(), "a question added on Facebook must appear after a refresh")

	def test_a_removed_question_goes_away(self):
		self._store([_question("age_group", "Age group"), _question("hba1c", "Latest HbA1c")])
		self._store([_question("age_group", "Age group")])
		self.assertNotIn("hba1c", self._rows())

	def test_a_reworded_question_arrives_as_a_new_key(self):
		"""Marketing rewords far more often than it adds; the new wording is a new key, and unmapped."""
		self._store([_question("are_you_physical_active?", "Are you physical active?")])
		self._store([_question("are_you_physically_active?", "Are you physically active?")])
		rows = self._rows()
		self.assertIn("are_you_physically_active?", rows)
		self.assertNotIn("are_you_physical_active?", rows)

	def test_a_refresh_keeps_the_operator_mapping(self):
		"""A refresh replaces the question rows, so a mapping carried by key would otherwise be lost on
		every nightly pass, silently unmapping every form anyone had configured."""
		self._store([_question("phone_number", "Phone"), _question("age_group", "Age group")])
		form = frappe.get_doc("Facebook Lead Form", FORM)
		# The phone mapping is what the operator gate requires, so this is a real operator save.
		{q.key: q for q in form.questions}["phone_number"].mapped_to_crm_field = IDENTITY_KEY
		form.save(ignore_permissions=True)

		self._store([
			_question("phone_number", "Phone"),
			_question("age_group", "Age group"),
			_question("hba1c", "Latest HbA1c"),
		])
		rows = self._rows()
		self.assertEqual(rows["phone_number"].mapped_to_crm_field, IDENTITY_KEY)
		self.assertFalse(rows["hba1c"].mapped_to_crm_field, "a newly seen question starts unmapped")

	def test_a_mirror_write_is_not_blocked_by_the_operator_mapping_gate(self):
		"""Most discovered forms are unmapped, so a refresh that had to satisfy the operator gate would
		throw on nearly every form and the nightly pass would bring nothing back at all."""
		self._store([_question("age_group", "Age group")])
		self._store([_question("age_group", "Age group"), _question("hba1c", "Latest HbA1c")])
		self.assertIn("hba1c", self._rows())

	def test_a_form_reported_without_questions_keeps_the_ones_it_has(self):
		"""An absent `questions` key means Graph was not asked. Treating it as 'this form has no questions'
		destroyed every question row and every operator mapping on the next refresh, in silence."""
		self._store([_question("phone_number", "Phone"), _question("age_group", "Age group")])
		form = frappe.get_doc("Facebook Lead Form", FORM)
		{q.key: q for q in form.questions}["phone_number"].mapped_to_crm_field = IDENTITY_KEY
		form.save(ignore_permissions=True)

		discovery.upsert_lead_form({"id": FORM, "name": "ZZ Form"}, PAGE)

		rows = self._rows()
		self.assertEqual(sorted(rows), ["age_group", "phone_number"], "an unasked form must keep its questions")
		self.assertEqual(rows["phone_number"].mapped_to_crm_field, IDENTITY_KEY, "the mapping must survive too")

	def test_a_null_questions_value_is_read_the_same_way_as_an_absent_one(self):
		self._store([_question("age_group", "Age group")])
		discovery.upsert_lead_form({"id": FORM, "name": "ZZ Form", "questions": None}, PAGE)
		self.assertIn("age_group", self._rows())

	def test_an_explicitly_empty_question_list_does_clear_the_form(self):
		"""An empty list is Facebook's own answer that the form carries none, which is a different fact
		from not having been asked, and it is the one that is allowed to clear the rows."""
		self._store([_question("age_group", "Age group")])
		self._store([])
		self.assertEqual(self._rows(), {})

	def test_a_mapping_is_carried_by_question_id_across_a_rewording(self):
		"""Facebook's question id survives a rewording, so the operator's mapping does too."""
		self._store([{**_question("are_you_active?", "Are you active?"), "id": "q-stable"}])
		form = frappe.get_doc("Facebook Lead Form", FORM)
		form.questions[0].mapped_to_crm_field = IDENTITY_KEY
		form.save(ignore_permissions=True)

		self._store([{**_question("are_you_physically_active?", "Are you physically active?"), "id": "q-stable"}])
		self.assertEqual(self._rows()["are_you_physically_active?"].mapped_to_crm_field, IDENTITY_KEY)

	def test_two_questions_sharing_a_key_do_not_inherit_one_anothers_mapping(self):
		"""Last-wins applied ONE operator decision to a question it was never made for, and lost the other.
		An ambiguous key carries nothing, so the form shows both unmapped and the operator can see it."""
		self._store([
			{**_question("contact", "Phone"), "id": "q-one"},
			{**_question("contact", "Email"), "id": "q-two"},
		])
		form = frappe.get_doc("Facebook Lead Form", FORM)
		form.questions[0].mapped_to_crm_field = IDENTITY_KEY
		form.questions[1].mapped_to_crm_field = "lead:email"
		form.save(ignore_permissions=True)

		# Refreshed without ids, so `key` is the only thing left to match on and it names two questions.
		self._store([
			{**_question("contact", "Phone"), "id": None},
			{**_question("contact", "Email"), "id": None},
		])
		carried = [q.mapped_to_crm_field for q in frappe.get_doc("Facebook Lead Form", FORM).questions]
		self.assertEqual(carried, [None, None], f"an ambiguous key must carry nothing, carried {carried}")

	def test_duplicate_keys_that_agree_still_carry(self):
		self._store([
			{**_question("contact", "Phone"), "id": "q-one"},
			{**_question("contact", "Phone again"), "id": "q-two"},
		])
		form = frappe.get_doc("Facebook Lead Form", FORM)
		for row in form.questions:
			row.mapped_to_crm_field = IDENTITY_KEY
		form.save(ignore_permissions=True)

		self._store([
			{**_question("contact", "Phone"), "id": None},
			{**_question("contact", "Phone again"), "id": None},
		])
		carried = [q.mapped_to_crm_field for q in frappe.get_doc("Facebook Lead Form", FORM).questions]
		self.assertEqual(carried, [IDENTITY_KEY, IDENTITY_KEY])

	def test_the_options_of_a_changed_question_are_refreshed(self):
		self._store([_question("age_group", "Age group", [{"key": "25_30", "value": "25-30"}])])
		self._store([_question("age_group", "Age group", [{"key": "31_40", "value": "31-40"}])])
		self.assertEqual(frappe.parse_json(self._rows()["age_group"].options)[0]["value"], "31-40")


class TestPageRefresh(FrappeTestCase):
	def setUp(self):
		if frappe.db.exists("Facebook Page", PAGE):
			frappe.delete_doc("Facebook Page", PAGE, force=True, ignore_permissions=True)

	def tearDown(self):
		if frappe.db.exists("Facebook Page", PAGE):
			frappe.delete_doc("Facebook Page", PAGE, force=True, ignore_permissions=True)

	def _run(self, page_token):
		page = {"id": PAGE, "name": "ZZ Refresh Page", "category": "Health", "access_token": page_token}
		with patch.object(discovery, "graph_get") as graph, \
			patch.object(discovery, "_fetch_and_store_forms", return_value=[]):
			graph.side_effect = [{"id": "zz-account"}, {"data": [page]}]
			discovery.fetch_and_store_pages("zz-user-token")

	def test_a_re_pasted_token_reaches_the_page(self):
		"""The crawl reads the PAGE token, so a Page skipped on refresh keeps a credential already dead."""
		self._run("zz-page-token-1")
		self._run("zz-page-token-2")
		stored = frappe.utils.password.get_decrypted_password(
			"Facebook Page", PAGE, "access_token", raise_exception=False
		)
		self.assertEqual(stored, "zz-page-token-2", "a refreshed Page must carry the newly derived token")

	def test_the_page_is_not_duplicated_on_refresh(self):
		self._run("zz-page-token-1")
		self._run("zz-page-token-2")
		self.assertEqual(frappe.db.count("Facebook Page", {"id": PAGE}), 1)


class TestNightlyRefresh(FrappeTestCase):
	"""The 01:00 pass: dormant until switched on, one refresh per token, and one bad source never stops the rest.

	`refresh_all_sources` commits, so every row these tests create is removed explicitly rather than left
	to the test-case rollback.
	"""

	PREFIX = "zz-src-refresh"
	SHARED = "EAAzzshared0000000000token"
	OWN = "EAAzzown00000000000000token"

	def setUp(self):
		self.sources = []
		self._make("zz-src-refresh-a", self.SHARED)
		self._make("zz-src-refresh-b", self.SHARED)
		self._make("zz-src-refresh-c", self.OWN)

	def tearDown(self):
		for name in self.sources:
			if frappe.db.exists("Lead Sync Source", name):
				frappe.delete_doc("Lead Sync Source", name, force=True, ignore_permissions=True)
		frappe.db.delete("Error Log", {"method": ("like", f"%{self.PREFIX}%")})
		frappe.db.commit()

	def _make(self, name, token):
		"""Created disabled, then enabled straight in the table: enabling through the controller would run
		discovery against Facebook, and no test may reach it."""
		if frappe.db.exists("Lead Sync Source", name):
			frappe.delete_doc("Lead Sync Source", name, force=True, ignore_permissions=True)
		with patch("tatva_connect.lead_sync.source.fetch_and_store_pages", return_value=[]), \
			patch("tatva_connect.lead_sync.source.refresh_credential", return_value=None):
			frappe.get_doc({
				"doctype": "Lead Sync Source", "name": name, "type": "Facebook",
				"access_token": token, "background_sync_frequency": "Daily", "enabled": 0,
			}).insert(ignore_permissions=True)
		frappe.db.set_value("Lead Sync Source", name, "enabled", 1)
		frappe.db.commit()
		self.sources.append(name)

	def _run(self, enabled=True, failing_token=None):
		"""Returns the tokens this pass actually refreshed, in order."""
		seen = []

		def _record(token):
			seen.append(token)
			if token == failing_token:
				raise RuntimeError("refresh boom")

		with patch("tatva_connect.automation.is_enabled", return_value=enabled), \
			patch.object(discovery, "fetch_and_store_pages", _record):
			discovery.refresh_all_sources()
		return [t for t in seen if t in (self.SHARED, self.OWN)]

	def test_the_pass_does_nothing_while_the_operator_switch_is_off(self):
		"""Every automation in this app is an operator toggle, and this one ran whatever the toggle said."""
		self.assertEqual(self._run(enabled=False), [], "a dormant automation must do no work")

	def test_two_sources_sharing_a_token_are_refreshed_once(self):
		self.assertEqual(sorted(self._run()), sorted([self.OWN, self.SHARED]))

	def test_a_failing_source_does_not_stop_the_ones_after_it(self):
		refreshed = self._run(failing_token=self.SHARED)
		self.assertIn(self.OWN, refreshed, "one unreadable source must not cost the rest their refresh")

	def test_a_failure_is_logged_without_the_token(self):
		self._run(failing_token=self.SHARED)
		logged = "\n".join(
			frappe.get_all(
				"Error Log", filters={"method": ("like", f"%{self.PREFIX}%")}, pluck="error"
			) or [""]
		)
		self.assertTrue(logged.strip(), "a failed source has to leave a trace")
		self.assertNotIn(self.SHARED, logged, "the access token must never reach the Error Log in plaintext")


class TestQuestionDrift(FrappeTestCase):
	"""A form that keeps its id but changes its questions is drift a form-id comparison cannot see."""

	SOURCE = "zz-src-question-drift"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("Facebook Page", PAGE):
			frappe.get_doc({
				"doctype": "Facebook Page", "id": PAGE, "page_name": "ZZ Refresh Page",
				"category": "Health", "access_token": "zz-page-token", "account_id": "zz-account",
			}).insert(ignore_permissions=True)
		# The source Links to the form, so the form exists before it rather than only in setUp.
		discovery.upsert_lead_form(
			{"id": FORM, "name": "ZZ Form", "questions": [_question("age_group", "Age group")]}, PAGE
		)
		# A real DORMANT source: the log's `source` is a Link, so a stand-in dict cannot be logged against.
		with patch("tatva_connect.lead_sync.source.fetch_and_store_pages", return_value=[]), \
			patch("tatva_connect.lead_sync.source.refresh_credential", return_value=None):
			if not frappe.db.exists("Lead Sync Source", cls.SOURCE):
				frappe.get_doc({
					"doctype": "Lead Sync Source", "name": cls.SOURCE, "type": "Facebook",
					"access_token": "zz-token", "facebook_lead_form": FORM,
					"background_sync_frequency": "Daily", "enabled": 0,
				}).insert(ignore_permissions=True)

	@classmethod
	def tearDownClass(cls):
		frappe.db.delete("Failed Lead Sync Log", {"source": cls.SOURCE})
		frappe.delete_doc("Lead Sync Source", cls.SOURCE, force=True, ignore_permissions=True)
		for doctype, name in (("Facebook Lead Form", FORM), ("Facebook Page", PAGE)):
			if frappe.db.exists(doctype, name):
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
		# The drift log commits, so these fixtures outlive the case rollback and the removal must commit too.
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		# The drift log is a real insert that outlives a rollback, so each test asserts only its own.
		frappe.db.delete("Failed Lead Sync Log", {"source": self.SOURCE})
		discovery.upsert_lead_form(
			{"id": FORM, "name": "ZZ Form", "questions": [_question("age_group", "Age group")]}, PAGE
		)
		self.source = frappe.get_doc("Lead Sync Source", self.SOURCE)

	def _live(self, keys):
		return [{"id": FORM, "questions": [_question(k, k) for k in keys]}]

	def _payloads(self):
		return [
			frappe.parse_json(row)
			for row in frappe.get_all(
				"Failed Lead Sync Log",
				filters={"source": self.SOURCE, "type": drift.LOG_TYPE_QUESTIONS},
				pluck="lead_data",
			)
		]

	def test_an_unchanged_question_set_is_not_drift(self):
		with patch.object(drift, "list_forms", return_value=self._live(["age_group"])):
			self.assertFalse(drift.report_form_drift(self.source))
		self.assertEqual(self._payloads(), [])

	def test_an_added_question_is_reported(self):
		with patch.object(drift, "list_forms", return_value=self._live(["age_group", "hba1c"])):
			self.assertTrue(drift.report_form_drift(self.source))
		payloads = self._payloads()
		self.assertEqual(len(payloads), 1, "drift raises one log per crawl, not one per question")
		self.assertIn("hba1c", payloads[0]["added"])

	def test_a_removed_question_is_reported(self):
		with patch.object(drift, "list_forms", return_value=self._live(["hba1c"])):
			self.assertTrue(drift.report_form_drift(self.source))
		self.assertIn("age_group", self._payloads()[0]["removed"])

	def test_a_listing_without_questions_is_not_read_as_every_question_removed(self):
		"""An absent `questions` key means Graph was not asked, which is not a form carrying none."""
		with patch.object(drift, "list_forms", return_value=[{"id": FORM}]):
			self.assertFalse(drift.report_form_drift(self.source))
		self.assertEqual(self._payloads(), [])

	def test_the_same_drift_is_reported_once_however_often_the_crawl_runs(self):
		"""The crawl runs every five to fifteen minutes and finds the same drift every pass, so an
		unconditional insert buried the log under up to 288 identical rows a day."""
		with patch.object(drift, "list_forms", return_value=self._live(["age_group", "hba1c"])):
			for _ in range(4):
				self.assertTrue(drift.report_form_drift(self.source))
		self.assertEqual(len(self._payloads()), 1, "one persisting drift must leave one row, not one per crawl")

	def test_a_drift_that_changes_is_reported_again(self):
		"""Silence has to mean 'nothing new', so a second question going astray still reaches the operator."""
		with patch.object(drift, "list_forms", return_value=self._live(["age_group", "hba1c"])):
			drift.report_form_drift(self.source)
		with patch.object(drift, "list_forms", return_value=self._live(["age_group", "hba1c", "weight"])):
			drift.report_form_drift(self.source)
		payloads = self._payloads()
		self.assertEqual(len(payloads), 2)
		self.assertIn("weight", payloads[-1]["added"] + payloads[0]["added"])

	def test_the_drift_is_reported_again_after_the_log_is_cleared(self):
		"""Clearing the log is how an operator says 'seen', and it must not silence a live problem."""
		with patch.object(drift, "list_forms", return_value=self._live(["age_group", "hba1c"])):
			drift.report_form_drift(self.source)
			frappe.db.delete("Failed Lead Sync Log", {"source": self.SOURCE})
			frappe.db.commit()
			drift.report_form_drift(self.source)
		self.assertEqual(len(self._payloads()), 1)
