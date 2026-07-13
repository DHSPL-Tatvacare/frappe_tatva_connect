# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The file-layer registry suite — one test per REACHABLE caller in the file registry.

NOTHING is faked. These tests upload to the REAL Azure container and assert the blob is really
there. A mocked BlobStore is what let this class of bug through: `test_offload_spine` asserts
`mock_offload.called` and proves nothing about whether a consumer can still read the file.

Azure has no transaction, so FrappeTestCase's rollback does NOT remove an uploaded blob — every
test registers its keys and tearDown deletes them (18 orphans in `develop` are the receipts of
earlier runs that did not).

Plan: docs/plans/azure-file-layer-tdd.md
Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.storage.test_file_layer_registry
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.storage.blob_store import BlobStore, blob_key_from_url

_REAL_PNG = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x08\x00\x00\x00\x08\x08\x02\x00\x00\x00Km)\xdc\x00\x00\x00&IDATx\x9cc`\x90\xb3\x89\xaa\x98\xb6\xe5\xd2\x07>\x1d\xaf\x8c\xb6%\x87\x1e\xfc\x93\xb1\x8a(\x9b\xb2\x89ahI\x00\x00\x0b\xb5Z\xc1\xce\x87\xae\xdd\x00\x00\x00\x00IEND\xaeB`\x82'

_PNG = (
	b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
	b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00"
	b"\x00\x00IEND\xaeB`\x82"
)


class FileLayerCase(FrappeTestCase):
	"""Base: real uploads, real blobs, explicit blob teardown."""

	def setUp(self):
		self.store = BlobStore()
		self._keys = []

	def tearDown(self):
		for key in self._keys:
			try:
				self.store.delete(key)
			except Exception:
				pass

	def upload(self, *, file_name, content=_PNG, attached_to_doctype=None, attached_to_name=None):
		"""A real upload through the real File doctype — doc_events offload it to real Azure."""
		doc = frappe.get_doc({
			"doctype": "File",
			"file_name": file_name,
			"content": content,
			"attached_to_doctype": attached_to_doctype,
			"attached_to_name": attached_to_name,
		}).insert(ignore_permissions=True)
		key = blob_key_from_url(doc.file_url)
		if key:
			self._keys.append(key)
		return doc, key

	def assert_in_azure(self, key, label=""):
		self.assertIsNotNone(key, "file_url carries no blob key — it never offloaded")
		self.assertTrue(self.store.exists(key), f"blob missing from Azure: {key}")
		print(f"\n  [azure] {label} key={key}\n  [azure] sas={self.store.sas_url(key)[:100]}...")


class TestGetContentResolvesByUrl(FileLayerCase):
	"""PHASE 1 — the P0.

	`communication/email.py:279` copies an emailed attachment with ONLY file_url + is_private, so
	`custom_uploaded_to_azure` is dropped. `file_override.get_content` gates on that flag, bails to
	core, and core open()s the proxy URL as a disk path -> FileNotFoundError -> the mail never sends.
	The blob key lives inside file_url, which the copy DOES carry: resolve from the URL, not the flag.
	"""

	def _core_style_copy(self, src, attached_to_doctype, attached_to_name):
		"""Exactly the fields core copies (email.py:279-283 / crm comment.py:101-104)."""
		return frappe.get_doc({
			"doctype": "File",
			"file_url": src.file_url,
			"is_private": src.is_private,
			"attached_to_doctype": attached_to_doctype,
			"attached_to_name": attached_to_name,
			"folder": "Home/Attachments",
		}).insert(ignore_permissions=True)

	def test_email_attachment_copy_returns_bytes(self):
		# The email worker (email_queue.py:452) calls get_content() on the Communication's copy.
		src, key = self.upload(file_name="phase1-email.png")
		self.assert_in_azure(key, "email attachment")

		copy = self._core_style_copy(src, "Communication", "PHASE1-EMAIL")
		self.assertEqual(copy.custom_uploaded_to_azure, 0, "core's copy drops the flag — that is the premise")
		self.assertEqual(copy.get_content(), _PNG, "the email worker cannot read the attachment")

	def test_comment_attachment_copy_returns_bytes(self):
		# crm/api/comment.py:101 copies the same way for a comment attachment.
		src, _ = self.upload(file_name="phase1-comment.png")
		copy = self._core_style_copy(src, "Comment", "PHASE1-COMMENT")
		self.assertEqual(copy.get_content(), _PNG)

	def test_get_content_without_azure_flag(self):
		# The flag is not the truth — the URL is. A row with the proxy URL must resolve regardless.
		src, _ = self.upload(file_name="phase1-flagless.png")
		frappe.db.set_value("File", src.name, "custom_uploaded_to_azure", 0, update_modified=False)
		reloaded = frappe.get_doc("File", src.name)
		self.assertEqual(reloaded.get_content(), _PNG)

	def test_offloaded_original_still_reads(self):
		# No regression on the path that works today: the original row (flag=1) still resolves.
		src, _ = self.upload(file_name="phase1-original.png")
		self.assertEqual(frappe.get_doc("File", src.name).get_content(), _PNG)


