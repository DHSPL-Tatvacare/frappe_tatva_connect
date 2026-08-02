"""The name a user reads for a stored file is resolved ONCE, in `storage.file_names`.

A blob key is slugged at mint so it can travel inside a URL, which makes it a safe name and never the
real one. Every surface that shows a file's name must therefore READ `File.file_name` and never derive
from the URL. These tests drive the real suppliers — the per-document map and the activity form's
payload — and assert the name a user actually sees.

The defect they lock: a file named `Vivitra & Sigrima invoice.pdf` was shown as `vivitra_`, because the
client parsed the URL and the `&` inside the key ended the query parameter.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.api import list_link_titles
from tatva_connect.storage import blob_store, file_names

HOSTILE = "Vivitra & Sigrima invoice.pdf"


def _proxy_url(key):
	return blob_store.download_url(key)


class TestFileDisplayNames(FrappeTestCase):
	"""The utility itself: the File row is the truth, and the URL is only a last resort."""

	def setUp(self):
		self.key = blob_store.BlobStore.new_key(HOSTILE, "CRM Lead", "probe-lead")
		self.url = _proxy_url(self.key)
		self.file = frappe.get_doc({
			"doctype": "File",
			"file_name": HOSTILE,
			"file_url": self.url,
			"is_private": 1,
		}).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture, no user gate involved

	def test_the_key_is_slugged_so_it_can_never_be_the_name(self):
		"""If this ever fails the mint changed, and the whole premise of reading the name is gone."""
		self.assertNotIn("&", self.key)
		self.assertNotIn(" ", self.key)
		self.assertNotEqual(self.key.rsplit("/", 1)[-1], HOSTILE)

	def test_the_real_name_comes_back_whole(self):
		self.assertEqual(file_names.display_name(self.url), HOSTILE)

	def test_a_batch_is_deduped_and_answered_together(self):
		"""A form or a grid resolves a page of urls in one call, and a repeat costs nothing extra."""
		self.assertEqual(file_names.display_names([self.url] * 5), {self.url: HOSTILE})

	def test_no_file_row_falls_back_to_the_key_not_the_whole_url(self):
		"""An external link or an already-deleted row still reads as something, never a raw URL."""
		orphan = _proxy_url("crm/crm_lead/x1/aabbccddee_lab_report.pdf")
		self.assertEqual(file_names.display_name(orphan), "lab_report.pdf")

	def test_empty_input_is_answered_without_a_query(self):
		self.assertEqual(file_names.display_names([]), {})
		self.assertEqual(file_names.display_names(None), {})
		self.assertIsNone(file_names.display_name(None))


class TestDocumentSupplierShipsTheName(FrappeTestCase):
	"""The side panel / field layout supplier: `get_doc_link_titles` carries `File::<url>`."""

	def setUp(self):
		self.key = blob_store.BlobStore.new_key(HOSTILE, "CRM Lead", "probe-lead-2")
		self.url = _proxy_url(self.key)
		frappe.get_doc({
			"doctype": "File",
			"file_name": HOSTILE,
			"file_url": self.url,
			"is_private": 1,
		}).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture, no user gate involved
		self.lead = frappe.get_doc({
			"doctype": "CRM Lead",
			"first_name": "Attach",
			"last_name": "Label Probe",
			"image": self.url,
		}).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture, no user gate involved

	def test_the_map_carries_the_real_name_for_the_attach_field(self):
		titles = list_link_titles.get_doc_link_titles("CRM Lead", self.lead.name)
		self.assertEqual(titles.get(f"File::{self.url}"), HOSTILE,
						 "the per-doc map must carry the file's real name, keyed by its url")

	def test_a_lead_with_no_attachment_gets_no_file_entry(self):
		"""The map must not grow an entry for an empty field, or the client renders a label for nothing."""
		bare = frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "No", "last_name": "Attachment",
		}).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture, no user gate involved
		titles = list_link_titles.get_doc_link_titles("CRM Lead", bare.name)
		self.assertEqual([k for k in titles if k.startswith("File::")], [])
