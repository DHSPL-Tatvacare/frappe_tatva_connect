# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The lifecycle seam: our decisions must run BEFORE core writes the bytes.

`FileOverride.before_insert` calls `super().before_insert()`, and core picks the directory from
`is_private` AT THAT MOMENT (`file.py:815-821`). Both of our decisions — the privacy checkpoint and
screening — run afterwards, as `validate` / `before_insert` doc_events, because frappe's hook composer
runs the controller method first and app hooks second (`model/document.py:1576-1590`). So today a
patient document's bytes are written to `public/files`, a rejected file is written before it is
screened, and core judges `is_private = 0` before we have set it — which bare-raises
`only_for("System Manager")` under `only_allow_system_managers_to_upload_public_files`.

These tests assert the OUTCOME on the real filesystem and in the real Azure container: which directory
the bytes are really in, whether a rejected file really left bytes behind, whether a non-System-Manager
can really attach a document. Nothing is faked — a mocked BlobStore is what let three months of file
bugs through. `frappe.enqueue` is run in-process where a test needs the worker's REAL function against
the REAL database (a scan-log row, the local-copy drop); the assertion is always the row or the file,
never the call.

Azure has no transaction, so FrappeTestCase's rollback does not remove a blob and does not remove a
local file — every test registers its blob keys and its filename stems, and tearDown deletes both.