class TestAttachFieldBond(FileLayerCase):
	"""PHASE 2 — the bond.

	Core's `attach_files_to_document` (file/utils.py:325) skips any Attach value that does not start
	with /files — remote URLs are excluded BY DESIGN, since core does not manage remote files. So an
	avatar/logo/image uploaded by the SPA's bare <FileUploader> (no doctype/docname) is never bonded:
	`attached_to_doctype` stays NULL forever. Four of our own systems key on that bond — the privacy
	floor (file_events.py:44), the delete cascade (file_manager.remove_all -> File.on_trash -> blob
	delete), the File automation subject (subjects.py:19), and the attachments sidebar.
	"""

	def _lead(self):
		doc = frappe.get_doc({"doctype": "CRM Lead", "first_name": "PHASE2-LEAD", "status": "New"})
		return doc.insert(ignore_permissions=True)

	def test_attach_field_bonds_file_on_save(self):
		# The SPA uploads unattached, then writes the URL into the Attach field and saves the record.
		lead = self._lead()
		f, key = self.upload(file_name="phase2-lead-image.png")
		self.assert_in_azure(key, "lead image")
		self.assertIsNone(f.attached_to_doctype, "premise: the upload arrives unattached")

		lead.image = f.file_url
		lead.save(ignore_permissions=True)

		bonded = frappe.get_doc("File", f.name)
		self.assertEqual(bonded.attached_to_doctype, "CRM Lead")
		self.assertEqual(bonded.attached_to_name, lead.name)
		self.assertEqual(bonded.attached_to_field, "image")

	def test_avatar_bonds_to_user(self):
		# ProfileSettings.vue:198 — user_image is an Attach Image field on core User.
		f, _ = self.upload(file_name="phase2-avatar.png")
		user = frappe.get_doc("User", "Administrator")
		user.user_image = f.file_url
		user.save(ignore_permissions=True)

		bonded = frappe.get_doc("File", f.name)
		self.assertEqual(bonded.attached_to_doctype, "User")
		self.assertEqual(bonded.attached_to_name, "Administrator")

	def test_bonded_file_appears_in_attachments(self):
		# frappe.desk.form.load.get_attachments keys on attached_to_* — the sidebar/Attachments tab.
		from frappe.desk.form.load import get_attachments

		lead = self._lead()
		f, _ = self.upload(file_name="phase2-sidebar.png")
		lead.image = f.file_url
		lead.save(ignore_permissions=True)

		names = [a.name for a in get_attachments("CRM Lead", lead.name)]
		self.assertIn(f.name, names)

	def test_delete_record_deletes_blob(self):
		# delete_doc -> file_manager.remove_all (keys on attached_to_*) -> File.on_trash -> blob delete.
		lead = self._lead()
		f, key = self.upload(file_name="phase2-cascade.png")
		lead.image = f.file_url
		lead.save(ignore_permissions=True)

		frappe.delete_doc("CRM Lead", lead.name, force=True, ignore_permissions=True)

		self.assertFalse(frappe.db.exists("File", f.name), "the File row survived its record")
		self.assertFalse(self.store.exists(key), f"the blob is orphaned in Azure: {key}")

	def test_aliased_blob_survives_one_delete(self):
		# Email/comment copies deliberately alias ONE blob from TWO File rows (comment.py:101).
		# Deleting one copy must NOT delete the blob out from under the other. Pins the on_trash refcount.
		src, key = self.upload(file_name="phase2-alias.png")
		alias = frappe.get_doc({
			"doctype": "File",
			"file_url": src.file_url,
			"is_private": src.is_private,
			"attached_to_doctype": "Comment",
			"attached_to_name": "PHASE2-ALIAS",
		}).insert(ignore_permissions=True)

		frappe.delete_doc("File", alias.name, force=True, ignore_permissions=True)

		self.assertTrue(self.store.exists(key), "the surviving row's blob was deleted with its alias")
		self.assertEqual(frappe.get_doc("File", src.name).get_content(), _PNG)


