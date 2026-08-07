# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Privacy is DERIVED on every save, the owner is named once, and a link names a public address.

Three defects, all one shape — a File's stored state was trusted after birth:

D1  `apply_privacy_policy` ran from `FileOverride.before_insert` and nowhere else, so a plain `.save()`
    setting `is_private = 0` persisted. The bytes then served to anyone: on a local site out of
    `public/files`, on an offloaded site through `storage/api.download_file`, which skips its permission
    check on a public row and is `allow_guest=True`.
D2  `attached_to_doctype`, `attached_to_name`, `content_hash`, `file_size` and `file_url` are all
    `read_only = 1` on the File doctype, and frappe does NOT enforce read_only server-side. A save
    rewrote all of them. That is also D1's bypass: once privacy is re-derived from the owner, rewriting
    the owner re-homes the file onto an allowlisted doctype and makes it public BY THE RULE — so the two
    only close together.
D3  A File row could name any URL, including an internal address. Nothing fetches it today, which is why
    this is a value that must not be storable rather than a live SSRF.

Nothing is faked. These tests write real bytes to the real disk, read the real directory back, and
upload to the real Azure container — a mocked BlobStore is what let three months of file bugs through.
The assertion is always the stored row or the file on disk, never that a function was called.

Every test must fail on today's code: D1's stays green only because the recompute lands in
`FileOverride.validate`, D2's owner test only because the constant-owner guard is there, D2's column
test only because `lockdown._PERMLEVEL_1_FIELDS` now carries File, and D3's only because
`file_events.assert_link_target_safe` is called at save.

The over-block direction is tested just as hard: an upload must still name its parent and keep the URL
core derived for it, `offload` must still repoint `file_url`, `file_manager.rehome` must still re-home,
and a file the operator allowlisted must still be public after a save.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.storage.test_file_privacy_lifecycle
"""
import os
from contextlib import contextmanager

import frappe
from frappe.utils import get_files_path

from tatva_connect.access import lockdown
from tatva_connect.storage import file_manager
from tatva_connect.storage.blob_store import blob_key_from_url
from tatva_connect.tests.storage.test_file_lifecycle_seam import SeamCase

_PRIVACY = "Storage::File::privacy"
_OFFLOAD = "Storage::Azure::offload"
_METADATA_URL = "http://169.254.169.254/latest/meta-data/iam/security-credentials/"
# A public CDN host, the same one the lifecycle-seam suite already leans on — an external link that is genuinely safe.
_PUBLIC_CDN = "https://us1.discourse-cdn.com/openai1/original/4X/3/2/1/"

# Sales Manager deliberately: `crm.permissions.org_hierarchy._has_permission` grants one outside the sales
# tree write on any lead, so File's own has_permission (which delegates to the parent record) never gets in
# the way — and unlike System Manager it holds NO permlevel-1 write, which is the thing under test.
USER = "zz-file-privacy@example.com"
CLAIMER = "zz-file-claimer@example.com"  # a second real rep — the one who tries to claim what USER uploaded


class PrivacyLifecycleCase(SeamCase):
	"""Real bytes, a real non-privileged user, and the permlevel declaration really applied."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		if not frappe.db.exists("User", USER):
			user = frappe.get_doc({
				"doctype": "User", "email": USER, "first_name": "File Privacy",
				"send_welcome_email": 0, "user_type": "System User",
			}).insert(ignore_permissions=True)
			user.append("roles", {"role": "Sales Manager"})
			user.save(ignore_permissions=True)
		# The declaration is only a lock once after_migrate has applied it; apply it here so the suite proves the CODE and not whether somebody remembered to migrate.
		lockdown.apply_field_permlevels()
		lockdown.apply_field_levels()

	@classmethod
	def tearDownClass(cls):
		super().tearDownClass()
		frappe.clear_cache()  # the property setters above are rolled back with the transaction; the meta cache is not

	def tearDown(self):
		frappe.set_user("Administrator")  # blob + local-file teardown must not run as the test user
		super().tearDown()

	@contextmanager
	def _as_user(self):
		frappe.set_user(USER)
		try:
			yield
		finally:
			frappe.set_user("Administrator")

	def _insert_unchecked(self, **fields):
		"""An insert the PERMISSION engine really judges — `_insert` passes ignore_permissions, which skips
		the permlevel reset entirely and would make every column test vacuously green."""
		doc = frappe.get_doc(dict(doctype="File", **fields)).insert()
		key = blob_key_from_url(doc.file_url)
		if key:
			self._keys.append(key)
		return doc

	def _lead_attachment(self, label):
		"""A patient document: the file this whole layer exists for. Returns (doc, content)."""
		lead = self._lead(label)
		stem, content = self._stem(), self._png()
		doc = self._insert(
			file_name=f"{stem}.png",
			content=content,
			attached_to_doctype="CRM Lead",
			attached_to_name=lead.name,
		)
		return doc, content

	def _stored(self, name, fields):
		# A list of fields wants a dict; a single fieldname wants the value itself, not a one-key dict.
		return frappe.db.get_value("File", name, fields, as_dict=isinstance(fields, list))


