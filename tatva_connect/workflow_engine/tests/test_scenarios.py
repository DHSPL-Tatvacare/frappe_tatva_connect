# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Phase 3 - the three acceptance scenarios (design §10) driven end-to-end on the compressed "document
loop" sample workflow (§7), on a test lead (Pareekshith, +91 9059067327). Every scenario asserts
OUTCOMES (Instance state, Lead fields, CRM Task rows, Signal rows, Step Log), never that a function was
called, and drives the REAL interpreter / triggers / signals / sweeps against REAL doctypes. The only
things mocked are the EXTERNAL boundaries: the outbound Webhook HTTP (captured, never sent) and the
inbound API result (delivered as a signal). Sends stay DORMANT - the sends gate records a
"suppressed: sends dormant" marker and no message leaves.

`advance()` commits at the suspension boundary, so these tests CANNOT lean on FrappeTestCase's rollback
for the rows advance touches: fixtures are committed in setUp/setUpClass and each class deletes its own
Instances / Signals / Tasks / Leads in teardown (the same hard-safety pattern the P1/P2 suites use). The
three engine switches (engine, sweep, sends) are set for the class and RESTORED to their prior state in
teardown - never left live behind us.

The workflow graph (entry node n1; timers compressed to minutes):
  n1  Branch  cycle==1?                     t->n2(Welcome)      f->n3c
  n2  Step    AG_Welcome  (Send WhatsApp)   -> n3c
  n3c Assign  {'_corr': cycle}              -> n3               (per-cycle correlation token)
  n3  Step    AG_Extract  (Call Webhook)    -> n4
  n4  Wait[Until Event] "ai_result"         on_event->n5        accepts status/data.diagnosis/data.confidence
  n5  Branch  ai_status=="OK"?              t->n6(Approved)     f->n7(Review)
  n6  Step    AG_Approved (Update Fields + Send WhatsApp) -> n10
  n7  Step    AG_Review  (Create Task)      -> n8
  n8  Wait[Until Event] "review_done"       on_event->n9        accepts verdict
  n9  Step    AG_Confirm (Call Webhook + Send WhatsApp) -> n10
  n10 Branch  cycle==2?                     t->n11(MonthTask)   f->n12
  n11 Step    AG_MonthTask (Create Task)    -> n12
  n12 Wait[For Duration] {'minutes':2}      -> n13
  n13 Step    AG_Nudge  (Send WhatsApp)     -> n14
  n14 Branch  cycle==3?                     t->n15(Terminal)    f->n16
  n15 Terminal
  n16 Assign  {'cycle': cycle+1}            -> n17
  n17 Wait[Event-or-Timeout] "docs_uploaded"  on_event->n3c  on_timeout->n13  wait_expression {'minutes':5}
