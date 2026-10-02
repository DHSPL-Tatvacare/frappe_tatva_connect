# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A tag that never closes is stripped before it is stored, and prose containing `<` is stored unchanged.
Frappe's filter skips values its parser sees no tag in, so the tests read the stored value back."""
import frappe
from frappe.tests import IntegrationTestCase

# Cut to 140 chars by the field length, so the tag is left unterminated.
PAYLOAD = (
	'Courtesy Visit Field Visit<iframe xmlns="http://www.w3.org/1999/xhtml" '
	'src="javascript:alert(document.cookie);" width="400" heigh'
)


class TestUnterminatedTagBypass(IntegrationTestCase):
	# The guard ships dormant, so each test arms it and restores the old value after.
	_SWITCH = "Access::Desk::sanitize"

	def setUp(self):
		self._was = frappe.db.get_value("CRM Tatva Automation", self._SWITCH, "enabled")
		frappe.db.set_value("CRM Tatva Automation", self._SWITCH, "enabled", 1)
		frappe.clear_cache()

	def tearDown(self):
		frappe.db.set_value("CRM Tatva Automation", self._SWITCH, "enabled", self._was)
		frappe.clear_cache()

	def _task(self, title):
		doc = frappe.get_doc({"doctype": "CRM Task", "title": title}).insert()
		self.addCleanup(frappe.delete_doc, "CRM Task", doc.name, force=True)
		return frappe.db.get_value("CRM Task", doc.name, "title")

	def test_the_stored_payload_never_reaches_the_database(self):
		stored = self._task(PAYLOAD)
		self.assertNotIn("<iframe", stored)
		self.assertNotIn("javascript:", stored)
		self.assertEqual(stored, "Courtesy Visit Field Visit")

	def test_an_unterminated_img_onerror_is_neutralised(self):
		stored = self._task("ZZ probe<img src=x onerror=alert(1)")
		self.assertEqual(stored, "ZZ probe")

	def test_a_terminated_tag_is_still_neutralised(self):
		stored = self._task('ZZ probe<iframe src="javascript:alert(1)"></iframe>')
		self.assertEqual(stored, "ZZ probe")

	def test_a_title_that_is_ENTIRELY_markup_is_refused(self):
		"""A title that is only markup is empty after cleaning, so the mandatory check refuses the save."""
		with self.assertRaises(frappe.MandatoryError):
			frappe.get_doc({"doctype": "CRM Task", "title": "<img src=x onerror=alert(1)"}).insert()

	def test_prose_with_a_less_than_survives_byte_identical(self):
		for prose in ("BP < 120 mmHg", "Dose A < B", "count <= 5", "2 < 3 and 4 > 1"):
			with self.subTest(prose=prose):
				self.assertEqual(self._task(prose), prose)

	def test_a_child_row_is_covered_too(self):
		doc = frappe.get_doc(
			{
				"doctype": "CRM Task",
				"title": "ZZ child row probe",
				"custom_engagement": [{"reasons": "ZZ item<img src=x onerror=alert(1)"}],
			}
		).insert()
		self.addCleanup(frappe.delete_doc, "CRM Task", doc.name, force=True)
		stored = frappe.db.get_value("CRM Task Engagement", {"parent": doc.name}, "reasons")
		self.assertNotIn("<img", stored)
		self.assertNotIn("onerror", stored)
