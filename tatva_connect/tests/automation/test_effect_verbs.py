# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Task 6 sign-off — explicit per-verb end-to-end coverage for the EFFECT-lane handlers now living in
`automation/actions.py` (Update Field, Create Task, Create Note, Call Webhook). This is a NEW test
file, not a re-wiring: every verb was already reachable via `_ACTION_LANES` before this task; the
extraction into `actions.py` is behavior-identical (A.8/A.12) and Task 5/Leg E-G already cover the
Expression/value-mode surface — this suite's job is one clean end-to-end proof per verb plus a
planted-bad, on the handler's NEW home.

Real Frappe engine as the oracle — real saves, real allowlist, real Comment/Task rows, a real spy on
`frappe.enqueue` (never a hardcoded verdict, S.6).
"""
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import actions, dispatcher, versions
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist
from tatva_connect.tests.automation import field_allowlist

_RUN_LOG = "CRM Automation Run Log"
_FIELD = field_allowlist.DOCTYPE
_GRAIN = GRAINS[0]
_AXES = (_GRAIN["vertical"], _GRAIN["group"], _GRAIN["program"])


def _action_row(**fields):
	"""A duck-typed action row - the handlers read attributes off it (same shape as a real
	`CRM Automation Action` child row)."""
	class _A:
		pass
	a = _A()
	for k, v in fields.items():
		setattr(a, k, v)
	return a


def _make_lead(**extra):
	payload = {
		"doctype": "CRM Lead", "first_name": "EffectVerb", "lead_name": "EffectVerb Probe", "status": "New",
		"custom_vertical": _GRAIN["vertical"], "custom_group": _GRAIN["group"],
		"custom_current_program": _GRAIN["program"], "custom_dob": "1990-01-01",
	}
	payload.update(extra)
	return frappe.get_doc(payload).insert(ignore_permissions=True)


class TestUpdateFieldDerivesStage(FrappeTestCase):
	"""Update Field via the unified load+save write path must ride the SAME `validate_stage` hook a
	human edit would — proving the move into actions.py did not bypass load+save for a raw
	`frappe.db.set_value` shortcut (plan's explicit callout, Task 6 Step 1)."""

	STAGE_SWITCH = "Lead::CRM Lead::stage"  # dormant by default (A.6) - validate_stage no-ops unless on.

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		frappe.db.set_value("CRM Tatva Automation", cls.STAGE_SWITCH, "enabled", 1)
		cls.allowlist = field_allowlist.seed_settable(
			"CRM Lead", "custom_substage", _GRAIN["vertical"], _GRAIN["group"], _GRAIN["program"]
		)
		cls.parent_pk = f"{_GRAIN['program']}::EVParentStage"
		cls.leaf_pk = f"{_GRAIN['program']}::EVLeafStage"
		if not frappe.db.exists("CRM Lead Stage", cls.parent_pk):
			frappe.get_doc({
				"doctype": "CRM Lead Stage", "program": _GRAIN["program"], "stage": "EVParentStage", "selectable": 0,
			}).insert(ignore_permissions=True)
		if not frappe.db.exists("CRM Lead Stage", cls.leaf_pk):
			frappe.get_doc({
				"doctype": "CRM Lead Stage", "program": _GRAIN["program"], "stage": "EVLeafStage",
				"substage_of": cls.parent_pk, "selectable": 1,
			}).insert(ignore_permissions=True)
		cls.lead = _make_lead()

	@classmethod
	def tearDownClass(cls):
		frappe.db.delete("CRM Lead", {"name": cls.lead.name})
		frappe.db.delete(_FIELD, {"name": cls.allowlist})
		frappe.db.delete("CRM Lead Stage", {"name": cls.leaf_pk})
		frappe.db.delete("CRM Lead Stage", {"name": cls.parent_pk})

	def test_update_field_substage_derives_parent_stage(self):
		a = _action_row(
			action_type="Update Field", target_doctype="CRM Lead", fieldname="custom_substage",
			value_mode="Literal", value=self.leaf_pk,
		)
		actions._action_set_field(a, self.lead.name, {}, _AXES, self.lead)
		substage, stage = frappe.db.get_value("CRM Lead", self.lead.name, ["custom_substage", "custom_stage"])
		self.assertEqual(substage, self.leaf_pk, "Update Field did not write the leaf substage")
		self.assertEqual(
			stage, self.parent_pk,
			"custom_stage was not derived from custom_substage — Update Field bypassed validate_stage",
		)

	def test_update_field_on_non_allowlisted_field_raises_and_does_not_write(self):
		"""Planted-bad: `custom_stage` itself is never allowlisted for direct write (only the substage
		pick is) — an attempt must raise, never silently derive-and-write around the allowlist."""
		before = frappe.db.get_value("CRM Lead", self.lead.name, "custom_stage")
		a = _action_row(
			action_type="Update Field", target_doctype="CRM Lead", fieldname="custom_stage",
			value_mode="Literal", value=self.parent_pk,
		)
		with self.assertRaises(PermissionError):
			actions._action_set_field(a, self.lead.name, {}, _AXES, self.lead)
		after = frappe.db.get_value("CRM Lead", self.lead.name, "custom_stage")
		self.assertEqual(before, after, "custom_stage was written despite the field not being allowlisted")


class TestCreateTaskFollowUp(FrappeTestCase):
	"""Create Task raises the composite-scoped follow-up with a due date resolved From Context, and
	carries the completing (trigger) task's assignee onto the new one."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		cls.lead = _make_lead()
		cls.tt_name = f"{_GRAIN['vertical']}::{_GRAIN['group']}::{_GRAIN['program']}::EVFollowUp"
		if not frappe.db.exists("CRM Task Type", cls.tt_name):
			frappe.get_doc({
				"doctype": "CRM Task Type", "type_name": "EVFollowUp",
				"vertical": _GRAIN["vertical"], "group": _GRAIN["group"], "program": _GRAIN["program"],
			}).insert(ignore_permissions=True)
		# A task type scoped ONLY via the deprecated `CRM Task Type Scope` child table (blank parent
		# grain) to a DIFFERENT grain — exercises the real grain backstop `_action_create_task` docs.
		# Blank vertical/group/program makes the `format:` autoname unpredictable across runs, so read
		# the real name back off the inserted doc rather than assuming its shape.
		other = GRAINS[1]
		cls.foreign_tt_name = frappe.db.get_value("CRM Task Type", {"type_name": "EVForeignScoped"}, "name")
		if not cls.foreign_tt_name:
			cls.foreign_tt_name = frappe.get_doc({
				"doctype": "CRM Task Type", "type_name": "EVForeignScoped",
				"vertical": "", "group": "", "program": "",
				"scope": [{"vertical": other["vertical"], "group": other["group"], "program": other["program"]}],
			}).insert(ignore_permissions=True).name

	@classmethod
	def tearDownClass(cls):
		frappe.db.delete("CRM Task", {"reference_doctype": "CRM Lead", "reference_docname": cls.lead.name})
		frappe.db.delete("CRM Task Type", {"name": cls.tt_name})
		frappe.db.delete("CRM Task Type", {"name": cls.foreign_tt_name})
		frappe.db.delete("CRM Lead", {"name": cls.lead.name})

	def _make_trigger_task(self, **extra):
		payload = {
			"doctype": "CRM Task", "title": "EV trigger task", "assigned_to": "Administrator",
			"reference_doctype": "CRM Lead", "reference_docname": self.lead.name, "status": "Done",
		}
		payload.update(extra)
		return frappe.get_doc(payload).insert(ignore_permissions=True)

	def test_create_task_from_context_due_date_and_carried_assignee(self):
		trigger = self._make_trigger_task()
		try:
			ctx = {"due_ctx": "2026-08-01 10:00:00"}
			a = _action_row(action_type="Create Task", task_type=self.tt_name, due_mode="From Context", due_from="due_ctx")
			actions._action_create_task(a, self.lead.name, ctx, _AXES, trigger)
			rows = frappe.get_all(
				"CRM Task",
				filters={"reference_doctype": "CRM Lead", "reference_docname": self.lead.name, "custom_task_type": self.tt_name},
				fields=["name", "due_date", "assigned_to"],
			)
			self.assertTrue(rows, "follow-up task was not created")
			self.assertEqual(
				frappe.utils.get_datetime(rows[0].due_date), frappe.utils.get_datetime("2026-08-01 10:00:00"),
				"due date was not resolved From Context",
			)
			self.assertEqual(rows[0].assigned_to, "Administrator", "assignee was not carried from the trigger task")
		finally:
			frappe.db.delete("CRM Task", {"name": trigger.name})

	def test_create_task_out_of_scope_grain_raises(self):
		"""Planted-bad: a task type scoped to a DIFFERENT grain than the lead's must be rejected, not
		silently raised on the wrong grain's lead."""
		trigger = self._make_trigger_task()
		try:
			a = _action_row(action_type="Create Task", task_type=self.foreign_tt_name, due_mode="From Context", due_from=None)
			with self.assertRaises(PermissionError):
				actions._action_create_task(a, self.lead.name, {}, _AXES, trigger)
			rows = frappe.get_all(
				"CRM Task",
				filters={"reference_doctype": "CRM Lead", "reference_docname": self.lead.name, "custom_task_type": self.foreign_tt_name},
			)
			self.assertFalse(rows, "an out-of-scope task type still raised a follow-up task")
		finally:
			frappe.db.delete("CRM Task", {"name": trigger.name})


class TestCreateNoteAppendsComment(FrappeTestCase):
	"""Create Note appends a real Comment row on the subject lead."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		cls.lead = _make_lead()

	@classmethod
	def tearDownClass(cls):
		frappe.db.delete("Comment", {"reference_doctype": "CRM Lead", "reference_name": cls.lead.name})
		frappe.db.delete("CRM Lead", {"name": cls.lead.name})

	def test_create_note_appends_comment_on_subject_lead(self):
		a = _action_row(action_type="Create Note", comment_mode="Literal", comment_text="EV: automated note landed")
		actions._action_add_comment(a, self.lead.name, {}, _AXES, self.lead)
		comments = frappe.get_all(
			"Comment", filters={"reference_doctype": "CRM Lead", "reference_name": self.lead.name}, pluck="content",
		)
		self.assertTrue(any("EV: automated note landed" in (c or "") for c in comments), "Create Note did not land a Comment on the lead")

	def test_create_note_blank_resolved_text_raises(self):
		"""Planted-bad: an empty resolved comment must raise, never insert a blank Comment row."""
		a = _action_row(action_type="Create Note", comment_mode="Literal", comment_text="")
		with self.assertRaises(ValueError):
			actions._action_add_comment(a, self.lead.name, {}, _AXES, self.lead)


class TestCallWebhookDeferredThunk(FrappeTestCase):
	"""Call Webhook must return a thunk (not fire `frappe.enqueue` immediately), and that thunk must
	fire ONLY if the whole rule's savepoint commits — never on a rolled-back rule."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		cls.lead = _make_lead()
		# `enabled=0` is load-bearing, not incidental: the native Webhook doctype self-registers on
		# `webhook_doctype`'s doc_events (Webhook.enabled defaults to 1) and would otherwise auto-fire
		# on every CRM Lead insert/update in this whole test run via its own after_commit hook (which a
		# savepoint rollback cannot clear) — we want ONLY our own `_action_call_webhook` thunk to ever
		# invoke it, never the stock auto-trigger.
		cls.webhook = "EV-webhook-direct"
		if not frappe.db.exists("Webhook", cls.webhook):
			frappe.get_doc({
				"doctype": "Webhook", "name": cls.webhook, "webhook_doctype": "CRM Lead", "enabled": 0,
				"request_url": "https://example.invalid/hook", "request_method": "POST",
			}).insert(ignore_permissions=True)
		cls.webhook_rollback = "EV-webhook-rollback"
		if not frappe.db.exists("Webhook", cls.webhook_rollback):
			frappe.get_doc({
				"doctype": "Webhook", "name": cls.webhook_rollback, "webhook_doctype": "CRM Lead", "enabled": 0,
				"request_url": "https://example.invalid/hook-rollback", "request_method": "POST",
			}).insert(ignore_permissions=True)

	@classmethod
	def tearDownClass(cls):
		frappe.db.delete("CRM Lead", {"name": cls.lead.name})
		frappe.db.delete("Webhook", {"name": cls.webhook})
		frappe.db.delete("Webhook", {"name": cls.webhook_rollback})

	def _spy_enqueue(self):
		"""A pure stub (never delegates to the real `frappe.enqueue`) — the real webhook delivery path
		registers its OWN `frappe.db.after_commit` hook independent of our rule's savepoint, which would
		outlive this test's fixtures; we only need to prove call-count/timing, not real delivery."""
		calls = []
		orig = frappe.enqueue

		def spy(*args, **kwargs):
			calls.append((args, kwargs))

		frappe.enqueue = spy
		return calls, orig

	def test_call_webhook_returns_deferred_thunk_not_immediate(self):
		a = _action_row(action_type="Call Webhook", webhook_endpoint=self.webhook)
		calls, orig = self._spy_enqueue()
		try:
			thunk = actions._action_call_webhook(a, self.lead.name, {}, _AXES, self.lead)
			self.assertTrue(callable(thunk), "Call Webhook must return a deferred thunk")
			self.assertEqual(calls, [], "Call Webhook enqueued before its thunk ran — not deferred")
			thunk()
			self.assertEqual(len(calls), 1, "the thunk did not enqueue exactly once when invoked")
		finally:
			frappe.enqueue = orig

	def test_call_webhook_does_not_fire_when_a_later_effect_fails(self):
		"""Planted-bad: a rule [Call Webhook, Update Field(fails at RUNTIME)] must roll back BOTH — the
		webhook thunk must never be invoked, proving the deferred list is cleared on rollback, not just
		delayed. Same known-bad shape as test_watch_entry's allowlist recall guard: author the 2nd
		action's field WHILE allowlisted (so the rule passes validate at save time), then disable the
		allowlist row before firing — the runtime recheck (defense in depth) is what actually fails it."""
		bad_field = "custom_patient_age"
		allow_row = field_allowlist.seed_settable("CRM Lead", bad_field, _GRAIN["vertical"], _GRAIN["group"], _GRAIN["program"])
		rule = frappe.get_doc({
			"doctype": "CRM Automation Rule", "rule_name": "EV-webhook-rollback-rule", "enabled": 1,
			"on_doctype": "CRM Lead", "event": "Updated",
			"vertical": _GRAIN["vertical"], "group": _GRAIN["group"], "program": _GRAIN["program"],
			"actions": [
				{"action_type": "Call Webhook", "webhook_endpoint": self.webhook_rollback},
				{"action_type": "Update Field", "target_doctype": "CRM Lead", "fieldname": bad_field,
				 "value_mode": "Literal", "value": "99"},
			],
		}).insert(ignore_permissions=True)
		frappe.db.set_value(_FIELD, allow_row, "enabled", 0)  # stale: authoring permitted it, runtime must not
		calls, orig = self._spy_enqueue()
		try:
			dispatcher.run_effects(
				self.lead.name, versions.current_name(rule.name), self.lead, _AXES, "grain", {}, {},
			)
			self.assertEqual(calls, [], "the webhook fired despite the rule rolling back — the thunk must be deferred to commit")
			logs = frappe.get_all(_RUN_LOG, filters={"rule": rule.name}, fields=["outcome", "actions_failed"])
			self.assertTrue(logs, "no Run Log row written for the failed fire")
			self.assertIn(logs[0].outcome, ("Partial", "Failed"))
			self.assertGreater(logs[0].actions_failed, 0)
		finally:
			frappe.enqueue = orig
			frappe.db.delete(_FIELD, {"name": allow_row})
			frappe.db.delete("CRM Automation Action", {"parent": rule.name})
			frappe.db.delete("CRM Automation Rule", {"name": rule.name})
			frappe.db.delete(_RUN_LOG, {"rule": rule.name})


if __name__ == "__main__":
	unittest.main()