"""
import unittest
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation.sends import SENDS_SWITCH
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist
from tatva_connect.tests.automation import field_allowlist
from tatva_connect.workflow_engine import (
	ENGINE_SWITCH,
	SWEEP_SWITCH,
	interpreter,
	signals,
	versions,
	wakeups,
)

_DEF_DT = "CRM Workflow Definition"
_NODE_DT = "CRM Workflow Node"
_VERSION_DT = "CRM Workflow Version"
_INSTANCE_DT = "CRM Workflow Instance"
_STEP_LOG_DT = "CRM Workflow Step Log"
_SIGNAL_DT = "CRM Workflow Signal"
_GROUP_DT = "CRM Action Group"
_ITEM_DT = "CRM Action Group Item"
_SWITCH_DT = "CRM Tatva Automation"
# retired: seeds now live on the catalog + contract; teardown uses field_allowlist.clear()

_GRAIN = GRAINS[2]  # TatvaPractice / India / FieldSales
_AXES = (_GRAIN["vertical"], _GRAIN["group"], _GRAIN["program"])

_WF = "WF3-loop"
_AG = "WF3-ag"  # every Action Group name is prefixed with this so cleanup is a single LIKE
_MOBILE = "+91 9059067327"

# The Lead fields the sample workflow writes (disjoint from any live automation rule, §9 coexistence).
_F_SUBSTAGE = "custom_source_origin"   # the sub_stage marker (schema has no `sub_stage`; see report deviation)
_F_DIAGNOSIS = "custom_uhid_mrd"       # payload data.diagnosis -> Lead (allowlisted Update Field)
_F_CONFIDENCE = "custom_latest_hba1c"  # payload data.confidence -> Lead


def _make_template():
	"""A `WhatsApp Templates` row so the Send WhatsApp action's Link resolves. WATI-mirrored + read-only,
	so it lands via `db_insert` (the same seam the automation sends tests use). Dormant sends never touch
	it; it exists only so the Action Group's `whatsapp_template` Link validates on save."""
	account = "WF3-wati-account"
	if not frappe.db.exists("WhatsApp Account", account):
		frappe.get_doc({
			"doctype": "WhatsApp Account", "account_name": account, "status": "Active",
			"url": "https://live-mt-server.wati.io/000000", "token": "wf3-test-token",
			"custom_provider": "WATI", "custom_wati_channel_number": "919000000003",
		}).insert(ignore_permissions=True)
	full_name = "WF3-template-en"
	if not frappe.db.exists("WhatsApp Templates", full_name):
		doc = frappe.new_doc("WhatsApp Templates")
		doc.update({
			"template_name": "WF3-template", "template": "<p>Hi</p>", "language_code": "en",
			"category": "UTILITY", "whatsapp_account": account, "actual_name": "WF3-template",
			"status": "APPROVED",
		})
		doc.name = full_name
		doc.db_insert()
	return full_name, account


def _make_webhook(name):
	"""A native Webhook the Call Webhook action targets. `enabled=0` is load-bearing: an enabled Webhook
	self-registers on its `webhook_doctype`'s doc_events and would auto-fire on every CRM Lead write - we
	want ONLY the engine's own deferred thunk to reach it (and that thunk's enqueue is captured, not run)."""
	if not frappe.db.exists("Webhook", name):
		frappe.get_doc({
			"doctype": "Webhook", "name": name, "webhook_doctype": "CRM Lead", "enabled": 0,
			"request_url": "https://example.invalid/hook", "request_method": "POST",
		}).insert(ignore_permissions=True)
	return name


def _make_task_type():
	name = f"{_GRAIN['vertical']}::{_GRAIN['group']}::{_GRAIN['program']}::WF3Review"
	if not frappe.db.exists("CRM Task Type", name):
		frappe.get_doc({
			"doctype": "CRM Task Type", "type_name": "WF3Review",
			"vertical": _GRAIN["vertical"], "group": _GRAIN["group"], "program": _GRAIN["program"],
		}).insert(ignore_permissions=True)
	return name


def _make_group(suffix, items):
	return frappe.get_doc({"doctype": _GROUP_DT, "group_name": f"{_AG}-{suffix}", "actions": items}).insert(ignore_permissions=True).name


def _make_lead(auto_entry=True):
	"""Pareekshith, +91 9059067327, at the sample workflow's grain. With `auto_entry=False` the entry hook
	is suppressed (in_workflow guard) so a test can start the Instance manually at a chosen node."""
	if not auto_entry:
		frappe.flags.in_workflow = True
	try:
		return frappe.get_doc({
			"doctype": "CRM Lead", "first_name": "Pareekshith", "lead_name": "Pareekshith",
			"status": "New", "mobile_no": _MOBILE, "lead_owner": "Administrator",
			"custom_vertical": _GRAIN["vertical"], "custom_group": _GRAIN["group"],
			"custom_current_program": _GRAIN["program"],
		}).insert(ignore_permissions=True)
	finally:
		frappe.flags.in_workflow = False


def _start(subject, current_node, state=None):
	return frappe.get_doc({
		"doctype": _INSTANCE_DT, "workflow": _WF,
		"workflow_version": versions.current_name(_WF),
		"subject_doctype": "CRM Lead", "subject_name": subject,
		"current_node": current_node, "state_json": frappe.as_json(state or {}), "status": "Running",
	}).insert(ignore_permissions=True)


def _instance_of(lead):
	rows = frappe.get_all(_INSTANCE_DT, filters={"workflow": _WF, "subject_name": lead}, fields=["name"])
	return rows[0].name if rows else None