Plan: docs/plans/2026-07-21-storage-file-lifecycle-seam.md (Phase 1, V1.1 - V1.12)
Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.storage.test_file_lifecycle_seam
"""
import glob
import inspect
import io
import os
from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.utils import get_files_path

from tatva_connect.storage.blob_store import blob_key_from_url
from tatva_connect.tests.storage.test_file_layer_registry import FileLayerCase

_OFFLOAD = "Storage::Azure::offload"
_PRIVACY = "Storage::File::privacy"
_SCREENING = "Storage::File::screening"
_AZURE_SETTINGS = "CRM Azure Storage Settings"
_SCREEN_SETTINGS = "CRM File Screening Settings"
_SCAN_LOG = "CRM File Scan Log"

_PNG_HEADER = (
	b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
	b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00"
	b"\x00\x00IEND\xaeB`\x82"
)

# A DOS/PE header wearing a .png name — a pure magic-byte mismatch, the one verdict that needs no scanner.
_EXE_HEADER = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00"


def _real_jpeg():
	"""A genuinely decodable 1x1 JPEG — core runs PIL over any image/jpeg to strip EXIF (image.py:34)."""
	from PIL import Image
	buf = io.BytesIO()
	Image.new("RGB", (1, 1), (255, 255, 255)).save(buf, format="JPEG")
	return buf.getvalue()


@contextmanager
def _jobs_inline(recorder=None):
	"""Run enqueued work in-process: the REAL function against the REAL database, not a stand-in.

	`enqueue_after_commit` never fires under a test transaction and a queued job lands in a worker we
	cannot read, so the scan-log row and the local-copy drop would be untestable. This executes them
	where the assertion can see their effect; nothing about the decision under test is replaced.

	`recorder`, when given, collects each enqueue's kwargs. Running a job in-process necessarily loses
	the one thing the commit semantics ARE — whether the row was queued to wait for a commit — so a test
	that cares about that reads it here rather than claiming to have proved it from the row.
	"""
	from frappe.utils import background_jobs

	reserved = set(inspect.signature(background_jobs.enqueue).parameters) - {"kwargs"}

	def run(method, **kwargs):
		if recorder is not None:
			recorder.append(kwargs)
		return method(**{k: v for k, v in kwargs.items() if k not in reserved})

	with patch("frappe.enqueue", side_effect=run):
		yield


class SeamCase(FileLayerCase):
	"""Real bytes on the real disk. Offload is off so the local copy is still there to be inspected —
	the plan's V1.1 says "assert the on-disk path before offload", and this is that moment."""

	def setUp(self):
		super().setUp()
		self._stems = []
		self._system_settings_before = {}
		self._toggle(_OFFLOAD, 0)
		self._toggle(_SCREENING, 0)

	def tearDown(self):
		for stem in self._stems:
			for path in self._on_disk(stem):
				try:
					os.remove(path)
				except OSError:
					pass
		if self._system_settings_before:
			self._system_setting(**self._system_settings_before)
		super().tearDown()

	def _toggle(self, key, on):
		frappe.db.set_value("CRM Tatva Automation", key, "enabled", 1 if on else 0)

	def _allow_public(self, doctypes):
		frappe.db.set_single_value(_AZURE_SETTINGS, "public_attachment_doctypes", doctypes)
		frappe.clear_document_cache(_AZURE_SETTINGS, _AZURE_SETTINGS)

	def _system_setting(self, **fields):
		"""Change System Settings and remember the previous value — the request-scoped cache is cleared
		too, or `get_system_settings` keeps answering with the old doc for the rest of the test run."""
		settings = frappe.get_doc("System Settings")
		for key, value in fields.items():
			self._system_settings_before.setdefault(key, settings.get(key))
			settings.set(key, value)
		settings.save(ignore_permissions=True)
		frappe.local.system_settings = None

	def _stem(self):
		stem = f"seam{frappe.generate_hash(length=10)}"
		self._stems.append(stem)
		return stem

	def _png(self):
		"""Unique bytes per upload: core dedups on `content_hash` + `is_private` and reuses the existing
		file's URL WITHOUT writing anything, which would make a directory assertion meaningless."""
		return _PNG_HEADER + b"#" + frappe.generate_hash(length=16).encode()

	def _insert(self, **fields):
		doc = frappe.get_doc(dict(doctype="File", **fields)).insert(ignore_permissions=True)
		key = blob_key_from_url(doc.file_url)
		if key:
			self._keys.append(key)
		return doc

	def _on_disk(self, stem):
		"""Every real file whose name starts with `stem`, in BOTH directories — core appends a
		uniqueness suffix, so the stem is the only part of the name we can predict."""
		return glob.glob(get_files_path(f"{stem}*", is_private=1)) + glob.glob(
			get_files_path(f"{stem}*", is_private=0)
		)

	def _lead(self, first_name):
		return frappe.get_doc(
			{"doctype": "CRM Lead", "first_name": first_name, "status": "New"}
		).insert(ignore_permissions=True)

	def assert_bytes_are_private(self, doc, content, label):
		"""The whole seam, stated as one outcome: the row says private, the URL says private, and the
		bytes are really in the private directory and really absent from the public one."""
		self.assertEqual(doc.is_private, 1, f"{label}: the row is not private")
		self.assertTrue(
			doc.file_url.startswith("/private/files/"),
			f"{label}: the bytes were written before privacy was decided — url is {doc.file_url}",
		)
		basename = doc.file_url.rsplit("/", 1)[-1]
		private = get_files_path(basename, is_private=1)
		public = get_files_path(basename, is_private=0)
		self.assertTrue(os.path.exists(private), f"{label}: nothing is in the private directory")
		self.assertFalse(os.path.exists(public), f"{label}: the bytes are in the PUBLIC directory")
		with open(private, "rb") as fh:
			self.assertEqual(fh.read(), content, f"{label}: the private copy is not the uploaded bytes")