class TestPrivacyIsDerivedOnEverySave(PrivacyLifecycleCase):
	"""D1. A flag decided at birth and never re-checked is not a checkpoint, it is a default."""

	def test_a_later_save_cannot_make_a_lead_attachment_public(self):
		# The defect, end to end: born private, then one ordinary save asks for public and gets it —
		# `is_private = 0` persists and core's own byte-mover carries the bytes into public/files, where
		# an anonymous request reads them. The recompute must overrule the caller on THIS save, not the first.
		doc, content = self._lead_attachment("PRIV-D1-FLIP")
		self.assert_bytes_are_private(doc, content, "lead attachment at birth")

		doc.is_private = 0
		doc.save(ignore_permissions=True)

		self.assertEqual(
			self._stored(doc.name, "is_private"),
			1,
			"a save set is_private = 0 on a patient document and it stuck — the bytes are now world-readable",
		)
		self.assert_bytes_are_private(
			frappe.get_doc("File", doc.name), content, "lead attachment after a save asked for public"
		)

	def test_a_save_cannot_publish_an_unattached_file(self):
		# The floor, on the update side. Unattached = no owner = private, and asking again later changes
		# nothing. This is the shape the SPA's bare uploader produces, so it is the easiest one to reach.
		stem, content = self._stem(), self._png()
		doc = self._insert(file_name=f"{stem}.png", content=content)
		self.assertEqual(doc.is_private, 1, "premise: an unattached file is born on the private floor")

		doc.is_private = 0
		doc.save(ignore_permissions=True)

		self.assertEqual(self._stored(doc.name, "is_private"), 1, "the private floor held only until the second save")

	def test_an_allowlisted_attachment_is_still_public_after_a_save(self):
		# OVER-BLOCK. The exception seam has to survive the recompute: an avatar the SPA renders for every
		# rep must stay public across an ordinary save, or the fix trades a leak for a site-wide 403.
		self._toggle(_PRIVACY, 1)
		self._allow_public("User")
		stem, content = self._stem(), self._png()
		doc = self._insert(
			file_name=f"{stem}.png", content=content,
			attached_to_doctype="User", attached_to_name="Administrator",
		)
		self.assertEqual(doc.is_private, 0, "premise: an allowlisted owner's file is born public")

		doc.save(ignore_permissions=True)

		stored = self._stored(doc.name, ["is_private", "file_url"])
		self.assertEqual(stored.is_private, 0, "the recompute revoked a file the operator explicitly allowed")
		self.assertTrue(
			os.path.exists(get_files_path(stored.file_url.rsplit("/", 1)[-1], is_private=0)),
			"the avatar's bytes left the public directory — every rep now gets a 403 on every photo",
		)


class TestTheOwnerIsNamedOnceAndOnlyOnce(PrivacyLifecycleCase):
	"""D2, the M1 half. The upload names the parent; nothing renames it afterwards through a save."""

	def test_a_save_cannot_rehome_a_file_onto_an_allowlisted_owner(self):
		# D1's bypass, and the reason the two land together: privacy is derived from the owner, so a
		# writable owner is a writable privacy flag. Re-home the patient document onto an allowlisted
		# doctype and the file becomes public BY THE RULE, with the checkpoint working exactly as designed.
		self._toggle(_PRIVACY, 1)
		self._allow_public("User")
		doc, _ = self._lead_attachment("PRIV-D2-REHOME")

		doc.attached_to_doctype = "User"
		doc.attached_to_name = "Administrator"
		with self.assertRaises(frappe.CannotChangeConstantError):
			doc.save(ignore_permissions=True)

		stored = self._stored(doc.name, ["attached_to_doctype", "is_private"])
		self.assertEqual(stored.attached_to_doctype, "CRM Lead", "the file was re-homed away from its lead")
		self.assertEqual(stored.is_private, 1, "the re-home made a patient document public by the privacy rule itself")

	def test_a_save_cannot_adopt_an_unattached_file(self):
		# The same bypass from the other end: upload with no owner (private floor), then claim one. An
		# empty owner is not a free slot — the bond is made by db.set_value, which never reaches validate.
		self._toggle(_PRIVACY, 1)
		self._allow_public("User")
		stem, content = self._stem(), self._png()
		doc = self._insert(file_name=f"{stem}.png", content=content)

		doc.attached_to_doctype = "User"
		doc.attached_to_name = "Administrator"
		with self.assertRaises(frappe.CannotChangeConstantError):
			doc.save(ignore_permissions=True)

		self.assertIsNone(self._stored(doc.name, "attached_to_doctype"), "an unattached file adopted an allowlisted owner")

	def test_the_app_can_still_re_home_a_file(self):
		# OVER-BLOCK. `file_manager.rehome` is the sanctioned re-parenting (WhatsApp's outbound adopt) and
		# it writes with db_set, which never reaches validate. A guard that stopped it would break sends.
		lead = self._lead("PRIV-D2-REHOME-OK")
		stem, content = self._stem(), self._png()
		doc = self._insert(file_name=f"{stem}.png", content=content)

		file_manager.rehome(doc, "CRM Lead", lead.name)

		stored = self._stored(doc.name, ["attached_to_doctype", "attached_to_name"])
		self.assertEqual(stored.attached_to_doctype, "CRM Lead", "the app's own re-home was blocked")
		self.assertEqual(stored.attached_to_name, lead.name)


