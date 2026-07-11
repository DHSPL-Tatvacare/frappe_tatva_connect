# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The document-review-and-approval flow, end to end, on the REAL machinery it was assembled from —
no parallel engine (see docs/plans/2026-07-11-document-review-approval-flow-design.md).

There is no new doctype and no new dispatcher: review is a `CRM Task Type` (Document Review), the
File is an automation SUBJECT, and the verdict rides the native activity writer (`compute_activity`)
and the native `Webhook` verb. So these tests drive exactly those brains — the wildcard router
(`router.run_for_event`) for the on-upload fire, `save_activity`/`compute_activity` for the rep's
disposition, the CRM Task `on_update` mirror for the badge, and `dispatcher.run_effects` for the
Call-Webhook payload routing — with the Frappe engine itself as the oracle (real Files, real Tasks,
real Run Logs, a real spy on `frappe.enqueue`), never a hardcoded verdict.

The Document Review task type and the two rules are built in setUp, NEVER seeded as fixtures (rules
are user-built and ship dormant — constitution). The `CRM Tatva Automation` switches are flipped ON
inside the test transaction (they roll back with it), so nothing is left enabled globally.

Firing posture: the production on-upload path is a File insert -> wildcard `after_insert` ->
enqueue-after-commit -> `run_for_event`. `enqueue_after_commit` never runs inside a FrappeTestCase
transaction (there is no commit), so — exactly as `test_deleted_event` calls `run_for_delete`
directly — we call `router.run_for_event("File", <name>, "Created", {})` ourselves after the real
insert. That IS the after-commit job: subject resolution, grain match, criteria, and the Create Task
effect all run through the same code production runs, deterministically and once.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.documents.test_document_review
"""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import dispatcher, router, versions
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist
from tatva_connect.tests.automation import field_allowlist

_RULE_DT = "CRM Automation Rule"
_RUN_LOG = "CRM Automation Run Log"
_VERSION_DT = versions.DOCTYPE
_TASK_TYPE = "Document Review"
_KILL_SWITCH = "Task::Automation::rules"
_MIRROR_SWITCH = "Task::Review::mirror"

_GRAIN_A = GRAINS[0]  # GoodFlip Care::Anaya::Nivolumab — the grain that HAS a review rule
_GRAIN_B = GRAINS[1]  # GoodFlip Care::Anaya::Tukavo   — same vertical+group, DIFFERENT program
_AXES_A = (_GRAIN_A["vertical"], _GRAIN_A["group"], _GRAIN_A["program"])


# -- shared builders ---------------------------------------------------------


def _dr_type_name(grain):
	return "{}::{}::{}::{}".format(grain["vertical"], grain["group"], grain["program"], _TASK_TYPE)


def _make_doc_review_type(grain):
	"""The Document Review CRM Task Type for a grain — is_logged_complete=1, with the three schema
	rows the spec §5 names: `document` (Attach, holds the file), `approval_status` (Select ->
	custom_outcome, a promoted column), `reason` (Small Text, required only when Rejected). Built
	here, never a fixture."""
	name = _dr_type_name(grain)
	if frappe.db.exists("CRM Task Type", name):
		return name
	frappe.get_doc({
		"doctype": "CRM Task Type", "type_name": _TASK_TYPE,
		"vertical": grain["vertical"], "group": grain["group"], "program": grain["program"],
		"is_logged_complete": 1,
		"schema": [
			{"label": "Document", "fieldname": "document", "fieldtype": "Attach", "reqd": 1, "target": ""},
			{"label": "Approval Status", "fieldname": "approval_status", "fieldtype": "Select",
			 "options": "Approved\nRejected", "reqd": 1, "target": "custom_outcome"},
			{"label": "Reason", "fieldname": "reason", "fieldtype": "Small Text", "reqd": 1,
			 "depends_on": "eval:doc.approval_status=='Rejected'", "target": ""},
		],
	}).insert(ignore_permissions=True)
	return name


def _make_lead(grain, **extra):
	payload = {
		"doctype": "CRM Lead", "first_name": "DocReview", "lead_name": "DocReview Probe", "status": "New",
		"custom_vertical": grain["vertical"], "custom_group": grain["group"],
		"custom_current_program": grain["program"], "lead_owner": "Administrator",
	}
	payload.update(extra)
	return frappe.get_doc(payload).insert(ignore_permissions=True)


def _make_file(lead, source, file_type, external_id, attached_to_doctype="CRM Lead", attached_to_name=None):
	"""A real private File on the lead (the patient upload), stamped with the ingestion `custom_source`
	and `custom_file_type` a rule scopes on, plus the caller's own `external_id` label
	(custom_external_id — a label, never an address; the file is addressed by `name`)."""
	return frappe.get_doc({
		"doctype": "File", "file_name": f"rev-{external_id}.txt", "content": "review-doc-bytes",
		"is_private": 1,
		"attached_to_doctype": attached_to_doctype, "attached_to_name": attached_to_name or lead,
		"custom_source": source, "custom_file_type": file_type, "custom_external_id": external_id,
	}).insert(ignore_permissions=True)


def _make_create_rule(name, grain, source, file_types):
	"""On File Created, if custom_source is <source> and custom_file_type is one of <file_types>,
	Create Task (Document Review). Grain-scoped — the rule's presence per grain is the switch."""
	return frappe.get_doc({
		"doctype": _RULE_DT, "rule_name": name, "enabled": 1,
		"on_doctype": "File", "event": "Created",
		"vertical": grain["vertical"], "group": grain["group"], "program": grain["program"],
		"criteria": [
			{"field": "custom_source", "operator": "is", "value": source},
			{"field": "custom_file_type", "operator": "is one of", "value": file_types},
		],
		"actions": [{
			"action_type": "Create Task", "task_type": _dr_type_name(grain),
			"due_mode": "From Context", "due_from": None,
		}],
	}).insert(ignore_permissions=True)