class TestBytesLandInTheDecidedDirectory(SeamCase):
	"""V1.1 - V1.4. Core writes the bytes to the folder `is_private` names at that instant, so the
	directory IS the record of when our checkpoint ran. `handle_is_private_changed` is gated on
	`not self.is_new()` (file.py:176) and never fires for a new file, so a later flip of the flag moves
	nothing: a lead attachment marked private is served out of `public/files` for the rest of its life.
	"""

	def test_lead_attachment_bytes_are_in_the_private_directory(self):
		# V1.1 — a patient document. Today is_private is 0 when core writes, so the bytes go to
		# public/files and the row is flipped to private afterwards, over a URL that never moved.
		lead = self._lead("SEAM-V11")
		stem, content = self._stem(), self._png()
		doc = self._insert(
			file_name=f"{stem}.png",
			content=content,
			attached_to_doctype="CRM Lead",
			attached_to_name=lead.name,
		)
		self.assert_bytes_are_private(doc, content, "lead attachment")

	def test_allowlisted_avatar_bytes_are_in_the_public_directory(self):
		# V1.2 — the exception seam. Some files MUST be public: an avatar the SPA renders for every rep,
		# the login favicon, Website Settings branding. Moving the checkpoint earlier must not close it.
		self._toggle(_PRIVACY, 1)
		self._allow_public("User")
		stem, content = self._stem(), self._png()
		doc = self._insert(
			file_name=f"{stem}.png",
			content=content,
			attached_to_doctype="User",
			attached_to_name="Administrator",
		)
		self.assertEqual(doc.is_private, 0, "an allowlisted doctype's file must stay public")
		self.assertTrue(doc.file_url.startswith("/files/"), f"the avatar is not public: {doc.file_url}")
		basename = doc.file_url.rsplit("/", 1)[-1]
		self.assertTrue(
			os.path.exists(get_files_path(basename, is_private=0)),
			"the avatar's bytes are not in the public directory — every rep gets a 403 on every photo",
		)

	def test_avatar_is_private_when_the_privacy_toggle_is_off(self):
		# V1.3 — fail-closed. The toggle governs the public EXCEPTIONS only: off can make a file more
		# private, never leak one. Today the bytes land public whatever the toggle says.
		self._toggle(_PRIVACY, 0)
		self._allow_public("User")
		stem, content = self._stem(), self._png()
		doc = self._insert(
			file_name=f"{stem}.png",
			content=content,
			attached_to_doctype="User",
			attached_to_name="Administrator",
		)
		self.assert_bytes_are_private(doc, content, "avatar with the toggle off")

	def test_an_allowlisted_doctype_with_no_record_is_still_private(self):
		"""The checkpoint reads an owner core is about to discard.

		Core's before_insert opens by nulling a doctype that carries no name (file.py:104-106). Running
		our checkpoint first meant it saw attached_to_doctype="User", matched the allowlist and set
		is_private=0 — and then core nulled the doctype, leaving an UNATTACHED PUBLIC file. That is the
		exact inverse of the stated rule ("unattached = no doctype = private, the floor"). An owner is
		the PAIR or it is not an owner.
		"""
		self._toggle(_PRIVACY, 1)
		self._allow_public("User")
		stem, content = self._stem(), self._png()

		doc = self._insert(file_name=f"{stem}.png", content=content, attached_to_doctype="User")

		self.assertIsNone(doc.attached_to_doctype, "premise: core discards a doctype with no name")
		self.assert_bytes_are_private(doc, content, "allowlisted doctype with no record")

	def test_external_link_stays_public_and_writes_no_bytes(self):
		# V1.4 — an external link is not our file. We never held its bytes, so we do not lock it and we
		# do not write it. apply_privacy_policy is called WHOLE, so its link branch has to survive.
		lead = self._lead("SEAM-V14")
		stem = self._stem()
		doc = self._insert(
			file_url=f"https://us1.discourse-cdn.com/openai1/original/4X/3/2/1/{stem}.png",
			file_name=f"{stem}.png",
			attached_to_doctype="CRM Lead",
			attached_to_name=lead.name,
		)
		self.assertEqual(doc.is_private, 0, "a padlock on a public CDN link is a lie the system tells")
		self.assertIsNone(blob_key_from_url(doc.file_url), "a link must never be offloaded")
		self.assertEqual(self._on_disk(stem), [], "a link we never received bytes for was written to disk")


class TestPublicFileRestrictionNoLongerRefusesAnAttachment(SeamCase):
	"""V1.7 — the original production 403.

	`File.validate` -> `enforce_public_file_restrictions` (file.py:197) runs while `is_private` is still
	0, because our checkpoint has not run yet. It calls `frappe.only_for("System Manager")` and rethrows
	as "Only System Managers can make this file public." — so a rep attaching a patient document to a
	lead is refused, for a file that was always going to be private.

	The user MUST NOT be Administrator: `frappe.only_for` returns early for Administrator, which is
	exactly why every existing test in this suite (all Administrator) stayed green through the outage.
	"""

	def setUp(self):
		super().setUp()
		self._system_setting(only_allow_system_managers_to_upload_public_files=1)

	def _rep(self):
		email = f"seam-rep-{frappe.generate_hash(length=8)}@example.com"
		user = frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": "Seam Rep", "send_welcome_email": 0}
		)
		user.flags.no_welcome_mail = True
		user.insert(ignore_permissions=True)
		self.assertNotIn(
			"System Manager", frappe.get_roles(email), "premise: the uploader is not a System Manager"
		)
		return email

	def test_a_non_system_manager_can_attach_a_document_to_a_lead(self):
		lead = self._lead("SEAM-V17")
		rep = self._rep()
		stem, content = self._stem(), self._png()

		frappe.set_user(rep)
		try:
			doc = self._insert(
				file_name=f"{stem}.png",
				content=content,
				attached_to_doctype="CRM Lead",
				attached_to_name=lead.name,
			)
		finally:
			frappe.set_user("Administrator")

		self.assert_bytes_are_private(doc, content, "rep's lead attachment")


