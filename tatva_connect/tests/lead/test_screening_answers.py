# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A screening answer is kept exactly as it was asked, mapped to nothing, and nothing said is thrown away.

A lead answered nine clinical questions on a Facebook form and the app kept four contact fields. Every
clinical answer was dropped at the "unmapped" line, and a checkbox answer that survived kept only its
first value: a patient who ticked high cholesterol AND fatty liver was recorded as having one of them.
Both are wrong clinical records, not cosmetic bugs.

Answers are now held one row per question, and the question is its own identity. Nothing is declared,
entitled or bound first: a form is enabled with only its phone question mapped and every other answer it
collects is stored, readable and selectable. The identity is a digest of the question rather than its
text, because a question key is unbounded and any index prefix is a length a longer one can exceed.
ADR 0005.

Graph is never called: `sync_single_lead` is handed the lead payload Graph would have returned, copied
from a real /{form_id}/leads response.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.lead.test_screening_answers
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import cstr

from tatva_connect.access import entitlement
from tatva_connect.api import partner
from tatva_connect.lead import detail, keyvalue
from tatva_connect.lead_sync.form import IDENTITY_KEY
from tatva_connect.lead_sync.source import TatvaFacebookSyncSource
from tatva_connect.smartview import api as smartview
from tatva_connect.tests.api import partner_fixture

ANSWERS_TABLE = "custom_screening_answers"
PHONE = "+916100020001"

# The wording marketing shipped. A question is stored under its own wording, so a rewording is simply a
# different question, which is the honest reading: it was asked differently.
RAW_HBA1C = "do_you_know_your_latest_hba1c_level?_(hba1c_is_the_key_marker)"
RAW_CONDITIONS = "have_you_been_diagnosed_with_any_of_these_conditions?"
RAW_TREATMENT = "what_is_your_current_diabetes_treatment?"
RAW_AGE = "what_is_your_age_group?_(diabetes_affects_different_ages_differently)"

# A grain the fixture never mints, for asserting what a viewer outside the lead's grain is refused.
OUT_OF_GRAIN = {("ZZ Other Vertical", "ZZ Other Group", "")}

_CACHE_BUCKETS = (
	"tatva_connect:smartview_catalog", "tatva_connect:smartview_sections", "tatva_connect:smartview_answers",
	"tatva_connect:internal_contract_ticks", "tatva_connect:internal_universal_fields",
	"tatva_connect:field_restrictions", "tatva_connect:entitled_grains",
)


def _forget_caches():
	"""The catalog is request-cached; a test that edits it inside one request must drop what it read."""
	frappe.cache().delete_value(partner._CATALOG_CACHE_KEY)
	for bucket in _CACHE_BUCKETS:
		setattr(frappe.local, bucket, None)


def _purge_test_leads():
	for name in frappe.get_all("CRM Lead", filters={"mobile_no": ["like", "+91610002%"]}, pluck="name"):
		frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)


def _graph_lead(lead_id, answers):
	"""The payload Graph returns for one lead. `answers` maps a raw question to its values; a question
	with None for its values is one the person SKIPPED, which Graph sends with no `values` key at all."""
	return {
		"id": lead_id,
		"field_data": [
			{"name": raw} if values is None else {"name": raw, "values": values}
			for raw, values in answers.items()
		],
	}


