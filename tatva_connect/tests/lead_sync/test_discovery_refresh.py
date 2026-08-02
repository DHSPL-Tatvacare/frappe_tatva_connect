# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A Page is refreshed on every pass; a form is written once and never again.

They differ because the underlying objects differ. A Page holds the token minted from whichever user
token was current, so a re-pasted credential has to reach it or the crawl keeps a dead one. A FORM cannot
change: Meta's Marketing API has no update operation on a leadgen form — create, read, and a status POST
to archive or reactivate — and a published form cannot be edited in Ads Manager either. An edit is a
duplicate, and a duplicate is a new id, which arrives here as a form nobody has seen.

Graph is stubbed throughout: these assert OUR handling of its answer, never Facebook's availability.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.lead_sync.test_discovery_refresh
"""
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.lead_sync import discovery
from tatva_connect.lead_sync.form import IDENTITY_KEY
from tatva_connect.tests.lead_sync import ensure_app

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

	def test_a_form_already_held_is_left_exactly_as_it_is(self):
		"""A form cannot change, so re-reading it is a save per form per pass for nothing. The listing that
		finds new forms still reports every form on the Page, and each one already stored is skipped."""
		self._store([_question("age_group", "Age group")])
		before = frappe.db.get_value("Facebook Lead Form", FORM, "modified")
		self._store([_question("age_group", "Age group"), _question("hba1c", "Latest HbA1c")], form_name="Renamed")
		self.assertEqual(sorted(self._rows()), ["age_group"], "a stored form must not be rewritten")
		self.assertEqual(
			frappe.db.get_value("Facebook Lead Form", FORM, "modified"), before,
			"a stored form must not even be saved",
		)

	def test_a_stored_mapping_is_never_at_risk_from_a_pass(self):
		"""The mapping used to be carried across a rewrite that no longer happens. Nothing touches it now,
		which is the strongest form of the same guarantee."""
		self._store([_question("phone_number", "Phone")])
		form = frappe.get_doc("Facebook Lead Form", FORM)
		form.questions[0].mapped_to_crm_field = IDENTITY_KEY
		form.save(ignore_permissions=True)

		self._store([_question("phone_number", "Phone"), _question("hba1c", "Latest HbA1c")])
		self.assertEqual(self._rows()["phone_number"].mapped_to_crm_field, IDENTITY_KEY)

	def test_a_new_form_is_inserted_unmapped_without_tripping_the_operator_gate(self):
		"""Most discovered forms are unmapped, so an insert that had to satisfy the operator mapping gate
		would throw on nearly every form and a pass would bring nothing back at all."""
		self._store([_question("age_group", "Age group")])
		self.assertFalse(self._rows()["age_group"].mapped_to_crm_field)


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
			discovery.fetch_and_store_pages("zz-user-token", frappe.get_doc("CRM Facebook App", ensure_app()))

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
				"doctype": "Lead Sync Source", "facebook_app": ensure_app(), "name": name, "type": "Facebook",
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
