# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""ONE field, ONE catalog row — and a writable key that addresses its own field.

`CRM Lead API Field.field_key` is read two ways, and the two disagreed. `access/internal_contract.py:84`
treats it as an opaque id and takes the section off the row's own Link — it says so in its own comment.
The partner API does not: a writable key is split back into `<section>:<fieldname>` by
`api/partner.py::_split_keys`, so the SUFFIX of the key, not this row's `fieldname` column, is the column
a payload is collected into.

A row whose key does not address its own field therefore collects into a column that does not exist. The
contract lists the field, `lead_schema` omits it, and every value a partner sends through it is dropped
with a 200. That is what `lead:substage` was, sitting beside `lead:custom_substage` for the same
`custom_substage` — and which of the two a contract happened to hold decided whether its partner could set
stage at all, on three of four contracts, with nothing going red anywhere.

These tests lock both ends:
  * the LIVE catalog — every writable key really resolves to a real column, and no field is catalogued
    twice. This is the seed's lock: a `.sql` seed writes rows straight to the table and never runs
    `validate`, so the door below cannot see it.
  * the DOOR — `CRMLeadAPIField.validate` refuses both shapes, so neither can be authored again.

The resolution test drives `_split_keys` itself rather than re-splitting the key here. Re-splitting would
be a third brain, and a third brain is what this whole file is about.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.api.test_catalog_key_addresses_its_field
"""
import frappe
from frappe.model import no_value_fields
from frappe.tests.utils import FrappeTestCase

from tatva_connect.api import partner

CATALOG = "CRM Lead API Field"
PARENT_SECTION = "lead"


def _resolved_fieldnames(key):
	"""What the API would actually collect for `key` — its own splitter, never a copy of it."""
	parent_fields, child_allow = partner._split_keys([key])
	return [*parent_fields, *(fn for fns in child_allow.values() for fn in fns)]


class TestCatalogKeyAddressesItsField(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# Read back through the SAME accessor `_split_keys` uses, so both see one catalog, not two.
		frappe.cache().delete_value(partner._CATALOG_CACHE_KEY)
		cls.catalog = partner._catalog()

	def test_no_field_is_catalogued_twice(self):
		"""Two rows for one (section, fieldname) is two answers to one question: a contract ticks one of
		them, and which one it holds decides whether the field works."""
		seen, twins = {}, []
		for r in frappe.get_all(CATALOG, fields=["name", "section", "fieldname"], order_by="name asc"):
			at = (r.section, r.fieldname)
			if at in seen:
				twins.append(f"{seen[at]} and {r.name} both catalogue {r.section}.{r.fieldname}")
			seen[at] = r.name
		self.assertEqual(twins, [], "a field is catalogued more than once: " + "; ".join(twins))

	def test_every_writable_key_collects_into_a_real_column(self):
		"""The defect itself, at the seam that had it: a writable key that splits to a name no column
		carries is a field a partner can be granted and can never actually set."""
		section_doctype = self.catalog["section_doctype"]
		broken = []
		for key in self.catalog["keys"]:
			section = key.partition(":")[0]
			target = section_doctype.get(section)
			if not target:
				broken.append(f"{key}: section {section!r} is not a lead section")
				continue
			for fieldname in _resolved_fieldnames(key):
				if not frappe.get_meta(target).get_field(fieldname):
					broken.append(f"{key}: collects into {target}.{fieldname}, which does not exist")
		self.assertEqual(
			broken, [],
			"writable catalog keys that resolve to nothing (a partner is granted the field and every "
			"value they send is dropped with a 200): " + "; ".join(broken),
		)

	def test_a_second_row_for_the_same_field_is_refused(self):
		"""A routing field, because those are the rows allowed a friendlier key — so this isolates the
		duplicate rule from the key rule rather than tripping both at once."""
		existing = frappe.db.get_value(
			CATALOG, {"section": PARENT_SECTION, "fieldname": "custom_vertical"}, "name"
		)
		self.assertTrue(existing, "expected custom_vertical to be catalogued (lead:product_line)")
		twin = frappe.get_doc({
			"doctype": CATALOG, "field_key": "lead:vertical", "label": "Vertical",
			"section": PARENT_SECTION, "fieldname": "custom_vertical",
		})
		with self.assertRaises(frappe.ValidationError) as caught:
			twin.insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no session user
		self.assertIn(existing, str(caught.exception))

	def test_a_writable_key_that_does_not_address_its_field_is_refused(self):
		"""The exact shape `lead:substage` had: a real, writable, not-yet-catalogued lead column behind a
		key whose suffix names something else."""
		catalogued = set(frappe.get_all(
			CATALOG, filters={"section": PARENT_SECTION}, pluck="fieldname"
		))
		spare = next(
			(
				df.fieldname for df in frappe.get_meta("CRM Lead").fields
				if df.fieldtype not in no_value_fields
				and df.fieldname not in catalogued
				and partner.is_writable(df.fieldname)
				and df.fieldname not in partner.ROUTING_FIELDS
			),
			None,
		)
		if not spare:
			self.skipTest("every writable CRM Lead field is already catalogued — nothing to author")
		row = frappe.get_doc({
			"doctype": CATALOG, "field_key": f"{PARENT_SECTION}:not_the_fieldname", "label": "Alias",
			"section": PARENT_SECTION, "fieldname": spare,
		})
		with self.assertRaises(frappe.ValidationError) as caught:
			row.insert(ignore_permissions=True)  # authz-ok: tier-c — test fixture, no session user
		self.assertIn(f"{PARENT_SECTION}:{spare}", str(caught.exception))