class TestScreeningAnswers(FrappeTestCase):
	PAGE = "zz-screen-page"
	FORM = "zz-screen-form"
	SOURCE = "zz-screen-src"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		_purge_test_leads()
		partner_fixture.mint_grain()
		# Nothing is declared for screening, deliberately: the fixture below is a form and a contract,
		# and every assertion after it is what lands without a catalog row existing at all.
		cls.grain = (partner_fixture.VERTICAL, partner_fixture.GROUP, "")
		cls.contract = cls._mint_contract(cls.SOURCE, is_internal=0)

		if not frappe.db.exists("Facebook Page", cls.PAGE):
			frappe.get_doc({
				"doctype": "Facebook Page", "id": cls.PAGE, "page_name": "ZZ Screen Page",
				"category": "Test", "access_token": "zz-token", "account_id": "zz-account",
			}).insert(ignore_permissions=True)
		if not frappe.db.exists("Facebook Lead Form", cls.FORM):
			frappe.get_doc({
				"doctype": "Facebook Lead Form", "id": cls.FORM, "page": cls.PAGE,
				"form_name": "ZZ Screen Form",
				"questions": [
					{"key": "q_phone", "label": "Phone", "mapped_to_crm_field": IDENTITY_KEY},
					{"key": RAW_HBA1C, "label": "Latest HbA1c"},
					{"key": RAW_CONDITIONS, "label": "Conditions"},
					{"key": RAW_TREATMENT, "label": "Treatment"},
					{"key": RAW_AGE, "label": "Age group"},
				],
			}).insert(ignore_permissions=True)
		# Graph is never asked: discovery and the credential refresh are the two calls a save would make.
		with patch("tatva_connect.lead_sync.source.fetch_and_store_pages", return_value=[]), \
		     patch("tatva_connect.lead_sync.source.refresh_credential", return_value=None):
			if not frappe.db.exists("Lead Sync Source", cls.SOURCE):
				frappe.get_doc({
					"doctype": "Lead Sync Source", "name": cls.SOURCE, "type": "Facebook",
					"access_token": "zz-token", "facebook_lead_form": cls.FORM,
					"api_mapping": cls.contract, "background_sync_frequency": "Daily", "enabled": 0,
				}).insert(ignore_permissions=True)
		frappe.db.commit()  # the fold resolves its contract live, past the per-test rollback

	@classmethod
	def _mint_contract(cls, contract_name, is_internal):
		existing = frappe.db.get_value("CRM Lead API Mapping", {"contract_name": contract_name}, "name")
		if existing:
			return existing
		doc = frappe.get_doc({
			"doctype": "CRM Lead API Mapping", "contract_name": contract_name, "enabled": 1,
			"is_internal": is_internal, "vertical": partner_fixture.VERTICAL,
			"crm_group": partner_fixture.GROUP,
		}).insert(ignore_permissions=True)
		return doc.name

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_purge_test_leads()
		for view in frappe.get_all("CRM Smart View", filters={"label": ["like", "ZZ Screening%"]}, pluck="name"):
			frappe.delete_doc("CRM Smart View", view, force=True, ignore_permissions=True)
		frappe.delete_doc("Lead Sync Source", cls.SOURCE, force=True, ignore_permissions=True)
		frappe.delete_doc("Facebook Lead Form", cls.FORM, force=True, ignore_permissions=True)
		frappe.delete_doc("Facebook Page", cls.PAGE, force=True, ignore_permissions=True)
		for contract_name in (cls.SOURCE, f"{cls.SOURCE}-internal"):
			name = frappe.db.get_value("CRM Lead API Mapping", {"contract_name": contract_name}, "name")
			if name:
				frappe.delete_doc("CRM Lead API Mapping", name, force=True, ignore_permissions=True)
		partner_fixture.teardown()
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		self.sp = f"screening_{frappe.generate_hash(length=6)}"
		frappe.db.savepoint(self.sp)
		_forget_caches()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point=self.sp)
		_forget_caches()

	# -- helpers ---------------------------------------------------------------

	def _sync(self, lead_id, answers, phone=PHONE):
		"""One Facebook lead through the real fold, with the phone that deduplicates it."""
		payload = _graph_lead(lead_id, {"q_phone": [phone], **answers})
		fold = TatvaFacebookSyncSource("zz-token", self.FORM, source_name=self.SOURCE)
		doc = fold.sync_single_lead(payload, raise_exception=True)
		self.assertIsNotNone(doc, "the fold must return the lead it wrote")
		return frappe.get_doc("CRM Lead", doc.name)

	def _rows(self, lead):
		return [(r.question or "", r.value or "") for r in lead.get(ANSWERS_TABLE)]

	# -- ingestion -------------------------------------------------------------

	def test_an_answer_lands_although_nothing_was_ever_declared(self):
		"""The whole point: no catalog row, no tick, no picker step, and the answer is still kept."""
		lead = self._sync("fb-1", {RAW_HBA1C: ["7.5-9"]})
		self.assertIn((RAW_HBA1C, "7.5-9"), self._rows(lead))
		self.assertFalse(
			frappe.db.exists("CRM Lead API Field", {"section": "screening"}),
			"a screening question is declared nowhere; if a row appeared, the mapping step grew back",
		)

	def test_the_row_carries_the_wording_the_patient_saw(self):
		"""What a patient was asked is a fact about that patient, so the row stays readable on its own."""
		lead = self._sync("fb-1b", {RAW_HBA1C: ["7.5-9"]})
		row = next(r for r in lead.get(ANSWERS_TABLE) if r.question == RAW_HBA1C)
		self.assertEqual(row.label, "Latest HbA1c")
		self.assertEqual(row.question_hash, keyvalue.identity_of(RAW_HBA1C))

	def test_a_multi_select_answer_keeps_every_value(self):
		"""values[0] recorded a patient with high cholesterol and fatty liver as having one of them."""
		lead = self._sync("fb-2", {RAW_CONDITIONS: ["high_cholesterol", "fatty_liver"]})
		values = [v for q, v in self._rows(lead) if q == RAW_CONDITIONS]
		self.assertEqual(values, ["high_cholesterol, fatty_liver"])

	def test_a_contact_question_still_lands_on_the_lead_itself(self):
		"""Phone is a lead column and stays mapped; only screening questions are kept as asked."""
		lead = self._sync("fb-2b", {RAW_AGE: ["45-54"]})
		self.assertEqual(lead.mobile_no, PHONE)
		self.assertNotIn("q_phone", [q for q, _v in self._rows(lead)])

	def test_a_skipped_question_writes_no_row(self):
		"""Graph omits `values` entirely for a question the person passed over. Absent is not blank."""
		lead = self._sync("fb-4", {RAW_TREATMENT: None, RAW_AGE: ["45-54"]})
		self.assertEqual([q for q, _v in self._rows(lead)], [RAW_AGE])

	def test_a_question_answered_blank_writes_a_row(self):
		lead = self._sync("fb-5", {RAW_AGE: []})
		self.assertIn((RAW_AGE, ""), self._rows(lead))

	def test_the_same_question_hashes_alike_however_long_it_is(self):
		"""A digest is fixed width for any input, so no index ever depends on a length a question exceeds."""
		long_question = "z" * 900
		self.assertEqual(len(keyvalue.identity_of(long_question)), 64)
		self.assertEqual(keyvalue.identity_of(RAW_AGE), keyvalue.identity_of(RAW_AGE))
		self.assertNotEqual(keyvalue.identity_of(RAW_AGE), keyvalue.identity_of(RAW_HBA1C))

	def test_a_long_question_is_stored_whole_and_never_shortened(self):
		long_question = "what_is_your_age_group?_" + ("x" * 400)
		lead = self._sync("fb-5b", {long_question: ["45-54"]})
		stored = [q for q, _v in self._rows(lead)]
		self.assertIn(long_question, stored, "the question is kept whole, not truncated to fit a column")

	def test_an_unchanged_answer_is_not_written_twice(self):
		"""A crawl re-reading the same answer must not manufacture a change; only a DIFFERENT answer is
		a new fact, and that one is kept beside the earlier it replaced."""
		self._sync("fb-6", {RAW_HBA1C: ["7.5-9"], RAW_AGE: ["45-54"]})
		lead = self._sync("fb-7", {RAW_HBA1C: [">9"], RAW_AGE: ["45-54"]})
		rows = self._rows(lead)
		self.assertEqual(len([v for q, v in rows if q == RAW_AGE]), 1, "the unchanged answer stays one row")
		self.assertEqual(
			sorted(v for q, v in rows if q == RAW_HBA1C), sorted(["7.5-9", ">9"]),
			"the changed answer is kept beside the one it succeeded",
		)

	def test_a_row_carries_no_grain(self):
		"""The lead already carries all three axes; a second copy is a second place for them to drift."""
		columns = {f.fieldname for f in frappe.get_meta("CRM Lead Screening Answer").fields}
		self.assertEqual(columns & {"vertical", "group", "program", "custom_vertical"}, set())

	# -- the Data tab ----------------------------------------------------------

	def test_the_data_tab_shows_every_answer_under_the_wording_the_patient_saw(self):
		lead = self._sync("fb-9", {RAW_HBA1C: ["7.5-9"], RAW_AGE: ["45-54"]})
		fields = self._screening_fields(lead.name)
		shown = {f["label"]: f["value"] for f in fields}
		self.assertEqual(shown.get("Latest HbA1c"), "7.5-9", "the label, not the underscored key")
		self.assertEqual(shown.get("Age group"), "45-54")
		self.assertTrue(all(f["read_only"] for f in fields), "there is no field to write an answer back through")

	def _screening_fields(self, lead_name):
		sections = detail.lead_detail(lead_name)["sections"]
		return next((s["fields"] for s in sections if s["key"] == "screening"), [])

	# -- Smart Views -----------------------------------------------------------

	def test_a_smart_view_projects_filters_and_sorts_a_question(self):
		self._sync("fb-10", {RAW_HBA1C: ["7.5-9"]}, phone=PHONE)
		other = self._sync("fb-11", {RAW_HBA1C: [">9"]}, phone="+916100020002")
		key = f"screening:{keyvalue.identity_of(RAW_HBA1C)}"
		view = self._view(key)
		rows = smartview.get_data(view, filters=frappe.as_json([[key, "=", ">9"]]))
		self.assertEqual([r["name"] for r in rows["rows"]], [other.name])
		self.assertEqual(rows["total"], 1, "a joined question must not multiply or inflate the count")
		answered = frappe.as_json([[key, "is set", None]])
		ascending = smartview.get_data(view, filters=answered, sort=frappe.as_json([key, "asc"]))
		descending = smartview.get_data(view, filters=answered, sort=frappe.as_json([key, "desc"]))
		projected = [r[key] for r in ascending["rows"]]
		self.assertEqual(sorted(projected), sorted(["7.5-9", ">9"]))
		self.assertEqual([r[key] for r in descending["rows"]], list(reversed(projected)))

	def test_the_builder_offers_a_question_only_once_it_has_been_asked(self):
		"""The list is read from the data, so it shows what has been asked rather than what was declared."""
		key = f"screening:{keyvalue.identity_of(RAW_CONDITIONS)}"
		_forget_caches()
		self.assertNotIn(key, smartview._catalog_fields("Lead", None, {self.grain}, frappe.get_roles()))
		# No commit: the row is in this transaction and the catalog reads it there. Committing would
		# destroy the savepoint tearDown rolls back to, and leave the answer behind on the bench.
		self._sync("fb-12", {RAW_CONDITIONS: ["high_cholesterol"]})
		_forget_caches()
		offered = smartview._catalog_fields("Lead", None, {self.grain}, frappe.get_roles())
		self.assertIn(key, offered)
		self.assertEqual(offered[key].label, "Conditions", "offered under the wording, not the digest")

	def test_two_questions_in_one_view_do_not_multiply_the_lead(self):
		"""Each question is its own grouped join, so selecting several must not duplicate a lead or its count."""
		lead = self._sync("fb-13", {RAW_HBA1C: ["7.5-9"], RAW_AGE: ["45-54"]})
		hba1c = f"screening:{keyvalue.identity_of(RAW_HBA1C)}"
		age = f"screening:{keyvalue.identity_of(RAW_AGE)}"
		_forget_caches()
		view = self._view(hba1c, age)
		rows = smartview.get_data(view, filters=frappe.as_json([[hba1c, "=", "7.5-9"]]))
		self.assertEqual([r["name"] for r in rows["rows"]], [lead.name])
		self.assertEqual(rows["total"], 1, "two joined questions must not multiply the lead or its count")
		self.assertEqual(rows["rows"][0][age], "45-54", "both questions project on the one row")

	def test_a_saved_view_filtered_on_a_vanished_question_shows_nothing(self):
		"""It used to show EVERY lead: the condition could not resolve, so it was dropped and the view
		widened to the whole table. A saved predicate is the view's definition and now fails closed."""
		gone = f"screening:{keyvalue.identity_of('a_question_no_lead_has_ever_answered')}"
		view = frappe.get_doc({
			"doctype": "CRM Smart View", "label": "ZZ Screening Vanished", "base_object": "Lead",
			"vertical": partner_fixture.VERTICAL, "group": partner_fixture.GROUP, "is_standard": 1,
			"columns": frappe.as_json(["lead:mobile_no"]),
			"predicate": frappe.as_json({"field": gone, "operator": "=", "value": "anything"}),
		}).insert(ignore_permissions=True)
		rows = smartview.get_data(view.name)
		self.assertEqual(rows["rows"], [], "an unresolvable saved condition must narrow, never widen")
		self.assertEqual(rows["total"], 0)

	def test_not_equals_returns_only_leads_that_answered_something_else(self):
		"""Pinned so the behaviour cannot drift unnoticed: a lead never asked the question is NOT returned,
		because the join leaves it NULL. Whether that is the right reading is a product decision, not a
		silent one."""
		self._sync("fb-14", {RAW_HBA1C: ["7.5-9"]}, phone=PHONE)
		other = self._sync("fb-15", {RAW_HBA1C: [">9"]}, phone="+916100020003")
		key = f"screening:{keyvalue.identity_of(RAW_HBA1C)}"
		_forget_caches()
		rows = smartview.get_data(self._view(key), filters=frappe.as_json([[key, "!=", "7.5-9"]]))
		self.assertEqual([r["name"] for r in rows["rows"]], [other.name])

	def test_the_data_tab_omits_the_section_when_the_lead_answered_nothing(self):
		"""An empty section is not shown at all, rather than shown empty."""
		lead = self._sync("fb-16", {})
		keys = [s["key"] for s in detail.lead_detail(lead.name)["sections"]]
		self.assertNotIn("screening", keys)

	# -- history: a changed answer is kept, the newest is shown ----------------

	def test_a_changed_answer_is_kept_alongside_the_earlier_one(self):
		"""Upserting on the question alone destroyed the earlier answer when a later campaign asked the
		same thing. A changed answer IS the clinical fact, so both rows survive."""
		self._sync("fb-17", {RAW_HBA1C: ["7.5-9"]})
		lead = self._sync("fb-18", {RAW_HBA1C: [">9"]})
		answers = [v for q, v in self._rows(lead) if q == RAW_HBA1C]
		self.assertEqual(sorted(answers), sorted(["7.5-9", ">9"]), "both answers are kept")

	def test_the_data_tab_shows_the_newest_answer_and_offers_the_history(self):
		self._sync("fb-19", {RAW_HBA1C: ["7.5-9"]})
		lead = self._sync("fb-20", {RAW_HBA1C: [">9"]})
		field = next(f for f in self._screening_fields(lead.name) if f["label"] == "Latest HbA1c")
		self.assertEqual(field["value"], ">9", "the newest answer is the one shown")
		self.assertTrue(field["has_more"], "more than one answer must offer its history")

	def test_a_question_answered_once_offers_no_history(self):
		lead = self._sync("fb-21", {RAW_HBA1C: ["7.5-9"]})
		field = next(f for f in self._screening_fields(lead.name) if f["label"] == "Latest HbA1c")
		self.assertFalse(field["has_more"], "one answer is not a history")

	def test_the_history_returns_every_answer_newest_first(self):
		self._sync("fb-22", {RAW_HBA1C: ["7.5-9"]})
		lead = self._sync("fb-23", {RAW_HBA1C: [">9"]})
		field = next(f for f in self._screening_fields(lead.name) if f["label"] == "Latest HbA1c")
		history = detail.section_history(lead.name, field["field_key"])
		self.assertEqual(history["label"], "Latest HbA1c")
		self.assertEqual([e["value"] for e in history["entries"]], [">9", "7.5-9"])
		self.assertTrue(all(e["on"] for e in history["entries"]), "each answer carries when it was given")

	def test_re_reading_the_same_answer_adds_no_history(self):
		"""Idempotent: a crawl that sees the same answer again must not manufacture a change."""
		self._sync("fb-24", {RAW_HBA1C: ["7.5-9"]})
		lead = self._sync("fb-25", {RAW_HBA1C: ["7.5-9"]})
		self.assertEqual(len([v for q, v in self._rows(lead) if q == RAW_HBA1C]), 1)

	def test_history_is_refused_for_a_lead_the_caller_cannot_read(self):
		"""An answer hangs off a lead, so its history is gated exactly as the lead is."""
		lead = self._sync("fb-26", {RAW_HBA1C: ["7.5-9"]})
		field = next(f for f in self._screening_fields(lead.name) if f["label"] == "Latest HbA1c")
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				detail.section_history(lead.name, field["field_key"])
		finally:
			frappe.set_user("Administrator")

	def _view(self, *keys):
		doc = frappe.get_doc({
			"doctype": "CRM Smart View", "label": "ZZ Screening View", "base_object": "Lead",
			"vertical": partner_fixture.VERTICAL, "group": partner_fixture.GROUP,
			"is_standard": 1, "columns": frappe.as_json(list(keys)),
		}).insert(ignore_permissions=True)
		return doc.name

	def _one_row(self, view, lead):
		"""The view's row for THIS lead, proven to be the only one it returns.

		A filter that quietly matches everything would otherwise let a projection assertion pass on
		whichever row happened to sort first — which is exactly how the first draft of the divergence
		test below stayed green against the very bug it was written to catch."""
		rows = smartview.get_data(view, filters=frappe.as_json([["lead:mobile_no", "=", lead.mobile_no]]))
		self.assertEqual(
			[r["name"] for r in rows["rows"]], [lead.name],
			"the filter must isolate the fixture lead, or nothing below is being compared",
		)
		return rows["rows"][0]

	def test_answering_the_same_thing_again_does_not_re_attribute_the_first_answer(self):
		"""An unchanged answer used to be MERGED, which wrote every sent field onto the stored row —
		including which form asked it. So a patient repeating an answer on a later campaign silently
		re-attributed their ORIGINAL answer to that campaign, and the fact it had been asked twice was
		gone. The docstring already promised a re-read changes nothing; now it actually does nothing."""
		second_form = "zz-screen-form-again"
		frappe.get_doc({
			"doctype": "Facebook Lead Form", "id": second_form, "page": self.PAGE,
			"form_name": "ZZ Screen Form Again",
			"questions": [
				{"key": "q_phone", "label": "Phone", "mapped_to_crm_field": IDENTITY_KEY},
				{"key": RAW_HBA1C, "label": "Latest HbA1c"},
			],
		}).insert(ignore_permissions=True)
		try:
			self._sync("fb-80", {RAW_HBA1C: ["7.5-9"]})
			payload = _graph_lead("fb-81", {"q_phone": [PHONE], RAW_HBA1C: ["7.5-9"]})
			other = TatvaFacebookSyncSource("zz-token", second_form, source_name=self.SOURCE)
			lead = frappe.get_doc("CRM Lead", other.sync_single_lead(payload, raise_exception=True).name)

			rows = [r for r in lead.get(ANSWERS_TABLE) if r.question == RAW_HBA1C]
			self.assertEqual(len(rows), 1, "an unchanged answer is still not a second row")
			self.assertEqual(
				rows[0].form, self.FORM,
				"the answer must still name the campaign that actually asked it",
			)
		finally:
			frappe.delete_doc("Facebook Lead Form", second_form, force=True, ignore_permissions=True)

	# -- every campaign that reaches a patient is kept -------------------------

	def _sync_at(self, lead_id, when, phone=PHONE):
		"""One lead carrying Graph's own submission time, which is what keys the acquisition touch."""
		payload = _graph_lead(lead_id, {"q_phone": [phone]})
		payload["created_time"] = when
		fold = TatvaFacebookSyncSource("zz-token", self.FORM, source_name=self.SOURCE)
		doc = fold.sync_single_lead(payload, raise_exception=True)
		return frappe.get_doc("CRM Lead", doc.name)

	def test_a_second_campaign_does_not_erase_the_first(self):
		"""`acq` used to be single-row, so a patient reached twice kept only the LAST campaign — the one
		that found them was silently overwritten and their January attribution vanished. Every touch is
		its own row now, keyed by when it happened."""
		self._sync_at("fb-70", "2026-01-05T09:00:00+0530")
		lead = self._sync_at("fb-71", "2026-03-12T09:00:00+0530")
		touches = sorted(cstr(r.touch_at) for r in lead.custom_acquisition_profile)
		self.assertEqual(len(touches), 2, "both campaign touches must survive")
		self.assertTrue(touches[0].startswith("2026-01-05"), "the January touch is still there")
		self.assertTrue(touches[1].startswith("2026-03-12"), "and so is the March one")

	def test_a_re_crawl_updates_the_touch_rather_than_adding_one(self):
		"""The key is Meta's submission time, not our clock — so re-reading the same lead addresses the SAME
		row. Stamping arrival time here would mint a new key every pass and record one campaign for ever."""
		self._sync_at("fb-72", "2026-01-05T09:00:00+0530")
		lead = self._sync_at("fb-72", "2026-01-05T09:00:00+0530")
		self.assertEqual(len(lead.custom_acquisition_profile), 1, "a re-crawl must not add a second touch")

	def test_the_touch_records_the_platform_and_the_campaign(self):
		"""Recorded in the table's own UTM vocabulary, so Google or a WhatsApp blast records the same way."""
		lead = self._sync_at("fb-73", "2026-02-01T09:00:00+0530")
		row = lead.custom_acquisition_profile[0]
		self.assertEqual(row.utm_source, "facebook")
		self.assertEqual(row.utm_campaign, "ZZ Screen Form", "the campaign is the form's name, not its id")

	# -- one question is ONE question, whatever form asked it ------------------

	def test_the_same_question_on_two_forms_is_one_question(self):
		"""The identity is a digest of the QUESTION and nothing else — no form id, no campaign.

		Marketing duplicates a form to edit it, so the same question arrives under a new form id every few
		weeks. If the identity carried the form, each duplicate would open a NEW column in every Smart View
		and a NEW row in the Data Tab, and a patient's answer history would shatter one campaign at a time.
		Mutating `identity_of` to mix in the form id passes every other test in this file; it fails here."""
		second_form = "zz-screen-form-two"
		frappe.get_doc({
			"doctype": "Facebook Lead Form", "id": second_form, "page": self.PAGE,
			"form_name": "ZZ Screen Form Two",
			"questions": [
				{"key": "q_phone", "label": "Phone", "mapped_to_crm_field": IDENTITY_KEY},
				{"key": RAW_HBA1C, "label": "Latest HbA1c"},
			],
		}).insert(ignore_permissions=True)
		try:
			self._sync("fb-60", {RAW_HBA1C: ["7.5-9"]})
			payload = _graph_lead("fb-61", {"q_phone": [PHONE], RAW_HBA1C: [">9"]})
			other = TatvaFacebookSyncSource("zz-token", second_form, source_name=self.SOURCE)
			lead = frappe.get_doc("CRM Lead", other.sync_single_lead(payload, raise_exception=True).name)

			asked = [f for f in self._screening_fields(lead.name) if f["label"] == "Latest HbA1c"]
			self.assertEqual(len(asked), 1, "two forms asking one question must not become two questions")
			self.assertTrue(asked[0]["has_more"], "both answers belong to the ONE question's history")
			history = detail.section_history(lead.name, asked[0]["field_key"])
			self.assertEqual([e["value"] for e in history["entries"]], [">9", "7.5-9"])
			self.assertEqual(
				[e["source"] for e in history["entries"]], [second_form, self.FORM],
				"and each answer still names the campaign that asked it",
			)
		finally:
			frappe.delete_doc("Facebook Lead Form", second_form, force=True, ignore_permissions=True)

	# -- screening answers are grain-gated like every field beside them --------

	def test_screening_is_hidden_from_a_viewer_outside_the_leads_grain(self):
		"""A key-value section declares no catalogued field, so `_select` never admits it — and the answers
		reached anyone holding read on the lead, ungated, while every NAMED field beside them was
		grain-filtered. The gate is `taxonomy.grain.covers`, the same wildcard matcher used everywhere else."""
		lead = self._sync("fb-52", {RAW_HBA1C: ["7.5-9"]})
		self.assertTrue(self._screening_fields(lead.name), "in grain, the answers show")
		with patch.object(entitlement, "entitled_grains", return_value=OUT_OF_GRAIN):
			self.assertEqual(
				self._screening_fields(lead.name), [],
				"out of grain, the answers must not be served at all",
			)

	def test_the_history_endpoint_refuses_a_viewer_outside_the_leads_grain(self):
		"""Or the modal answers precisely what the panel just declined to show."""
		lead = self._sync("fb-53", {RAW_HBA1C: ["7.5-9"]})
		field = next(f for f in self._screening_fields(lead.name) if f["label"] == "Latest HbA1c")
		with patch.object(entitlement, "entitled_grains", return_value=OUT_OF_GRAIN):
			with self.assertRaises(frappe.PermissionError):
				detail.section_history(lead.name, field["field_key"])

	# -- a refused field is refused, not re-routed -----------------------------

	def test_a_question_mapped_to_an_ungranted_field_is_dropped_not_rerouted(self):
		"""A mapped question whose field the contract does NOT tick used to fall through to the screening
		branch — so a value the grain gate had just refused was stored anyway, on the one lead surface
		that carried no field-level gate at all. Refusing means dropping it."""
		fold = TatvaFacebookSyncSource("zz-token", self.FORM, source_name=self.SOURCE)
		payload = _graph_lead("fb-46", {"q_phone": [PHONE], RAW_HBA1C: ["7.5-9"]})
		with patch.object(
			TatvaFacebookSyncSource, "get_form_questions_mapping",
			return_value={"q_phone": IDENTITY_KEY, RAW_HBA1C: "lead:email"},
		), patch("tatva_connect.lead_sync.source.allowed_field_keys", return_value={IDENTITY_KEY}):
			doc = fold.sync_single_lead(payload, raise_exception=True)
		lead = frappe.get_doc("CRM Lead", doc.name)
		self.assertFalse(
			[v for q, v in self._rows(lead) if q == RAW_HBA1C],
			"a refused field must be dropped, never re-routed into screening",
		)
		self.assertFalse(lead.get("email"), "and it must not reach the lead either")

	# -- the watermark ---------------------------------------------------------

	def test_the_watermark_keeps_its_time_of_day(self):
		"""`get_timestamp` is `mktime(getdate(x).timetuple())` — it drops the time, so the crawl's filter
		meant midnight of the last-synced DAY. Every pass then refetched the whole day and re-saved every
		lead in it. Asserted as a DIFFERENCE, so the old implementation collapses both to zero."""
		fold = TatvaFacebookSyncSource("zz-token", self.FORM, source_name=self.SOURCE)
		with patch.object(TatvaFacebookSyncSource, "last_synced_at", "2026-07-20 00:00:00"):
			midnight = fold.synced_upto_unix()
		with patch.object(TatvaFacebookSyncSource, "last_synced_at", "2026-07-20 14:30:00"):
			afternoon = fold.synced_upto_unix()
		self.assertEqual(
			afternoon - midnight, 14.5 * 3600,
			"the time of day must survive into the Graph filter, or the crawl refetches the whole day",
		)

	# -- the two consumers must name the same answer ---------------------------

	def test_the_worklist_and_the_data_tab_name_the_same_current_answer(self):
		"""THE divergence lock, which CLAUDE.md rule 4 requires for any rule with a Python and a SQL half.

		The Data Tab picks the newest answer; the Smart View picked `MAX(value)` — the lexicographically
		greatest, which has no relation to recency. The two answers below are chosen so alphabetical and
		chronological DISAGREE: ">9" is written first and sorts last, so a MAX() returns the answer that
		was already replaced. The suite's other two-answer fixtures happen to order the same way both
		ways, which is exactly why this went unseen."""
		self._sync("fb-40", {RAW_HBA1C: ["Yes"]})
		lead = self._sync("fb-41", {RAW_HBA1C: ["No"]})
		key = f"screening:{keyvalue.identity_of(RAW_HBA1C)}"
		_forget_caches()
		self.assertEqual(
			[v for q, v in self._rows(lead) if q == RAW_HBA1C], ["Yes", "No"],
			"the fixture must hold BOTH answers, oldest first, or nothing here is being compared",
		)
		field = next(f for f in self._screening_fields(lead.name) if f["label"] == "Latest HbA1c")
		self.assertEqual(field["value"], "No", "the Data Tab shows the newest answer")
		row = self._one_row(self._view(key), lead)
		self.assertEqual(
			row[key], field["value"],
			"the worklist and the Data Tab must name the SAME current answer",
		)

	def test_a_worklist_filter_selects_on_the_answer_the_lead_actually_shows(self):
		"""Membership, not only the printed cell: the replaced answer must not pull the lead into a cohort."""
		self._sync("fb-42", {RAW_HBA1C: ["Yes"]})
		lead = self._sync("fb-43", {RAW_HBA1C: ["No"]})
		key = f"screening:{keyvalue.identity_of(RAW_HBA1C)}"
		_forget_caches()
		view = self._view(key)
		current = smartview.get_data(view, filters=frappe.as_json([[key, "=", "No"]]))
		replaced = smartview.get_data(view, filters=frappe.as_json([[key, "=", "Yes"]]))
		self.assertIn(lead.name, [r["name"] for r in current["rows"]], "the current answer selects the lead")
		self.assertNotIn(lead.name, [r["name"] for r in replaced["rows"]], "the replaced answer must not")

	# -- what a view projects when it has chosen nothing -----------------------

	def test_a_view_with_no_columns_does_not_project_every_question(self):
		"""`empty` used to mean "every worklist field", and a question carries that surface — so a
		column-less view built one LEFT JOIN per question and died past MariaDB's 61-table ceiling once
		enough had been asked. Empty now means the starter set: the lead's own fields, never a join."""
		self._sync("fb-44", {RAW_HBA1C: ["7.5-9"], RAW_AGE: ["45-54"]})
		_forget_caches()
		projected = {c["key"] for c in smartview.get_data(self._view())["columns"]}
		self.assertTrue(projected, "a view with no columns still projects the starter set")
		self.assertFalse(
			[k for k in projected if k.startswith("screening:")],
			"a question must never enter a view that did not ask for it",
		)

	def test_an_unknown_column_is_refused_rather_than_swapped_for_every_column(self):
		"""The save path already threw on an unknown key; the read path dropped it and fell back, so a
		client sending the wrong identifier silently got the default set instead of an error."""
		self._sync("fb-45", {RAW_HBA1C: ["7.5-9"]})
		_forget_caches()
		with self.assertRaises(frappe.ValidationError):
			smartview.get_data(self._view(), columns=frappe.as_json(["screening:not_a_real_question"]))