class TestPrivacyFloor(FileLayerCase):
	"""PHASE 3 — the floor asks the wrong question.

	`apply_privacy_policy` (file_events.py:44) returns early when there is no attached_to_doctype, so an
	unattached upload keeps the uploader's choice — which for the SPA's bare <FileUploader> is PUBLIC.
	16 of 17 unattached files on UAT are is_private=0, and the download proxy is allow_guest.

	The trap: phase 2 now bonds avatars to User. Force them private and `File.is_downloadable()` demands
	read on User — which a rep does NOT have -> 403 on every avatar in the SPA. So profile/branding
	doctypes are PUBLIC by operator allowlist; patient documents are private. Both are pinned here.
	"""

	_TOGGLE = "Storage::File::privacy"
	_SETTINGS = "CRM Azure Storage Settings"
	_LIST = "public_attachment_doctypes"

	def _allow_public(self, doctypes):
		frappe.db.set_value("CRM Tatva Automation", self._TOGGLE, "enabled", 1)
		frappe.db.set_single_value(self._SETTINGS, self._LIST, doctypes)

	def test_unattached_upload_is_private(self):
		# A bare upload with nowhere to belong must fall to the private floor, not the uploader's choice.
		f, _ = self.upload(file_name="phase3-unattached.png")
		self.assertEqual(frappe.db.get_value("File", f.name, "is_private"), 1)

	def test_patient_document_is_private(self):
		# A lead attachment is patient data — private, always.
		self._allow_public("User\nContact")
		lead = frappe.get_doc({"doctype": "CRM Lead", "first_name": "PHASE3-LEAD", "status": "New"}).insert(
			ignore_permissions=True
		)
		f, _ = self.upload(
			file_name="phase3-report.png", attached_to_doctype="CRM Lead", attached_to_name=lead.name
		)
		self.assertEqual(frappe.db.get_value("File", f.name, "is_private"), 1)

	def test_avatar_is_public_once_bonded(self):
		# THE TRAP: phase 2 bonds the avatar to User. If the floor then privatises it, every rep gets a
		# 403 on every colleague's photo. An allowlisted doctype must come back out public after bonding.
		self._allow_public("User")
		f, _ = self.upload(file_name="phase3-avatar.png")

		user = frappe.get_doc("User", "Administrator")
		user.user_image = f.file_url
		user.save(ignore_permissions=True)

		bonded = frappe.get_doc("File", f.name)
		self.assertEqual(bonded.attached_to_doctype, "User")
		self.assertEqual(bonded.is_private, 0, "an allowlisted doctype's file must be public after bonding")

	def test_whatsapp_outbound_media_sends_as_bytes_while_private(self):
		# frappe_whatsapp's native path hands `attach` to Meta as a link Meta fetches anonymously
		# (whatsapp_message.py:76) — which would make a private file a 403 and force WA media public.
		# OUR override never does that: _send_attachment (whatsapp/message.py:109) pushes fd.get_content()
		# as BYTES to WATI, because "the provider cannot authenticate to our proxy URL". So the privacy
		# floor cannot break outbound media. This test pins that contract — if anyone ever switches the
		# send back to a URL, a private WA file stops sending and this test is the one that says why.
		from tatva_connect.whatsapp import media

		lead = frappe.get_doc({"doctype": "CRM Lead", "first_name": "PHASE3-WA", "status": "New"}).insert(
			ignore_permissions=True
		)
		f, _ = self.upload(file_name="phase3-wa-outbound.png")  # WhatsAppBox.vue:32 — bare uploader
		adopted = media.adopt_outbound_media(f.file_url, lead.name, "wamid.PHASE3")
		self._keys.append(blob_key_from_url(adopted))  # rehome re-keys the blob into the lead's folder

		row = frappe.db.get_value(
			"File", {"file_url": adopted}, ["name", "is_private", "attached_to_doctype"], as_dict=True
		)
		print(f"\n  [wa] adopted={adopted}\n  [wa] is_private={row.is_private} bonded={row.attached_to_doctype}")
		self.assertEqual(row.attached_to_doctype, "CRM Lead", "the sent media must land on the lead")
		self.assertEqual(row.is_private, 1, "patient WhatsApp media must be private")
		self.assertEqual(
			frappe.get_doc("File", row.name).get_content(), _PNG,
			"the send path reads bytes via get_content — privacy must not block it",
		)

	def test_caller_cannot_choose_public(self):
		# ONE checkpoint. A caller asking for public on a non-allowlisted doctype is overruled — whatever
		# the upload dialog ticked, whatever the python kwarg said. Privacy is not a preference.
		self._allow_public("User")
		lead = frappe.get_doc({"doctype": "CRM Lead", "first_name": "PHASE3-OVERRULE", "status": "New"}).insert(
			ignore_permissions=True
		)
		# The caller cannot even ASK any more — `private=` was removed from the front door, because a
		# parameter that looks like a decision and is silently overruled is how this class of bug starts.
		# What a caller CAN still do is set is_private on the doc directly; the checkpoint overrules it.
		doc = frappe.get_doc({
			"doctype": "File", "file_name": "phase3-caller-public.png", "content": _PNG,
			"is_private": 0,
			"attached_to_doctype": "CRM Lead", "attached_to_name": lead.name,
		}).insert(ignore_permissions=True)
		self._keys.append(blob_key_from_url(doc.file_url))
		self.assertEqual(
			frappe.db.get_value("File", doc.name, "is_private"), 1,
			"a caller asked for public on a lead attachment and got it — privacy has a second authority",
		)

	def test_unlisted_doctype_stays_private_after_bonding(self):
		# The allowlist is the ONLY way out of the floor — bonding alone must not make a file public.
		self._allow_public("")
		lead = frappe.get_doc({"doctype": "CRM Lead", "first_name": "PHASE3-LEAD2", "status": "New"}).insert(
			ignore_permissions=True
		)
		f, _ = self.upload(file_name="phase3-locked.png")
		lead.image = f.file_url
		lead.save(ignore_permissions=True)

		self.assertEqual(frappe.db.get_value("File", f.name, "is_private"), 1)