def _fire_created(doctype, docname):
	"""Drive the exact after-commit job production's wildcard `after_insert` enqueues — subject
	resolution -> grain match -> criteria -> effect lane — synchronously and once."""
	router.run_for_event(doctype, docname, "Created", {})


def _review_tasks(lead, grain):
	return frappe.get_all(
		"CRM Task",
		filters={"reference_doctype": "CRM Lead", "reference_docname": lead, "custom_task_type": _dr_type_name(grain)},
		fields=["name", "assigned_to", "status", "custom_activity_payload"],
	)


def _cleanup_rules(prefix):
	like = ("like", f"{prefix}%")
	frappe.db.delete("CRM Automation Action", {"parent": like})
	frappe.db.delete("CRM Automation Criterion", {"parent": like})
	frappe.db.delete(_RUN_LOG, {"rule": like})
	frappe.db.delete(_VERSION_DT, {"rule": like})
	frappe.db.delete(_RULE_DT, {"rule_name": like})


def _cleanup_lead(lead):
	frappe.db.delete("File", {"attached_to_doctype": "CRM Lead", "attached_to_name": lead})
	frappe.db.delete("CRM Task", {"reference_doctype": "CRM Lead", "reference_docname": lead})
	frappe.db.delete("CRM Lead", {"name": lead})


# -- 1) File becomes a subject + the trigger matrix + pin + idempotency ------