class TestDerivedColumnsAreNotClientWritable(PrivacyLifecycleCase):
	"""D2, the computed half. read_only is a form hint; the server never enforced it."""

	def test_a_request_cannot_forge_the_derived_columns(self):
		# A real non-privileged user saving a real File. content_hash, file_size and file_url are computed
		# from the bytes — a caller that can rewrite file_url can point a row at any blob the proxy will serve.
		doc, _ = self._lead_attachment("PRIV-D2-FORGE")
		before = self._stored(doc.name, ["file_url", "content_hash", "file_size"])

		with self._as_user():
			forged = frappe.get_doc("File", doc.name)
			forged.file_url = "/files/forged.png"
			forged.content_hash = "forgedhash"
			forged.file_size = 999999
			forged.save()

		after = self._stored(doc.name, ["file_url", "content_hash", "file_size"])
		self.assertEqual(after.file_url, before.file_url, "a request rewrote file_url — the row now points wherever it likes")
		self.assertEqual(after.content_hash, before.content_hash, "a request rewrote content_hash")
		self.assertEqual(after.file_size, before.file_size, "a request rewrote file_size")

	def test_a_normal_upload_still_names_its_parent_and_keeps_its_url(self):
		# OVER-BLOCK, and the trap in this whole change. Frappe enforces a permlevel by RESETTING the field
		# to the doctype default, and on an insert that reset runs AFTER before_insert — so it discards the
		# URL, size and hash core has just derived, and the upload lands with no URL at all. M1 is asserted
		# in the same breath: the upload named its parent, and the owner columns are NOT permlevelled.
		lead = self._lead("PRIV-D2-UPLOAD")
		stem, content = self._stem(), self._png()

		with self._as_user():
			doc = self._insert_unchecked(
				file_name=f"{stem}.png", content=content,
				attached_to_doctype="CRM Lead", attached_to_name=lead.name,
			)

		stored = self._stored(doc.name, ["file_url", "file_size", "content_hash", "attached_to_doctype", "attached_to_name"])
		self.assertTrue(stored.file_url, "the upload landed with NO file_url — permlevel reset what core derived")
		self.assertEqual(stored.attached_to_name, lead.name, "the upload did not name its parent — M1 is broken")
		self.assertEqual(stored.attached_to_doctype, "CRM Lead")
		self.assertEqual(stored.file_size, len(content), "file_size was reset to the doctype default")
		self.assertTrue(stored.content_hash, "content_hash was reset to the doctype default")
		self.assert_bytes_are_private(frappe.get_doc("File", doc.name), content, "a rep's own upload")

	def test_offload_still_repoints_file_url(self):
		# OVER-BLOCK. The offload writes file_url with db_set, which bypasses permlevel — but only if the
		# lock really is permlevel and not a validate-time refusal. Real Azure: the blob is really there
		# and the row really points at the proxy, uploaded by a user who holds no permlevel-1 write.
		self._toggle(_OFFLOAD, 1)
		lead = self._lead("PRIV-D2-OFFLOAD")
		stem, content = self._stem(), self._png()

		with self._as_user():
			doc = self._insert_unchecked(
				file_name=f"{stem}.png", content=content,
				attached_to_doctype="CRM Lead", attached_to_name=lead.name,
			)

		stored_url = self._stored(doc.name, "file_url")
		key = blob_key_from_url(stored_url)
		self.assert_in_azure(key, "offload after the permlevel lock")
		self.assertEqual(frappe.get_doc("File", doc.name).get_content(), content, "the offloaded bytes no longer read back")