class TestScreeningChannel(FileLayerCase):
	"""PHASE 4 — the screener asks the wrong question.

	`guard_file` (intake/guards.py:79) gates on `attached_to_doctype in _intake_sinks()`. But screening
	runs at File.before_insert, and a Web Form's attach control has no `frm` (frappe attach.js:80), so it
	sends no doctype/docname and the file arrives UNATTACHED — the guard returns and the magic-byte sniff
	and the ClamAV scan never run. Every intake form is login_required=0, so a patient's upload is an
	unauthenticated request: the channel is the REQUEST, not the attachment.
	"""

	_TOGGLE = "Storage::File::screening"
	_SCREEN_SETTINGS = "CRM File Screening Settings"
	# A DOS/PE header disguised as a .png — a pure magic-byte mismatch. Not disguised as a .pdf: frappe's
	# own unsafe-PDF check parses .pdf bytes first and dies in pypdf before our screener is ever consulted.
	_EXE_BYTES = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00"

	def setUp(self):
		super().setUp()
		frappe.db.set_value("CRM Tatva Automation", self._TOGGLE, "enabled", 1)
		settings = frappe.get_single(self._SCREEN_SETTINGS)
		settings.set("active_channels", [])
		settings.append("active_channels", {"channel": "Intake"})
		settings.save(ignore_permissions=True)

	def test_guest_webform_upload_is_screened(self):
		# A patient on the public enrolment form is Guest (every intake form is login_required=0), and
		# their file arrives unattached (attach.js:80 — a Web Form has no frm). It must still be screened.
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.ValidationError):
				frappe.get_doc({
					"doctype": "File",
					"file_name": "phase4-report.png",
					"content": self._EXE_BYTES,
				}).insert(ignore_permissions=True)
		finally:
			frappe.set_user("Administrator")

	def test_intake_sink_upload_still_screened(self):
		# No regression on the path that works today: a file attached to an intake sink is screened.
		from tatva_connect.intake.intake import _intake_doctypes

		sinks = list(_intake_doctypes())
		if not sinks:
			self.skipTest("no enabled intake form on this site")
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc({
				"doctype": "File",
				"file_name": "phase4-sink.png",
				"content": self._EXE_BYTES,
				"attached_to_doctype": sinks[0],
				"attached_to_name": "PHASE4",
			}).insert(ignore_permissions=True)

	def test_clean_guest_upload_passes_screening(self):
		# Screening must not become a wall: a genuine PNG from a patient still uploads.
		frappe.set_user("Guest")
		try:
			doc = frappe.get_doc({
				"doctype": "File", "file_name": "phase4-clean.png", "content": _PNG,
			}).insert(ignore_permissions=True)
			self._keys.append(blob_key_from_url(doc.file_url))
			self.assertTrue(doc.name)
		finally:
			frappe.set_user("Administrator")


