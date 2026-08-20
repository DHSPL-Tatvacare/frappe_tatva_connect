# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""WHO may read an offloaded blob, per surface, per user — the download proxy's authorisation.

WHY EVERY CASE REPEATS. The defect these tests were written against was not a wrong answer, it was an
UNSTABLE one: the proxy judged whichever row MariaDB returned first for the blob, and File names are
random hashes, so the same file gave an entitled colleague a 403 on roughly half of attempts. A single
trial per case certifies a coin flip as a pass — it did exactly that to us twice while this was being
diagnosed — so each case runs `_REPEATS` times and asserts ONE distinct outcome across them.

WHY GUEST IS A FIRST-CLASS ROW. Intake uploads files owned by the literal user "Guest"
(`intake/guards.py`), so any rule that treats an owner match as proof lets one anonymous visitor read
another's upload. That hole is invisible on every logged-in surface and shows up only here.

Bytes are never moved and Azure is never reached: rows are pointed at a proxy URL exactly as `offload`
does (`db_set`, no save), which is the state the gate actually sees in production.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.storage.test_blob_read_access
"""

import os

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.storage import blob_store, file_access

_REPEATS = 8
_KEY = "tc-test-blob-access/probe.png"
_PRIVATE_SRC = "/private/files/tc-blob-access-probe.png"


class TestBlobReadAccess(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.proxy = blob_store.download_url(_KEY)
		cls._ensure_probe_bytes()
		cls._pick_actors()

	@classmethod
	def _pick_actors(cls):
		"""A rep who really owns a lead AND can read it, plus two reps with no claim on it.

		Chosen from live data rather than fixtured: the verdict under test is the site's real permission
		matrix, and a hand-made lead would prove the fixture instead. Skips if the site has no such rep."""
		cls.leads = []
		cls.colleague = None
		reps = frappe.get_all(
			"Has Role", filters={"role": "Sales User", "parenttype": "User"}, pluck="parent", limit=60
		)
		for rep in reps:
			leads = frappe.get_all("CRM Lead", filters={"lead_owner": rep}, pluck="name", limit=_REPEATS)
			if len(leads) >= _REPEATS and frappe.has_permission("CRM Lead", "read", leads[0], user=rep):
				cls.colleague, cls.leads = rep, leads
				break
		others = [r for r in reps if r != cls.colleague]
		cls.uploader = others[0] if others else None
		cls.outsider = next(
			(r for r in others[1:] if not frappe.has_permission("CRM Lead", "read", cls.leads[0], user=r)),
			None,
		) if cls.leads else None

	@classmethod
	def _ensure_probe_bytes(cls):
		"""One real byte-file on disk so core's `validate_file_path` accepts the row. Idempotent, no DB row."""
		path = frappe.get_site_path(_PRIVATE_SRC.lstrip("/"))
		if not os.path.exists(path):
			os.makedirs(os.path.dirname(path), exist_ok=True)
			with open(path, "wb") as f:
				f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 64)

	def setUp(self):
		if not (self.leads and self.uploader and self.outsider):
			self.skipTest(f"site has no rep owning {_REPEATS} readable leads with two unrelated reps")

	def _row(self, attached_to_doctype=None, attached_to_name=None, owner=None):
		"""A File row on the shared blob, in the state `offload` leaves it."""
		doc = frappe.get_doc({
			"doctype": "File",
			"file_url": _PRIVATE_SRC,
			"file_name": "probe.png",
			"attached_to_doctype": attached_to_doctype,
			"attached_to_name": attached_to_name,
		}).insert(ignore_permissions=True)
		frappe.db.set_value("File", doc.name, {
			"owner": owner or self.uploader,
			"file_url": self.proxy,
			"custom_uploaded_to_azure": 1,
			"is_private": 1,
		}, update_modified=False)
		return doc.name

	def _comment(self, lead):
		doc = frappe.get_doc({
			"doctype": "Comment", "comment_type": "Comment", "reference_doctype": "CRM Lead",
			"reference_name": lead, "content": "probe", "comment_email": self.uploader,
		}).insert(ignore_permissions=True)
		frappe.db.set_value("Comment", doc.name, "owner", self.uploader, update_modified=False)
		return doc.name

	def _verdicts(self, build, user):
		"""`_REPEATS` independent trials of one case — the set of answers seen.

		Each trial uses its OWN lead: a Comment write touches the parent's `_comments` column, so
		repeating on one lead deadlocks the harness rather than the product."""
		seen = set()
		for lead in self.leads[:_REPEATS]:
			try:
				build(lead)
				seen.add(file_access.may_read_blob(_KEY, user))
			finally:
				frappe.db.rollback()
		return seen

	def _assert_stable(self, build, user, expected, msg):
		seen = self._verdicts(build, user)
		self.assertEqual(seen, {expected}, f"{msg} — saw {seen} across {_REPEATS} trials")

	# -- the lockout: a comment attachment is the lead's file, not the Comment's -------------

	def test_colleague_reads_a_comment_attachment(self):
		"""The whole defect: a rep entitled to the lead was 403'd on a file commented onto it."""
		def build(lead):
			self._row()  # the staged orphan the composer leaves behind
			self._row(attached_to_doctype="Comment", attached_to_name=self._comment(lead))

		self._assert_stable(build, self.colleague, True, "lead owner denied a comment attachment")

	def test_uploader_reads_their_own_comment_attachment(self):
		def build(lead):
			self._row(attached_to_doctype="Comment", attached_to_name=self._comment(lead))

		self._assert_stable(build, self.uploader, True, "uploader denied their own file")

	def test_rep_with_no_claim_on_the_lead_is_denied(self):
		def build(lead):
			self._row(attached_to_doctype="Comment", attached_to_name=self._comment(lead))

		self._assert_stable(build, self.outsider, False, "a rep with no claim on the lead got in")

	# -- the flake: one blob, several rows, and the answer must not depend on which row wins --

	def test_the_verdict_does_not_depend_on_which_row_is_found_first(self):
		"""A staged orphan beside a lead-parented row: judging one row answered ~50/50."""
		def build(lead):
			self._row()
			self._row(attached_to_doctype="CRM Lead", attached_to_name=lead)

		self._assert_stable(build, self.colleague, True, "verdict flipped between trials")

	def test_an_orphan_blob_is_readable_by_nobody_but_its_owner(self):
		self._assert_stable(lambda _lead: self._row(), self.colleague, False, "orphan blob leaked")
		self._assert_stable(lambda _lead: self._row(), self.uploader, True, "owner locked out of own orphan")

	# -- guest: every anonymous visitor is the literal "Guest", so an owner match is not proof --

	def test_a_guest_cannot_read_another_guests_intake_upload(self):
		"""Intake uploads are Guest-owned by design — owner-match would open every one of them."""
		def build(lead):
			self._row(owner="Guest")

		self._assert_stable(build, "Guest", False, "a guest read a Guest-owned private file")

	def test_a_guest_cannot_read_an_intake_file_linked_to_a_lead(self):
		def build(lead):
			self._row(attached_to_doctype="CRM Lead", attached_to_name=lead, owner="Guest")

		self._assert_stable(build, "Guest", False, "a guest read a lead's intake attachment")

	def test_the_rep_who_owns_the_lead_still_reads_its_intake_attachment(self):
		def build(lead):
			self._row(owner="Guest")
			self._row(attached_to_doctype="CRM Lead", attached_to_name=lead, owner="Guest")

		self._assert_stable(build, self.colleague, True, "lead owner denied an intake attachment")