class TestAnExternalLinkNamesAPublicAddress(PrivacyLifecycleCase):
	"""D3. We never held the bytes, but we do decide what address is worth storing."""

	def test_a_link_to_a_cloud_metadata_address_is_refused(self):
		# 169.254.169.254 is the cloud metadata service — the canonical SSRF target, and one that needs no
		# DNS to judge. Refused by the app's ONE outbound-URL guard, never by a second copy of it.
		lead = self._lead("PRIV-D3-META")
		with self.assertRaises(frappe.ValidationError):
			self._insert(
				file_url=_METADATA_URL, file_name="meta.json",
				attached_to_doctype="CRM Lead", attached_to_name=lead.name,
			)
		self.assertFalse(
			frappe.db.exists("File", {"file_url": _METADATA_URL}),
			"an internal address was stored on a File row — the value is one sink away from being fetched",
		)

	def test_a_link_to_a_public_host_is_stored(self):
		# OVER-BLOCK. The "Web Link" tab is a real feature: a genuinely public URL is stored, and it is not
		# ours to lock, so it stays public. A guard that refused this would remove the whole tab.
		lead = self._lead("PRIV-D3-OK")
		stem = self._stem()
		url = f"{_PUBLIC_CDN}{stem}.png"
		doc = self._insert(
			file_url=url, file_name=f"{stem}.png",
			attached_to_doctype="CRM Lead", attached_to_name=lead.name,
		)
		self.assertEqual(self._stored(doc.name, "file_url"), url, "a safe external link was refused or rewritten")
		self.assertEqual(doc.is_private, 0, "an external link is not ours to lock")


class TestOnlyTheUploaderMayClaimAFreeFile(PrivacyLifecycleCase):
	"""D4. Bonding names the owner, and the owner decides privacy — so bonding is an authorization step.

	`link_attach_fields` stamps ownership on any file that is still free and whose URL an Attach field
	names. It ran on every save of every record and asked only "is this file unclaimed", never "did this
	person upload it". Proven end to end over real HTTP before the fix: one rep uploaded a private file
	with no parent (`upload_file` skips its permission check entirely when no doctype is sent), a second
	rep pointed their own avatar at that URL, and the bond re-derived privacy against `User` — which is on
	the operator allowlist — so the file was republished and served to an unauthenticated request.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("User", CLAIMER):
			claimer = frappe.get_doc({
				"doctype": "User", "email": CLAIMER, "first_name": "File Claimer",
				"send_welcome_email": 0, "user_type": "System User",
			}).insert(ignore_permissions=True)  # authz-ok: tier-a — test persona seeding
			claimer.append("roles", {"role": "Sales Manager"})
			claimer.save(ignore_permissions=True)  # authz-ok: tier-a — test persona seeding

	def _free_file_uploaded_by(self, user):
		"""A private file with no parent — exactly what `upload_file` leaves when no doctype is sent.

		Offloaded, because that is what UAT and prod run and it is the path with teeth: our bond re-derives
		privacy from the operator allowlist, so a claim there PUBLISHES. Frappe's own linker handles the
		local `/files` shape instead and takes its answer from the url prefix, so a local claim re-parents
		the row but leaves it private — a different, weaker defect that is not ours to fix here.
		"""
		self._toggle(_OFFLOAD, 1)
		frappe.set_user(user)
		try:
			doc = self._insert(file_name=f"{self._stem()}.png", content=self._png())
		finally:
			frappe.set_user("Administrator")
		self.assertIsNone(self._stored(doc.name, "attached_to_doctype"), "the fixture must start unowned")
		# The premise, asserted: the guard keys on `owner`, so a fixture owned by anyone else proves nothing.
		self.assertEqual(self._stored(doc.name, "owner"), user, "the fixture was not uploaded by the user it claims")
		# Which linker will see it: ours only reads Azure proxy urls, frappe's own only reads /files paths.
		self.assertTrue(blob_key_from_url(doc.file_url), f"fixture is not offloaded — url is {doc.file_url}")
		return doc

	def _point_avatar_at(self, user, file_url):
		frappe.set_user(user)
		try:
			me = frappe.get_doc("User", user)
			me.user_image = file_url
			me.save()
		finally:
			frappe.set_user("Administrator")

	def test_a_second_rep_cannot_claim_a_file_they_did_not_upload(self):
		doc = self._free_file_uploaded_by(USER)
		self._point_avatar_at(CLAIMER, doc.file_url)
		stored = self._stored(doc.name, ["attached_to_doctype", "attached_to_name", "is_private"])
		self.assertIsNone(stored.attached_to_name, "a rep claimed a file another rep uploaded")
		self.assertEqual(stored.is_private, 1, "the claim republished someone else's private file")

	def test_the_uploader_may_still_claim_their_own_file(self):
		doc = self._free_file_uploaded_by(USER)
		self._point_avatar_at(USER, doc.file_url)
		stored = self._stored(doc.name, ["attached_to_doctype", "attached_to_name"])
		self.assertEqual(stored.attached_to_doctype, "User", "the uploader lost the ability to attach their own file")
		self.assertEqual(stored.attached_to_name, USER, "the bond named the wrong record")
