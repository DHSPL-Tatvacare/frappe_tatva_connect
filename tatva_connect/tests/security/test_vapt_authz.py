# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""VAPT authorization regressions by layer: doctype matrix, row scope, method gates and LMS vectors.
Each gate is driven through its real dispatch path, since a direct import skips the override."""
from typing import ClassVar

import frappe
from frappe.tests import IntegrationTestCase

from tatva_connect.automation import seed
from tatva_connect.tests.authz.base import dispatch

JUNK_ROLE = "Purchase Master Manager"  # unused ERPNext role; reproduces "reads all" on Contact
ATTACKER = "vapt-attacker@example.com"  # junk role
NOROLE = "vapt-norole@example.com"      # no roles
OWNER = "vapt-owner@example.com"        # Sales User — owns the data
PEER = "vapt-peer@example.com"          # Sales User — NOT on the data
MANAGER = "vapt-manager@example.com"    # Sales Manager
AGENT = "vapt-agent@example.com"        # Helpdesk Agent — the ONLY role that may touch HD doctypes
STUDENT = "vapt-student@example.com"    # LMS Student assigned to nothing
VICTIM = "vapt-victim@example.com"      # the colleague an IDOR names in `member`
LMS_TAG = "vapt-aug26"

# An authorized call to this method ends in native's own answer, reachable only past our guard.
NATIVE_ANSWER = {"crm.integrations.api.get_recording_url": "Recording URL not found"}

# Visibility switches that scope the record-level gates for the peer test.
BRAIN = [
	"Task::CRM Task::visibility",
	"Telephony::CRM Call Log::visibility",
	"Note::FCRM Note::visibility",
	"WhatsApp::WhatsApp Message::visibility",
]


class TestVAPTAuthz(IntegrationTestCase):
	def setUp(self):
		seed.sync_catalog()
		# OWNER also holds WhatsApp User because native get_whatsapp_messages requires that role.
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
		# HD and Comment fixtures are built inside their tests because some HD hooks commit.

	def tearDown(self):
		frappe.set_user("Administrator")
		for key in BRAIN:
			self._switch(key, 0)

	# (cmd, kwargs, record a peer cannot see); None marks a doctype-level gate a Sales User passes.
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
		# CRM reads gated on CRM Lead read, which OWNER as a Sales User passes.
		("crm.api.assignment_rule.get_assignment_rules_list", lambda f: dict(), None),
		("crm.api.views.get_views", lambda f: dict(doctype="CRM Lead"), None),
	]

	# ---------- L1: doctype matrix (Contact) ----------
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
				call = lambda cmd=cmd, kw=kw: dispatch(cmd, **kw(self.fx))  # noqa: E731
				if cmd in NATIVE_ANSWER:
					with self.assertRaisesRegex(frappe.DoesNotExistError, NATIVE_ANSWER[cmd]):
						self._as(OWNER, call)
					continue
				try:
					self._as(OWNER, call)
				except Exception as e:
					self.fail(f"L3 REGRESSION: authorized owner could not complete {cmd}: {e!r}")

	# ---------- L1: Helpdesk (agent-only internal) ----------
	def test_L1_hd_ticket_agent_only(self):
		hd_ticket = self._hd_ticket()
		for u in (NOROLE, ATTACKER, OWNER):  # a CRM rep is not a helpdesk agent
			self.assertFalse(self._as(u, lambda: frappe.has_permission("HD Ticket", "read", hd_ticket)),
							 f"L1 BREACH: {u} can READ HD Ticket (VAPT HD 1-4)")
		self.assertTrue(self._as(AGENT, lambda: frappe.has_permission("HD Ticket", "read", hd_ticket)),
						"L1 REGRESSION: Agent lost HD Ticket read")

	def test_L1_hd_article_stats_agent_only(self):
		# get_article_stats bypasses the engine, so our wrapper re-gates it on agent-only HD Article read.
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
		# A peer cannot edit a comment they do not own; the owner still can.
		comment = self._comment(OWNER)
		with self.assertRaises(frappe.PermissionError, msg="L1 BREACH: peer edited another user's Comment (VAPT P1)"):
			self._as(PEER, lambda: self._edit_comment(comment, "hacked by peer"))
		try:
			self._as(OWNER, lambda: self._edit_comment(comment, "owner edits own"))
		except frappe.PermissionError as e:
			self.fail(f"L1 REGRESSION: owner cannot edit their OWN comment: {e}")

	# ---------- File: profile-picture private-blob BAC ----------
	def test_file_no_role_cannot_reference_private_blob(self):
		# Attach to a CRM Lead, not a User, whose files are public by design for avatars.
		lead = self._lead("Administrator")
		victim = frappe.get_doc({"doctype": "File", "file_name": "zvapt_secret.txt",
								 "content": b"secret", "attached_to_doctype": "CRM Lead", "attached_to_name": lead})
		victim.insert(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value("File", victim.name, "is_private"), 1, "victim must be private")
		with self.assertRaises(frappe.PermissionError, msg="FILE BREACH: no-role forged a File referencing another's private blob"):
			self._as(NOROLE, lambda: frappe.get_doc({
				"doctype": "File", "file_url": victim.file_url, "is_private": 1,
				"attached_to_doctype": "CRM Lead", "attached_to_name": lead}).insert())

	# ---------- endpoint hardening ----------
	def test_add_task_to_call_log_cannot_write_a_task_the_caller_cannot_write(self):
		for key in BRAIN:
			self._switch(key, 1)
		peer_lead = self._lead(PEER)
		self._restrict(peer_lead, PEER)
		peer_log = self._call_log(peer_lead)
		with self.assertRaises(frappe.PermissionError, msg="BREACH: a call-log reader rewrote another lead's task"):
			self._as(PEER, lambda: dispatch("crm.integrations.api.add_task_to_call_log", call_sid=peer_log,
											task={"name": self.task, "title": "hijacked", "status": "Done"}))
		self.assertEqual(frappe.db.get_value("CRM Task", self.task, "status"), "Todo")

	def test_webhook_urls_withhold_the_token_from_a_read_only_role(self):
		reader = "vapt-am-reader@example.com"
		self._user(reader, ["Sales User", "Automation Manager"])
		acct = frappe.get_doc({"doctype": "CRM Telephony Account", "account_name": "ZVAPT-ACCT", "provider": "Acefone",
							   "caller_id": "000", "api_token": "x", "webhook_token": "zvapt-secret-token"})
		acct.insert(ignore_permissions=True)
		self.assertTrue(self._as(reader, lambda: frappe.has_permission("CRM Telephony Account", "read", acct.name)))
		from tatva_connect.webhooks.urls import get_account_webhook_urls
		res = self._as(reader, lambda: get_account_webhook_urls("CRM Telephony Account", acct.name))
		self.assertNotIn("zvapt-secret-token", frappe.as_json(res), "BREACH: a read-only role received the webhook secret")
		self.assertIn("zvapt-secret-token", frappe.as_json(get_account_webhook_urls("CRM Telephony Account", acct.name)),
					  "REGRESSION: the account's editor lost the webhook URL")

	def test_restate_copy_is_not_reachable_over_http(self):
		from tatva_connect.dashboard import seed as dashboard_seed
		self.assertNotIn(dashboard_seed.restate_copy, frappe.whitelisted)

	def test_bulk_update_override_keeps_natives_post_only(self):
		from tatva_connect.tasks import tasks
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[tasks.submit_cancel_or_update_docs], ["POST"])

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
		# An open status, since Lost or Won demand extra fields.
		statuses = frappe.get_all("CRM Deal Status", filters={"type": "Open"}, limit=1, pluck="name") \
			or frappe.get_all("CRM Deal Status", order_by="position", limit=1, pluck="name")
		d = frappe.get_doc({"doctype": "CRM Deal", "status": statuses[0] if statuses else None})
		d.insert(ignore_permissions=True)
		# crm scopes deals by assignment, not the owner field, so assign the owner and not the peer.
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
		doc = frappe.get_doc("Comment", name)  # the engine-checked load and save path
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

	# --- L5: the LMS report's nine requests, replayed ---

	def _lms_fixture(self):
		"""Everything is published, so a refusal proves the student was not assigned, not that it was unpublished.
		Old rows are purged first because lms commits inside its enrolment path."""
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
		"""A student assigned to nothing gets no rows and no answers from any of the reported requests."""
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
