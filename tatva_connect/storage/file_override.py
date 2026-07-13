# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt

"""The ONE class that knows an offloaded file's bytes live in Azure. How the file layer works, in full:

M1 OWNERSHIP — every file is born owned by a real record (`attached_to_doctype`); a blob's life is exactly
   its row's life. Delete the record -> delete the row -> `File.on_trash` drops the blob on the LAST
   reference. There are no orphans because there are no unowned files.
M2 BYTE ACCESS — nothing anywhere reads a file off the disk. Core (and every app on it) either asks for the
   BYTES (`get_content`) or asks for a PATH and opens it (`get_full_path`); BOTH are answered here, so Data
   Import, xlsx/csv, unzip, LMS, Insights and Wiki work untouched. A new caller that needs bytes calls one
   of these two — never `get_site_path`, never `open()`.
M3 DISPLAY — a URL is neither a filename nor a privacy flag. Read the File row (`file_name`, `is_private`).

Every file bug this app has had was one of those three assumptions breaking. A fix that is not M1, M2 or M3
is a deviation. Privacy has ONE checkpoint (`file_events.may_be_public`) and the caller never decides it.
"""

import os
import tempfile

import frappe
from frappe.core.doctype.file.file import File

from tatva_connect.storage.blob_store import BlobStore, blob_key_from_url

_HYDRATED = "_tatva_hydrated_files"  # per-request temp paths, so one download serves every reader


class FileOverride(File):
	def before_insert(self):
		self._inherit_file_name()  # before core's set_file_name() (file.py:112) carves a name out of the URL
		super().before_insert()

	def validate(self):
		self._guard_private_url_reference()
		super().validate()

	def _inherit_file_name(self):
		"""A copy carries the human filename, not our storage hash (core would derive it from the URL)."""
		if self.file_name or not blob_key_from_url(self.file_url):
			return
		self.file_name = frappe.db.get_value("File", {"file_url": self.file_url}, "file_name")

	def _guard_private_url_reference(self):
		"""VAPT: a new row may reference an existing PRIVATE blob only if the caller can already read one."""
		if not self.is_new():
			return
		if getattr(self, "content", None) is not None:
			return  # a fresh upload, not a reference to an existing blob
		if not self.file_url or not self.is_private:
			return
		if frappe.flags.ignore_permissions or "System Manager" in frappe.get_roles():
			return
		others = [
			f for f in frappe.get_all(
				"File", filters={"file_url": self.file_url, "is_private": 1}, fields=["name"]
			)
			if f.name != self.name
		]
		if not others:
			return  # no pre-existing row — this IS the originating upload
		if any(frappe.has_permission("File", "read", f.name) for f in others):
			return
		frappe.throw(
			frappe._("You are not permitted to reference this private file."), frappe.PermissionError
		)

	def get_content(self, *args, **kwargs):
		"""M2: the bytes. Resolved from the URL — core's copies drop our flag but always carry the URL."""
		key = blob_key_from_url(self.file_url)
		if not key:
			return super().get_content(*args, **kwargs)
		return BlobStore().download(key)

	def get_full_path(self):
		"""M2: a path that opens. Hydrate the blob to a temp file — core hands back the URL, which no one can open."""
		key = blob_key_from_url(self.file_url)
		if not key:
			return super().get_full_path()

		cache = getattr(frappe.local, _HYDRATED, None)
		if cache is None:
			cache = {}
			setattr(frappe.local, _HYDRATED, cache)
		path = cache.get(key)
		if path and os.path.exists(path):
			return path

		path = os.path.join(tempfile.mkdtemp(prefix="tatva-file-"), os.path.basename(key))
		with open(path, "wb") as fh:
			fh.write(BlobStore().download(key))
		cache[key] = path
		return path

	def validate_file_on_disk(self):
		"""Nothing to check: the bytes are in Azure, and checking would hydrate them on every save."""
		if blob_key_from_url(self.file_url):
			return True
		return super().validate_file_on_disk()

	def exists_on_disk(self):
		"""False, honestly — and it must not hydrate: core answers this by opening get_full_path()."""
		# True would also arm core's content-hash dedup (file.py:501): two records would share one blob and
		# one URL, and the proxy resolves a URL to *a* row — the permission check could hit the wrong one.
		if blob_key_from_url(self.file_url):
			return False
		return super().exists_on_disk()

	def make_thumbnail(self, *args, **kwargs):
		"""M2: built from the bytes we hold; core would look for a local image that isn't there."""
		key = blob_key_from_url(self.file_url)
		if not key:
			return super().make_thumbnail(*args, **kwargs)
		if self.thumbnail_url:
			return

		import io

		from PIL import Image

		from tatva_connect.storage import blob_store

		try:
			image = Image.open(io.BytesIO(self.get_content()))
			image.thumbnail((300, 300))
			buf = io.BytesIO()
			image.save(buf, format=image.format or "PNG")
		except Exception:
			return  # not a decodable image — a missing thumbnail never fails an upload

		root, ext = os.path.splitext(key)
		thumb_key = f"{root}_small{ext}"
		BlobStore().upload(thumb_key, buf.getvalue(), os.path.basename(thumb_key))
		self.db_set("thumbnail_url", blob_store.download_url(thumb_key), update_modified=False)
