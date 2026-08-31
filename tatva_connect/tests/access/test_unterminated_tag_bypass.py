# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A tag that never closes must not reach the database, and prose containing `<` must not be touched.

Frappe's write-time filter returns a value UNCHANGED when BeautifulSoup's strict `html.parser` finds no
tag in it, and an unterminated `<iframe src="javascript:...` contains no tag by that parser's reckoning.
Browsers disagree and run it. The payload below is the one that was actually stored, byte for byte.

These assert the STORED value read back from the database, not that a function was called.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.access.test_unterminated_tag_bypass
"""
import frappe
from frappe.tests.utils import FrappeTestCase

# The exact string found on the site, cut to 140 chars by the field's length and so left unterminated.
PAYLOAD = (
	'Courtesy Visit Field Visit<iframe xmlns="http://www.w3.org/1999/xhtml" '
	'src="javascript:alert(document.cookie);" width="400" heigh'
)


class TestUnterminatedTagBypass(FrappeTestCase):
	# The guard is an operator switch and ships dormant, so a suite that does not arm it proves only that
	# a dormant switch is dormant. It is armed here and restored after, never left set on the bench.
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
		"""Nothing legitimate survives the clean, so the mandatory check refuses the save. Refusal is the point."""
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