class TestEverySurfaceLandsPrivate(SeamCase):
	"""V1.10 — one seam, all surfaces.

	Every ingress creates the same `File` doctype and therefore passes the same override, so the fix is
	not per-caller. If any one of these still lands in `public/files`, a second decision point grew back.
	"""

	def test_desk_upload_lands_private(self):
		lead = self._lead("SEAM-DESK")
		stem, content = self._stem(), self._png()
		doc = self._insert(
			file_name=f"{stem}.png",
			content=content,
			attached_to_doctype="CRM Lead",
			attached_to_name=lead.name,
		)
		self.assert_bytes_are_private(doc, content, "desk upload")

	def test_intake_web_form_upload_lands_private(self):
		# A Web Form's attach control has no `frm` (attach.js:80), so a patient's file arrives with no
		# parent, from an unauthenticated session. It is still patient data and still goes private.
		stem, content = self._stem(), self._png()
		frappe.set_user("Guest")
		try:
			doc = self._insert(file_name=f"{stem}.png", content=content)
		finally:
			frappe.set_user("Administrator")
		self.assert_bytes_are_private(doc, content, "intake web form upload")

	def test_whatsapp_outbound_media_lands_private(self):
		# WhatsAppBox.vue:32 uploads unattached, then media.adopt_outbound_media re-homes it on the lead.
		from tatva_connect.whatsapp import media

		lead = self._lead("SEAM-WA")
		stem, content = self._stem(), self._png()
		doc = self._insert(file_name=f"{stem}.png", content=content)
		self.assert_bytes_are_private(doc, content, "whatsapp media at upload")

		media.adopt_outbound_media(doc.file_url, lead.name, "wamid.SEAM")
		row = frappe.db.get_value(
			"File", doc.name, ["is_private", "attached_to_doctype"], as_dict=True
		)
		self.assertEqual(row.attached_to_doctype, "CRM Lead", "the sent media did not land on the lead")
		self.assertEqual(row.is_private, 1, "patient WhatsApp media must be private")

	def test_partner_api_attachment_lands_private(self):
		# The partner file API attaches through file_manager.save — the one front door, never a
		# hand-rolled File insert. Privacy is not one of its parameters and must not become one.
		from tatva_connect.storage import file_manager

		lead = self._lead("SEAM-API")
		stem, content = self._stem(), self._png()
		doc = file_manager.save(
			content,
			filename=f"{stem}.png",
			attached_to_doctype="CRM Lead",
			attached_to_name=lead.name,
		)
		self.assert_bytes_are_private(doc, content, "partner api attachment")


