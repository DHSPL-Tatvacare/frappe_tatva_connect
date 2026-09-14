# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The hover preview: the server's declared rows, the doctype's own labels, the caller's permlevels, one read.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.api.test_lead_preview
"""
from unittest.mock import patch

import frappe
from frappe.model.meta import Meta
from frappe.tests.utils import FrappeTestCase

from tatva_connect.api import lead_preview
from tatva_connect.taxonomy import labels

USER = "zz-lead-preview@example.com"
PHONE = "+916100060001"
MISSING = "CRM-LEAD-0000-99999"
LEAD = lead_preview.LEAD


class TestLeadPreview(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._purge()
		if not frappe.db.exists("User", USER):
			# Default roles only: no CRM DocPerm at all, so the read gate refuses at the doctype level.
			frappe.get_doc({
				"doctype": "User", "email": USER, "first_name": "Lead Preview",
				"send_welcome_email": 0, "user_type": "System User",
			}).insert(ignore_permissions=True)
		cls.lead = frappe.get_doc({
			"doctype": LEAD, "first_name": "Preview", "last_name": "Patient",
			"mobile_no": PHONE, "status": "New",
		}).insert(ignore_permissions=True).name
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		cls._purge()
		if frappe.db.exists("User", USER):
			frappe.delete_doc("User", USER, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	@classmethod
	def _purge(cls):
		for name in frappe.get_all(LEAD, filters={"mobile_no": PHONE}, pluck="name"):
			frappe.delete_doc(LEAD, name, force=True, ignore_permissions=True)

	def test_the_payload_is_a_title_a_subtitle_an_image_and_rows(self):
		card = lead_preview.get_lead_preview(self.lead)
		self.assertEqual(set(card), {"title", "subtitle", "image", "rows"})
		self.assertEqual(card["subtitle"], PHONE)
		for row in card["rows"]:
			self.assertEqual(set(row), {"label", "value"})

	def test_the_rows_are_the_lead_id_then_the_declared_fields_under_the_doctypes_own_labels(self):
		meta = frappe.get_meta(LEAD)
		card = lead_preview.get_lead_preview(self.lead)
		expected = ["Lead ID", *(meta.get_field(f).label for f in lead_preview.ROWS)]
		self.assertEqual([r["label"] for r in card["rows"]], expected)
		self.assertEqual(card["rows"][0]["value"], self.lead)

	def test_a_link_value_reads_as_its_title_never_its_key(self):
		doc = frappe.get_doc(LEAD, self.lead)
		card = lead_preview.get_lead_preview(self.lead)
		values = [r["value"] for r in card["rows"][1:]]
		expected = [labels.shown(LEAD, f, doc.get(f)) or "" for f in lead_preview.ROWS]
		self.assertEqual(values, expected)

	def test_a_field_above_the_callers_permlevel_is_never_a_row(self):
		meta = frappe.get_meta(LEAD)
		hidden = {meta.get_field(f).label for f in lead_preview.ROWS if meta.get_field(f).permlevel}
		self.assertTrue(hidden, "no declared row sits above permlevel 0, so this lock proves nothing")
		with patch.object(Meta, "get_permlevel_access", return_value=[0]):
			card = lead_preview.get_lead_preview(self.lead)
		self.assertFalse(hidden & {r["label"] for r in card["rows"]})

	def test_one_card_open_is_one_document_read(self):
		real = frappe.get_cached_doc
		with patch.object(frappe, "get_cached_doc", side_effect=real) as loader:
			lead_preview.get_lead_preview(self.lead)
		reads = [c for c in loader.call_args_list if c.args and c.args[0] == LEAD]
		self.assertEqual(len(reads), 1, f"the preview read CRM Lead {len(reads)} times for one card")

	def test_a_lead_the_caller_may_not_read_is_refused(self):
		frappe.set_user(USER)
		try:
			with self.assertRaises(frappe.PermissionError):
				lead_preview.get_lead_preview(self.lead)
		finally:
			frappe.set_user("Administrator")

	def test_a_lead_that_does_not_exist_reads_as_missing_not_refused(self):
		for user in (USER, "Administrator"):
			frappe.set_user(user)
			try:
				with self.assertRaises(frappe.DoesNotExistError):
					lead_preview.get_lead_preview(MISSING)
			finally:
				frappe.set_user("Administrator")
