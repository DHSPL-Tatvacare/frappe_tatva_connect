# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The screening config surface: a picker where a picker belongs, and frappe's own detector for the bytes.

Two things were hand-rolled here and both already existed. Blocking Verdicts was free text validated in a
controller against a closed set — which is a picker done badly, next to `active_channels`, an actual
picker on the same form. And the byte check carried a hex signature table an operator was expected to
keep current, when frappe already ships `filetype` and already sniffs uploads with it
(`core/doctype/file/utils.py:76-80`).

The replacement's whole risk is what `filetype.match` answers, so these drive the REAL doctype and the
REAL upload rather than a parser:

  * plain text has NO fingerprint — `filetype.match` returns None for CSV, TXT and JSON — so the
    "no fingerprint, nothing can contradict it" branch is the one keeping every text upload alive. It is
    asserted first because losing it refuses every patient CSV on the site.
  * a .docx IS a zip, so filetype may answer `docx` or the bare `zip` for the same real file, and both
    are the truth. The container family is read out of filetype's own registry, never listed by us.
  * a verdict the Select does not offer cannot be saved at all, which is why the controller check for it
    is gone rather than kept alongside.

Plan: docs/plans/2026-07-21-storage-file-lifecycle-seam.md (Phase 2, 2.6 / V2.6)
Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.storage.test_screening_config_surface
"""
import io
import zipfile
from contextlib import contextmanager

import frappe

from tatva_connect.storage import file_screening
from tatva_connect.tests.storage.test_file_lifecycle_seam import SeamCase, _jobs_inline

_SCREENING = "Storage::File::screening"
_SCREEN_SETTINGS = "CRM File Screening Settings"
_VERDICT_TABLE = "CRM Screened Verdict"
_SCAN_LOG = "CRM File Scan Log"

# Every type these tests upload, so the platform list is never the thing that refuses one — the byte
# check is what is under test and it runs only after the extension gate has already said yes.
_EXTENSIONS = "CSV\nTXT\nPDF\nPNG\nJPG\nDOCX\nGIF"  # GIF: a disguise core itself has no opinion on


def _image(fmt):
	"""A genuinely decodable image in `fmt` — core opens a real PDF with pypdf and a real JPEG with PIL,
	so a hand-typed header would be refused for reasons that have nothing to do with screening. The pixel
	is coloured at random because core dedups on content_hash: identical bytes reuse the existing row
	without writing anything, and the outcome under test would never happen."""
	import random

	from PIL import Image

	buf = io.BytesIO()
	colour = (random.randrange(256), random.randrange(256), random.randrange(256))
	Image.new("RGB", (2, 2), colour).save(buf, format=fmt)
	return buf.getvalue()


def _ooxml(root):
	"""A real Office Open XML file: a zip whose members are the ones a .docx really carries."""
	buf = io.BytesIO()
	with zipfile.ZipFile(buf, "w") as archive:
		archive.writestr("[Content_Types].xml", "<Types/>")
		archive.writestr(f"{root}/document.xml", f"<x>{frappe.generate_hash(length=16)}</x>")
	return buf.getvalue()


class ScreeningCase(SeamCase):
	"""Screening on, scanner outage tolerated, one platform list wide enough to reach the byte check."""

	def setUp(self):
		super().setUp()
		self._toggle(_SCREENING, 1)
		self._screening(scanner_unavailable="allow", channels=("Intake", "Partner API"))
		self._system_setting(allowed_file_extensions=_EXTENSIONS)

	def _screening(self, *, channels=None, verdicts=None, **fields):
		settings = frappe.get_single(_SCREEN_SETTINGS)
		for key, value in fields.items():
			settings.set(key, value)
		if channels is not None:
			settings.set("active_channels", [])
			for channel in channels:
				settings.append("active_channels", {"channel": channel})
		if verdicts is not None:
			settings.set("blocking_verdicts", [])
			for verdict in verdicts:
				settings.append("blocking_verdicts", {"verdict": verdict})
		settings.save(ignore_permissions=True)
		frappe.clear_document_cache(_SCREEN_SETTINGS, _SCREEN_SETTINGS)

	@contextmanager
	def _as_patient(self):
		"""Intake publishes with login_required=0, so a patient's upload is an unauthenticated request."""
		frappe.set_user("Guest")
		try:
			yield
		finally:
			frappe.set_user("Administrator")

	def _unique(self, content):
		"""Core dedups on content_hash and reuses the existing row without writing, which would make an
		outcome assertion meaningless — every payload has to be bytes this site has not seen."""
		return content + b"\n" + frappe.generate_hash(length=16).encode()

	def _scan_rows(self, stem):
		return frappe.get_all(
			_SCAN_LOG, filters={"file_name": ["like", f"{stem}%"]}, fields=["verdict", "action"]
		)

	def assert_accepted(self, suffix, content, label):
		stem = self._stem()
		with _jobs_inline(), self._as_patient():
			doc = self._insert(file_name=f"{stem}.{suffix}", content=content)
		rows = self._scan_rows(stem)
		self.assertEqual(len(rows), 1, f"{label}: one upload did not produce exactly one scan")
		self.assertEqual(rows[0].action, "Accepted", f"{label}: a genuine file was refused")
		self.assertEqual(doc.is_private, 1, f"{label}: a patient upload must be private")

	def assert_refused(self, suffix, content, label):
		stem = self._stem()
		with _jobs_inline(), self._as_patient(), self.assertRaises(frappe.ValidationError):
			self._insert(file_name=f"{stem}.{suffix}", content=content)
		rows = self._scan_rows(stem)
		self.assertEqual(len(rows), 1, f"{label}: the upload was refused, but not by screening")
		self.assertEqual(rows[0].verdict, "Type Mismatch", f"{label}: refused for the wrong reason")
		self.assertEqual(self._on_disk(stem), [], f"{label}: a refused file's bytes are on the app server")