class TestTimelinePrivacySignal(FileLayerCase):
	"""PHASE 5 — the timeline lies about privacy.

	`crm/api/activities.py:513` decides the lock icon by sniffing the string "private/files" out of the
	href stored in the Attachment comment. An offloaded URL never contains it, so `is_private` is ALWAYS
	False and every private attachment renders as public in the Lead/Deal activity feed (Activities.vue:239
	never draws the lock). Privacy is a fact on the File row, not a guess about a URL.
	"""

	def test_lock_icon_for_private_attachment(self):
		from crm.api.activities import parse_attachment_log

		lead = frappe.get_doc({"doctype": "CRM Lead", "first_name": "PHASE5-LEAD", "status": "New"}).insert(
			ignore_permissions=True
		)
		f, _ = self.upload(
			file_name="phase5-report.png", attached_to_doctype="CRM Lead", attached_to_name=lead.name
		)
		self.assertEqual(f.is_private, 1, "premise: a lead attachment is private")

		html = f'<a href="{f.file_url}" target="_blank">{f.file_name}</a>'
		parsed = parse_attachment_log(html, "Attachment")
		self.assertTrue(
			parsed["is_private"],
			"a private attachment renders as public in the timeline — no lock icon, no warning",
		)

	def test_public_attachment_has_no_lock(self):
		# The signal must be true in both directions, or the lock means nothing.
		from crm.api.activities import parse_attachment_log

		frappe.db.set_value("CRM Tatva Automation", "Storage::File::privacy", "enabled", 1)
		frappe.db.set_single_value("CRM Azure Storage Settings", "public_attachment_doctypes", "User")
		f, _ = self.upload(
			file_name="phase5-avatar.png", attached_to_doctype="User", attached_to_name="Administrator"
		)
		self.assertEqual(f.is_private, 0, "premise: an allowlisted doctype's file is public")

		html = f'<a href="{f.file_url}" target="_blank">{f.file_name}</a>'
		self.assertFalse(parse_attachment_log(html, "Attachment")["is_private"])


class TestCopiedFileName(FileLayerCase):
	"""The blob-key hash leaks into the filename of every copied attachment.

	Core copies a File with only file_url + is_private (email.py:279, crm comment.py:101), so `file_name`
	is absent and core derives it from the URL: `file_name = self.file_url.split("/")[-1]` (file.py:511).
	The last segment of an offloaded URL is the BLOB KEY basename — `e545a06983_report.pdf`. So a comment
	chip reads `15ab6ba2d6_tatvacare_partner_api.openapi.json`, and an emailed attachment reaches the
	patient named after our storage hash (email_queue.py:453 sends `_file.file_name`).
	"""

	def _copy(self, src, dt, dn):
		return frappe.get_doc({
			"doctype": "File", "file_url": src.file_url, "is_private": src.is_private,
			"attached_to_doctype": dt, "attached_to_name": dn, "folder": "Home/Attachments",
		}).insert(ignore_permissions=True)

	def test_comment_copy_keeps_the_real_filename(self):
		src, _ = self.upload(file_name="quarterly-report.png", content=_PNG)
		copy = self._copy(src, "Comment", "PHASE6-COMMENT")
		self.assertEqual(copy.file_name, src.file_name, "the blob-key hash leaked into the filename")

	def test_email_copy_keeps_the_real_filename(self):
		# This is the name the patient sees on the attachment in their inbox.
		src, _ = self.upload(file_name="consent-form.png", content=_PNG)
		copy = self._copy(src, "Communication", "PHASE6-EMAIL")
		self.assertEqual(copy.file_name, src.file_name)
		self.assertNotIn("_", copy.file_name.split(".")[0][:11], "a hash prefix is still on the name")


