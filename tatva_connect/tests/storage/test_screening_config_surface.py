# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The screening constants are operator config, and a blank field is the shipped default.

Three things were hardcoded in `storage/file_screening.py` and reachable from no form: which verdicts
block, the byte-signature table, and how long a verdict is kept. The signature table was the costly one —
it covered PDF, PNG and JPG only, so a .docx and a .xlsx were allowed by System Settings and then verified
by NOTHING. An .html renamed report.xlsx was refused; a .exe renamed report.xlsx was not.

These assert the resolved config, not the call: what the parser really makes of the shipped table, and
that an Office file with the wrong bytes is really refused by it. They fail on the old `_MAGIC`, which has
no DOCX row at all.

Plan: docs/plans/2026-07-21-storage-file-lifecycle-seam.md (Phase 2, 2.6 / V2.6)
Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.storage.test_screening_config_surface
"""
import unittest

from tatva_connect.storage import file_screening

# A real zip local-file header, which is what every .docx and .xlsx genuinely begins with.
_ZIP = b"PK\x03\x04\x14\x00\x06\x00"
# An OLE2 compound-file header — the legacy .doc/.xls container.
_OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


class TestShippedSignatureTable(unittest.TestCase):
	def setUp(self):
		self.table = file_screening.parse_signatures(file_screening.DEFAULTS["type_signatures"])

	def _matches(self, file_type, raw):
		return any(raw.startswith(sig) for sig in self.table[file_type])

	# The gap this closes: an Office file was verified by nothing at all.
	def test_office_types_are_fingerprinted(self):
		for file_type in ("DOCX", "XLSX", "PPTX", "DOC", "XLS", "PPT"):
			self.assertIn(file_type, self.table, f"{file_type} has no signature — its bytes are unchecked")

	def test_a_real_office_file_matches_its_container(self):
		for file_type in ("DOCX", "XLSX", "PPTX"):
			self.assertTrue(self._matches(file_type, _ZIP))
		for file_type in ("DOC", "XLS", "PPT"):
			self.assertTrue(self._matches(file_type, _OLE2))

	# V2.6: a DOCX carrying PDF bytes is caught once the table covers DOCX.
	def test_a_docx_holding_other_bytes_is_refused(self):
		self.assertFalse(self._matches("DOCX", b"%PDF-1.7\n"))
		self.assertFalse(self._matches("XLSX", b"MZ\x90\x00"))
		self.assertFalse(self._matches("DOCX", b"<!DOCTYPE html>"))

	# The three types that were already covered must be untouched by the move to config.
	def test_the_original_three_are_unchanged(self):
		self.assertTrue(self._matches("PDF", b"%PDF-1.7\n"))
		self.assertTrue(self._matches("PNG", b"\x89PNG\r\n\x1a\n\x00"))
		self.assertTrue(self._matches("JPG", b"\xff\xd8\xff\xe0"))

	# Plain text has no fingerprint, so a row for it would be a lie, not a missing feature.
	def test_plain_text_types_are_deliberately_absent(self):
		self.assertNotIn("CSV", self.table)
		self.assertNotIn("TXT", self.table)


class TestSignatureNotation(unittest.TestCase):
	def test_several_signatures_per_type(self):
		table = file_screening.parse_signatures("GIF = 474946383761, 474946383961")
		self.assertEqual(table["GIF"], [b"GIF87a", b"GIF89a"])

	def test_spacing_and_case_do_not_matter(self):
		self.assertEqual(file_screening.parse_signatures("  pdf=25504446  "), {"PDF": [b"%PDF"]})

	# The settings form refuses a table that cannot be read, and this is the throw it refuses on.
	def test_a_table_that_is_not_hex_raises(self):
		with self.assertRaises(ValueError):
			file_screening.parse_signatures("PDF = not-hex")


class TestBlockingVerdicts(unittest.TestCase):
	# The shipped list is the three facts about a FILE, and nothing else.
	def test_scanner_unavailable_is_not_a_blocking_verdict(self):
		shipped = file_screening.DEFAULTS["blocking_verdicts"].splitlines()
		self.assertEqual(shipped, ["Type Not Allowed", "Type Mismatch", "Infected"])
		self.assertNotIn("Scanner Unavailable", shipped)


if __name__ == "__main__":
	unittest.main()