class TestTextUploadsAreNotRefused(ScreeningCase):
	"""THE risk of moving to filetype. `filetype.match` places 70-odd binary formats and answers None for
	everything else, and plain text is everything else: a CSV, a TXT and a JSON have no magic bytes to
	read. If None were treated as "could not verify" instead of "nothing can contradict this", every
	patient CSV and every exported report on this site would start failing at upload.

	The branch that keeps them alive is the `if not kind: return True` in `file_screening._sniff`."""

	def test_a_csv_is_accepted(self):
		self.assert_accepted("csv", self._unique(b"name,phone,city\nA,9000000000,Pune"), "csv")

	def test_a_txt_is_accepted(self):
		self.assert_accepted("txt", self._unique(b"a plain clinical note"), "txt")

	def test_filetype_really_places_nothing_on_text(self):
		"""The premise, stated where it can rot: the pass above is only correct while filetype answers None."""
		import filetype

		for content in (b"name,phone\nA,9\n", b"a plain note\n", b'{"a": 1}'):
			self.assertIsNone(filetype.match(content), "filetype now places text — the pass branch is wrong")


class TestRealFilesPass(ScreeningCase):
	"""A genuine file must never be refused by the check that exists to catch a fake one."""

	def test_a_real_pdf_is_accepted(self):
		self.assert_accepted("pdf", _image("PDF"), "pdf")

	def test_a_real_png_is_accepted(self):
		self.assert_accepted("png", _image("PNG"), "png")

	def test_a_real_jpeg_is_accepted(self):
		self.assert_accepted("jpg", _image("JPEG"), "jpeg")

	def test_a_real_docx_is_accepted(self):
		"""A .docx IS a zip. filetype answers `docx` for a well-formed one and `zip` for a real file whose
		members sit in another order, and both are the truth about the same bytes — so the comparison
		accepts the container family, which is read out of filetype's own registry."""
		self.assert_accepted("docx", _ooxml("word"), "docx")

	def test_a_docx_that_reads_as_a_bare_zip_is_accepted(self):
		plain = io.BytesIO()
		with zipfile.ZipFile(plain, "w") as archive:
			archive.writestr("word/document.xml", frappe.generate_hash(length=16))
		self.assert_accepted("docx", plain.getvalue(), "docx read as zip")