class TestScreeningPrecedesTheWrite(SeamCase):
	"""V1.5, V1.6, V1.8, V1.11, V1.12.

	Screening runs at the `before_insert` DOC_EVENT, which frappe composes after the controller method —
	so `super().before_insert()` has already written the rejected bytes to disk by the time the screener
	is consulted. A file we refused is on the app server, in the public directory, named by the caller.

	The extension list is the other half: two lists is a second brain, and it is what started this
	investigation — System Settings rejected PDF while our screening allowed it. Frappe owns the list;
	we own only the byte-signature check frappe does not have.
	"""

	def setUp(self):
		super().setUp()
		self._toggle(_SCREENING, 1)
		self._screening(scanner_unavailable="allow", channels=("Intake", "Partner API"))

	def _screening(self, *, channels=None, **fields):
		"""Set the screening config. Only fields that really exist may be passed: `settings.set` on a
		deleted fieldname is silently discarded, so a test that wrote `allowed_extensions=` here proved
		nothing about the deletion — it asserted against a value the doctype never stored."""
		settings = frappe.get_single(_SCREEN_SETTINGS)
		for key, value in fields.items():
			settings.set(key, value)
		if channels is not None:
			settings.set("active_channels", [])
			for channel in channels:
				settings.append("active_channels", {"channel": channel})
		settings.save(ignore_permissions=True)
		frappe.clear_document_cache(_SCREEN_SETTINGS, _SCREEN_SETTINGS)

	@contextmanager
	def _as_patient(self):
		"""Every CRM Intake Form is published with login_required=0, so a patient uploading a report is
		an unauthenticated request — the channel is the REQUEST, never the attachment."""
		frappe.set_user("Guest")
		try:
			yield
		finally:
			frappe.set_user("Administrator")

	def _scan_rows(self, stem):
		return frappe.get_all(
			_SCAN_LOG,
			filters={"file_name": ["like", f"{stem}%"]},
			fields=["verdict", "action"],
		)

	def test_a_blocked_file_leaves_no_bytes_on_disk(self):
		# V1.5 — the file was refused, so nothing of it may remain: not in private/files, not in
		# public/files. Today the bytes are written first and the verdict is read afterwards.
		stem = self._stem()
		with self._as_patient(), self.assertRaises(frappe.ValidationError):
			self._insert(file_name=f"{stem}.png", content=_EXE_HEADER)

		self.assertEqual(
			self._on_disk(stem), [], "a refused file's bytes are sitting on the app server"
		)

	def test_a_blocked_file_records_its_verdict_and_does_not_wait_for_a_commit(self):
		"""V1.6 — the audit trail for a file we refuse.

		This does NOT prove the row survives a rollback, and must not say it does: the job runs inline,
		inside this test's own transaction, so a rollback here would take the row with it. Proving
		survival needs a real commit, which would leak a row past the suite's rollback.

		What IS provable, and is the whole mechanism, is the commit semantics the block was queued with.
		A blocked verdict must go out with `enqueue_after_commit=False`: the throw guarantees the request
		rolls back, so a row queued to wait for a commit is a row that is never written at all.
		"""
		stem = self._stem()
		queued = []
		with _jobs_inline(queued), self._as_patient(), self.assertRaises(frappe.ValidationError):
			self._insert(file_name=f"{stem}.png", content=_EXE_HEADER)

		rows = self._scan_rows(stem)
		self.assertEqual(len(rows), 1, "a refused file left no scan-log row")
		self.assertEqual(rows[0].action, "Blocked")
		self.assertEqual(rows[0].verdict, "Type Mismatch")

		scans = [kwargs for kwargs in queued if kwargs.get("verdict")]
		self.assertEqual(len(scans), 1, "the refusal did not queue exactly one scan-log write")
		self.assertFalse(
			scans[0]["enqueue_after_commit"],
			"a blocked verdict queued after commit is lost — the throw rolls the request back",
		)

	def test_screening_runs_exactly_once_per_upload(self):
		# V1.8 — the call sites collapse from three to one. Two rows means a file is scanned twice
		# (double ClamAV cost, a duplicated audit trail); zero means a surface lost its screening
		# entirely when its explicit call was deleted, which is the silent failure worth catching.
		stem, content = self._stem(), self._png()
		with _jobs_inline(), self._as_patient():
			doc = self._insert(file_name=f"{stem}.png", content=content)

		self.assert_bytes_are_private(doc, content, "screened clean upload")
		self.assertEqual(len(self._scan_rows(stem)), 1, "one upload did not produce exactly one scan")

	def test_our_settings_doctype_has_no_extension_field_left(self):
		"""V1.11 — the deletion itself, asserted where it is real.

		The old test "proved" this by writing allowed_extensions= through settings.set, which a deleted
		fieldname silently discards: it would have passed just as happily with the field still there.
		The meta is the only honest witness.
		"""
		self.assertIsNone(
			frappe.get_meta(_SCREEN_SETTINGS).get_field("allowed_extensions"),
			"a second extension list is back on the screening settings doctype",
		)

	def test_a_type_the_platform_list_refuses_is_blocked_by_screening(self):
		"""V1.11 — System Settings is the ONE place an operator sets extensions, and screening enforces
		exactly that list. This also runs it where core does NOT: core's validate_file_extension early
		returns without a frappe.request, so the platform list never policed jobs or integrations."""
		self._system_setting(allowed_file_extensions="PNG")
		stem = self._stem()

		with _jobs_inline(), self._as_patient(), self.assertRaises(frappe.ValidationError):
			self._insert(
				file_name=f"{stem}.txt",
				content=f"a plain text report {frappe.generate_hash(length=16)}".encode(),
			)

		rows = self._scan_rows(stem)
		self.assertEqual(len(rows), 1, "the upload was refused, but not by screening")
		self.assertEqual(
			rows[0].verdict, "Type Not Allowed", "screening did not enforce the platform's list"
		)
		self.assertEqual(self._on_disk(stem), [], "a refused file's bytes are on the app server")

	def test_a_type_the_platform_list_allows_is_accepted(self):
		"""V1.12 — the delete has to be clean in the other direction too: with no list of our own left,
		nothing can narrow the platform list any more than widen it."""
		self._system_setting(allowed_file_extensions="PNG\nTXT")
		stem = self._stem()
		content = f"a plain text report {frappe.generate_hash(length=16)}".encode()

		with _jobs_inline(), self._as_patient():
			doc = self._insert(file_name=f"{stem}.txt", content=content)

		self.assert_bytes_are_private(doc, content, "text report frappe allows")
		rows = self._scan_rows(stem)
		self.assertEqual(len(rows), 1, "one upload did not produce exactly one scan")
		# Accepted, not the verdict name: a clean file reads "Clean" only while clamd is reachable, and
		# whether the scanner answered is a different question from whether our list had a vote.
		self.assertEqual(rows[0].action, "Accepted", "screening refused a type the platform allows")


