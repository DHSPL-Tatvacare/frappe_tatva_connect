# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A PDF sent under a .jpg name through the partner endpoint is refused cleanly, or saved as .pdf when the operator allows that rename."""
import base64
import io

import frappe
from PIL import Image

from tatva_connect.api import partner_file
from tatva_connect.tests.storage.test_file_lifecycle_seam import SeamCase, _jobs_inline

_SETTINGS = "CRM File Screening Settings"


def _pdf():
	"""A minimal one-page PDF with a correct cross-reference table, so core's own PDF check can read it."""
	objs = [b"<</Type/Catalog/Pages 2 0 R>>", b"<</Type/Pages/Kids[3 0 R]/Count 1>>", b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>"]
	out, offsets = bytearray(b"%PDF-1.4\n"), []
	for i, body in enumerate(objs, 1):
		offsets.append(len(out))
		out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
	xref = len(out)
	out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offsets)
	out += b"trailer\n<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
	return bytes(out)


def _png():
	out = io.BytesIO()
	Image.new("RGB", (4, 4)).save(out, format="PNG")
	return out.getvalue()


class TestAMismatchedFileIsRenamedOrRefused(SeamCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self._system_setting(strip_exif_metadata_from_uploaded_images=1, allowed_file_extensions="JPG\nPNG\nPDF")
		self.lead = self._lead("Rename Test")
		self.stem = self._stem()
		self._renames(on=0, pairs=())

	def _renames(self, *, on, pairs):
		settings = frappe.get_single(_SETTINGS)
		settings.correct_mismatched_extension = on
		settings.set("extension_corrections", [{"named_as": a, "actually_is": b} for a, b in pairs])
		settings.save(ignore_permissions=True)
		frappe.clear_document_cache(_SETTINGS, _SETTINGS)

	def _attach(self, extension="jpg", content=None):
		frappe.local.response = frappe._dict()
		frappe.form_dict = frappe._dict(
			lead=self.lead.name, filename=f"{self.stem}.{extension}", content_base64=base64.b64encode(content or _pdf()).decode()
		)
		partner_file.file_attach()
		return dict(frappe.local.response)

	def _files_on_lead(self):
		return frappe.get_all("File", {"attached_to_doctype": "CRM Lead", "attached_to_name": self.lead.name}, pluck="file_name")

	def test_switch_off_refuses_with_type_mismatch_and_stores_nothing(self):
		body = self._attach()
		self.assertEqual(body.get("http_status_code"), 400, body)
		self.assertEqual(body["error"]["code"], "validation_error", body)
		self.assertEqual(body["error"]["detail"]["verdict"], "Type Mismatch", body)
		self.assertEqual(self._files_on_lead(), [], "a refused file must not be stored")

	def test_screening_off_saves_a_mismatch_core_can_store(self):
		for extension, content in (("png", _pdf()), ("jpg", _png())):
			with self.subTest(extension=extension):
				body = self._attach(extension, content)
				self.assertEqual(body.get("status"), "success", body)
				self.assertIn(f"{self.stem}.{extension}", self._files_on_lead())

	def test_a_listed_rename_saves_the_real_type_and_says_so(self):
		self._renames(on=1, pairs=[("jpg", "pdf")])
		log = []
		with _jobs_inline(log):
			body = self._attach()
		self.assertEqual(body.get("status"), "success", body)
		self.assertEqual(body["data"]["renamed_from"], f"{self.stem}.jpg")
		self.assertEqual(self._files_on_lead(), [f"{self.stem}.pdf"])
		self.assertTrue(frappe.db.exists("Comment", {
			"reference_doctype": "CRM Lead", "reference_name": self.lead.name, "comment_type": "Comment",
			"content": ["like", f"%{self.stem}.jpg was renamed to {self.stem}.pdf%"],
		}), "the rename is not noted on the lead")
		self.assertTrue(frappe.db.exists("CRM File Scan Log", {
			"verdict": "Corrected", "file_name": f"{self.stem}.jpg", "renamed_to": f"{self.stem}.pdf",
		}), "the rename is not in the scan log")

	def test_an_unlisted_pair_is_still_refused(self):
		self._renames(on=1, pairs=[("png", "pdf")])
		body = self._attach()
		self.assertEqual(body["error"]["detail"]["verdict"], "Type Mismatch", body)
		self.assertEqual(self._files_on_lead(), [])

	def test_settings_refuse_a_rename_the_site_could_never_store(self):
		with self.assertRaises(frappe.ValidationError):
			self._renames(on=1, pairs=[("jpg", "xyz")])