class TestDisguisedFilesAreRefused(ScreeningCase):
	"""The check's entire purpose: the name says one thing and the bytes say another."""

	def test_a_docx_carrying_pdf_bytes_is_refused(self):
		self.assert_refused("docx", _image("PDF"), "pdf wearing .docx")

	def test_a_pdf_carrying_png_bytes_is_refused(self):
		self.assert_refused("pdf", _image("PNG"), "png wearing .pdf")

	def test_a_png_carrying_jpeg_bytes_is_refused(self):
		self.assert_refused("png", _image("JPEG"), "jpeg wearing .png")

	def test_a_pdf_carrying_an_executable_is_refused(self):
		self.assert_refused("pdf", self._unique(b"MZ\x90\x00\x03" + b"\x00" * 64), "exe wearing .pdf")


class TestBlockingVerdictsAreAPicker(ScreeningCase):
	"""Free text validated against a closed set is a picker done badly. The Select IS the enforcement, so
	there is no controller check left to keep in step with it."""

	def test_scanner_unavailable_is_not_on_offer(self):
		"""It is a 503 about OUR outage, not a 400 about their file, and it has its own field."""
		options = frappe.get_meta(_VERDICT_TABLE).get_field("verdict").options.splitlines()
		self.assertEqual(options, ["Type Not Allowed", "Type Mismatch", "Infected"])
		self.assertNotIn("Scanner Unavailable", options)

	def test_scanner_unavailable_cannot_be_saved_as_a_blocking_verdict(self):
		with self.assertRaises(frappe.ValidationError):
			self._screening(verdicts=("Scanner Unavailable",))

	def test_an_empty_grid_still_blocks_all_three(self):
		self._screening(verdicts=())
		self.assertEqual(
			file_screening._blocking_verdicts(),
			["Type Not Allowed", "Type Mismatch", "Infected"],
			"an empty grid must be the shipped list, never 'block nothing'",
		)

	def test_an_empty_grid_really_refuses_a_disguised_file(self):
		"""The fallback asserted as an outcome rather than as a list: nothing is configured, and the file
		is still refused."""
		self._screening(verdicts=())
		self.assert_refused("pdf", _image("PNG"), "empty grid")

	def test_a_verdict_removed_from_the_grid_stops_refusing(self):
		"""The reason the grid exists: a check can be watched before it is enforced. The verdict is still
		reached and still logged — it simply no longer stops the file.

		Disguised as GIF, not as PDF: core runs its own JavaScript scan over anything claiming to be a PDF
		(file.py:472 -> pdf_contains_js) and refuses bytes it cannot parse, which is a different gate from
		ours and would refuse this file no matter what the grid says."""
		self._screening(verdicts=("Type Not Allowed", "Infected"))
		stem = self._stem()
		with _jobs_inline(), self._as_patient():
			doc = self._insert(file_name=f"{stem}.gif", content=_image("PNG"))
		rows = self._scan_rows(stem)
		self.assertEqual(len(rows), 1, "the upload was not screened")
		self.assertEqual(rows[0].verdict, "Type Mismatch", "the verdict was not reached")
		self.assertEqual(rows[0].action, "Accepted", "a verdict off the grid still refused the file")
		self.assertEqual(doc.is_private, 1)


class TestTheHandRolledSurfacesAreGone(ScreeningCase):
	"""A demoted field is a second source of truth, so both are deleted outright rather than hidden."""

	def test_the_signature_table_field_is_gone(self):
		self.assertIsNone(
			frappe.get_meta(_SCREEN_SETTINGS).get_field("type_signatures"),
			"a hand-maintained signature table is back beside frappe's own detector",
		)

	def test_blocking_verdicts_is_a_table_of_the_verdict_child(self):
		field = frappe.get_meta(_SCREEN_SETTINGS).get_field("blocking_verdicts")
		self.assertEqual(field.fieldtype, "Table")
		self.assertEqual(field.options, _VERDICT_TABLE)
