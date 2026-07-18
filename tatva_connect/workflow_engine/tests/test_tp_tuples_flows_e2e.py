# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""THE parity proof for the fold: the 38 real TatvaPractice business rules, rebuilt as FLOWS on the ONE
engine, fire end-to-end with the SAME business outcome the rule engine produced. This is the gate the
plan (docs/plans/unified-flow-engine.md §11) puts before deleting the automation-rule engine: "every
seeded rule rebuilt as a Flow with byte-identical effect (before/after compared)."

The 38 tuples are a VERBATIM copy of `RULES` in the rule-engine parity suite
(tatva_connect/tests/automation/test_tp_tuples_e2e.py) and its seed
(docs/go-live/3-seed/db-seeds/2026-07-05-tp-automation-rules.bench-console.py). This suite is
SELF-CONTAINED (own copy, own fixtures) precisely because the rule-engine suite it mirrors is deleted in
the fold — the two cannot import each other across that deletion.

Each tuple maps onto a wait-free (ephemeral) Flow — a rule IS a Flow that runs once:

    Trigger: CRM Task · Updated
    When:    status changed to Done AND custom_task_type is <trigger composite> AND <field> is <value>
    Then:    Step[ Update Field custom_substage = <set_stage> (if any)
                 + Create Task <create_task composite> due From Context <due_field> (if any) ] → Terminal

The observable outcome asserted is the SAME the rule suite asserts: (a) custom_stage (the read-only rollup
`leads.validate_stage` mirrors from custom_substage), (b) the correct single follow-up task with its due
date resolved from context. The rule engine's Run Log assertion is intentionally NOT carried over — an
ephemeral Flow persists no per-run log yet (that is Phase-B observability); the business effect is the
proof. A real sends spy (never a hardcoded verdict) proves the engine never side-fires a message.

