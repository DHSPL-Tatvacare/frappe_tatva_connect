# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""VAPT authorization regression suite — adversarial, organised by enforcement layer.

Written TDD: every test asserts the SECURE end-state, so the suite is RED before the fixes and
GREEN after. Each layer is exercised through its REAL path (frappe.has_permission / get_list /
delete_doc, and the actual override dispatch for native methods — a direct import would bypass
override_whitelisted_methods and give a false green).

  L1  doctype matrix    — junk/no-role users can't read/delete Contact; legit roles preserved
  L2  row-scope (brain) — a peer can't see a Task on a lead they can't see
  L3  method gates      — for EVERY engine-bypassing native method (data-driven):
                            • no-role is denied                         (privilege escalation)
                            • a peer is denied on ANOTHER's record      (adversarial escalation)
                            • an authorized owner is NOT denied         (no regression)
  L4  drift guard       — locked doctypes stay closed to All/Guest
"""
from typing import ClassVar

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access import lockdown
from tatva_connect.automation import seed

JUNK_ROLE = "Purchase Master Manager"  # unused ERPNext role; reproduces "reads all" on Contact
ATTACKER = "vapt-attacker@example.com"  # junk role
NOROLE = "vapt-norole@example.com"      # no roles
OWNER = "vapt-owner@example.com"        # Sales User — owns the data
PEER = "vapt-peer@example.com"          # Sales User — NOT on the data
MANAGER = "vapt-manager@example.com"    # Sales Manager
AGENT = "vapt-agent@example.com"        # Helpdesk Agent — the ONLY role that may touch HD doctypes
STUDENT = "vapt-student@example.com"    # LMS Student assigned to NOTHING — the Aug'26 report's own actor
VICTIM = "vapt-victim@example.com"      # the colleague that report's IDOR names in `member`
LMS_TAG = "vapt-aug26"

# brain switches that scope the doc-specific gates (Lead/Call Log visibility) for the peer test
BRAIN = [
	"Task::CRM Task::visibility",
	"Telephony::CRM Call Log::visibility",
	"Note::FCRM Note::visibility",
	"WhatsApp::WhatsApp Message::visibility",
]


def dispatch(cmd, **kwargs):
	"""Mirror frappe.handler.execute_cmd's override resolution — the REAL HTTP dispatch path,
	so override_whitelisted_methods is honoured (a direct import would not be: false green)."""
	for hook in (frappe.get_hooks("override_whitelisted_methods") or {}).get(cmd, []):
		cmd = hook
		break
	return frappe.call(frappe.get_attr(cmd), **kwargs)


class TestVAPTAuthz(FrappeTestCase):
	def setUp(self):
		seed.sync_catalog()
		# OWNER carries WhatsApp User too: get_whatsapp_messages' native gate requires a WhatsApp
		# capability role, so an "authorized owner" must hold it to be genuinely authorized (not a
		# VAPT change — corrects a latent gap the WhatsApp-role gate exposed).
		for u, roles in [(ATTACKER, [JUNK_ROLE]), (NOROLE, []), (OWNER, ["Sales User", "WhatsApp User"]),
						 (PEER, ["Sales User"]), (MANAGER, ["Sales Manager"]), (AGENT, ["Agent"]),
						 (STUDENT, ["LMS Student"]), (VICTIM, ["LMS Student"])]:
			self._user(u, roles)
		self.contact = self._contact()
		self.lead = self._lead(OWNER)
		self._restrict(self.lead, OWNER)  # only OWNER can see the lead -> PEER genuinely cannot
		self.call_log = self._call_log(self.lead)
		self.task = self._task(self.lead)
		self.deal = self._deal(OWNER)
		self.fx = {"lead": self.lead, "call_log": self.call_log, "deal": self.deal, "contact": self.contact}
		# HD / Comment fixtures are created lazily INSIDE the tests that need them (some HD hooks commit,
		# which would break FrappeTestCase's per-test rollback if run in this shared setUp).

	def tearDown(self):
		frappe.set_user("Administrator")
		for key in BRAIN:
			self._switch(key, 0)

	# data-driven spec: (cmd, kwargs(fixtures), escalation_ref)
	#   escalation_ref = the record a PEER cannot see (Lead/Call Log); None when the gate is
	#   doctype-level (a Sales User legitimately passes it, so only no-role is denied).
	L3: ClassVar = [
		("crm.api.doc.get_assigned_users", lambda f: dict(doctype="CRM Lead", name=f["lead"]), "lead"),
		("crm.api.doc.get_linked_docs_of_document", lambda f: dict(doctype="CRM Lead", docname=f["lead"]), "lead"),
		("crm.api.whatsapp.get_whatsapp_messages", lambda f: dict(reference_doctype="CRM Lead", reference_name=f["lead"]), "lead"),
		("crm.integrations.api.add_task_to_call_log", lambda f: dict(call_sid=f["call_log"], task={"title": "x", "status": "Todo"}), "call_log"),
		("crm.integrations.api.add_note_to_call_log", lambda f: dict(call_sid=f["call_log"], note={"content": "x"}), "call_log"),
		("crm.integrations.api.get_recording_url", lambda f: dict(call_log_name=f["call_log"]), "call_log"),
		("crm.fcrm.doctype.crm_deal.api.get_deal_contacts", lambda f: dict(name=f["deal"]), "deal"),
		("crm.fcrm.doctype.crm_deal.crm_deal.create_deal", lambda f: dict(doc={"lead": f["lead"]}), None),
		("crm.integrations.api.get_contact_by_phone_number", lambda f: dict(phone_number="999"), None),
		("crm.integrations.api.get_contact_lead_or_deal_from_number", lambda f: dict(number="999"), None),
		("crm.integrations.api.set_default_calling_medium", lambda f: dict(medium="Acefone"), None),
		# VAPT Jun'26 — CRM reads a no-App-Access user reached (gated on CRM Lead read; OWNER=Sales User passes).
		("crm.api.assignment_rule.get_assignment_rules_list", lambda f: dict(), None),
		("crm.api.views.get_views", lambda f: dict(doctype="CRM Lead"), None),
	]

	# ---------- L1: doctype matrix (Contact) ----------
	def test_L1_attacker_cannot_read_contacts(self):
		for u in (NOROLE, ATTACKER):
			self.assertFalse(self._as(u, lambda: frappe.has_permission("Contact", "read", self.contact)),
							 f"L1 BREACH: {u} can READ contacts (VAPT #1)")

	def test_L1_attacker_cannot_delete_contact(self):
		with self.assertRaises(frappe.PermissionError, msg="L1 BREACH: junk-role user can DELETE a contact (VAPT #1)"):
			self._as(ATTACKER, lambda: frappe.delete_doc("Contact", self.contact))

	def test_L1_legit_access_preserved(self):
		self.assertTrue(self._as(OWNER, lambda: frappe.has_permission("Contact", "read", self.contact)),
						"L1 REGRESSION: Sales User lost Contact read")
		self.assertFalse(self._as(OWNER, lambda: frappe.has_permission("Contact", "delete", self.contact)),
						 "L1: a rep should not be able to delete a contact")
		self.assertTrue(self._as(MANAGER, lambda: frappe.has_permission("Contact", "delete", self.contact)),
						"L1 REGRESSION: Sales Manager lost Contact delete")

	# ---------- L2: row-scope (brain) ----------
	def test_L2_peer_cannot_see_task_on_hidden_lead(self):
		self._switch("Task::CRM Task::visibility", 1)
		self.assertFalse(self._as(PEER, lambda: frappe.has_permission("CRM Task", "read", self.task)),
						 "L2 BREACH: a peer can see a Task on a lead they cannot see")

	# ---------- L3: no-role denied (privilege escalation) ----------
	def test_L3_no_role_denied(self):
		for cmd, kw, _ in self.L3:
			with self.subTest(cmd=cmd):
				with self.assertRaises(frappe.PermissionError, msg=f"L3 BREACH: no-role reached {cmd}"):
					self._as(NOROLE, lambda cmd=cmd, kw=kw: dispatch(cmd, **kw(self.fx)))

	# ---------- L3: peer denied on another's record (adversarial escalation) ----------
	def test_L3_peer_cannot_act_on_others_record(self):
		for key in BRAIN:
			self._switch(key, 1)
		for cmd, kw, esc in self.L3:
			if not esc:
				continue
			with self.subTest(cmd=cmd):
				with self.assertRaises(frappe.PermissionError, msg=f"L3 ESCALATION: peer reached {cmd} on another's {esc}"):
					self._as(PEER, lambda cmd=cmd, kw=kw: dispatch(cmd, **kw(self.fx)))

	# ---------- L3: authorized NOT denied (no regression) ----------
	def test_L3_authorized_not_denied(self):
		for key in BRAIN:
			self._switch(key, 1)
		for cmd, kw, _ in self.L3:
			with self.subTest(cmd=cmd):
				try:
					self._as(OWNER, lambda cmd=cmd, kw=kw: dispatch(cmd, **kw(self.fx)))
				except frappe.PermissionError as e:
					self.fail(f"L3 REGRESSION: authorized owner denied on {cmd}: {e}")
				except Exception:
					pass  # native errors (DoesNotExist / validation) are fine — not our gate

	# ---------- L4: drift guard ----------
	def test_L4_locked_doctypes_have_no_all_or_guest_crud(self):
		bad = [g for dt in lockdown.LOCKED_MATRIX for g in lockdown.effective_all_guest_grants(dt)]
		self.assertEqual(bad, [], f"L4 BREACH: locked doctype(s) still open to All/Guest: {bad}")

	# ---------- L1: Helpdesk (agent-only internal) ----------
	def test_L1_hd_ticket_agent_only(self):
		hd_ticket = self._hd_ticket()
		for u in (NOROLE, ATTACKER, OWNER):  # OWNER is a Sales User — a CRM rep is NOT a helpdesk agent
			self.assertFalse(self._as(u, lambda: frappe.has_permission("HD Ticket", "read", hd_ticket)),
							 f"L1 BREACH: {u} can READ HD Ticket (VAPT HD 1-4)")
		self.assertTrue(self._as(AGENT, lambda: frappe.has_permission("HD Ticket", "read", hd_ticket)),
						"L1 REGRESSION: Agent lost HD Ticket read")

	def test_L1_hd_article_stats_agent_only(self):
		# get_article_stats bypasses the engine (db.get_value/count) — the wrapper re-gates on HD Article
		# read, which is now agent-only. No-role/CRM-rep denied; agent passes.
		hd_article = self._hd_article()
		for u in (NOROLE, OWNER):
			with self.assertRaises(frappe.PermissionError, msg=f"L1 BREACH: {u} reached get_article_stats"):
				self._as(u, lambda: dispatch("helpdesk.api.article.get_article_stats", article_name=hd_article))
		try:
			self._as(AGENT, lambda: dispatch("helpdesk.api.article.get_article_stats", article_name=hd_article))
		except frappe.PermissionError as e:
			self.fail(f"L1 REGRESSION: Agent denied get_article_stats: {e}")

	# ---------- L1: Comment IDOR (VAPT P1) ----------
	def test_L1_comment_idor_peer_cannot_edit_others(self):
		# A peer rep must NOT edit a comment they don't own (if_owner scope); the owner still can.
		comment = self._comment(OWNER)
		with self.assertRaises(frappe.PermissionError, msg="L1 BREACH: peer edited another user's Comment (VAPT P1)"):
			self._as(PEER, lambda: self._edit_comment(comment, "hacked by peer"))
		try:
			self._as(OWNER, lambda: self._edit_comment(comment, "owner edits own"))
		except frappe.PermissionError as e:
			self.fail(f"L1 REGRESSION: owner cannot edit their OWN comment: {e}")

	# ---------- LMS narrowing (internal, Mode 2) ----------
	def test_lms_non_privileged_clamped_to_what_they_are_in(self):
		# The wrapper still NARROWS (never denies), but to MEMBERSHIP — audit Aug'26: a crafted filter
		# enumerates nothing at all. `published` is not re-injected HERE because the rule already applies it
		# upstream in lms_visibility (assigned AND live); this asserts the clamp, not the second condition.
		from tatva_connect.access import native_guards as ng
		clamped = self._as(NOROLE, lambda: ng._scoped_to({"published": 0}, set()))
		self.assertEqual(clamped["name"], ["in", [""]],
						 "LMS BREACH: a caller who is in nothing was not clamped to nothing")
		self.assertEqual(clamped["published"], 0,
						 "published must not be re-injected as a permission — membership is the rule")

	# ---------- File: profile-picture private-blob BAC ----------
	def test_file_no_role_cannot_reference_private_blob(self):
		# The victim must be GENUINELY private. Attach it to a CRM Lead (patient data, NOT on the public
		# allowlist) — not to User, which the allowlist makes public for avatars, so a User-attached file is
		# public by design and referencing it is no breach.
		lead = self._lead("Administrator")
		victim = frappe.get_doc({"doctype": "File", "file_name": "zvapt_secret.txt",
								 "content": b"secret", "attached_to_doctype": "CRM Lead", "attached_to_name": lead})
		victim.insert(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value("File", victim.name, "is_private"), 1, "victim must be private")
		with self.assertRaises(frappe.PermissionError, msg="FILE BREACH: no-role forged a File referencing another's private blob"):
			self._as(NOROLE, lambda: frappe.get_doc({
				"doctype": "File", "file_url": victim.file_url, "is_private": 1,
				"attached_to_doctype": "CRM Lead", "attached_to_name": lead}).insert())

	# ---------- fixtures ----------
	def _user(self, email, roles):
		if frappe.db.exists("User", email):
			frappe.delete_doc("User", email, force=True, ignore_permissions=True)
		frappe.get_doc({
			"doctype": "User", "email": email, "first_name": email.split("@")[0],
			"send_welcome_email": 0, "roles": [{"role": r} for r in roles],
		}).insert(ignore_permissions=True)

	def _contact(self):
		c = frappe.get_doc({"doctype": "Contact", "first_name": "ZVAPT-CONTACT"})
		c.insert(ignore_permissions=True)
		return c.name

	def _lead(self, owner):
		d = frappe.get_doc({"doctype": "CRM Lead", "first_name": "ZVAPT-LEAD", "status": "New", "lead_owner": owner})
		d.insert(ignore_permissions=True)
		d.db_set("owner", owner)
		return d.name

	def _restrict(self, lead, user):
		frappe.get_doc({"doctype": "User Permission", "user": user, "allow": "CRM Lead", "for_value": lead}).insert(ignore_permissions=True)

	def _call_log(self, lead):
		d = frappe.new_doc("CRM Call Log")
		d.id = frappe.generate_hash(length=12)
		d.type, d.status = "Incoming", "Completed"
		setattr(d, "from", "111")
		d.to = "222"
		d.reference_doctype, d.reference_docname = "CRM Lead", lead
		d.insert(ignore_permissions=True)
		return d.name

	def _task(self, lead):
		d = frappe.new_doc("CRM Task")
		d.title, d.status = "ZVAPT-TASK", "Todo"
		d.reference_doctype, d.reference_docname = "CRM Lead", lead
		d.insert(ignore_permissions=True)
		return d.name

	def _deal(self, owner):
		# an OPEN status — terminal ones (Lost/Won) demand extra fields (lost reason, etc.)
		statuses = frappe.get_all("CRM Deal Status", filters={"type": "Open"}, limit=1, pluck="name") \
			or frappe.get_all("CRM Deal Status", order_by="position", limit=1, pluck="name")
		d = frappe.get_doc({"doctype": "CRM Deal", "status": statuses[0] if statuses else None})
		d.insert(ignore_permissions=True)
		# crm scopes deals by ASSIGNMENT (not the owner field) — assign so `owner` can see it,
		# while a peer who isn't assigned cannot.
		from frappe.desk.form.assign_to import add as assign_to
		assign_to({"doctype": "CRM Deal", "name": d.name, "assign_to": [owner]})
		return d.name

	def _hd_ticket(self):
		d = frappe.get_doc({"doctype": "HD Ticket", "subject": "ZVAPT-TICKET"})
		d.insert(ignore_permissions=True)
		return d.name

	def _hd_article(self):
		d = frappe.get_doc({"doctype": "HD Article", "title": "ZVAPT-ARTICLE"})
		d.insert(ignore_permissions=True)
		return d.name

	def _comment(self, owner):
		c = frappe.get_doc({"doctype": "Comment", "comment_type": "Comment",
							"reference_doctype": "CRM Lead", "reference_name": self.lead, "content": "owned by OWNER"})
		c.insert(ignore_permissions=True)
		c.db_set("owner", owner)  # stamp ownership so if_owner scoping applies
		return c.name

	def _edit_comment(self, name, content):
		doc = frappe.get_doc("Comment", name)  # engine-checked load+save path (the VAPT set_value route)
		doc.content = content
		doc.save()

	def _switch(self, key, on):
		frappe.db.set_value("CRM Tatva Automation", key, "enabled", 1 if on else 0)

	def _as(self, user, fn):
		frappe.set_user(user)
		try:
			return fn()
		finally:
			frappe.set_user("Administrator")

	# --- L5: audit Aug'26 — the report's own nine requests, replayed ------------------------------

	def _lms_fixture(self):
		"""The smallest world the nine vectors need. Purged first: lms commits inside its enrolment path.

		EVERYTHING IS PUBLISHED, deliberately. An unpublished fixture is refused for two reasons at once —
		not assigned AND not live — so it cannot tell which condition did the work, and it never proves the
		harder half: published content still reaches nobody it was not assigned to. The report's own targets
		were live courses, so this is also the more faithful replay.
		"""
		like = ["like", f"{LMS_TAG}-%"]
		for doctype, filters in (("LMS Enrollment", {"member": ["in", [STUDENT, VICTIM]]}),
								 ("LMS Quiz", {"name": like}), ("LMS Program", {"name": like}),
								 ("LMS Batch", {"name": like}), ("LMS Course", {"name": like})):
			for name in frappe.get_all(doctype, filters=filters, pluck="name"):
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True, ignore_missing=True)
		course = frappe.get_doc({
			"doctype": "LMS Course", "title": f"{LMS_TAG}-course", "description": "d",
			"short_introduction": "d", "published": 1, "instructors": [{"instructor": MANAGER}],
		}).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture seeding
		question = frappe.get_doc({
			"doctype": "LMS Question", "question": "Pick A", "type": "Choices",
			"option_1": "A", "is_correct_1": 1, "option_2": "B", "is_correct_2": 0,
		}).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture seeding
		quiz = frappe.get_doc({
			"doctype": "LMS Quiz", "title": f"{LMS_TAG}-quiz", "course": course.name, "show_answers": 1,
			"passing_percentage": 50, "questions": [{"question": question.name, "marks": 1}],
		}).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture seeding
		batch = frappe.get_doc({
			"doctype": "LMS Batch", "title": f"{LMS_TAG}-batch", "description": "d", "batch_details": "d",
			"start_date": "2026-01-01", "end_date": "2026-12-31", "start_time": "09:00:00",
			"end_time": "17:00:00", "timezone": "Asia/Kolkata", "published": 1,
			"instructors": [{"instructor": MANAGER}], "courses": [{"course": course.name}],
		}).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture seeding
		program = frappe.get_doc({
			"doctype": "LMS Program", "title": f"{LMS_TAG}-program", "published": 1,
			"program_courses": [{"course": course.name}],
			"program_members": [{"member": VICTIM, "full_name": "A Colleague"}],
		}).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture seeding
		return course, quiz, batch, program

	def test_L5_aug26_lms_report_vectors_are_all_refused(self):
		"""Each entry is the request as the Aug'26 report filed it, against our own fixture ids."""
		from frappe.client import get as client_get
		from frappe.client import get_count as client_get_count
		from frappe.client import get_list as client_get_list
		from frappe.client import insert as client_insert

		course, quiz, batch, program = self._lms_fixture()
		empty = [
			("F1 Batch Course of a batch the student is not in", lambda: client_get_list(
				"Batch Course", fields=["name", "course", "title", "evaluator"],
				filters={"parent": batch.name, "parenttype": "LMS Batch"}, parent="LMS Batch")),
			("F4 LMS Program Member of a program the student is not in", lambda: client_get_list(
				"LMS Program Member", fields=["member", "full_name", "progress", "name"],
				filters={"parent": program.name, "parenttype": "LMS Program",
						 "parentfield": "program_members"}, parent="LMS Program")),
			("F7 LMS Program Course of a program the student is not in", lambda: client_get_list(
				"LMS Program Course", fields=["course", "course_title", "name", "idx"],
				filters={"parent": program.name, "parenttype": "LMS Program",
						 "parentfield": "program_courses"}, parent="LMS Program")),
			("F9 quiz count", lambda: client_get_count("LMS Quiz", filters={"name": quiz.name})),
		]
		refused = [
			("F2 get_reviews", lambda: dispatch("lms.lms.utils.get_reviews", course=course.name)),
			("F3 get_course_outline", lambda: dispatch(
				"lms.lms.utils.get_course_outline", course=course.name, progress=True)),
			("F5 client.get of an arbitrary quiz", lambda: client_get("LMS Quiz", quiz.name)),
			("F6 check_answer without taking the quiz", lambda: dispatch(
				"lms.lms.doctype.lms_quiz.lms_quiz.check_answer", quiz=quiz.name,
				question=quiz.questions[0].question, question_type="Choices", answers='["1"]')),
			("F8 enrol another user", lambda: client_insert({
				"doctype": "LMS Enrollment", "course": course.name, "member": VICTIM,
				"payment": None, "purchased_certificate": False})),
		]
		for label, call in empty:
			with self.subTest(vector=label):
				self.assertFalse(self._as(STUDENT, call), f"AUG26 BREACH: {label} returned rows")
		for label, call in refused:
			with self.subTest(vector=label):
				with self.assertRaises(frappe.PermissionError, msg=f"AUG26 BREACH: {label} was answered"):
					self._as(STUDENT, call)
		self.assertFalse(
			frappe.db.exists("LMS Enrollment", {"member": VICTIM, "course": course.name}),
			"AUG26 BREACH: the named victim gained an enrolment",
		)
