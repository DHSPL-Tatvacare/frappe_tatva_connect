# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""LMS ownership-spoof (mass-assignment IDOR) — the regression wall for the `member` write class.

THE DISEASE. `frappe.client.insert` accepts a client-supplied `member`, and several LMS doctypes ship a
`pass` controller that never checks it — so a student creates a record attributed to any victim. Upstream
cures this per-doctype and forgot four; `access.lms_member_guard` cures those four in one handler.

TWO LOCKS, same shape as test_bypass_writes.py:

PART A — drift lock (`test_every_student_creatable_member_doctype_is_guarded`). A pure-schema audit: every
LMS parent doctype with a `member`→User field that a non-privileged role can CREATE must be covered by a
reviewed mechanism — our guard (wired in hooks doc_events), or an UPSTREAM_SAFE entry (upstream forces
`member`, or the create is role-gated). A NEW such doctype that is neither FAILS the build. This is how the
disease cannot recur without a code change — no runtime scanning, no defensive coding.

PART B — behavioural vectors (tuples). As a pure LMS Student: spoofing `member` to another user RAISES
PermissionError; creating one's OWN record SUCCEEDS (the authorised path is not broken). As a privileged
role: creating on behalf of a member SUCCEEDS. LMS Lesson Note is the representative guarded doctype
(simplest valid FK tuple: a lesson).
"""

import frappe

from tatva_connect.tests.authz.base import AuthzTestCase, set_user

# Upstream already forces `member` from the session, or role-gates the create — reviewed 2026-08-08.
# Each entry is a fact about upstream code, re-checked when the drift lock names a newcomer.
UPSTREAM_SAFE = {
	"LMS Assignment Submission": "controller forces member = session.user (throws first)",
	"LMS Certificate Request": "controller forces member = session.user (throws first)",
	"LMS Enrollment": "cured by TatvaLMSEnrollment (own class override, runs before upstream before_insert)",
	"LMS Badge Assignment": "validate_owner requires Admin to assign to another user",
	"LMS Batch Enrollment": "validate_owner requires Moderator/Batch Evaluator; self-enrol path only",
}

# Roles a non-privileged LMS user may hold — a create grant to any of these is a student-reachable surface.
_STUDENT_ROLES = {"LMS Student", "All", "Guest"}


def _guarded_by_our_hook():
	"""Doctypes our member guard is wired to, read straight from the live hooks — never a second copy."""
	out = set()
	for doctype, events in (frappe.get_hooks("doc_events") or {}).items():
		for handlers in events.values():
			handlers = handlers if isinstance(handlers, list) else [handlers]
			if any("lms_member_guard.enforce_member" in h for h in handlers):
				out.add(doctype)
	return out


def _student_creatable_member_doctypes():
	"""LMS parent doctypes with a `member`→User field a non-privileged role can create — derived, not typed."""
	found = []
	lms_dts = frappe.get_all(
		"DocType", filters={"module": ["like", "%LMS%"], "istable": 0, "issingle": 0}, pluck="name"
	)
	for dt in lms_dts:
		meta = frappe.get_meta(dt)
		if not any(f.fieldname == "member" and f.fieldtype == "Link" and f.options == "User" for f in meta.fields):
			continue
		src = "Custom DocPerm" if frappe.db.exists("Custom DocPerm", {"parent": dt}) else "DocPerm"
		creators = {
			r.role for r in frappe.get_all(src, filters={"parent": dt, "create": 1}, fields=["role"])
		}
		if creators & _STUDENT_ROLES:
			found.append(dt)
	return found


class TestLMSMemberIDOR(AuthzTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.attacker = "vapt-idor-student@example.com"
		cls.victim = "vapt-idor-victim@example.com"
		for email, roles in ((cls.attacker, ["LMS Student"]), (cls.victim, ["LMS Student"])):
			if not frappe.db.exists("User", email):
				u = frappe.new_doc("User")
				u.email = email
				u.first_name = email.split("@")[0]
				u.send_welcome_email = 0
				for r in roles:
					u.append("roles", {"role": r})
				u.insert(ignore_permissions=True)
		# a lesson to hang a Lesson Note on — any published course's first lesson
		cls.lesson = frappe.get_all("Course Lesson", pluck="name", limit_page_length=1)
		cls.lesson = cls.lesson[0] if cls.lesson else None

	def _make_note(self, member):
		doc = frappe.get_doc({
			"doctype": "LMS Lesson Note", "member": member,
			"lesson": self.lesson, "color": "Red", "note": "idor probe",
		})
		doc.insert()
		return doc

	def test_every_student_creatable_member_doctype_is_guarded(self):
		"""PART A drift lock — a student-creatable `member` doctype must be guarded or reviewed-safe."""
		guarded = _guarded_by_our_hook()
		unguarded = [
			dt for dt in _student_creatable_member_doctypes()
			if dt not in guarded and dt not in UPSTREAM_SAFE
		]
		self.assertEqual(
			unguarded, [],
			"New student-creatable LMS doctype(s) with an unguarded `member` field: {0}. "
			"Wire access.lms_member_guard.enforce_member in hooks doc_events, or add an "
			"UPSTREAM_SAFE entry citing why upstream already enforces it.".format(unguarded),
		)

	def test_student_cannot_spoof_member(self):
		"""PART B vector — a student setting `member` to another user is refused."""
		if not self.lesson:
			self.skipTest("no Course Lesson seeded")
		with set_user(self.attacker):
			with self.assertRaises(frappe.PermissionError):
				self._make_note(self.victim)

	def test_student_can_create_own_note(self):
		"""PART B vector — the authorised path is intact: a student creates their OWN note."""
		if not self.lesson:
			self.skipTest("no Course Lesson seeded")
		with set_user(self.attacker):
			doc = self._make_note(self.attacker)
			self.assertEqual(doc.member, self.attacker)

	def test_student_blank_member_defaults_to_self(self):
		"""PART B vector — omitting `member` pins it to the caller, never left blank/forgeable."""
		if not self.lesson:
			self.skipTest("no Course Lesson seeded")
		with set_user(self.attacker):
			doc = frappe.get_doc({
				"doctype": "LMS Lesson Note", "lesson": self.lesson, "color": "Blue", "note": "own",
			})
			doc.insert()
			self.assertEqual(doc.member, self.attacker)

	def test_privileged_can_create_on_behalf(self):
		"""PART B vector — a Moderator may still file a record for another member (not broken)."""
		if not self.lesson:
			self.skipTest("no Course Lesson seeded")
		with set_user("Administrator"):
			doc = self._make_note(self.victim)
			self.assertEqual(doc.member, self.victim)
