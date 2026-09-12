# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A watched child-section column can CHANGE, and the dispatcher must see it.

`changed to` reads a before-value, and only a watched field carries one. The diff resolved every watched
name off the lead's own meta, so a tick on a section column — `touch_at` on Acquisition, a counter on
Activity Metrics — was accepted by the catalog and then watched nothing: the name is not a field of CRM
Lead at all, so the diff skipped it and every transition criterion built on it was silent.

A section column is addressed the way a criterion names it, `<child_table>.<column>`, and is diffed on the
section's CURRENT reading — the same value `section_values` gives the rule, the Data tab and a Smart View.
For a multi-row section that reading is its newest, which is what makes one more acquisition touch a
patient arriving again rather than a record being edited.

A lead BORN with a section row has not changed: there is no before, and a first arrival is not a return.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.automation.test_section_watch
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.api import partner
from tatva_connect.automation import context, fields
from tatva_connect.lead_sync import catalog_seed
from tatva_connect.tests.api import partner_fixture

PHONE = "+916100060001"
OLD_TOUCH = "2026-07-20 10:00:00"
NEW_TOUCH = "2026-08-20 10:00:00"


class TestSectionWatch(FrappeTestCase):
	PARTNER_USER = "zz-section-watch@example.com"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._purge_leads()
		partner_fixture.mint_partner(cls.PARTNER_USER)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		cls._purge_leads()
		partner_fixture.teardown()
		frappe.db.commit()
		super().tearDownClass()

	@classmethod
	def _purge_leads(cls):
		for name in frappe.get_all("CRM Lead", filters={"mobile_no": ["like", "+91610006%"]}, pluck="name"):
			frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)

	def setUp(self):
		frappe.set_user("Administrator")
		self.table, self.column = partner_fixture.touch_address()
		self.path = f"{self.table}.{self.column}"
		self.sp = f"secwatch_{frappe.generate_hash(length=6)}"
		frappe.db.savepoint(self.sp)
		self.lead = self._lead_with_a_touch()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point=self.sp)
		self._watch(False)

	def _watch(self, on):
		"""Tick the catalog row, then drop the per-request cache the dispatcher reads it through."""
		frappe.db.set_value("CRM Lead API Field", catalog_seed.TOUCH_KEY, "can_watch", 1 if on else 0)
		frappe.flags.pop("_watchable_fields_cache", None)

	def _create(self, extra=None):
		frappe.set_user(self.PARTNER_USER)
		try:
			payload = frappe._dict({"mobile_no": PHONE, "first_name": "Asha Section"})
			payload.update(extra or {})
			_user, mp, is_sysmgr, parent_fields, child_allow = partner._caller_fields()
			doc, _action = partner._upsert_one(payload, mp, is_sysmgr, parent_fields, child_allow, [])
		finally:
			frappe.set_user("Administrator")
		return doc

	def _lead_with_a_touch(self):
		doc = self._create({self.table: [{self.column: OLD_TOUCH}]})
		return frappe.get_doc("CRM Lead", doc.name)

	def _save_with(self, **values):
		"""Save the lead and hand back the diff the dispatcher would build from that save."""
		doc = frappe.get_doc("CRM Lead", self.lead.name)
		for field, value in values.items():
			doc.set(field, value)
		doc.save(ignore_permissions=True)
		return context.diff_watched_fields(doc)

	def _save_with_a_new_touch(self):
		doc = frappe.get_doc("CRM Lead", self.lead.name)
		doc.append(self.table, {self.column: NEW_TOUCH})
		doc.save(ignore_permissions=True)
		return context.diff_watched_fields(doc)

	# -- the tick is what decides, and it decides both ways ---------------------

	def test_a_watched_section_column_is_named_the_way_a_rule_names_it(self):
		self._watch(True)
		self.assertIn(self.path, fields.watchable_fields("CRM Lead"))
		self.assertTrue(fields.is_watchable("CRM Lead", self.path))

	def test_a_new_touch_is_a_change(self):
		self._watch(True)
		changed = self._save_with_a_new_touch()
		self.assertIn(self.path, changed, "a newer acquisition touch was not seen as a change")
		was, now = changed[self.path]
		self.assertEqual(frappe.utils.get_datetime(was), frappe.utils.get_datetime(OLD_TOUCH))
		self.assertEqual(frappe.utils.get_datetime(now), frappe.utils.get_datetime(NEW_TOUCH))

	def test_an_edit_that_leaves_the_section_alone_is_not_a_change(self):
		"""The defect this guards: a rep renaming a lead must not read as the patient arriving again."""
		self._watch(True)
		self.assertNotIn(self.path, self._save_with(first_name="Asha Renamed"))

	def test_an_unticked_column_is_watched_by_nobody(self):
		self._watch(False)
		self.assertNotIn(self.path, self._save_with_a_new_touch())

	def test_a_lead_born_with_a_touch_has_not_changed(self):
		"""A first arrival is not a return, so a Created save carries no section before-value."""
		self._watch(True)
		frappe.delete_doc("CRM Lead", self.lead.name, force=True, ignore_permissions=True)
		born = self._create({self.table: [{self.column: OLD_TOUCH}]})
		self.assertNotIn(self.path, context.diff_watched_fields(born))
