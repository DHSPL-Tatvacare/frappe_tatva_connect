# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""Override of core `File`: read bytes from Azure for offloaded files, else local; plus a VAPT
guard closing the profile-picture private-file BAC (a forged File row re-owning an existing blob)."""

import frappe
from frappe.core.doctype.file.file import File

from tatva_connect.storage.blob_store import BlobStore, blob_key_from_url


class FileOverride(File):
	def validate(self):
		self._guard_private_url_reference()
		super().validate()

	def _guard_private_url_reference(self):
		"""VAPT: a user can attach an EXISTING private file as their profile picture by supplying its
		`file_url` (Attach > Link), which mints a new File row they OWN pointing at the same blob —
		and Frappe then authorises `/private/files/...` because ANY File row at that URL is readable.
		`upload_file`'s `library_file_name` path already gates this with has_permission("File", …);
		the raw `file_url` path does not. We close it here: a non-privileged user may only reference an
		already-existing private blob if they can already READ an existing File row for it.
		"""
		if not self.is_new():
			return  # only a NEW row can forge a reference
		if getattr(self, "content", None) is not None:
			return  # a fresh upload (bytes present), not a reference to an existing blob
		if not self.file_url or not self.is_private:
			return  # public or no URL — the /private/ auth gate doesn't apply
		if frappe.flags.ignore_permissions or "System Manager" in frappe.get_roles():
			return  # server-trusted write, or an admin who can read every file anyway
		others = [
			f for f in frappe.get_all(
				"File", filters={"file_url": self.file_url, "is_private": 1}, fields=["name"]
			)
			if f.name != self.name
		]
		if not others:
			return  # no pre-existing blob row — this IS the originating upload, allow
		if any(frappe.has_permission("File", "read", f.name) for f in others):
			return  # the caller can already read the referenced file — legitimate reference
		frappe.throw(
			frappe._("You are not permitted to reference this private file."), frappe.PermissionError
		)

	def get_content(self, *args, **kwargs):
		if not getattr(self, "custom_uploaded_to_azure", 0):
			return super().get_content(*args, **kwargs)
		key = blob_key_from_url(self.file_url)
		if not key:
			frappe.throw(frappe._("Cannot resolve the Azure blob for {0}.").format(self.file_url))
		return BlobStore().download(key)