class TestBase64ContentIsResolvedExactlyOnce(SeamCase):
	"""Core's `get_content` is not idempotent, and the seam made us its FIRST caller.

	It resolves `self.content`, decodes it when `self.decode` is set, and clears `self.decode` — WITHOUT
	writing the decoded bytes back to `self.content` (core's own TODO says as much). So the first call
	returns the real bytes and every later call returns the RAW BASE64 TEXT. Screening now runs before
	core's `save_file(content=self.get_content())`, so core's call was the second one and the base64 text
	was what reached the disk: every base64 upload silently stored as its own encoding.

	Reachable from the whitelisted `frappe.client.attach_file(decode_base64=True)`.
	"""

	def test_a_base64_upload_stores_the_decoded_bytes(self):
		import base64

		lead = self._lead("SEAM-B64")
		stem, content = self._stem(), self._png()
		doc = self._insert(
			file_name=f"{stem}.png",
			content=base64.b64encode(content).decode(),
			decode=True,
			attached_to_doctype="CRM Lead",
			attached_to_name=lead.name,
		)

		# assert_bytes_are_private reads the file off the DISK and compares it to the real bytes, which is exactly the corruption: the stored file was 'iVBORw0KGgo...' text, not a PNG.
		self.assert_bytes_are_private(doc, content, "base64 upload")

	def test_get_content_answers_the_same_bytes_every_time(self):
		import base64

		lead = self._lead("SEAM-B64-IDEM")
		stem, content = self._stem(), self._png()
		doc = self._insert(
			file_name=f"{stem}.png",
			content=base64.b64encode(content).decode(),
			decode=True,
			attached_to_doctype="CRM Lead",
			attached_to_name=lead.name,
		)

		self.assertEqual(doc.get_content(), content, "the first read is not the uploaded bytes")
		self.assertEqual(doc.get_content(), content, "a second read returned different bytes")


