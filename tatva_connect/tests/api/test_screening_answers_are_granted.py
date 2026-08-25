# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A key-value section is writable by a contract GRANTED it, and by nobody else.

An answer is what a person said to a form. Its questions are unbounded and authored by whoever asks
them, so there is nothing per-question to tick: the grant addresses the whole section (`screening:*`)
and is opt-in, because an empty field grid means "the whole catalog" and a grant riding that
fallthrough would reach every contract that never asked for one.

This file replaces `test_screening_is_facebook_only`, which locked the section against every writer.
The lock is now a grant: the same structural claim (a per-question catalog row NEVER grants a write)
is asserted below, alongside what the grant does allow and what it still refuses.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.api.test_screening_answers_are_granted
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.api import partner
from tatva_connect.lead import keyvalue
from tatva_connect.tests.api import partner_fixture

GRANTED = "zz-screening-granted@example.com"
UNGRANTED = "zz-screening-ungranted@example.com"
MOBILE = "+919900900101"
QUESTION = "zz_condition_managing"


def _key_value_section():
	sections = frappe.get_all("CRM Lead Section", filters={"is_key_value": 1}, order_by="name", pluck="name")
	assert sections, "no key-value section exists, so this file asserts nothing"
	return frappe.get_cached_doc("CRM Lead Section", sections[0])


class TestScreeningAnswersAreGranted(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.section = _key_value_section()
		cls.cf = cls.section.child_table_field
		cls.grant = partner_fixture.mint_catalog_row(partner.SECTION_GRANT, section=cls.section.name)
		# The operator action the old lock was about: catalogue ONE question, which shows it and must never grant a write.
		cls.question_key = partner_fixture.mint_catalog_row("zz_shown_question", section=cls.section.name)
		partner_fixture.mint_partner(GRANTED, ticks=[cls.grant])
		partner_fixture.mint_partner(UNGRANTED, ticks=[cls.question_key])
		frappe.cache().delete_value(partner._CATALOG_CACHE_KEY)

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for name in frappe.get_all("CRM Lead", filters={"mobile_no": MOBILE}, pluck="name"):
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)
		partner_fixture.teardown()
		frappe.cache().delete_value(partner._CATALOG_CACHE_KEY)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.cache().delete_value(partner._CATALOG_CACHE_KEY)
		for name in frappe.get_all("CRM Lead", filters={"mobile_no": MOBILE}, pluck="name"):
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)

	def tearDown(self):
		frappe.set_user("Administrator")

	# -- the grant ---------------------------------------------------------------------------

	def test_a_question_row_never_grants_a_write(self):
		"""The claim the old lock made, kept: cataloguing a question shows it and nothing more."""
		cat = partner._catalog()
		self.assertIn(self.question_key, cat["read_only_keys"])
		self.assertNotIn(self.question_key, cat["key_set"])

	def test_the_section_grant_is_writable_and_declared_as_a_grant(self):
		cat = partner._catalog()
		self.assertIn(self.grant, cat["key_set"])
		self.assertIn(self.grant, cat["section_grant_keys"])

	def test_an_empty_grid_does_not_pick_the_grant_up(self):
		"""`_allowed_keys` falls through to the whole catalog for a contract that ticks nothing. A grant
		riding that fallthrough would reach every contract on the site."""
		self.assertNotIn(self.grant, partner._allowed_keys("zz-nobody@example.com", has_mapping=False))

	def test_a_ticked_grant_reaches_the_write_engine(self):
		frappe.set_user(GRANTED)
		_u, _mp, _is, parent_fields, child_allow = partner._caller_fields()
		payload = frappe._dict({"mobile_no": MOBILE, self.cf: [
			{"question": QUESTION, "label": "Which condition?", "value": "Type 2 Diabetes"}]})
		_p, children = partner._collect(payload, parent_fields, child_allow, allow_routing=False)
		self.assertIn(self.cf, children, "a granted contract's answers must reach the write engine")

	def test_without_the_grant_the_table_is_dropped(self):
		frappe.set_user(UNGRANTED)
		_u, _mp, _is, parent_fields, child_allow = partner._caller_fields()
		payload = frappe._dict({"mobile_no": MOBILE, self.cf: [
			{"question": QUESTION, "label": "Which condition?", "value": "smuggled"}]})
		_p, children = partner._collect(payload, parent_fields, child_allow, allow_routing=False)
		self.assertNotIn(self.cf, children, "an ungranted contract may not author an answer")

	# -- what the grant does to a row --------------------------------------------------------

	def _write(self, rows, **parent):
		frappe.set_user(GRANTED)
		_user, mp, is_sysmgr, parent_fields, child_allow = partner._caller_fields()
		item = {"mobile_no": MOBILE, self.cf: rows, **parent}
		doc, _action = partner._upsert_one(item, mp, is_sysmgr, parent_fields, child_allow)
		return doc

	def test_the_identity_is_derived_from_the_question_not_sent(self):
		doc = self._write([{"question": QUESTION, "value": "Type 2 Diabetes",
		                    "question_hash": "0" * 64}])
		row = doc.get(self.cf)[0]
		self.assertEqual(row.get(self.section.row_key_field), keyvalue.identity_of(QUESTION))

	def test_a_list_answer_becomes_one_joined_answer(self):
		doc = self._write([{"question": QUESTION, "value": ["Weight loss", "Better sleep"]}])
		self.assertEqual(doc.get(self.cf)[0].get(self.section.value_field), "Weight loss, Better sleep")

	def test_the_origin_is_stamped_from_the_payload_that_carried_it(self):
		doc = self._write([{"question": QUESTION, "value": "Type 2 Diabetes"}],
		                  custom_source_origin="LP: zz-fixture-page")
		self.assertEqual(doc.get(self.cf)[0].get(keyvalue.ORIGIN_FIELD), "LP: zz-fixture-page")

	def test_the_origin_falls_back_to_the_key_that_sent_it(self):
		doc = self._write([{"question": QUESTION, "value": "Type 2 Diabetes"}])
		self.assertEqual(doc.get(self.cf)[0].get(keyvalue.ORIGIN_FIELD), GRANTED)

	def test_a_changed_answer_is_appended_and_an_unchanged_one_is_not(self):
		self._write([{"question": QUESTION, "value": "8.1"}])
		self._write([{"question": QUESTION, "value": "8.1"}])
		doc = self._write([{"question": QUESTION, "value": "7.4"}])
		rows = [r for r in doc.get(self.cf) if r.get(self.section.question_field) == QUESTION]
		self.assertEqual([r.get(self.section.value_field) for r in rows], ["8.1", "7.4"])

	# -- what it still refuses ---------------------------------------------------------------

	def test_the_same_question_twice_in_one_payload_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._write([{"question": QUESTION, "value": "A"}, {"question": QUESTION, "value": "B"}])

	def test_a_row_naming_no_question_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._write([{"value": "an answer to nothing"}])

	def test_a_nested_answer_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._write([{"question": QUESTION, "value": [{"x": 1}]}])

	# -- the other lane ----------------------------------------------------------------------

	def test_the_facebook_fold_still_resolves_its_section(self):
		"""The fold names the section rather than reading the writable catalog, so the grant must not be
		the only way in — its own door is still there."""
		from tatva_connect.lead_sync.contract import screening_key

		self.assertTrue(screening_key())