class TestActivityFeedAttachmentLabel(FileLayerCase):
	"""The activity feed prints a raw storage URL where a filename belongs.

	"Administrator changed Image from /api/method/tatva_connect.storage.api.downl... to
	/api/method/tatva_connect.storage.api.downl..." — an Attach field's value is a URL, and the feed
	renders values verbatim (crm/api/activities.py builds its field map with label+options only, so it
	cannot tell an Attach field from a Data field). A user should read the file's name.
	"""

	def test_attach_value_renders_as_filename(self):
		from crm.api.activities import attachment_label

		f, _ = self.upload(file_name="consent-form.png")
		# frappe appends a uniqueness suffix at upload, so assert against the row's stored name — the point
		# is that the feed shows the FILE's name and never the storage URL or its blob-key hash.
		self.assertEqual(attachment_label(f.file_url), f.file_name)
		self.assertNotIn("/api/method", attachment_label(f.file_url))
		self.assertTrue(attachment_label(f.file_url).startswith("consent-form"))

	def test_unknown_url_falls_back_to_basename(self):
		from crm.api.activities import attachment_label

		url = "/api/method/tatva_connect.storage.api.download_file?file_name=crm/crm_lead/x/ab12cd34ef_scan.pdf"
		self.assertEqual(attachment_label(url), "scan.pdf")


class TestLeadAttachmentsAggregate(FileLayerCase):
	"""Every file that belongs to a lead should be findable on the lead.

	Frappe gives a File exactly ONE parent (`attached_to_doctype`), and each surface parents its own:
	a comment file -> Comment, a note file -> FCRM Note, a task file -> CRM Task, an email attachment ->
	Communication, WhatsApp media -> the message or the lead. `get_attachments()` filters on a single
	parent, so the Attachments tab could only ever show files parented to the lead itself — a rep had to
	remember WHERE a document was added in order to find it again. Stock CRM has no aggregation.

	This is a READ-side union: the file keeps its true parent (the delete cascade, the privacy floor and
	the blob refcount all key on that), and only the tab looks wider.
	"""

	def setUp(self):
		super().setUp()
		self.lead = frappe.get_doc(
			{"doctype": "CRM Lead", "first_name": "AGG-LEAD", "status": "New"}
		).insert(ignore_permissions=True)

	def _attach(self, dt, dn, file_name):
		f, _ = self.upload(file_name=file_name, attached_to_doctype=dt, attached_to_name=dn)
		return f

	def test_lead_attachments_include_every_surface(self):
		from crm.api.activities import get_attachments

		direct = self._attach("CRM Lead", self.lead.name, "agg-direct.png")

		comment = frappe.get_doc({
			"doctype": "Comment", "comment_type": "Comment", "reference_doctype": "CRM Lead",
			"reference_name": self.lead.name, "content": "see attached",
		}).insert(ignore_permissions=True)
		on_comment = self._attach("Comment", comment.name, "agg-comment.png")

		note = frappe.get_doc({
			"doctype": "FCRM Note", "title": "n", "reference_doctype": "CRM Lead",
			"reference_docname": self.lead.name,
		}).insert(ignore_permissions=True)
		on_note = self._attach("FCRM Note", note.name, "agg-note.png")

		task = frappe.get_doc({
			"doctype": "CRM Task", "title": "t", "status": "Backlog", "reference_doctype": "CRM Lead",
			"reference_docname": self.lead.name,
		}).insert(ignore_permissions=True)
		on_task = self._attach("CRM Task", task.name, "agg-task.png")

		comm = frappe.get_doc({
			"doctype": "Communication", "communication_type": "Communication", "content": "hi",
			"subject": "s", "reference_doctype": "CRM Lead", "reference_name": self.lead.name,
		}).insert(ignore_permissions=True)
		on_email = self._attach("Communication", comm.name, "agg-email.png")

		found = {a["file_name"]: a for a in get_attachments("CRM Lead", self.lead.name)}
		for f in (direct, on_comment, on_note, on_task, on_email):
			self.assertIn(f.file_name, found, f"{f.file_name} is invisible on the lead")

		self.assertEqual(found[on_comment.file_name].get("source"), "Comment")
		self.assertEqual(found[on_note.file_name].get("source"), "Note")
		self.assertEqual(found[on_task.file_name].get("source"), "Task")
		self.assertEqual(found[on_email.file_name].get("source"), "Email")

	def test_child_attachments_tab_is_unchanged(self):
		# get_attachments is ALSO called for a Comment and a Communication (the chips under each
		# activity). Aggregation must not leak the lead's other files into those.
		from crm.api.activities import get_attachments

		comment = frappe.get_doc({
			"doctype": "Comment", "comment_type": "Comment", "reference_doctype": "CRM Lead",
			"reference_name": self.lead.name, "content": "c",
		}).insert(ignore_permissions=True)
		on_comment = self._attach("Comment", comment.name, "agg-scope.png")
		self._attach("CRM Lead", self.lead.name, "agg-elsewhere.png")

		names = [a["file_name"] for a in get_attachments("Comment", comment.name)]
		self.assertEqual(names, [on_comment.file_name], "a comment's chips must show only its own files")