class TestOneExtensionList(SeamCase):
	"""The one list must also be read ONE way (constitution rule 4: a rule expressed twice is locked).

	Deleting our `allowed_extensions` field left one list — but two readings of it. Core matches
	`self.file_type`, which `set_file_type()` derives through mimetypes, while the screener matched the
	literal filename suffix. Same System Settings entry 'JPG', same upload 'report.jpeg': core resolves it
	to JPG and ALLOWS, a suffix match reads 'jpeg' and BLOCKS. The screener now judges core's own derived
	value, and this drives both deciders over the same names and fails the moment they disagree.
	"""

	# Chosen so a suffix reading and a mimetype reading differ where they can: .jpeg -> JPG is the divergence itself, .PNG covers case, and no-extension covers an absent file_type.
	_NAMES = ("report.jpeg", "report.jpg", "scan.PNG", "notes.txt", "deck.pdf", "tool.exe", "noextension")

	def _core_verdict(self, file_name):
		"""Core's OWN answer: set_file_type() then validate_file_extension(), driven under a request —
		core early-returns without one, and that early-out is the single difference we deliberately keep."""
		from frappe.core.doctype.file.exceptions import FileTypeNotAllowed

		doc = frappe.new_doc("File")
		doc.file_name = file_name
		doc.set_file_type()
		with patch("frappe.request", object()):
			try:
				doc.validate_file_extension()
			except FileTypeNotAllowed:
				return False, doc.file_type
		return True, doc.file_type

	def test_our_gate_and_cores_gate_agree_on_every_name(self):
		from tatva_connect.storage import file_screening

		self._system_setting(allowed_file_extensions="JPG\nPNG\nPDF")
		for name in self._NAMES:
			core_allows, file_type = self._core_verdict(name)
			self.assertEqual(
				file_screening._allowed(file_type),
				core_allows,
				f"{name} (file_type={file_type}): the screener and core's validate_file_extension "
				"disagree about the SAME System Settings list — one list is being read two ways",
			)

	def test_a_jpeg_the_platform_allows_is_not_blocked_by_screening(self):
		"""The divergence as a user meets it: a phone camera writes .jpeg, System Settings lists JPG."""
		self._toggle(_SCREENING, 1)
		self._screening(scanner_unavailable="allow", channels=("Intake", "Partner API"))
		self._system_setting(allowed_file_extensions="JPG\nPNG")
		stem = self._stem()
		content = _real_jpeg()  # core strips EXIF on image/jpeg, so PIL must be able to open it

		with _jobs_inline(), self._as_patient():
			doc = self._insert(file_name=f"{stem}.jpeg", content=content)

		self.assertEqual(doc.is_private, 1, "a patient's photo must be private")
		rows = self._scan_rows(stem)
		self.assertEqual(len(rows), 1, "one upload did not produce exactly one scan")
		self.assertEqual(
			rows[0].action, "Accepted", "a .jpeg was refused against a list that allows JPG"
		)

	@contextmanager
	def _as_patient(self):
		frappe.set_user("Guest")
		try:
			yield
		finally:
			frappe.set_user("Administrator")

	def _screening(self, *, channels=None, **fields):
		settings = frappe.get_single(_SCREEN_SETTINGS)
		for key, value in fields.items():
			settings.set(key, value)
		if channels is not None:
			settings.set("active_channels", [])
			for channel in channels:
				settings.append("active_channels", {"channel": channel})
		settings.save(ignore_permissions=True)
		frappe.clear_document_cache(_SCREEN_SETTINGS, _SCREEN_SETTINGS)

	def _scan_rows(self, stem):
		return frappe.get_all(_SCAN_LOG, filters={"file_name": ["like", f"{stem}%"]},
		                      fields=["verdict", "action"])


