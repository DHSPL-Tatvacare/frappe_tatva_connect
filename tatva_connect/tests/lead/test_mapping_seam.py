# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""One answer to "which catalogue fields may this map into, and into which section".

Intake and the Facebook question mapper both asked it, and answered it twice: intake walked
`CRM Lead API Field` filtered by section and scoped through `entitlement.field_in_grains_via_contract`,
Facebook went through `contract.allowed_field_keys`. Two routes to one question is the shape a second
brain grows in, and the Desk bulk import would have been the third.

The two SCOPE SOURCES are not the same question and are not collapsed: a contract answers "what may this
contract write", a grain answers "what is visible to this grain internally" — intake needs the latter
because an unsaved builder form carries axes but no contract yet. What is shared, and what this locks,
is everything else: the catalogue is read once, the section is resolved once, and a catalogued row that
names no live column is dropped once. That last rule lived only in intake before this.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.lead import mapping
from tatva_connect.lead_sync.contract import allowed_field_keys
from tatva_connect.tests.api import partner_fixture

PARTNER = "mapping.seam.partner@example.test"

# A row whose column is RETIRED after it was written — the catalogue refuses minting one, so it is reached as in life.
STALE_FIELDNAME = "zz_map_seam_retired"
STALE_KEY = f"{partner_fixture.PARENT_SECTION}:{STALE_FIELDNAME}"


class TestMappingSeam(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.live_key = partner_fixture.mint_catalog_row("zz_map_seam_live")
		partner_fixture.mint_catalog_row(STALE_FIELDNAME)
		partner_fixture.mint_partner(PARTNER, ticks=(cls.live_key, STALE_KEY))
		cls.contract = frappe.get_doc("CRM Lead API Mapping", {"partner_user": PARTNER})
		frappe.delete_doc("Custom Field", f"CRM Lead-{STALE_FIELDNAME}", force=True,
		                  ignore_permissions=True)  # authz-ok: tier-a — test fixture, retires the column
		frappe.clear_cache(doctype="CRM Lead")
		frappe.db.commit()  # survives the per-test rollback; the seam resolves the contract live

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		partner_fixture.teardown()
		frappe.db.commit()
		super().tearDownClass()

	def test_sections_come_from_the_section_brain_in_its_own_display_order(self):
		"""The seed decides which sections exist and in what order; the seam never restates them."""
		expected = frappe.get_all("CRM Lead Section", pluck="section_key", order_by="display_order asc")
		self.assertEqual([s["section_key"] for s in mapping.mappable_sections()], expected)

	def test_contract_mode_offers_the_contracts_ticks_and_nothing_wider(self):
		offered = {f["field_key"] for f in mapping.mappable_fields(contract=self.contract)}
		self.assertTrue(offered <= allowed_field_keys(self.contract),
		                "the seam offered a key the contract never ticked")
		self.assertIn(self.live_key, offered)

	def test_a_row_whose_column_was_retired_is_no_longer_offered(self):
		"""Intake's rule, now shared: a picker must not offer what can no longer hold a value."""
		self.assertIn(STALE_KEY, allowed_field_keys(self.contract), "the fixture did not tick the stale key")
		offered = {f["field_key"] for f in mapping.mappable_fields(contract=self.contract)}
		self.assertNotIn(STALE_KEY, offered)

	def test_a_section_filter_narrows_to_that_section_only(self):
		for field in mapping.mappable_fields(section=partner_fixture.PARENT_SECTION,
		                                     contract=self.contract):
			self.assertEqual(field["section"], partner_fixture.PARENT_SECTION)

	def test_every_offered_field_carries_what_a_picker_needs(self):
		"""A picker renders a label and stores a fieldname; both are the seam's job, not the client's."""
		for field in mapping.mappable_fields(contract=self.contract):
			self.assertTrue(field["label"], f"{field['field_key']} was offered with no label")
			self.assertTrue(field["fieldname"])
			self.assertTrue(field["fieldtype"])
			self.assertEqual(field["field_key"], f"{field['section']}:{field['fieldname']}")

	def test_an_unscoped_call_is_refused_rather_than_answered_widely(self):
		"""No contract and no grain is a caller bug; answering it would leak the whole catalogue."""
		with self.assertRaises(frappe.ValidationError):
			mapping.mappable_fields()