class TestExternalLinkAttachment(FileLayerCase):
	"""A web-link attachment is a LINK, not a file we hold.

	FilesUploader's "Web Link" tab (FilesUploader.vue:159) creates a File whose file_url is somebody
	else's URL. We never receive its bytes: nothing is offloaded, nothing is screened, and our download
	proxy is never in the path — the browser fetches it straight from that host. Stamping is_private=1 on
	it made the Attachments tab draw a padlock next to an image anyone on the internet can open. The
	privacy floor governs the files we STORE; it has no jurisdiction over a link.
	"""

	def test_external_link_is_not_marked_private(self):
		lead = frappe.get_doc({"doctype": "CRM Lead", "first_name": "LINK-LEAD", "status": "New"}).insert(
			ignore_permissions=True
		)
		f = frappe.get_doc({
			"doctype": "File",
			"file_url": "https://us1.discourse-cdn.com/openai1/original/4X/3/2/1/abc.png",
			"file_name": "abc.png",
			"attached_to_doctype": "CRM Lead",
			"attached_to_name": lead.name,
		}).insert(ignore_permissions=True)

		self.assertEqual(f.is_private, 0, "a padlock on a public CDN link is a lie the system tells")
		self.assertEqual(getattr(f, "custom_uploaded_to_azure", 0), 0, "a link must never be offloaded")
		self.assertIsNone(blob_key_from_url(f.file_url), "a link carries no blob key")

	def test_stored_file_still_hits_the_floor(self):
		# The floor must not be loosened by the link exception: a real upload is still private.
		f, _ = self.upload(file_name="link-guard.png")
		self.assertEqual(frappe.db.get_value("File", f.name, "is_private"), 1)


class TestByteAccessThroughOverride(FileLayerCase):
	"""PHASE 7 (M2) — nothing may read a file off the disk.

	Proven against the real container: get_content() works (FileOverride owns it) while get_full_path()
	hands back the proxy URL, so every caller that wants a PATH explodes:

	    OPEN(get_full_path()) -> FileNotFoundError

	That single failure is Data Import (importer.py:443), the xlsx/csv readers (xlsxutils.py:633,
	csvutils.py:32), File.unzip, zip_files, optimize_file, LMS course import/export, Insights' CSV ->
	DuckDB/pandas, and the Wiki migrator. They are not eight bugs; they are one, and it is ours: the
	bytes moved to Azure and the path did not follow. FileOverride is the only class that knows where the
	bytes live, so it must answer for the path too — then every app is fixed without touching a line of it.
	"""

	def test_get_full_path_is_readable(self):
		f, _ = self.upload(file_name="p7-path.png")
		path = frappe.get_doc("File", f.name).get_full_path()
		with open(path, "rb") as fh:
			self.assertEqual(fh.read(), _PNG, "the path handed to every disk-reader is not readable")

	def test_csv_reader_reads_an_offloaded_file(self):
		# frappe.utils.csvutils.read_csv_content_from_attached_file — the real Data Import path.
		from frappe.utils.csvutils import read_csv_content_from_attached_file

		csv_bytes = b"first_name,status\nCSV-LEAD,New\n"
		lead = frappe.get_doc({"doctype": "CRM Lead", "first_name": "P7-CSV", "status": "New"}).insert(
			ignore_permissions=True
		)
		self.upload(
			file_name="p7-import.csv", content=csv_bytes,
			attached_to_doctype="CRM Lead", attached_to_name=lead.name,
		)
		rows = read_csv_content_from_attached_file(frappe.get_doc("CRM Lead", lead.name))
		self.assertEqual(rows[0], ["first_name", "status"])
		self.assertEqual(rows[1], ["CSV-LEAD", "New"])

	def test_resaving_an_offloaded_file_does_not_throw(self):
		# validate_file_on_disk / generate_content_hash must not go looking for bytes on the disk.
		f, _ = self.upload(file_name="p7-resave.png")
		doc = frappe.get_doc("File", f.name)
		doc.save(ignore_permissions=True)
		self.assertTrue(frappe.db.exists("File", f.name))

	def test_thumbnail_is_generated_for_an_offloaded_image(self):
		# make_thumbnail() takes the local-image branch and silently swallows the failure, so an offloaded
		# image never gets a thumbnail. Generate it from the bytes we already hold.
		f, _ = self.upload(file_name="p7-thumb.png", content=_REAL_PNG)
		doc = frappe.get_doc("File", f.name)
		doc.make_thumbnail()
		self.assertTrue(doc.thumbnail_url, "no thumbnail was generated for an offloaded image")