class TestFileSubjectAndTriggerMatrix(FrappeTestCase):
	"""Creating a File on a lead is what fires the review. A rule scoped to (grain A, source Partner
	API, type in {Prescription, Lab Report}) raises ONE Document Review task, pins the file onto it,
	and stamps the File Pending + linked — and fires for nothing outside that scope."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		frappe.db.set_value("CRM Tatva Automation", _KILL_SWITCH, "enabled", 1)
		# custom_source / custom_file_type must be READ-allowlisted to be a legal criterion field
		# (the builder vocabulary IS the read allowlist — describe._criterion_fields). Read, not watch:
		# a Created rule tests these, it never fires on their change.
		field_allowlist.seed_readable("File", "custom_source")
		field_allowlist.seed_readable("File", "custom_file_type")
		cls.tt = _make_doc_review_type(_GRAIN_A)
		cls.rule = _make_create_rule("DR-create", _GRAIN_A, "Partner API", "Prescription\nLab Report")

	@classmethod
	def tearDownClass(cls):
		_cleanup_rules("DR-create")
		frappe.db.delete("CRM Task Type", {"name": cls.tt})
		field_allowlist.clear("File")

	def setUp(self):
		self.lead = _make_lead(_GRAIN_A)

	def tearDown(self):
		_cleanup_lead(self.lead.name)

	def test_file_in_grain_with_rule_creates_and_pins_review_task(self):
		f = _make_file(self.lead.name, "Partner API", "Prescription", "doc-1")
		_fire_created("File", f.name)

		tasks = _review_tasks(self.lead.name, _GRAIN_A)
		self.assertEqual(len(tasks), 1, "a matching upload must raise exactly one Document Review task")
		task = tasks[0]

		# Assigned to the lead owner — a File trigger carries no assignee, so the review must NOT land
		# unassigned (on no rep's list). It is pinned onto the File and the File shows Pending.
		self.assertEqual(task.assigned_to, "Administrator", "the review task was left unassigned")
		f.reload()
		self.assertEqual(f.custom_review_status, "Pending", "the File was not stamped Pending on upload")
		# CRM Task autonames as an integer; the File Link column stores it as a string — compare as text.
		self.assertEqual(str(f.custom_review_task), str(task.name), "the File was not linked to its review task")
		payload = frappe.parse_json(task.custom_activity_payload) if (task.custom_activity_payload or "").strip() else {}
		self.assertEqual(payload.get("document"), f.file_url, "the exact document was not pinned onto the review task")

	def test_file_type_outside_the_set_creates_nothing(self):
		f = _make_file(self.lead.name, "Partner API", "Other", "doc-2")
		_fire_created("File", f.name)
		self.assertEqual(_review_tasks(self.lead.name, _GRAIN_A), [], "an out-of-set file_type raised a review task")
		f.reload()
		self.assertFalse(f.custom_review_status, "a non-reviewable file was stamped with a review status")

	def test_source_not_matching_creates_nothing(self):
		# A rep's Desk upload of the same document type must NOT enter review (source excludes it).
		f = _make_file(self.lead.name, "Desk", "Prescription", "doc-3")
		_fire_created("File", f.name)
		self.assertEqual(_review_tasks(self.lead.name, _GRAIN_A), [], "a Desk-sourced file entered review")

	def test_file_attached_to_non_lead_resolves_no_lead_and_fires_nothing(self):
		# A File attached to a CRM Task (not a CRM Lead) resolves to no lead — the dynamic-link guard
		# in subjects.resolve_lead_name is fail-closed, so no rule can fire.
		host = frappe.get_doc({
			"doctype": "CRM Task", "title": "host task", "reference_doctype": "CRM Lead",
			"reference_docname": self.lead.name, "status": "Todo",
		}).insert(ignore_permissions=True)
		f = _make_file(self.lead.name, "Partner API", "Prescription", "doc-4",
					   attached_to_doctype="CRM Task", attached_to_name=host.name)
		_fire_created("File", f.name)
		self.assertEqual(_review_tasks(self.lead.name, _GRAIN_A), [], "a File on a non-Lead resolved to a lead and fired")

	def test_second_fire_on_the_same_file_is_idempotent(self):
		# Same document (same external_id -> same File row) must never raise a second review task: the
		# File's custom_review_task back-reference is the idempotency key.
		f = _make_file(self.lead.name, "Partner API", "Lab Report", "doc-5")
		_fire_created("File", f.name)
		_fire_created("File", f.name)
		self.assertEqual(len(_review_tasks(self.lead.name, _GRAIN_A)), 1, "a re-fire on the same File raised a duplicate review task")

	def test_grain_a_rule_does_not_fire_on_a_grain_b_lead(self):
		# The review rule lives only in grain A. An identical upload on a grain B lead matches no rule.
		lead_b = _make_lead(_GRAIN_B)
		try:
			f = _make_file(lead_b.name, "Partner API", "Prescription", "doc-6")
			_fire_created("File", f.name)
			self.assertEqual(_review_tasks(lead_b.name, _GRAIN_A), [], "a grain-A rule fired on a grain-B lead")
			self.assertEqual(_review_tasks(lead_b.name, _GRAIN_B), [], "a review task appeared in grain B, which has no rule")
		finally:
			_cleanup_lead(lead_b.name)

	def test_lead_in_grain_with_no_rule_creates_nothing(self):
		# Grain B has no review rule at all — an upload there behaves exactly as today (no task).
		lead_b = _make_lead(_GRAIN_B)
		try:
			f = _make_file(lead_b.name, "Partner API", "Prescription", "doc-7")
			_fire_created("File", f.name)
			self.assertFalse(
				frappe.get_all("CRM Task", filters={"reference_docname": lead_b.name, "custom_task_type": _dr_type_name(_GRAIN_B)}),
				"a task was raised in a grain with no review rule",
			)
			f.reload()
			self.assertFalse(f.custom_review_status, "a file in a rule-less grain was stamped for review")
		finally:
			_cleanup_lead(lead_b.name)


# -- 2) Disposition: the rep approves/rejects natively, the File mirrors ------


class TestDisposition(FrappeTestCase):
	"""The rep's verdict rides the NATIVE activity writer (`save_activity`/`compute_activity`), which
	writes custom_outcome onto the task; the CRM Task on_update mirror then copies it onto the File
	(the badge). Rejected without a reason is blocked by the schema's required-when rule — the engine
	never touches this step."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		frappe.db.set_value("CRM Tatva Automation", _KILL_SWITCH, "enabled", 1)
		frappe.db.set_value("CRM Tatva Automation", _MIRROR_SWITCH, "enabled", 1)
		field_allowlist.seed_readable("File", "custom_source")
		field_allowlist.seed_readable("File", "custom_file_type")
		cls.tt = _make_doc_review_type(_GRAIN_A)
		cls.rule = _make_create_rule("DR-disp", _GRAIN_A, "Partner API", "Prescription\nLab Report")

	@classmethod
	def tearDownClass(cls):
		_cleanup_rules("DR-disp")
		frappe.db.delete("CRM Task Type", {"name": cls.tt})
		field_allowlist.clear("File")

	def setUp(self):
		self.lead = _make_lead(_GRAIN_A)
		self.file = _make_file(self.lead.name, "Partner API", "Prescription", "disp-1")
		_fire_created("File", self.file.name)
		self.file.reload()
		self.task = self.file.custom_review_task
		self.assertTrue(self.task, "setUp premise: the review task must exist before disposition")

	def tearDown(self):
		frappe.db.delete("CRM Visit Audit", {"lead": self.lead.name})
		_cleanup_lead(self.lead.name)

	def _dispose(self, **values):
		from tatva_connect.activity.api import save_activity

		values.setdefault("document", self.file.file_url)
		return save_activity(self.lead.name, self.tt, values, task=self.task)

	def test_approved_writes_outcome_and_mirrors_to_file(self):
		self._dispose(approval_status="Approved")
		self.assertEqual(frappe.db.get_value("CRM Task", self.task, "custom_outcome"), "Approved",
						 "the native activity writer did not record the verdict on the task")
		self.assertEqual(frappe.db.get_value("CRM Task", self.task, "status"), "Done",
						 "an is_logged_complete review task must close on save")
		self.file.reload()
		self.assertEqual(self.file.custom_review_status, "Approved", "the verdict was not mirrored onto the File badge")

	def test_rejected_with_reason_writes_outcome_and_mirrors(self):
		self._dispose(approval_status="Rejected", reason="Blurry scan, unreadable")
		self.assertEqual(frappe.db.get_value("CRM Task", self.task, "custom_outcome"), "Rejected")
		self.file.reload()
		self.assertEqual(self.file.custom_review_status, "Rejected", "a Rejected verdict was not mirrored onto the File")

	def test_rejected_without_a_reason_is_blocked(self):
		# `reason` is required-when-Rejected (depends_on eval). compute_activity enforces required only
		# on fields the depends_on shows — so Rejected with no reason must throw, and nothing lands.
		with self.assertRaises(frappe.exceptions.ValidationError):
			self._dispose(approval_status="Rejected")
		self.assertNotEqual(frappe.db.get_value("CRM Task", self.task, "custom_outcome"), "Rejected",
							"a Rejected verdict landed despite the missing reason")
		self.file.reload()
		self.assertEqual(self.file.custom_review_status, "Pending", "the File left Pending must not have mirrored a blocked verdict")


