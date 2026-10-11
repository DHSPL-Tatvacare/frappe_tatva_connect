# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Every screening check, the rename included, runs only behind the gate (the switch on AND the channel listed); a listed channel renames a mismatch only when its own row says so, and refuses an image core cannot store."""
import base64
import io

import frappe
from PIL import Image

from tatva_connect.api import partner_file
from tatva_connect.tests.storage.test_file_lifecycle_seam import _SCREENING, SeamCase, _jobs_inline

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


def _image(fmt):
	out = io.BytesIO()
	Image.new("RGB", (4, 4)).save(out, format=fmt)
	return out.getvalue()


# The EICAR antivirus test string: harmless, and every scanner reports it as infected.
_EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


def _corrupt_jpeg():
	"""A real JPEG header over a broken body: the name check passes, core's EXIF step cannot decode it."""
	return _image("JPEG")[:20] + b"\x00" * 400


class TestAMismatchedFileIsRenamedOrRefused(SeamCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self._system_setting(strip_exif_metadata_from_uploaded_images=1, allowed_file_extensions="JPG\nPNG\nPDF")
		self.lead = self._lead("Rename Test")
		self.stem = self._stem()
		self._gate(on=0, channels={})

	def _gate(self, *, on, channels, pairs=(("jpg", "pdf"),), blocking=()):
		"""Arm the switch and list `channels` as {channel: rename tick}; the pairs are the one shared Allowed Renames list, `blocking` the Blocking Verdicts grid."""
		self._toggle(_SCREENING, on)
		settings = frappe.get_single(_SETTINGS)
		settings.scanner_unavailable = "allow"
		settings.set("active_channels", [{"channel": c, "rename_mismatched": r} for c, r in channels.items()])
		settings.set("extension_corrections", [{"named_as": a, "actually_is": b} for a, b in pairs])
		settings.set("blocking_verdicts", [{"verdict": v} for v in blocking])
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

	def _scan_rows(self):
		return frappe.get_all("CRM File Scan Log", {"file_name": ["like", f"{self.stem}%"]}, pluck="verdict")

	def test_off_the_gate_nothing_of_ours_runs(self):
		"""RED before: the rename ran on every channel, screened or not. PNG bytes under .jpg, which core itself can store."""
		for on, channels in ((0, {"Partner API": 1}), (1, {"Intake": 1})):
			with self.subTest(switch=on, channels=channels):
				self._gate(on=on, channels=channels, pairs=(("jpg", "png"),))
				self.stem = self._stem()
				log = []
				with _jobs_inline(log):
					body = self._attach("jpg", _image("PNG"))
				self.assertEqual(body.get("status"), "success", body)
				self.assertNotIn("renamed_from", body["data"], "a rename ran off the gate")
				self.assertIn(f"{self.stem}.jpg", self._files_on_lead(), "saved under any other name than the one sent")
				self.assertEqual(self._scan_rows(), [], "a scan-log row means screening ran off the gate")

	def test_a_listed_channel_without_the_tick_refuses_and_stores_nothing(self):
		self._gate(on=1, channels={"Partner API": 0})
		body = self._attach()
		self.assertEqual(body.get("http_status_code"), 400, body)
		self.assertEqual(body["error"]["detail"]["verdict"], "Type Mismatch", body)
		self.assertEqual(self._files_on_lead(), [], "a refused file must not be stored")

	def test_a_listed_channel_with_the_tick_saves_the_real_type_and_says_so(self):
		self._gate(on=1, channels={"Partner API": 1})
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
		self._gate(on=1, channels={"Partner API": 1}, pairs=(("png", "pdf"),))
		body = self._attach()
		self.assertEqual(body["error"]["detail"]["verdict"], "Type Mismatch", body)
		self.assertEqual(self._files_on_lead(), [])

	def test_an_image_core_cannot_store_is_refused_with_its_own_verdict(self):
		"""RED before: the name matched, core's EXIF step raised, and the partner got a 500."""
		self._gate(on=1, channels={"Partner API": 0})
		body = self._attach("jpg", _corrupt_jpeg())
		self.assertEqual(body.get("http_status_code"), 400, body)
		self.assertEqual(body["error"]["code"], "validation_error", body)
		self.assertEqual(body["error"]["detail"]["verdict"], "Unreadable Image", body)
		self.assertEqual(self._files_on_lead(), [], "a refused file must not be stored")

	def test_a_good_image_on_a_listed_channel_is_stored(self):
		self._gate(on=1, channels={"Partner API": 0})
		body = self._attach("jpg", _image("JPEG"))
		self.assertEqual(body.get("status"), "success", body)
		self.assertEqual(self._files_on_lead(), [f"{self.stem}.jpg"])

	def test_a_type_the_operator_lets_through_is_still_scanned(self):
		"""RED before: a Type Not Allowed left out of Blocking Verdicts was stored without the malware scan."""
		self._system_setting(allowed_file_extensions="JPG\nPNG\nPDF")
		self._gate(on=1, channels={"Partner API": 0}, blocking=("Type Mismatch", "Infected"))
		body = self._attach("txt", _EICAR)
		self.assertEqual(body.get("http_status_code"), 400, body)
		self.assertEqual(body["error"]["detail"]["verdict"], "Infected", body)
		self.assertEqual(self._files_on_lead(), [], "an infected file must not be stored")

	def test_a_clean_type_the_operator_lets_through_is_stored_and_logged(self):
		self._system_setting(allowed_file_extensions="JPG\nPNG\nPDF")
		self._gate(on=1, channels={"Partner API": 0}, blocking=("Type Mismatch", "Infected"))
		log = []
		with _jobs_inline(log):
			body = self._attach("txt", b"plain notes")
		self.assertEqual(body.get("status"), "success", body)
		self.assertEqual(self._files_on_lead(), [f"{self.stem}.txt"])
		self.assertEqual(self._scan_rows(), ["Type Not Allowed"], "the verdict the operator allowed is still recorded")

	def test_settings_refuse_a_rename_the_site_could_never_store(self):
		with self.assertRaises(frappe.ValidationError):
			self._gate(on=0, channels={}, pairs=(("jpg", "xyz"),))
