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
import shutil
import tempfile

import frappe
from frappe.core.doctype.file.file import File

from tatva_connect.storage import file_screening
from tatva_connect.storage.blob_store import BlobStore, blob_key_from_url
from tatva_connect.storage.file_events import apply_privacy_policy

_HYDRATED = "_tatva_hydrated_files"  # per-request temp paths, so one download serves every reader


def discard_hydrated(**_kwargs):
	"""Delete the temp copies `get_full_path` made, at the end of the request or job that made them.

	Hydration is deliberate (M2): a path-reader in frappe, lms, insights or wiki cannot be handed bytes,
	so the blob is written to a real path for the life of one request. The path was cached per request
	but the temp DIRECTORY was never removed, so every hydration left a plaintext patient file on the app
	server for good. This is the other half of "cached per request": the cache dies with frappe.local,
	and now the bytes die with it too.

	Registered on after_request AND after_job, because imports and the offload run in workers. Kwargs are
	absorbed because frappe calls the two hooks with different ones. `ignore_errors` keeps a failed
	cleanup from failing a request that already succeeded.
	"""
	cache = getattr(frappe.local, _HYDRATED, None)
	if not cache:
		return
	for path in cache.values():
		shutil.rmtree(os.path.dirname(path), ignore_errors=True)
	setattr(frappe.local, _HYDRATED, {})


class FileOverride(File):
	def before_insert(self):
		"""THE seam. Both of our decisions are made here, in this order, because core's own before_insert
		WRITES THE BYTES — `save_file` -> `save_file_on_filesystem` picks public/ or private/ from
		is_private at that instant (file.py:815-821), and its byte-mover handle_is_private_changed is
		gated on `not self.is_new()` (file.py:176) so it never fires for a new file. A doc_event cannot
		do this: `Document.hook` runs the controller method first and app hooks second
		(model/document.py:1576-1590), which is exactly why privacy-as-validate landed private files in
		the public directory and screening-as-before_insert scanned bytes already on disk."""
		self._inherit_file_name()  # before core's set_file_name() (file.py:112) carves a name out of the URL
		apply_privacy_policy(self)  # decide privacy BEFORE core reads it to pick the directory
		self._screen_content()  # refuse bad bytes BEFORE core writes them
		super().before_insert()  # core writes the bytes, now to the directory the checkpoint chose

	def _screen_content(self):
		"""The ONE screening call site, for every channel — the channel itself is resolved in the screener.

		Core's OWN derivations are run first, never re-implemented: set_file_name() carves the name out of
		file_url when the caller sent none, and set_file_type() is the single mimetype derivation whose
		answer core's validate_file_extension then judges (file.py:456-468). Screening the literal filename
		suffix instead was a second reading of the one System Settings list: core resolves 'report.jpeg' to
		JPG and allows it against an entry of 'JPG', while a suffix match refused it. Both are idempotent,
		so core re-running them inside super().before_insert() changes nothing.
		"""
		if self.is_folder or not self.content:
			return  # a folder, or a link whose bytes were never ours — core sets content to b"" for both
		self.set_file_name()
		self.set_file_type()
		# Core's get_content CONSUMES self.decode without rewriting self.content, so a base64 upload resolves exactly once: bank the resolved bytes here or core's own later save_file(content=self.get_content()) writes the BASE64 TEXT to disk.
		self.content = self.get_content()
		file_screening.screen(
			file_name=self.file_name,
			file_type=self.file_type,
			raw=self.content,
			attached_to_doctype=self.attached_to_doctype,
			attached_to_name=self.attached_to_name,
		)

	def validate(self):
		self._guard_private_url_reference()
		super().validate()

	def check_content(self):
		"""Core reads a PDF's structure here to refuse embedded JavaScript (file.py:471, pdf_contains_js).

		A truncated or corrupt PDF makes that parse RAISE (pypdf), and the exception escaped as a 500: the
		caller's bad bytes reported as our fault. A file that claims to be a PDF and cannot be parsed as
		one is refused in the caller's language, like any other invalid upload.

		Only pypdf's own errors are caught. A bare `except Exception` would relabel a PermissionError or a
		frappe throw as a type mismatch, and PermissionError does not subclass ValidationError.

		This check and the screener's byte sniff both refuse a file whose contents contradict its name,
		but they are different checks reading different things — so this one carries its OWN `detail`,
		the same way `file_screening._block_exception` carries the screener's. Sharing one string with no
		verdict left a partner unable to tell which of the two had fired.
		"""
		from pypdf.errors import PdfReadError

		try:
			super().check_content()
		except PdfReadError:
			message = frappe._(
				"This file is named as a PDF but its structure could not be read, so its contents "
				"could not be checked. Re-export the PDF and attach it again, or send the file under "
				"the extension its bytes really are."
			)
			exc = frappe.ValidationError(message)
			exc.detail = {"check": "pdf_structure", "verdict": "Unreadable PDF", "file_name": self.file_name}
			frappe.throw(message, exc)

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
