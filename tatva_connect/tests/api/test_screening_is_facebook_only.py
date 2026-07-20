# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A screening answer is READABLE by a partner if an operator catalogues it, and writable by nobody.

An answer is what a patient said to a campaign. A partner may be shown it — that is an operator's call,
made by adding a catalog row — but no partner may author or edit one, because there is no per-row grant
to check an edit against and rewriting a patient's answer is not a thing the API should be able to do.

This file used to assert something stronger and wrong: that no catalog row may name a key-value section
at all. That made the section unshowable as well as unwritable, and it held by CONVENTION — the moment an
operator added the row, screening became writable with nothing going red. It also proved its last claim by
string-searching `partner.py`, which passes just as well when the branch moves into a helper.

The rule is now structural: a key-value section's rows never enter the WRITABLE catalog, exactly as
`owner` and `creation` never do, while staying in what a partner may read.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.api.test_screening_is_facebook_only
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.api import partner
from tatva_connect.tests.api import partner_fixture

ANSWER = "zz_screening_lock_question"


def _key_value_sections():
	return set(frappe.get_all("CRM Lead Section", filters={"is_key_value": 1}, pluck="name"))


class TestScreeningIsReadOnly(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.sections = _key_value_sections()
		assert cls.sections, "no key-value section exists, so this lock asserts nothing"
		cls.section = sorted(cls.sections)[0]
		# The operator action this whole file is about: catalogue a screening question so a partner can
		# be shown it. Before this fix that ALSO made it writable, which is the defect being locked out.
		cls.field_key = partner_fixture.mint_catalog_row(ANSWER, section=cls.section)
		frappe.cache().delete_value(partner._CATALOG_CACHE_KEY)

	@classmethod
	def tearDownClass(cls):
		partner_fixture.teardown()
		frappe.cache().delete_value(partner._CATALOG_CACHE_KEY)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.cache().delete_value(partner._CATALOG_CACHE_KEY)

	def _table(self):
		return partner._catalog()["section_child"][self.section]

	def test_the_catalogued_question_is_readable(self):
		"""The operator's row did land — otherwise the write test below would pass for the wrong reason."""
		self.assertIn(
			self.field_key, partner._catalog()["read_only_keys"],
			"an operator who catalogues a screening question must be able to show it to a partner",
		)
		self.assertNotIn(
			self.field_key, partner._catalog()["key_set"],
			"and cataloguing it must not make it writable",
		)

	def test_a_partner_payload_carrying_an_answer_writes_nothing(self):
		"""The behavioural proof: hand `_collect` a full-catalog grant and a screening payload, and the
		table must not survive into what the write engine is allowed to apply."""
		_parent, child_allow = partner._split_keys(partner._catalog()["keys"])
		table = self._table()
		payload = frappe._dict({
			"mobile_no": "+919812300444",
			table: [{"question": "smuggled", "value": "smuggled"}],
		})
		_collected, children = partner._collect(
			payload, parent_fields=["mobile_no"], child_allow=child_allow, allow_routing=False
		)
		self.assertNotIn(table, children, "a partner payload must never reach the screening table")

	def test_the_facebook_fold_can_still_write_one(self):
		"""The fold does not read the writable catalog for its screening grant — it names the section — so
		locking partners out must not lock the source of the answers out with them."""
		from tatva_connect.lead_sync.contract import screening_key

		self.assertTrue(screening_key(), "the fold still resolves a key-value section to write into")