class TestBothBulkLanesAreScreened(SeamCase):
	"""A bulk payload is screened on BOTH submit lanes, or the Desk lane is an unscreened front door.

	`submit_job` is one path with two callers. The API lane carries `frappe.local.partner_ctx`, so a
	request-only channel resolver answered "Partner API" and screened it. The Desk lane carries no
	partner_ctx, is not Guest, and its payload hangs off "CRM Bulk Job" — no intake sink — so the same
	resolver answered None and an operator-uploaded payload went to disk unscreened. The channel is now
	keyed on what the file IS, so both lanes resolve identically without either naming itself.
	"""

	def setUp(self):
		super().setUp()
		self._toggle(_SCREENING, 1)
		self._screening(channels=("Intake", "Partner API"))

	def _screening(self, *, channels=None, **fields):
		settings = frappe.get_single(_SCREEN_SETTINGS)
		for key, value in fields.items():
			settings.set(key, value)
		if channels is not None:
			settings.set("active_channels", [])
			for channel in channels:
				settings.append("active_channels", {"channel": channel})
		settings.save(ignore_permissions=True)
		frappe.clear_document_cache(_SCREEN_SETTINGS, _SCREEN_SETTINGS)

	def _job(self, input_format):
		"""A CRM Bulk Job in the state submit_job attaches its payload in — no partner_ctx anywhere, which
		is precisely the Desk lane."""
		job = frappe.get_doc({
			"doctype": "CRM Bulk Job",
			"partner": "Administrator",
			"operation": "lead_create",
			"input_format": input_format,
			"status": "UploadComplete",
		}).insert(ignore_permissions=True)
		self.assertIsNone(
			getattr(frappe.local, "partner_ctx", None), "premise: this is the Desk lane, not the API lane"
		)
		return job

	def _scan_rows(self, stem):
		return frappe.get_all(_SCAN_LOG, filters={"file_name": ["like", f"{stem}%"]},
		                      fields=["verdict", "action"])

	def test_a_desk_uploaded_payload_is_screened(self):
		job = self._job("csv")
		stem = self._stem()

		with _jobs_inline(), self.assertRaises(frappe.ValidationError):
			self._insert(
				file_name=f"{stem}.pdf",
				content=_EXE_HEADER,
				attached_to_doctype="CRM Bulk Job",
				attached_to_name=job.name,
			)

		rows = self._scan_rows(stem)
		self.assertEqual(len(rows), 1, "a Desk-lane bulk payload was never screened")
		self.assertEqual(rows[0].action, "Blocked")
		self.assertEqual(rows[0].verdict, "Type Mismatch")
		self.assertEqual(self._on_disk(stem), [], "a refused payload's bytes are on the app server")

	def test_an_inline_payload_is_not_screened(self):
		"""`inline` is the app's OWN serialization of an already-parsed JSON body — never an upload, so
		there is nothing to screen. The old worker skipped it deliberately and that intent survives."""
		job = self._job("inline")
		stem = self._stem()

		with _jobs_inline():
			doc = self._insert(
				file_name=f"{stem}.jsonl",
				content=b'{"first_name": "INLINE"}',
				attached_to_doctype="CRM Bulk Job",
				attached_to_name=job.name,
			)

		self.assertEqual(doc.is_private, 1, "a bulk payload must be private")
		self.assertEqual(self._scan_rows(stem), [], "the app's own serialization was screened as an upload")


class TestOffloadEndToEnd(FileLayerCase):
	"""V1.9 — the whole cycle against the real container, unchanged by the seam.

	The bytes reach Azure, the local copy is really removed once the blob is confirmed, and the row can
	still read its own content back through the proxy. If the sequencing change breaks any leg of this,
	the failure is silent in production: an attachment that 404s, or a plaintext patient file left on
	the app server forever.
	"""

	def setUp(self):
		super().setUp()
		self._stems = []
		frappe.db.set_value("CRM Tatva Automation", _OFFLOAD, "enabled", 1)
		frappe.db.set_single_value(_AZURE_SETTINGS, "remove_local_after_upload", 1)
		frappe.clear_document_cache(_AZURE_SETTINGS, _AZURE_SETTINGS)

	def tearDown(self):
		for stem in self._stems:
			for path in glob.glob(get_files_path(f"{stem}*", is_private=1)) + glob.glob(
				get_files_path(f"{stem}*", is_private=0)
			):
				try:
					os.remove(path)
				except OSError:
					pass
		super().tearDown()

	def test_offloaded_lead_attachment_is_in_azure_and_gone_from_disk(self):
		stem = f"seam{frappe.generate_hash(length=10)}"
		self._stems.append(stem)
		content = _PNG_HEADER + b"#" + frappe.generate_hash(length=16).encode()
		lead = frappe.get_doc(
			{"doctype": "CRM Lead", "first_name": "SEAM-V19", "status": "New"}
		).insert(ignore_permissions=True)

		with _jobs_inline():
			doc, key = self.upload(
				file_name=f"{stem}.png",
				content=content,
				attached_to_doctype="CRM Lead",
				attached_to_name=lead.name,
			)

		self.assert_in_azure(key, "end to end")
		self.assertEqual(
			frappe.db.get_value("File", doc.name, "is_private"), 1, "a lead attachment must be private"
		)
		leftovers = glob.glob(get_files_path(f"{stem}*", is_private=1)) + glob.glob(
			get_files_path(f"{stem}*", is_private=0)
		)
		self.assertEqual(leftovers, [], f"a plaintext copy was left on the app server: {leftovers}")
		self.assertEqual(
			frappe.get_doc("File", doc.name).get_content(),
			content,
			"the file cannot be read back through the proxy — every consumer of it is broken",
		)