def _row(name, fields):
	return frappe.db.get_value(_INSTANCE_DT, name, fields, as_dict=True)


def _logs(instance_name):
	return frappe.get_all(
		_STEP_LOG_DT, filters={"workflow_instance": instance_name},
		fields=["node_id", "node_type", "outcome", "detail"], order_by="creation asc, name asc",
	)


# -- mock drivers (test helpers; the ONLY external boundaries we stand in for) --------------------------


def mock_api_respond(lead, status, data, correlation):
	"""The external AI/API posting its result back as a signal - `deliver_signal("ai_result", ...)`. Any
	service posting ANY JSON is one call to this. `status` drives the OK/reject branch; `data` is the
	payload body the accepts-map declares paths into; `correlation` ties the reply to the exact wait."""
	name = signals.deliver_signal("CRM Lead", lead, "ai_result", correlation=correlation, payload={"status": status, "data": data})
	frappe.db.commit()
	return name


def mock_review_done(task_name):
	"""The rep marking the Document Review CRM Task Done - a REAL `CRM Task.on_update` lifecycle event
	(`triggers.on_task_done`) delivers `review_done`, NOT an explicit deliver_signal. A reconciler pass
	after the save guarantees the resume even if the inline wake is lost (F5), and is a claimed no-op if
	the inline wake already advanced."""
	task = frappe.get_doc("CRM Task", task_name)
	task.status = "Done"
	task.save(ignore_permissions=True)
	frappe.db.commit()
	wakeups.reconciler_sweep()
	frappe.db.commit()


def mock_upload(lead, correlation):
	"""The mobile document upload looping the cycle - `deliver_signal("docs_uploaded", ...)`."""
	name = signals.deliver_signal("CRM Lead", lead, "docs_uploaded", correlation=correlation, payload={})
	frappe.db.commit()
	return name


# -- the sample workflow fixtures (built in setUp; torn down; never left live) --------------------------


