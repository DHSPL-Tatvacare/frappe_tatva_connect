# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""Override of core `File`: read bytes from Azure for offloaded files, else local."""

import frappe
from frappe.core.doctype.file.file import File

from tatva_connect.storage.blob_store import BlobStore, blob_key_from_url


class FileOverride(File):
	def get_content(self, *args, **kwargs):
		if not getattr(self, "custom_uploaded_to_azure", 0):
			return super().get_content(*args, **kwargs)
		key = blob_key_from_url(self.file_url)
		if not key:
			frappe.throw(frappe._("Cannot resolve the Azure blob for {0}.").format(self.file_url))
		return BlobStore().download(key)
