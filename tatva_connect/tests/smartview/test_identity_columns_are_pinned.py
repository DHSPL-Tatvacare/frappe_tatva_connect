# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A row says whose it is, whatever the view's author picked.

A Smart View could project any column set at all, so a view of Created On and Stage was a table nobody
could read: no name, no number, nothing that named the patient. `_identity_key` had to GUESS which cell
identified the row (the first Data column) precisely because the catalog carried no dependable identity.

Frappe's own saved views already work this way — `crm_view_settings.create` adds `default_list_data()`'s
rows to whatever the author chose — so "columns a view must carry" is a native idea, not one invented here.

CATALOG-BOUNDED LIKE EVERYTHING ELSE. A pinned key the caller's grain or role withholds is DROPPED, never
forced: a column nobody may see is the leak this surface exists to refuse.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.smartview.test_identity_columns_are_pinned
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.smartview import api as smartview


class TestIdentityColumnsArePinned(FrappeTestCase):
	def _cat(self):
		return smartview._catalog_fields("Lead", None, smartview.entitlement.ALL_GRAINS, frappe.get_roles())

	def test_a_saved_set_that_omits_them_still_carries_them(self):
		"""THE defect: a view saved with four unrelated columns showed four unrelated columns."""
		cat = self._cat()
		pinned = smartview._pinned("Lead", cat)
		self.assertTrue(pinned, "this bench catalogs no identity column to pin")
		keys = smartview._with_pinned(["lead:custom_substage"], "Lead", cat)
		self.assertEqual(keys[: len(pinned)], list(pinned), "the identity columns lead the set")
		self.assertIn("lead:custom_substage", keys, "the author's own choice is kept")

	def test_they_are_not_duplicated_when_the_author_chose_them_too(self):
		cat = self._cat()
		pinned = list(smartview._pinned("Lead", cat))
		keys = smartview._with_pinned([pinned[0], "lead:custom_substage"], "Lead", cat)
		self.assertEqual(len(keys), len(set(keys)), "a chosen pinned column must not appear twice")

	def test_a_pinned_key_outside_the_callers_catalog_is_dropped_not_forced(self):
		"""Fail-closed: pinning may never widen what a caller sees."""
		narrowed = {k: v for k, v in self._cat().items() if k != "lead:mobile_no"}
		self.assertNotIn("lead:mobile_no", smartview._pinned("Lead", narrowed))

	def test_an_activity_view_pins_nothing(self):
		"""Its catalog is the task type's declared form fields — a title or a due date is not a key it
		could name, and the type is already constant for the whole view."""
		self.assertEqual(smartview._pinned("Activity", self._cat()), ())

	def test_the_picker_is_told_the_same_answer_the_composer_projects(self):
		"""One declaration. A picker that offered to drop a column the read path puts back would be lying."""
		cat = self._cat()
		offered = {c["field_key"] for c in smartview.field_catalog("Lead") if c.get("pinned")}
		self.assertEqual(offered, set(smartview._pinned("Lead", cat)))
