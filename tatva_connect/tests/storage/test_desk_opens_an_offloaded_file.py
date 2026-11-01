# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Desk's File form opens an offloaded file: it appends frappe's own `?fid=<row>` to `file_url`, and the proxy still finds the blob and still judges it."""
import frappe

from tatva_connect.storage import api
from tatva_connect.storage.blob_store import blob_key_from_url
from tatva_connect.tests.storage.test_file_privacy_lifecycle import _OFFLOAD, PrivacyLifecycleCase


class TestDeskOpensAnOffloadedFile(PrivacyLifecycleCase):
	def _desk_open(self, doc):
		"""What View File, Download and the preview request: the stored key with frappe's `?fid=` on the end."""
		frappe.local.response = frappe._dict()
		api.download_file(f"{blob_key_from_url(doc.file_url)}?fid={doc.name}")
		return frappe.local.response

	def test_the_file_form_opens_it(self):
		self._toggle(_OFFLOAD, 1)
		doc, _content = self._lead_attachment("DESK-FID-OPEN")
		self.assertTrue(blob_key_from_url(doc.file_url), f"fixture is not offloaded — url is {doc.file_url}")
		response = self._desk_open(doc)
		self.assertEqual(response.get("type"), "redirect", "Desk's View File did not reach the file")

	def test_a_guest_is_still_refused(self):
		self._toggle(_OFFLOAD, 1)
		doc, _content = self._lead_attachment("DESK-FID-GUEST")
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				self._desk_open(doc)
		finally:
			frappe.set_user("Administrator")