class TestOwnedAtBirth(FileLayerCase):
	"""M1: an upload names its parent, so no file is ever born unowned.

	The six SPA uploaders (avatar, lead/contact/org image, brand assets, WhatsApp media) uploaded with no
	doctype even though the record was on screen — 17 rows on this site are the sole reference to a blob
	nothing can ever reclaim. They now pass doctype/docname, which is Frappe's own native way to say who
	owns a file: `upload_file` attaches it at insert, before our linker is ever consulted. The linker
	stays as the fallback for a record that does not exist yet (an attach inside a create modal).
	"""

	def test_upload_with_a_parent_is_bonded_at_insert(self):
		lead = frappe.get_doc({"doctype": "CRM Lead", "first_name": "M1-LEAD", "status": "New"}).insert(
			ignore_permissions=True
		)
		f, key = self.upload(
			file_name="m1-owned.png", attached_to_doctype="CRM Lead", attached_to_name=lead.name
		)
		self.assertEqual(f.attached_to_doctype, "CRM Lead", "the upload did not name its parent")
		self.assert_in_azure(key, "owned at birth")

	def test_an_owned_file_is_reclaimed_with_its_record(self):
		# The whole point of ownership: the blob cannot outlive the record.
		lead = frappe.get_doc({"doctype": "CRM Lead", "first_name": "M1-DEL", "status": "New"}).insert(
			ignore_permissions=True
		)
		f, key = self.upload(
			file_name="m1-reclaim.png", attached_to_doctype="CRM Lead", attached_to_name=lead.name
		)
		frappe.delete_doc("CRM Lead", lead.name, force=True, ignore_permissions=True)
		self.assertFalse(frappe.db.exists("File", f.name))
		self.assertFalse(self.store.exists(key), "the blob outlived the record that owned it")


class TestOverrideCoversCore(FileLayerCase):
	"""The tripwire: FileOverride must keep covering every way core reaches a file's bytes.

	Frappe's next upgrade is the threat model. If core renames one of these, or grows a new byte/path
	reader we do not cover, the failure today is SILENT — a 404, an unsent email, an import that reads
	nothing — and we find out from a user six weeks later. This test turns that into a red build.
	"""

	_COVERED = ("get_content", "get_full_path", "exists_on_disk", "validate_file_on_disk", "make_thumbnail")

	def test_every_covered_method_still_exists_on_core(self):
		from frappe.core.doctype.file.file import File

		for name in self._COVERED:
			self.assertTrue(
				hasattr(File, name),
				f"core File no longer has {name}() — our override is now dead code, and the readers it "
				f"protected are silently broken again",
			)

	def test_our_override_actually_overrides_them(self):
		from frappe.core.doctype.file.file import File

		from tatva_connect.storage.file_override import FileOverride

		for name in self._COVERED:
			self.assertIsNot(
				getattr(FileOverride, name), getattr(File, name),
				f"FileOverride.{name}() is no longer overriding core — Azure-backed files fall back to disk",
			)

	def test_the_override_is_the_one_bound_to_the_doctype(self):
		# hooks.py must still bind it, or every override above is inert.
		doc = frappe.get_doc("File", {"custom_uploaded_to_azure": 1, "is_folder": 0})
		self.assertEqual(type(doc).__name__, "FileOverride", "hooks.py no longer binds our File class")