class _SampleWorkflowCase(FrappeTestCase):
	"""Base: switches on (engine + sweep; sends forced DORMANT), the seven Action Groups, and the document
	loop Definition - all committed, all torn down. A capturing `frappe.enqueue` stands in for the outbound
	Webhook HTTP (records the enqueue, never delivers) while delegating every other enqueue (e.g. the
	signal-resume job) to the real one."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		cls._prior = {k: frappe.db.get_value(_SWITCH_DT, k, "enabled") for k in (ENGINE_SWITCH, SWEEP_SWITCH, SENDS_SWITCH)}
		frappe.db.set_value(_SWITCH_DT, ENGINE_SWITCH, "enabled", 1)
		frappe.db.set_value(_SWITCH_DT, SWEEP_SWITCH, "enabled", 1)
		frappe.db.set_value(_SWITCH_DT, SENDS_SWITCH, "enabled", 0)  # sends DORMANT - assert the marker, never a live message
		cls.template, cls.account = _make_template()
		cls.hook_ai = _make_webhook("WF3-hook-ai")
		cls.hook_api2 = _make_webhook("WF3-hook-api2")
		cls.task_type = _make_task_type()
		cls.set_rows = [
			field_allowlist.seed_settable("CRM Lead", _F_SUBSTAGE, *_AXES),
			field_allowlist.seed_settable("CRM Lead", _F_DIAGNOSIS, *_AXES),
			field_allowlist.seed_settable("CRM Lead", _F_CONFIDENCE, *_AXES),
		]
		cls._build_action_groups()
		cls._build_workflow()
		frappe.db.commit()

	@classmethod
	def _build_action_groups(cls):
		_make_group("welcome", [{"action_type": "Send WhatsApp", "whatsapp_template": cls.template}])
		_make_group("extract", [{"action_type": "Call Webhook", "webhook_endpoint": cls.hook_ai}])
		_make_group("approved", [
			{"action_type": "Update Field", "target_doctype": "CRM Lead", "fieldname": _F_SUBSTAGE, "value_mode": "Literal", "value": "Value1"},
			{"action_type": "Update Field", "target_doctype": "CRM Lead", "fieldname": _F_DIAGNOSIS, "value_mode": "From Context", "context_field": "diagnosis"},
			{"action_type": "Update Field", "target_doctype": "CRM Lead", "fieldname": _F_CONFIDENCE, "value_mode": "From Context", "context_field": "confidence"},
			{"action_type": "Send WhatsApp", "whatsapp_template": cls.template},
		])
		_make_group("review", [{"action_type": "Create Task", "task_type": cls.task_type}])
		_make_group("confirm", [
			{"action_type": "Call Webhook", "webhook_endpoint": cls.hook_api2},
			{"action_type": "Send WhatsApp", "whatsapp_template": cls.template},
		])
		_make_group("nudge", [{"action_type": "Send WhatsApp", "whatsapp_template": cls.template}])
		_make_group("month", [{"action_type": "Create Task", "task_type": cls.task_type}])

	@classmethod
	def _build_workflow(cls):
		frappe.get_doc({
			"doctype": _DEF_DT, "workflow_name": _WF, "enabled": 1,
			"vertical": _GRAIN["vertical"], "group": _GRAIN["group"], "program": _GRAIN["program"],
			"entry_doctype": "CRM Lead", "entry_event": "Created",
			"nodes": [
				{"node_id": "n1", "node_type": "Branch", "condition": "ctx.get('cycle', 1) == 1", "on_true": "n2", "on_false": "n3c"},
				{"node_id": "n2", "node_type": "Step", "action_group": f"{_AG}-welcome", "next_node": "n3c"},
				{"node_id": "n3c", "node_type": "Assign", "assign_json": "{'_corr': ctx.get('cycle', 1)}", "next_node": "n3"},
				{"node_id": "n3", "node_type": "Step", "action_group": f"{_AG}-extract", "next_node": "n4"},
				{"node_id": "n4", "node_type": "Wait", "wait_mode": "Until Event", "signal_name": "ai_result",
				 "accepts_json": '{"status": "ai_status", "data.diagnosis": "diagnosis", "data.confidence": "confidence"}', "on_event": "n5"},
				{"node_id": "n5", "node_type": "Branch", "condition": "ctx.get('ai_status') == 'OK'", "on_true": "n6", "on_false": "n7"},
				{"node_id": "n6", "node_type": "Step", "action_group": f"{_AG}-approved", "next_node": "n10"},
				{"node_id": "n7", "node_type": "Step", "action_group": f"{_AG}-review", "next_node": "n8"},
				{"node_id": "n8", "node_type": "Wait", "wait_mode": "Until Event", "signal_name": "review_done",
				 "accepts_json": '{"verdict": "review_verdict"}', "on_event": "n9"},
				{"node_id": "n9", "node_type": "Step", "action_group": f"{_AG}-confirm", "next_node": "n10"},
				{"node_id": "n10", "node_type": "Branch", "condition": "ctx.get('cycle', 1) == 2", "on_true": "n11", "on_false": "n12"},
				{"node_id": "n11", "node_type": "Step", "action_group": f"{_AG}-month", "next_node": "n12"},
				{"node_id": "n12", "node_type": "Wait", "wait_mode": "For Duration", "wait_expression": "{'minutes': 2}", "next_node": "n13"},
				{"node_id": "n13", "node_type": "Step", "action_group": f"{_AG}-nudge", "next_node": "n14"},
				{"node_id": "n14", "node_type": "Branch", "condition": "ctx.get('cycle', 1) == 3", "on_true": "n15", "on_false": "n16"},
				{"node_id": "n15", "node_type": "Terminal"},
				{"node_id": "n16", "node_type": "Assign", "assign_json": "{'cycle': ctx.get('cycle', 1) + 1}", "next_node": "n17"},
				{"node_id": "n17", "node_type": "Wait", "wait_mode": "Event-or-Timeout", "signal_name": "docs_uploaded",
				 "wait_expression": "{'minutes': 5}", "accepts_json": "{}", "on_event": "n3c", "on_timeout": "n13"},
			],
		}).insert(ignore_permissions=True)

	@classmethod
	def tearDownClass(cls):
		for inst in frappe.get_all(_INSTANCE_DT, filters={"workflow": _WF}, pluck="name"):
			frappe.db.delete(_STEP_LOG_DT, {"workflow_instance": inst})
		frappe.db.delete(_INSTANCE_DT, {"workflow": _WF})
		frappe.db.delete(_VERSION_DT, {"workflow": _WF})
		frappe.db.delete(_NODE_DT, {"parent": _WF})
		frappe.db.delete(_DEF_DT, {"workflow_name": _WF})
		frappe.db.delete(_ITEM_DT, {"parent": ["like", f"{_AG}%"]})
		frappe.db.delete(_GROUP_DT, {"group_name": ["like", f"{_AG}%"]})
		field_allowlist.clear()
		frappe.delete_doc("WhatsApp Templates", cls.template, force=True, ignore_permissions=True)
		frappe.delete_doc("WhatsApp Account", cls.account, force=True, ignore_permissions=True)
		frappe.db.delete("Webhook", {"name": ["in", [cls.hook_ai, cls.hook_api2]]})
		frappe.db.delete("CRM Task Type", {"name": cls.task_type})
		for k, v in cls._prior.items():
			frappe.db.set_value(_SWITCH_DT, k, "enabled", v or 0)
		frappe.db.commit()

	# -- per-test lead + the webhook-egress capture --------------------------------------------------

	def _install_webhook_capture(self):
		"""Stand in for the ONLY external side effect - the outbound Webhook HTTP. Records each
		`enqueue_webhook` enqueue (proving egress fired) WITHOUT delivering it, and delegates every other
		enqueue (notably the signal-resume job) to the real `frappe.enqueue` so the engine runs for real."""
		self.webhook_calls = []
		real_enqueue = frappe.enqueue

		def capture(method=None, *args, **kwargs):
			target = method if isinstance(method, str) else kwargs.get("method")
			if isinstance(target, str) and target.endswith("enqueue_webhook"):
				self.webhook_calls.append((kwargs.get("webhook") or {}).get("name"))
				return None
			return real_enqueue(method, *args, **kwargs)

		frappe.enqueue = capture
		self.addCleanup(lambda: setattr(frappe, "enqueue", real_enqueue))

	def _lead_cleanup(self, lead):
		inst = _instance_of(lead)
		if inst:
			frappe.db.delete(_STEP_LOG_DT, {"workflow_instance": inst})
		frappe.db.delete(_INSTANCE_DT, {"subject_name": lead})
		frappe.db.delete(_SIGNAL_DT, {"subject_name": lead})
		frappe.db.delete("CRM Task", {"reference_doctype": "CRM Lead", "reference_docname": lead})
		frappe.db.delete("CRM Lead", {"name": lead})
		frappe.db.commit()


class TestScenarioA_Approve(_SampleWorkflowCase):
	"""§10 Scenario A - the happy path: entry starts an Instance; AG_Extract fires the AI webhook; the AI
	posts status OK with a payload; the OK Branch runs AG_Approved, which sets the sub_stage marker and
	writes the two custom fields FROM the payload (payload->state->Lead) and records a dormant WhatsApp
	marker; the Instance parks at the post-approve Wait. Exactly one Instance; Step Log walks each node ok."""

	def setUp(self):
		self._install_webhook_capture()
		self.lead = _make_lead(auto_entry=True).name  # inserting the lead FIRES the entry hook -> Instance starts
		frappe.db.commit()

	def tearDown(self):
		self._lead_cleanup(self.lead)

	def test_ai_approve_sets_lead_fields_and_dormant_whatsapp(self):
		inst = _instance_of(self.lead)
		self.assertIsNotNone(inst, "the Lead's creation must start exactly one Instance")
		parked = _row(inst, ["status", "current_node", "awaiting_signal", "awaiting_correlation"])
		self.assertEqual(parked.status, "Parked")
		self.assertEqual(parked.current_node, "n4", "the entry segment parks awaiting the AI result")
		self.assertEqual(parked.awaiting_signal, "ai_result")
		self.assertIn(self.hook_ai, self.webhook_calls, "AG_Extract must have fired the AI webhook (egress)")

		# The AI posts an approval with a payload; the accepts-map declares which paths land in state.
		mock_api_respond(self.lead, "OK", {"diagnosis": "Diabetes Type 2", "confidence": 0.91}, correlation=parked.awaiting_correlation)

		row = _row(inst, ["status", "current_node", "active_key"])
		self.assertEqual(row.current_node, "n12", "the OK branch runs Approved then parks at the post-approve Wait")
		self.assertEqual(row.status, "Parked")
		self.assertTrue(row.active_key, "a live Instance carries an active_key")

		lead = frappe.db.get_value("CRM Lead", self.lead, [_F_SUBSTAGE, _F_DIAGNOSIS, _F_CONFIDENCE], as_dict=True)
		self.assertEqual(lead[_F_SUBSTAGE], "Value1", "AG_Approved set the sub_stage marker")
		self.assertEqual(lead[_F_DIAGNOSIS], "Diabetes Type 2", "the diagnosis moved payload->state->Lead")
		self.assertEqual(frappe.utils.flt(lead[_F_CONFIDENCE]), 0.91, "the confidence moved payload->state->Lead")

		self.assertEqual(
			frappe.db.count(_INSTANCE_DT, {"workflow": _WF, "subject_name": self.lead, "active_key": ["is", "set"]}), 1,
			"exactly one live Instance for (workflow, subject)",
		)

		outcomes = [(l.node_id, l.outcome) for l in _logs(inst)]
		self.assertIn(("n6", "ok"), outcomes, "AG_Approved ran ok")
		approve_detail = next(l.detail for l in _logs(inst) if l.node_id == "n6")
		self.assertIn("suppressed: sends dormant", approve_detail, "the dormant WhatsApp marker is recorded, not a live message")
		walked = [o[0] for o in outcomes]
		for expected in ("n1", "n2", "n3c", "n3", "n4", "n5", "n6", "n10", "n12"):
			self.assertIn(expected, walked, f"Step Log must show node {expected} was walked")
		self.assertTrue(all(o[1] in ("ok", "resumed", "parked") for o in outcomes), "every walked node logged a non-failure outcome")


class TestScenarioB_RejectReviewConfirm(_SampleWorkflowCase):
	"""§10 Scenario B - reject -> human review -> Task-Done signal -> confirm API: the AI posts REJECTED,
	the False Branch creates a Document Review CRM Task and the Instance parks on `review_done`; marking
	that Task Done fires a REAL lifecycle event that delivers the signal (no API call); the Confirm webhook
	goes out to API-2 and the Instance advances; marking Done a SECOND time does not double-advance."""

	def setUp(self):
		self._install_webhook_capture()
		self.lead = _make_lead(auto_entry=True).name
		frappe.db.commit()

	def tearDown(self):
		self._lead_cleanup(self.lead)

	def test_reject_to_human_review_to_confirm_api(self):
		inst = _instance_of(self.lead)
		parked = _row(inst, ["awaiting_correlation", "current_node"])
		self.assertEqual(parked.current_node, "n4")

		# The AI rejects -> the False branch raises a Document Review task and parks on review_done.
		mock_api_respond(self.lead, "REJECTED", {}, correlation=parked.awaiting_correlation)

		review_row = _row(inst, ["status", "current_node", "awaiting_signal"])
		self.assertEqual(review_row.current_node, "n8", "reject routes to Review then parks on review_done")
		self.assertEqual(review_row.awaiting_signal, "review_done")
		task = frappe.get_all(
			"CRM Task",
			filters={"reference_doctype": "CRM Lead", "reference_docname": self.lead, "custom_task_type": self.task_type},
			fields=["name", "status", "assigned_to"],
		)
		self.assertEqual(len(task), 1, "a Document Review CRM Task exists while the Instance is parked on review_done")
		self.assertNotIn(task[0].status, ("Done", "Canceled"), "the review task is open, awaiting the rep")
		task_name = task[0].name

		# The rep marks the Task Done -> a REAL CRM Task.on_update lifecycle event delivers review_done.
		mock_review_done(task_name)

		confirmed = _row(inst, ["status", "current_node"])
		self.assertEqual(confirmed.current_node, "n12", "the Task-Done signal resumed the Instance through Confirm")
		self.assertIn(self.hook_api2, self.webhook_calls, "AG_Confirm fired the API-2 webhook")
		self.assertEqual([l.node_id for l in _logs(inst) if l.node_id == "n9"], ["n9"], "Confirm ran exactly once")

		# Idempotency: marking the SAME Task Done again must not double-advance (no parked review wait now).
		api2_before = self.webhook_calls.count(self.hook_api2)
		mock_review_done(task_name)
		again = _row(inst, ["current_node"])
		self.assertEqual(again.current_node, "n12", "a repeated Task-Done does not advance the Instance again")
		self.assertEqual(self.webhook_calls.count(self.hook_api2), api2_before, "no second Confirm webhook on a repeated Task-Done")
		self.assertEqual([l.node_id for l in _logs(inst) if l.node_id == "n9"], ["n9"], "Confirm still ran exactly once")


class TestScenarioC_Reliability(_SampleWorkflowCase):
	"""§10 Scenario C - the reliability spine, five checks on the sample workflow (each on its own Instance
	for isolation; the design's "one instance" is narrative). Early/duplicate/stale signal (F1/F2), the
	Event-or-Timeout re-nudge, and crash-rollback + reconciler-drives-once + transient-not-Failed (F3/F4/F5)."""

	def setUp(self):
		self._install_webhook_capture()
		self.lead = _make_lead(auto_entry=False).name  # manual control: start Instances at chosen nodes
		frappe.db.commit()

	def tearDown(self):
		self._lead_cleanup(self.lead)

	def test_1_early_signal_is_buffered_then_consumed(self):
		"""F1 - the AI result delivered BEFORE the interpreter reaches the Wait waits in the inbox and is
		consumed the moment the Wait is reached. No lost wakeup."""
		# cycle-1 correlation is the cycle number (1); deliver the reply early, before any Instance parks.
		sig = signals.deliver_signal("CRM Lead", self.lead, "ai_result", correlation="1", payload={"status": "OK", "data": {"diagnosis": "Dx", "confidence": 0.5}})
		frappe.db.commit()
		self.assertEqual(frappe.db.get_value(_SIGNAL_DT, sig, "status"), "Pending", "an early delivery buffers - it touches no Instance")

		inst = _start(self.lead, "n1").name  # walk from entry; n3c stamps _corr=1, n4 consumes the buffered reply
		frappe.db.commit()
		interpreter.advance(frappe.get_doc(_INSTANCE_DT, inst))

		self.assertEqual(frappe.db.get_value(_SIGNAL_DT, sig, "status"), "Consumed", "the buffered signal is consumed at the Wait")
		self.assertIn(("n4", "resumed"), [(l.node_id, l.outcome) for l in _logs(inst)], "the Wait resumed from the inbox, not a park")
		self.assertNotIn(_row(inst, ["current_node"]).current_node, ("n4",), "the Instance advanced past the Wait")

	def test_2_duplicate_correlation_advances_once(self):
		"""F2 - two deliveries with the SAME correlation advance the Instance exactly once."""
		inst = _start(self.lead, "n1").name
		frappe.db.commit()
		interpreter.advance(frappe.get_doc(_INSTANCE_DT, inst))  # parks at n4 awaiting ai_result / corr 1
		corr = _row(inst, ["awaiting_correlation"]).awaiting_correlation

		# First delivery resumes inline (now=True); the second finds the Instance no longer parked here.
		mock_api_respond(self.lead, "OK", {"diagnosis": "Dx", "confidence": 0.5}, correlation=corr)
		mock_api_respond(self.lead, "OK", {"diagnosis": "Dx", "confidence": 0.5}, correlation=corr)

		self.assertEqual(frappe.db.count(_SIGNAL_DT, {"subject_name": self.lead, "signal_name": "ai_result", "status": "Consumed"}), 1,
			"exactly one of the duplicate deliveries is consumed")
		self.assertEqual([l.node_id for l in _logs(inst) if l.node_id == "n6"], ["n6"], "the OK branch ran once, not twice")

	def test_3_stale_correlation_is_ignored(self):
		"""F2 - a reply carrying a PRIOR cycle's correlation arriving during the current wait is ignored."""
		inst = _start(self.lead, "n4", {"cycle": 2, "_corr": 2}).name  # parked on the cycle-2 wait
		frappe.db.commit()
		interpreter.advance(frappe.get_doc(_INSTANCE_DT, inst))
		self.assertEqual(_row(inst, ["status"]).status, "Parked")

		mock_api_respond(self.lead, "OK", {"diagnosis": "Dx", "confidence": 0.5}, correlation="1")  # stale cycle-1 token

		row = _row(inst, ["status", "current_node"])
		self.assertEqual(row.status, "Parked", "a stale-correlation reply must not wake the current wait")
		self.assertEqual(row.current_node, "n4")
		self.assertEqual(frappe.db.get_value(_SIGNAL_DT, {"subject_name": self.lead, "correlation": "1"}, "status"), "Pending",
			"the stale reply is left in the inbox, never consumed")

	def test_4_event_or_timeout_re_nudges_instead_of_hanging(self):
		"""The Event-or-Timeout upload wait (n17) with no upload past its deadline routes to on_timeout ->
		re-nudge (n13), never hangs; the loop then re-parks on the next upload wait."""
		inst = _start(self.lead, "n17", {"cycle": 2, "_corr": 2}).name
		frappe.db.commit()
		interpreter.advance(frappe.get_doc(_INSTANCE_DT, inst))  # parks awaiting docs_uploaded + resume_at
		parked = _row(inst, ["status", "awaiting_signal", "resume_at"])
		self.assertEqual(parked.awaiting_signal, "docs_uploaded")
		self.assertIsNotNone(parked.resume_at)

		frappe.db.set_value(_INSTANCE_DT, inst, "resume_at", frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-1))
		frappe.db.commit()
		wakeups.timer_sweep()  # no upload -> the clock wins -> on_timeout -> re-nudge

		outcomes = [(l.node_id, l.detail) for l in _logs(inst)]
		self.assertIn(("n17", "timeout"), outcomes, "the deadline routes via on_timeout, not on_event")
		self.assertIn("n13", [l.node_id for l in _logs(inst)], "the re-nudge (AG_Nudge) ran")
		self.assertEqual(_row(inst, ["current_node"]).current_node, "n17", "the loop re-parked on the next upload wait - never hung")

	def test_5_crash_rolls_back_then_reconciler_drives_once(self):
		"""F3/F4/F5 - a crash mid-segment rolls back to the last durable park; a transient DB error leaves
		it Parked + retrying (never Failed); the reconciler then drives it to completion EXACTLY once with no
		duplicate nudge."""
		inst = _start(self.lead, "n12", {"cycle": 3}).name  # cycle 3: on wake, nudge then Terminal
		frappe.db.commit()
		interpreter.advance(frappe.get_doc(_INSTANCE_DT, inst))  # parks at n12 (For Duration)
		frappe.db.set_value(_INSTANCE_DT, inst, "resume_at", frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-1))
		frappe.db.commit()

		# First drive crashes mid-segment with a TRANSIENT DB error -> rollback -> Parked + retry, not Failed.
		with mock.patch.object(interpreter, "_run_step", side_effect=frappe.QueryDeadlockError("simulated deadlock")):
			wakeups.reconciler_sweep()
		crashed = _row(inst, ["status", "current_node", "retry_count"])
		self.assertEqual(crashed.status, "Parked", "a transient crash leaves the Instance Parked, never Failed (F4)")
		self.assertEqual(crashed.current_node, "n12", "the segment rolled back to the last durable park (F3)")
		self.assertEqual(crashed.retry_count, 1, "the transient-retry counter is bumped")
		self.assertEqual([l.node_id for l in _logs(inst) if l.node_id == "n13"], [], "the crashed segment left no nudge behind")

		# The reconciler re-drives from durable state to completion - exactly once.
		wakeups.reconciler_sweep()
		done = _row(inst, ["status", "current_node"])
		self.assertEqual(done.status, "Done", "the reconciler drives the Instance to completion (F5)")
		self.assertEqual(done.current_node, "n15")
		self.assertEqual([l.node_id for l in _logs(inst) if l.node_id == "n13"], ["n13"], "the nudge ran exactly once - no duplicate on re-drive")


if __name__ == "__main__":
	unittest.main()