# -- 3) Call Webhook sends the TASK (verdict + reason), not the lead ----------


class TestWebhookPayloadRouting(FrappeTestCase):
	"""The Call Webhook verb learns a per-action `webhook_payload_source`: `Trigger Doc` sends the
	record that fired the rule (the Document Review task, so the disposition + reason ride the body),
	`Lead` keeps every existing rule unchanged. We spy `frappe.enqueue` and assert exactly one enqueue
	carrying the TASK doc — never the lead."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		cls.tt = _make_doc_review_type(_GRAIN_A)
		cls.lead = _make_lead(_GRAIN_A)
		# enabled=0 is load-bearing: the native Webhook self-registers on CRM Task doc_events and would
		# otherwise auto-fire on every task write in this run; we only want our own thunk to invoke it.
		for wh in ("DR-webhook-task", "DR-webhook-lead"):
			if not frappe.db.exists("Webhook", wh):
				frappe.get_doc({
					"doctype": "Webhook", "name": wh, "webhook_doctype": "CRM Task", "enabled": 0,
					"request_url": "https://example.invalid/hook", "request_method": "POST",
				}).insert(ignore_permissions=True)

	@classmethod
	def tearDownClass(cls):
		_cleanup_rules("DR-hook")
		frappe.db.delete("Webhook", {"name": ("in", ["DR-webhook-task", "DR-webhook-lead"])})
		frappe.db.delete("CRM Task", {"reference_docname": cls.lead.name})
		frappe.db.delete("CRM Task Type", {"name": cls.tt})
		frappe.db.delete("CRM Lead", {"name": cls.lead.name})

	def _review_task(self):
		"""A disposed Document Review task — the record the webhook must carry."""
		return frappe.get_doc({
			"doctype": "CRM Task", "title": "DR webhook task", "custom_task_type": self.tt,
			"reference_doctype": "CRM Lead", "reference_docname": self.lead.name,
			"status": "Done", "custom_outcome": "Rejected",
		}).insert(ignore_permissions=True)

	def _rule(self, name, payload_source):
		return frappe.get_doc({
			"doctype": _RULE_DT, "rule_name": name, "enabled": 1,
			"on_doctype": "CRM Task", "event": "Updated",
			"vertical": _GRAIN_A["vertical"], "group": _GRAIN_A["group"], "program": _GRAIN_A["program"],
			"actions": [{"action_type": "Call Webhook", "webhook_endpoint": name.replace("DR-hook", "DR-webhook"),
						 "webhook_payload_source": payload_source}],
		}).insert(ignore_permissions=True)

	def _fire_and_capture(self, rule, trigger_doc):
		calls = []
		orig = frappe.enqueue
		frappe.enqueue = lambda *a, **k: calls.append((a, k))  # pure stub — prove routing/count, not delivery
		try:
			dispatcher.run_effects(self.lead.name, versions.current_name(rule.name), trigger_doc, _AXES_A, "grain", {}, {})
		finally:
			frappe.enqueue = orig
		return calls

	def test_trigger_doc_source_sends_the_task_not_the_lead(self):
		task = self._review_task()
		try:
			rule = self._rule("DR-hook-task", "Trigger Doc")
			calls = self._fire_and_capture(rule, task)
			self.assertEqual(len(calls), 1, "Call Webhook must enqueue exactly once")
			doc = calls[0][1]["doc"]
			self.assertEqual(doc.doctype, "CRM Task", "the webhook carried the wrong doctype — must be the TASK")
			self.assertEqual(doc.name, task.name, "the webhook did not carry the triggering review task")
			self.assertEqual(doc.custom_outcome, "Rejected", "the verdict did not ride the outbound body")
		finally:
			frappe.db.delete("CRM Task", {"name": task.name})

	def test_lead_source_still_sends_the_lead(self):
		# The default keeps every existing rule unchanged: payload source Lead sends the CRM Lead.
		task = self._review_task()
		try:
			rule = self._rule("DR-hook-lead", "Lead")
			calls = self._fire_and_capture(rule, task)
			self.assertEqual(len(calls), 1)
			doc = calls[0][1]["doc"]
			self.assertEqual(doc.doctype, "CRM Lead", "payload source Lead must send the lead, not the task")
			self.assertEqual(doc.name, self.lead.name)
		finally:
			frappe.db.delete("CRM Task", {"name": task.name})


# -- 4) The WhatsApp inbound follow-up is folded away ------------------------


class TestWhatsAppFold(FrappeTestCase):
	"""The hand-coded 'raise ONE reply task on every inbound WhatsApp Message' side-effect is RETIRED.
	WhatsApp Message is now an automation SUBJECT, so the identical follow-up is a user-built rule and
	is dormant until an operator builds it — no imperative task creation remains on inbound."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()

	def test_no_imperative_task_hook_survives_on_whatsapp_message(self):
		from tatva_connect import hooks

		wm_events = hooks.doc_events.get("WhatsApp Message", {})
		# The fold removed the after_insert follow-up entirely — no per-message code side-effect remains
		# (the wildcard router carries after_insert instead).
		self.assertNotIn("after_insert", wm_events,
						 "a per-message after_insert hook survived the fold — the imperative task path is back")

	def test_on_inbound_message_symbol_is_gone(self):
		from tatva_connect.whatsapp import inbound

		self.assertFalse(hasattr(inbound, "on_inbound_message"),
						 "the retired on_inbound_message handler is still defined — the fold is incomplete")

	def test_whatsapp_message_is_a_resolvable_subject(self):
		# The rebuilt follow-up depends on WhatsApp Message being a subject that resolves to its lead
		# via reference_name (guarded to a CRM Lead reference). Built in memory — resolution reads the
		# link fields off the doc, no insert needed.
		from tatva_connect.automation import subjects

		self.assertTrue(subjects.is_subject("WhatsApp Message"))
		wm = frappe.get_doc({"doctype": "WhatsApp Message", "content_type": "text", "type": "Incoming",
							 "reference_doctype": "CRM Lead", "reference_name": "LEAD-XYZ"})
		self.assertEqual(subjects.resolve_lead_name(wm), "LEAD-XYZ")
		# The guard is fail-closed: a WhatsApp Message pointing at anything else resolves to no lead.
		wm.reference_doctype = "CRM Deal"
		self.assertIsNone(subjects.resolve_lead_name(wm))


if __name__ == "__main__":
	unittest.main()
