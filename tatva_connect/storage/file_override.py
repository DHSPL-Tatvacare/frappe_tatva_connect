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
from frappe.utils import cstr

from tatva_connect.storage import blob_store, file_names, file_screening
from tatva_connect.storage.blob_store import BlobStore, blob_key_from_url
from tatva_connect.storage.file_events import apply_privacy_policy, assert_link_target_safe

_HYDRATED = "_tatva_hydrated_files"  # per-request temp paths, so one download serves every reader
_DERIVED = ("file_url", "file_size", "content_hash")  # core computes these from the bytes; a request never sends them (permlevel 1)
_OWNER = ("attached_to_doctype", "attached_to_name")  # M1: the upload names the parent, and only the upload


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
		self.file_name = file_names.fit(self.file_name)  # a name past the column is refused by MariaDB and the file is lost; the label is trimmed, identity is the row and the blob key
		apply_privacy_policy(self)  # decide privacy BEFORE core reads it to pick the directory
		self._screen_content()  # refuse bad bytes BEFORE core writes them
		self._flag_existing_blob_reference()  # a reference re-writes nothing — core's own flag, set before core reads it
		sent = {f: self.get(f) for f in _DERIVED}  # what the CALLER sent, before core derives its own answers
		super().before_insert()  # core writes the bytes, now to the directory the checkpoint chose
		self._inherit_blob_facts()  # core derives from bytes it writes, and a reference writes none — this is that derivation
		self._derived = {f: self.get(f) for f in _DERIVED if self.get(f) != sent[f]}  # only what core itself changed — see _restore_derived

	def _flag_existing_blob_reference(self):
		"""A row that only REFERENCES bytes another row already owns must not make a second copy of them.

		Core re-reads and re-SAVES any local `file_url` it is handed (file.py:134), and `get_content`
		returns TEXT for a file that decodes — so the copy was hashed as a string and written under a new
		name: `file_manager.link` left a second file on disk and a reference whose url and `content_hash`
		both disagreed with the source it was supposed to be pointing at. Size was right, which is what
		made it quiet.

		Core has a flag for exactly this (`copy_from_existing_file`, file.py:121) and it is set here rather
		than at the call site because every surface builds references — `file_manager.link`, crm's comment
		`add_attachments`, helpdesk's `attach_file_with_doc` — and the rule belongs where the other file
		decisions are, not repeated in three apps. It is read AFTER core's naming, type, extension and
		private-access checks, so a reference is still validated exactly like any other upload; only the
		byte-write and the duplicate-entry pass it makes redundant are skipped.

		An OFFLOADED reference never reached this: a proxy url is one of core's own URL_PREFIXES, so
		`is_remote_file` already sent it down the branch that writes nothing. That is why a site with
		offload on could not see this, and why the seam test — which turns offload off to inspect the
		bytes on disk — is what caught it.
		"""
		if self.is_folder or self.get("content") or not self.file_url:
			return  # a folder, or bytes the caller actually sent: core writes those, and must
		if not blob_store.is_local_url(self.file_url):
			return  # remote/proxy: core's own is_remote_file branch already writes nothing
		if frappe.db.exists("File", {"file_url": self.file_url}):
			self.flags.copy_from_existing_file = True

	def _inherit_blob_facts(self):
		"""Size and hash for a row that REFERENCES a blob someone else already wrote.

		Core derives both from bytes it writes (file.py:780), so a reference derives nothing and lands at
		0/None — a whole prescription read "0.00 B" on the lead. It belongs HERE and not at the call site
		because both are permlevel 1, and frappe enforces a permlevel by RESETTING the field, so a value
		passed in by a caller is discarded; set before the `_derived` snapshot it is banked as ours.
		Only a source that already measured (`file_size > 0`) is read — a zero row would copy the gap on.
		"""
		if self.is_folder or self.file_size or not self.file_url:
			return
		owned = frappe.db.get_value(
			"File",
			{"file_url": self.file_url, "file_size": [">", 0]},
			["file_size", "content_hash"],
			as_dict=True,
		)
		if owned:
			self.file_size = owned.file_size
			self.content_hash = owned.content_hash

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
		"""THE second seam: every rule that must hold on EVERY save, not only on the first one.

		Privacy is DERIVED here, never stored and trusted. `apply_privacy_policy` was called from
		before_insert alone, so a plain `.save()` setting `is_private = 0` persisted and the bytes went
		public — the flag decided at birth was never re-checked. Re-deriving before `super().validate()`
		also puts the answer in front of core's own byte-mover (`handle_is_private_changed`, file.py:176),
		which relocates a LOCAL file between public/ and private/, so the flag and the directory still
		agree; it early-returns on a remote URL, so an offloaded file only re-flags.

		The insert path is untouched, and the before_insert docstring's reasoning is untouched with it:
		before_insert still decides first, before core writes a byte, and this pass reads the same owner
		and returns the same answer. This is the controller's own method, not a doc_event — a doc_event
		still runs after core, which is exactly what it could never be.
		"""
		self._restore_derived()
		self._guard_owner_immutable()
		if self.has_value_changed("file_url"):
			assert_link_target_safe(self)
		self._guard_private_url_reference()
		if not self.is_folder:
			apply_privacy_policy(self)
		self._sanitize_file_name()
		super().validate()

	def _restore_derived(self):
		"""Put back what core's own before_insert derived from the bytes, after permlevel 1 has reset it.

		`file_url`, `file_size` and `content_hash` are computed, never user input, so they sit at permlevel 1
		(`access/lockdown._PERMLEVEL_1_FIELDS`) — and frappe enforces a permlevel by RESETTING the field to
		the doctype default, not by refusing the save. On an insert that reset runs AFTER before_insert
		(`Document.insert`: before_insert -> validate_higher_perm_levels -> validate), so it would discard
		core's freshly written URL, size and hash along with the caller's forgery, and the upload would land
		with no URL at all. Only the values core CHANGED are banked, so a caller-sent value is still reset.
		On an update nothing is banked and the reset restores the stored row, which IS the lock.
		"""
		for fieldname, value in (getattr(self, "_derived", None) or {}).items():
			self.set(fieldname, value)

	def _guard_owner_immutable(self):
		"""M1: the upload names the file's parent, and no later save renames it.

		`attached_to_doctype`/`attached_to_name` are real user input at upload, so permlevel is the wrong
		tool — the reset would strip them and every upload would land unowned. They are constant instead.
		This is not decoration on top of the privacy fix, it is the other half of it: privacy is derived
		from the owner on every save, so a request that could re-home a file to an allowlisted doctype
		would make a patient document public BY THE RULE.

		The app's own re-home (`file_manager.rehome`), the attach-field bond (`file_events.link_attach_fields`)
		and core's own linker all write with `db_set`/`db.set_value`, which never reach validate — so every
		legitimate re-parenting keeps working untouched.
		"""
		before = self.get_doc_before_save()
		if not before:
			return
		changed = [f for f in _OWNER if cstr(self.get(f)) != cstr(before.get(f))]
		if not changed:
			return
		frappe.throw(
			frappe._("A file's owner is set when the file is uploaded and cannot be changed afterwards ({0}).").format(
				", ".join(changed)
			),
			frappe.CannotChangeConstantError,
		)

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

	def _sanitize_file_name(self):
		"""Strip angle brackets from the filename — no file system needs them, and they are the
		prerequisite for every HTML-injection vector. Frappe's own _sanitize_content strips event
		handlers and script tags from `file_name` but leaves benign HTML elements (e.g. <img>)
		intact — harmless via `{{ }}` text interpolation, but a future `v-html` consumer would
		render them as DOM elements. Stripping the brackets at write time removes the prerequisite."""
		if not self.has_value_changed("file_name"):
			return
		cleaned = self.file_name
		for char in ("<", ">"):
			if char in (cleaned or ""):
				cleaned = cleaned.replace(char, "")
		if cleaned != self.file_name:
			self.file_name = cleaned

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