Real Frappe as the oracle: real saves, real allowlist, real activity-schema routing. Fixtures live in
this FrappeTestCase and are deleted by the `TPFlow-` naming prefix in teardown (manual-cleanup
convention). The engine never commits mid-save for these wait-free Flows, so per-test rollback also holds.
"""
import datetime
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist
from tatva_connect.tests.automation import field_allowlist
from tatva_connect.workflow_engine import ENGINE_SWITCH

_SWITCH_DT = "CRM Tatva Automation"
_DEF_DT = "CRM Workflow Definition"
_GROUP_DT = "CRM Action Group"
_INSTANCE_DT = "CRM Workflow Instance"
_PREFIX = "TPFlow-"

_GRAIN = next(g for g in GRAINS if g["key"] == "TatvaPractice::India::FieldSales")
VERTICAL, GROUP, PROGRAM = _GRAIN["vertical"], _GRAIN["group"], _GRAIN["program"]

S_DEMO = f"{PROGRAM}::Demo"
S_DROPPED = f"{PROGRAM}::Dropped Doctor"
S_ONBOARDED = f"{PROGRAM}::Doctor Onboarded"
S_ACTIVATED = f"{PROGRAM}::Doctor Activated"
BASELINE_STAGE = f"{PROGRAM}::New Doctor"

# The location backstop + capture, forced OFF so an In-Person trigger task type (Physical/Field Visit)
# is not blocked by tasks.enforce_location on its Done save — this suite is about the transition Flows,
# not the location guard (which its own PoC suite covers).
_OFF_SWITCHES = ("Task::CRM Task::guards", "Location::Google::capture")

# ---- the 38 canonical tuples (verbatim copy — keep in sync with the seed + the rule-engine suite) ----
RULES = [
	("Introductory Meeting Phone Call", "call_completed_next_steps", "Schedule Demo",   S_DEMO,    "Demo Scheduled Status Phone Call", "demo_date_time"),
	("Introductory Meeting Phone Call", "call_completed_next_steps", "Call Reschedule",  None,      "Introductory Meeting Phone Call",  "follow_up_date_time"),
	("Introductory Meeting Phone Call", "call_completed_next_steps", "Dropped",          S_DROPPED, None,                               None),
	("Introductory Meeting Phone Call", "phone_call_status",         "Intro Call Not Completed", None, "Introductory Meeting Phone Call", "follow_up_date_time"),
	("Introductory Meeting Physical Visit", "visit_completed_next_steps", "Schedule Demo",    S_DEMO,    "Demo Scheduled Status Field Visit",   "demo_scheduled_date_time"),
	("Introductory Meeting Physical Visit", "visit_completed_next_steps", "Reschedule Visit", None,      "Introductory Meeting Physical Visit", "visit_follow_up_date_time"),
	("Introductory Meeting Physical Visit", "visit_completed_next_steps", "Dropped",          S_DROPPED, None,                                  None),
	("Introductory Meeting Physical Visit", "visit_status",               "Visit Not Done", None, "Introductory Meeting Physical Visit","visit_follow_up_date_time"),
	("Demo Scheduled Status Phone Call", "outcome", "Demo Completed", None,      "Onboarding Status Phone Call",      "onboarding_date_time"),
	("Demo Scheduled Status Phone Call", "outcome", "Reschedule",     None,      "Demo Scheduled Status Phone Call",  "demo_status_followup_date_time"),
	("Demo Scheduled Status Phone Call", "outcome", "Dropped",        S_DROPPED, None,                                None),
	("Demo Scheduled Status Field Visit", "outcome", "Demo Completed", None,      "Onboarding Status Field Visit",     "onboarding_date_time"),
	("Demo Scheduled Status Field Visit", "outcome", "Reschedule",     None,      "Demo Scheduled Status Field Visit", "demo_status_followup_date_time"),
	("Demo Scheduled Status Field Visit", "outcome", "Dropped",        S_DROPPED, None,                                None),
	("Onboarding Status Phone Call", "outcome", "Doctor Onboarded",     S_ONBOARDED, "Doctor Training Phone Call",  "schedule_training"),
	("Onboarding Status Phone Call", "outcome", "Follow up to Onboard", None,        "Onboarding Status Phone Call","follow_up_onboard"),
	("Onboarding Status Phone Call", "outcome", "Dropped",              S_DROPPED,   None,                          None),
	("Onboarding Status Field Visit", "outcome", "Doctor Onboarded",     S_ONBOARDED, "Doctor Training Field Visit",  "schedule_training"),
	("Onboarding Status Field Visit", "outcome", "Follow up to Onboard", None,        "Onboarding Status Field Visit","follow_up_onboard"),
	("Onboarding Status Field Visit", "outcome", "Dropped",              S_DROPPED,   None,                           None),
	("Doctor Training Phone Call", "training_status", "Training Completed",   None,      "Doctor Activation Phone Call", "doctor_activation_date_time"),
	("Doctor Training Phone Call", "training_status", "Training Rescheduled", None,      "Doctor Training Phone Call",   "training_reschedule_date_time"),
	("Doctor Training Phone Call", "training_status", "Dropped",              S_DROPPED, None,                           None),
	("Doctor Training Field Visit", "training_status", "Training Completed",   None,      "Doctor Activation Field Visit", "doctor_activation_date_time"),
	("Doctor Training Field Visit", "training_status", "Training Rescheduled", None,      "Doctor Training Field Visit",   "training_reschedule_date_time"),
	("Doctor Training Field Visit", "training_status", "Dropped",              S_DROPPED, None,                            None),
	("Doctor Activation Phone Call", "doctor_activation_status", "Completed",  S_ACTIVATED, "Courtesy Visit Phone Call",     "next_visit_date_time"),
	("Doctor Activation Phone Call", "doctor_activation_status", "Reschedule", None,        "Doctor Activation Phone Call",  "reschedule_date_time"),
	("Doctor Activation Phone Call", "doctor_activation_status", "Dropped",    S_DROPPED,   None,                            None),
	("Doctor Activation Field Visit", "doctor_activation_status", "Completed",  S_ACTIVATED, "Courtesy Visit Field Visit",    "next_visit_date_time"),
	("Doctor Activation Field Visit", "doctor_activation_status", "Reschedule", None,        "Doctor Activation Field Visit", "reschedule_date_time"),
	("Doctor Activation Field Visit", "doctor_activation_status", "Dropped",    S_DROPPED,   None,                            None),
	("Courtesy Visit Phone Call", "visit_status", "Visit Completed",   None,      "Courtesy Visit Phone Call", "next_visit_date_time"),
	("Courtesy Visit Phone Call", "visit_status", "Visit Rescheduled", None,      "Courtesy Visit Phone Call", "reschedule_date_time"),
	("Courtesy Visit Phone Call", "visit_status", "Dropped",           S_DROPPED, None,                        None),
	("Courtesy Visit Field Visit", "visit_status", "Visit Completed",   None,      "Courtesy Visit Field Visit", "next_visit_date_time"),
	("Courtesy Visit Field Visit", "visit_status", "Visit Rescheduled", None,      "Courtesy Visit Field Visit", "reschedule_date_time"),
	("Courtesy Visit Field Visit", "visit_status", "Dropped",           S_DROPPED, None,                         None),
]
assert len(RULES) == 38, f"expected 38 canonical tuples, found {len(RULES)}"

_RECALL_FAMILY_INDEXES = {
	"call_completed_next_steps": 0, "phone_call_status": 3, "visit_completed_next_steps": 4,
	"visit_status": 7, "outcome": 8, "training_status": 20, "doctor_activation_status": 26,
}
_DUE_BASE = datetime.datetime(2026, 8, 1, 10, 0, 0)


def _tt(bare):
	return f"{VERTICAL}::{GROUP}::{PROGRAM}::{bare}" if bare else None


def _build_flow(idx, trigger, field, value, set_stage, create_task, due_from):
	"""One ephemeral Flow from a tuple: When = the three AND-ed criteria; Then = a single Step whose
	Action Group carries the Update Field and/or Create Task effect(s), then Terminal."""
	items = []
	if set_stage:
		items.append({
			"action_type": "Update Field", "target_doctype": "CRM Lead",
			"fieldname": "custom_substage", "value_mode": "Literal", "value": set_stage,
		})
	if create_task:
		items.append({
			"action_type": "Create Task", "task_type": _tt(create_task),
			"due_mode": "From Context", "due_from": due_from,
		})
	group = frappe.get_doc({"doctype": _GROUP_DT, "group_name": f"{_PREFIX}ag-{idx:02d}", "actions": items}).insert(ignore_permissions=True).name
	return frappe.get_doc({
		"doctype": _DEF_DT, "workflow_name": f"{_PREFIX}{idx:02d}", "enabled": 1,
		"vertical": VERTICAL, "group": GROUP, "program": PROGRAM,
		"entry_doctype": "CRM Task", "entry_event": "Updated",
		"criteria": [
			{"field": "status", "operator": "changed to", "value": "Done"},
			{"field": "custom_task_type", "operator": "is", "value": _tt(trigger)},
			{"field": field, "operator": "is", "value": value},
		],
		"nodes": [
			{"node_id": "n1", "node_type": "Step", "action_group": group, "next_node": "end"},
			{"node_id": "end", "node_type": "Terminal"},
		],
	}).insert(ignore_permissions=True).name


def _make_lead(idx):
	return frappe.get_doc({
		"doctype": "CRM Lead", "first_name": "TPFlow", "lead_name": f"TPFlow Probe {idx}", "status": "New",
		"custom_vertical": VERTICAL, "custom_group": GROUP, "custom_current_program": PROGRAM,
		"custom_substage": BASELINE_STAGE,
	}).insert(ignore_permissions=True)


def _make_trigger_task(lead_name, task_type):
	task = frappe.get_doc({
		"doctype": "CRM Task", "title": "TPFlow trigger", "assigned_to": "Administrator",
		"reference_doctype": "CRM Lead", "reference_docname": lead_name,
		"status": "Todo", "custom_task_type": task_type,
	}).insert(ignore_permissions=True)
	task.reload()  # native after_insert assign bumped `modified` on the DB row
	return task


def _activity_target(task_type, fieldname):
	return frappe.db.get_value(
		"CRM Task Type Field",
		{"parent": task_type, "parenttype": "CRM Task Type", "fieldname": fieldname}, "target",
	) or ""


def _apply_activity_field(task, task_type, fieldname, value):
	target = _activity_target(task_type, fieldname)
	if target:
		task.set(target, value)
		return
	payload = frappe.parse_json(task.custom_activity_payload or "{}") or {}
	payload[fieldname] = value
	task.custom_activity_payload = frappe.as_json(payload)


def _new_tasks_for(lead_name, exclude):
	return frappe.get_all(
		"CRM Task",
		filters={"reference_doctype": "CRM Lead", "reference_docname": lead_name, "name": ["!=", exclude]},
		fields=["name", "custom_task_type", "due_date"],
	)


class TestTpTuplesAsFlows(FrappeTestCase):
	"""THE 38-tuple parity proof on the unified Flow engine + a metamorphic recall guard."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		for stage in (S_DEMO, S_DROPPED, S_ONBOARDED, S_ACTIVATED, BASELINE_STAGE):
			if not frappe.db.exists("CRM Lead Stage", stage):
				frappe.throw(f"prereq CRM Lead Stage {stage!r} does not exist — seed tp-04 first")
		for _, _f, _v, _s, create_task, _d in RULES:
			if create_task and not frappe.db.exists("CRM Task Type", _tt(create_task)):
				frappe.throw(f"prereq CRM Task Type {_tt(create_task)!r} does not exist — seed tp-04 first")

		cls._prior = {k: frappe.db.get_value(_SWITCH_DT, k, "enabled")
		              for k in (ENGINE_SWITCH, "Lead::CRM Lead::stage", *_OFF_SWITCHES)}
		frappe.db.set_value(_SWITCH_DT, ENGINE_SWITCH, "enabled", 1)
		frappe.db.set_value(_SWITCH_DT, "Lead::CRM Lead::stage", "enabled", 1)  # custom_stage mirror-derivation
		for k in _OFF_SWITCHES:
			frappe.db.set_value(_SWITCH_DT, k, "enabled", 0)

		cls.tp_watch_fields = sorted({f for _t, f, *_ in RULES})
		# status is a watchable native Task column now (CRM Task Field, seeded by patch), so the flow uses
		# `changed to Done`: the dispatcher captures status's before-value and the rule fires ONLY on the
		# not-Done→Done transition — once, never on a re-save of an already-Done task.
		field_allowlist.seed_settable("CRM Lead", "custom_substage", VERTICAL, GROUP, PROGRAM)
		frappe.flags.pop("_watchable_fields_cache", None)

		cls.flows = [_build_flow(i, *tup) for i, tup in enumerate(RULES)]
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		lead_names = frappe.get_all("CRM Lead", filters={"first_name": "TPFlow"}, pluck="name")
		if lead_names:
			frappe.db.delete("CRM Task", {"reference_doctype": "CRM Lead", "reference_docname": ("in", lead_names)})
			frappe.db.delete("CRM Lead", {"name": ("in", lead_names)})
		for name in cls.flows:
			frappe.db.delete("CRM Workflow Version", {"workflow": name})
			frappe.db.delete(_DEF_DT, {"name": name})
		frappe.db.delete("CRM Action Group Item", {"parent": ("like", f"{_PREFIX}%")})
		frappe.db.delete(_GROUP_DT, {"group_name": ("like", f"{_PREFIX}%")})
		field_allowlist.clear()
		for k, v in cls._prior.items():
			frappe.db.set_value(_SWITCH_DT, k, "enabled", v or 0)
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.flags.in_test = True

	def tearDown(self):
		frappe.flags.in_test = False

	def _spy_sends(self):
		import tatva_connect.automation.sends as sends_mod

		calls = {"whatsapp": 0, "email": 0}
		orig_wa, orig_email = sends_mod.send_whatsapp, sends_mod.send_email

		def _wa(*a, **kw):
			calls["whatsapp"] += 1
			return orig_wa(*a, **kw)

		def _email(*a, **kw):
			calls["email"] += 1
			return orig_email(*a, **kw)

		sends_mod.send_whatsapp, sends_mod.send_email = _wa, _email
		return calls, (sends_mod, orig_wa, orig_email)

	def _unspy_sends(self, patched):
		sends_mod, orig_wa, orig_email = patched
		sends_mod.send_whatsapp, sends_mod.send_email = orig_wa, orig_email

	def test_all_38_tuples_fire_correctly(self):
		calls, patched = self._spy_sends()
		try:
			for idx, (trigger, field, value, set_stage, create_task, due_from) in enumerate(RULES):
				with self.subTest(idx=idx, trigger=trigger, field=field, value=value):
					self._fire_and_assert(idx, trigger, field, value, set_stage, create_task, due_from)
		finally:
			self._unspy_sends(patched)
		self.assertEqual(calls["whatsapp"], 0, "a tuple fire reached the WhatsApp adapter")
		self.assertEqual(calls["email"], 0, "a tuple fire reached the email adapter")

	def test_fire_once_on_done_transition_not_on_resave(self):
		"""Fire-once proof — the whole point of the native-Task watchable home. A not-Done→Done TRANSITION
		fires the rule once; re-saving the already-Done task does NOT re-fire (status is watchable via
		CRM Task Field, so `changed to Done` sees the before-value and matches only on the transition)."""
		idx = next(i for i, r in enumerate(RULES) if r[4])  # first tuple that creates a follow-up
		trigger, field, value, set_stage, create_task, _due = RULES[idx]
		followup_type = _tt(create_task)
		lead = _make_lead(f"fireonce-{idx}")
		task = _make_trigger_task(lead.name, _tt(trigger))
		_apply_activity_field(task, _tt(trigger), field, value)

		task.status = "Done"
		task.save(ignore_permissions=True)  # the not-Done → Done transition
		first = [t for t in _new_tasks_for(lead.name, exclude=task.name) if t.custom_task_type == followup_type]
		self.assertEqual(len(first), 1, "the Done transition must fire the rule exactly ONCE")

		# Re-save the ALREADY-Done task (a real edit, status unchanged) — no transition, so it must NOT re-fire.
		task.reload()
		task.title = "TPFlow trigger (re-saved)"
		task.save(ignore_permissions=True)
		again = [t for t in _new_tasks_for(lead.name, exclude=task.name) if t.custom_task_type == followup_type]
		self.assertEqual(len(again), 1, "re-saving an already-Done task must NOT create a second follow-up")

	def _fire_and_assert(self, idx, trigger, field, value, set_stage, create_task, due_from):
		task_type = _tt(trigger)
		lead = _make_lead(f"{idx}")
		task = _make_trigger_task(lead.name, task_type)
		due_at = None
		_apply_activity_field(task, task_type, field, value)
		if create_task and due_from:
			due_at = _DUE_BASE + datetime.timedelta(hours=idx)
			_apply_activity_field(task, task_type, due_from, due_at)
		task.status = "Done"
		task.save(ignore_permissions=True)

		stage_after = frappe.db.get_value("CRM Lead", lead.name, "custom_stage")
		if set_stage:
			self.assertEqual(stage_after, set_stage, f"tuple {idx}: custom_stage was not set to {set_stage!r}")
		else:
			self.assertEqual(stage_after, BASELINE_STAGE, f"tuple {idx}: custom_stage moved when it should not have")

		followups = _new_tasks_for(lead.name, exclude=task.name)
		if create_task:
			matches = [t for t in followups if t.custom_task_type == _tt(create_task)]
			self.assertTrue(matches, f"tuple {idx}: expected follow-up task {create_task!r} was not created")
			self.assertEqual(len(matches), 1, f"tuple {idx}: more than one follow-up task was created")
			if due_at is not None:
				self.assertEqual(
					frappe.utils.get_datetime(matches[0].due_date), frappe.utils.get_datetime(due_at),
					f"tuple {idx}: follow-up due date was not resolved from {due_from!r}",
				)
		else:
			self.assertFalse(followups, f"tuple {idx}: a follow-up task was created despite create_task=None")

		# ephemeral parity: a rule-shaped Flow persists no Instance
		self.assertEqual(
			frappe.get_all(_INSTANCE_DT, filters={"subject_name": task.name}, pluck="name"), [],
			f"tuple {idx}: a wait-free Flow left an Instance behind",
		)

	def test_recall_guard_one_planted_bad_per_criterion_family(self):
		for family, idx in _RECALL_FAMILY_INDEXES.items():
			with self.subTest(family=family, idx=idx):
				self._assert_planted_bad_does_not_fire(idx)

	def _assert_planted_bad_does_not_fire(self, idx):
		trigger, field, _value, _set_stage, _create_task, _due_from = RULES[idx]
		task_type = _tt(trigger)
		lead = _make_lead(f"recall-{idx}")
		task = _make_trigger_task(lead.name, task_type)
		_apply_activity_field(task, task_type, field, "TPFLOW-RECALL-NO-SUCH-VALUE")
		task.status = "Done"
		task.save(ignore_permissions=True)

		self.assertEqual(
			frappe.db.get_value("CRM Lead", lead.name, "custom_stage"), BASELINE_STAGE,
			f"recall {idx} ({field}): stage moved on a non-matching value",
		)
		self.assertFalse(
			_new_tasks_for(lead.name, exclude=task.name),
			f"recall {idx} ({field}): a follow-up task was created on a non-matching value",
		)


if __name__ == "__main__":
	unittest.main()
