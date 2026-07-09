# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Task 13 - THE make-or-break proof: the assembled v2 engine fires the REAL tatvapractice business
rules end-to-end. The 38 tuples below are a VERBATIM copy of the `RULES` list in
`docs/go-live/3-seed/db-seeds/2026-07-05-tp-automation-rules.bench-console.py` (docs/ is gitignored -
this suite cannot import that script, so the two lists are kept in sync BY HAND; a change to one must
mirror in the other). Each tuple maps onto the v2 vocabulary per the plan's Task 13 mapping:

    On CRM Task · Updated
    If status changed to Done AND custom_task_type is <trigger composite> AND <field> is <value>
    Then Update Field custom_substage = <set_stage> (only if set_stage)
       + Create Task <create_task composite> due From Context <due_field> (only if create_task)

Driving these 38 tuples through the REAL router/executor surfaced TWO genuine engine bugs no earlier
task's narrower suite could have caught - both fixed alongside this test, never papered over:

  (1) CRM Task's real business fields (outcome/training_status/call_completed_next_steps/...) are NOT
      doctype meta fields - they are per-task-type activity SCHEMA fields (`CRM Task Type Field`) that
      `activity.api.compute_activity` either promotes onto one of the 9 shared columns or folds into
      `custom_activity_payload` JSON. A criterion authored against one of them could neither be VALIDATED
      (`fields_for_doctype` only walked the meta) nor EVALUATED (`router._context_for` only exposed
      `doc.get_valid_dict()`) nor ALLOWLISTED (`CRMAutomationField._require_real_field` rejected it as
      "no such field"). Fixed by unioning the activity-schema vocabulary into all three seams
      (`describe.activity_schema_fields`/`fields_for_doctype`, `crm_automation_field._require_real_field`,
      `router._activity_values`/`_context_for`) - reusing the ONE existing brain
      (`activity.api._task_values` + `_type_config`, the exact merge the Lead task board already renders
      from), never a second payload parser (A.8).
  (2) `custom_stage` (what the old v1 seed set) is a READ-ONLY parent rollup `leads.validate_stage`
      auto-derives from `custom_substage` on every CRM Lead save - the allowlist explicitly forbids
      setting it directly (proven already by
      test_effect_verbs.TestUpdateFieldDerivesStage.test_update_field_on_non_allowlisted_field_raises_and_does_not_write).
      The REAL settable field is `custom_substage` (the leaf a rep/automation picks); for a FLAT grain
      like TatvaPractice (no two-level ladder), `validate_stage` mirrors it straight into `custom_stage`,
      so asserting `custom_stage` post-fire (as this suite does, matching the brief) still proves the
      right thing happened - the ACTION just has to target `custom_substage`, matching
      TestUpdateFieldDerivesStage's own proof. The seed rewrite carries this fix too.

Real Frappe engine as the oracle throughout - real saves, real allowlist, real Run Log rows, a real
spy on the WhatsApp/email adapters (S.6, never a hardcoded verdict). Every fixture row is created
INSIDE this FrappeTestCase (setUp/tearDown, hard safety constraint - see task-13-brief.md) and
explicitly deleted in tearDownClass by this suite's own `TP38Probe-` naming prefix, matching this
repo's established manual-cleanup convention for this test base class (test_report.py/test_watch_entry.py).
"""
import datetime
import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import router
from tatva_connect.tests.authz.grains import GRAINS, assert_masters_exist
from tatva_connect.tests.automation import field_allowlist

_DT = "CRM Automation Rule"
_RUN_LOG = "CRM Automation Run Log"
_PREFIX = "TP38Probe-"

_GRAIN = next(g for g in GRAINS if g["key"] == "TatvaPractice::India::FieldSales")
VERTICAL, GROUP, PROGRAM = _GRAIN["vertical"], _GRAIN["group"], _GRAIN["program"]

# Set-Field targets - CRM Lead Stage composite keys `{program}::{stage}` - must exist (asserted below).
S_DEMO = f"{PROGRAM}::Demo"
S_DROPPED = f"{PROGRAM}::Dropped Doctor"
S_ONBOARDED = f"{PROGRAM}::Doctor Onboarded"
S_ACTIVATED = f"{PROGRAM}::Doctor Activated"
BASELINE_STAGE = f"{PROGRAM}::New Doctor"  # a real stage distinct from all 4 above - proves "unchanged"

# ============================================================================================
# VERBATIM copy of RULES in docs/go-live/3-seed/db-seeds/2026-07-05-tp-automation-rules.bench-console.py
# (trigger_task_type, criterion_field, criterion_value, set_stage_or_None, create_task_or_None, due_from_field_or_None)
# ============================================================================================
RULES = [
	# --- Introductory Meeting — Phone Call ---------------------------------
	("Introductory Meeting Phone Call", "call_completed_next_steps", "Schedule Demo",   S_DEMO,    "Demo Scheduled Status Phone Call", "demo_date_time"),
	("Introductory Meeting Phone Call", "call_completed_next_steps", "Call Reschedule",  None,      "Introductory Meeting Phone Call",  "follow_up_date_time"),
	("Introductory Meeting Phone Call", "call_completed_next_steps", "Dropped",          S_DROPPED, None,                               None),
	("Introductory Meeting Phone Call", "phone_call_status",         "Intro Call Not Completed", None, "Introductory Meeting Phone Call", "follow_up_date_time"),
	# --- Introductory Meeting — Physical Visit -----------------------------
	("Introductory Meeting Physical Visit", "visit_completed_next_steps", "Schedule Demo",    S_DEMO,    "Demo Scheduled Status Field Visit",   "demo_scheduled_date_time"),
	("Introductory Meeting Physical Visit", "visit_completed_next_steps", "Reschedule Visit", None,      "Introductory Meeting Physical Visit", "visit_follow_up_date_time"),
	("Introductory Meeting Physical Visit", "visit_completed_next_steps", "Dropped",          S_DROPPED, None,                                  None),
	("Introductory Meeting Physical Visit", "visit_status",               "Visit Not Done", None, "Introductory Meeting Physical Visit","visit_follow_up_date_time"),
	# --- Demo Scheduled Status — Phone Call --------------------------------
	("Demo Scheduled Status Phone Call", "outcome", "Demo Completed", None,      "Onboarding Status Phone Call",      "onboarding_date_time"),
	("Demo Scheduled Status Phone Call", "outcome", "Reschedule",     None,      "Demo Scheduled Status Phone Call",  "demo_status_followup_date_time"),
	("Demo Scheduled Status Phone Call", "outcome", "Dropped",        S_DROPPED, None,                                None),
	# --- Demo Scheduled Status — Field Visit -------------------------------
	("Demo Scheduled Status Field Visit", "outcome", "Demo Completed", None,      "Onboarding Status Field Visit",     "onboarding_date_time"),
	("Demo Scheduled Status Field Visit", "outcome", "Reschedule",     None,      "Demo Scheduled Status Field Visit", "demo_status_followup_date_time"),
	("Demo Scheduled Status Field Visit", "outcome", "Dropped",        S_DROPPED, None,                                None),
	# --- Onboarding Status — Phone Call ------------------------------------
	("Onboarding Status Phone Call", "outcome", "Doctor Onboarded",     S_ONBOARDED, "Doctor Training Phone Call",  "schedule_training"),
	("Onboarding Status Phone Call", "outcome", "Follow up to Onboard", None,        "Onboarding Status Phone Call","follow_up_onboard"),
	("Onboarding Status Phone Call", "outcome", "Dropped",              S_DROPPED,   None,                          None),
	# --- Onboarding Status — Field Visit -----------------------------------
	("Onboarding Status Field Visit", "outcome", "Doctor Onboarded",     S_ONBOARDED, "Doctor Training Field Visit",  "schedule_training"),
	("Onboarding Status Field Visit", "outcome", "Follow up to Onboard", None,        "Onboarding Status Field Visit","follow_up_onboard"),
	("Onboarding Status Field Visit", "outcome", "Dropped",              S_DROPPED,   None,                           None),
	# --- Doctor Training — Phone Call --------------------------------------
	("Doctor Training Phone Call", "training_status", "Training Completed",   None,      "Doctor Activation Phone Call", "doctor_activation_date_time"),
	("Doctor Training Phone Call", "training_status", "Training Rescheduled", None,      "Doctor Training Phone Call",   "training_reschedule_date_time"),
	("Doctor Training Phone Call", "training_status", "Dropped",              S_DROPPED, None,                           None),
	# --- Doctor Training — Field Visit -------------------------------------
	("Doctor Training Field Visit", "training_status", "Training Completed",   None,      "Doctor Activation Field Visit", "doctor_activation_date_time"),
	("Doctor Training Field Visit", "training_status", "Training Rescheduled", None,      "Doctor Training Field Visit",   "training_reschedule_date_time"),
	("Doctor Training Field Visit", "training_status", "Dropped",              S_DROPPED, None,                            None),
	# --- Doctor Activation — Phone Call ------------------------------------
	("Doctor Activation Phone Call", "doctor_activation_status", "Completed",  S_ACTIVATED, "Courtesy Visit Phone Call",     "next_visit_date_time"),
	("Doctor Activation Phone Call", "doctor_activation_status", "Reschedule", None,        "Doctor Activation Phone Call",  "reschedule_date_time"),
	("Doctor Activation Phone Call", "doctor_activation_status", "Dropped",    S_DROPPED,   None,                            None),
	# --- Doctor Activation — Field Visit -----------------------------------
	("Doctor Activation Field Visit", "doctor_activation_status", "Completed",  S_ACTIVATED, "Courtesy Visit Field Visit",    "next_visit_date_time"),
	("Doctor Activation Field Visit", "doctor_activation_status", "Reschedule", None,        "Doctor Activation Field Visit", "reschedule_date_time"),
	("Doctor Activation Field Visit", "doctor_activation_status", "Dropped",    S_DROPPED,   None,                            None),
	# --- Courtesy Visit — Phone Call ---------------------------------------
	("Courtesy Visit Phone Call", "visit_status", "Visit Completed",   None,      "Courtesy Visit Phone Call", "next_visit_date_time"),
	("Courtesy Visit Phone Call", "visit_status", "Visit Rescheduled", None,      "Courtesy Visit Phone Call", "reschedule_date_time"),
	("Courtesy Visit Phone Call", "visit_status", "Dropped",           S_DROPPED, None,                        None),
	# --- Courtesy Visit — Field Visit --------------------------------------
	("Courtesy Visit Field Visit", "visit_status", "Visit Completed",   None,      "Courtesy Visit Field Visit", "next_visit_date_time"),
	("Courtesy Visit Field Visit", "visit_status", "Visit Rescheduled", None,      "Courtesy Visit Field Visit", "reschedule_date_time"),
	("Courtesy Visit Field Visit", "visit_status", "Dropped",           S_DROPPED, None,                         None),
]

assert len(RULES) == 38, f"expected 38 canonical tuples, found {len(RULES)} - the mirrored list drifted"

# One representative tuple index per DISTINCT criterion-field family, for the metamorphic recall guard
# (Step 3 of the brief) - a planted-bad per family so recall==1.0 on non-fires, not just the happy path.
_RECALL_FAMILY_INDEXES = {
	"call_completed_next_steps": 0,
	"phone_call_status": 3,
	"visit_completed_next_steps": 4,
	"visit_status": 7,
	"outcome": 8,
	"training_status": 20,
	"doctor_activation_status": 26,
}
assert {RULES[i][1] for i in _RECALL_FAMILY_INDEXES.values()} == set(_RECALL_FAMILY_INDEXES), (
	"a recall-family index no longer points at the field family it's named for - RULES was reordered"
)

_DUE_BASE = datetime.datetime(2026, 8, 1, 10, 0, 0)


def _tt(bare):
	"""A bare task-type name -> its grain composite PK (A.7) - mirrors the seed's own `_tt()`."""
	return f"{VERTICAL}::{GROUP}::{PROGRAM}::{bare}" if bare else None


def _rule_name(idx):
	return f"{_PREFIX}{idx:02d}"


def _build_rule(idx, trigger, field, value, set_stage, create_task, due_from):
	"""One v2 rule from a tuple, per the Task 13 mapping (module docstring)."""
	criteria = [
		{"field": "status", "operator": "changed to", "value": "Done"},
		{"field": "custom_task_type", "operator": "is", "value": _tt(trigger)},
		{"field": field, "operator": "is", "value": value},
	]
	actions = []
	if set_stage:
		actions.append({
			"action_type": "Update Field", "target_doctype": "CRM Lead",
			"fieldname": "custom_substage", "value_mode": "Literal", "value": set_stage,
		})
	if create_task:
		actions.append({
			"action_type": "Create Task", "task_type": _tt(create_task),
			"due_mode": "From Context", "due_from": due_from,
		})
	return frappe.get_doc({
		"doctype": _DT, "rule_name": _rule_name(idx), "enabled": 1, "priority": 0,
		"on_doctype": "CRM Task", "event": "Updated",
		"vertical": VERTICAL, "group": GROUP, "program": PROGRAM,
		"description": f"TP38 probe {idx}: {trigger} / {field}={value}",
		"criteria": criteria, "actions": actions,
	}).insert(ignore_permissions=True)


def _make_lead(idx):
	# NB: native CRMLead.set_full_name() unconditionally overwrites lead_name from first_name (+ last/
	# middle/salutation) on every save - passing an idx-specific lead_name here is pointless, it never
	# survives insert. Cleanup (tearDownClass) filters by first_name instead, for exactly this reason.
	return frappe.get_doc({
		"doctype": "CRM Lead", "first_name": "TP38", "lead_name": f"TP38 Probe {idx}", "status": "New",
		"custom_vertical": VERTICAL, "custom_group": GROUP, "custom_current_program": PROGRAM,
		"custom_substage": BASELINE_STAGE,
	}).insert(ignore_permissions=True)


def _make_trigger_task(lead_name, task_type):
	"""Native `CRMTask.after_insert` auto-assigns (`assign_to.add`), which writes `_assign` straight
	to the DB row (bumping `modified`) without touching this in-memory doc - `reload()` before the
	caller mutates + re-saves it, or the second `.save()` raises TimestampMismatchError (same reason a
	real client always re-fetches a task before editing it)."""
	task = frappe.get_doc({
		"doctype": "CRM Task", "title": "TP38 trigger", "assigned_to": "Administrator",
		"reference_doctype": "CRM Lead", "reference_docname": lead_name,
		"status": "Todo", "custom_task_type": task_type,
	}).insert(ignore_permissions=True)
	task.reload()
	return task


def _activity_target(task_type, fieldname):
	"""The CRM Task column this activity-schema field promotes to, or "" for payload-only - read
	straight off the REAL tp-04 schema (read-only), never hand-transcribed, so this suite stays
	correct even if tp-04's promoted-column choices ever change. Mirrors `compute_activity`'s own
	`target or f.fieldname` routing rule (never a second copy of that decision)."""
	return frappe.db.get_value(
		"CRM Task Type Field",
		{"parent": task_type, "parenttype": "CRM Task Type", "fieldname": fieldname},
		"target",
	) or ""


def _apply_activity_field(task, task_type, fieldname, value):
	"""Simulate ONE submitted activity-form field landing on a CRM Task exactly as
	`activity.api.compute_activity` would route it: onto its promoted column, or into the
	`custom_activity_payload` JSON when the schema field carries no target."""
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


class TestTpTuplesEndToEnd(FrappeTestCase):
	"""THE 38-tuple proof + the metamorphic recall guard (Task 13)."""

	@classmethod
	def setUpClass(cls):
		assert_masters_exist()
		for stage in (S_DEMO, S_DROPPED, S_ONBOARDED, S_ACTIVATED, BASELINE_STAGE):
			if not frappe.db.exists("CRM Lead Stage", stage):
				frappe.throw(f"prereq CRM Lead Stage {stage!r} does not exist on this site - seed tp-04 first")
		for _, _f, _v, _s, create_task, _d in RULES:
			if create_task and not frappe.db.exists("CRM Task Type", _tt(create_task)):
				frappe.throw(f"prereq CRM Task Type {_tt(create_task)!r} does not exist - seed tp-04 first")

		# Engine on for this suite's own transaction only (A.17 - a fresh DB ships with both gates off).
		frappe.db.set_value("CRM Tatva Automation", router.KILL_SWITCH, "enabled", 1)
		# custom_stage's mirror-derivation must actually run so asserting on it (per the brief) is
		# meaningful - see TestUpdateFieldDerivesStage in test_effect_verbs.py for the same wiring.
		frappe.db.set_value("CRM Tatva Automation", "Lead::CRM Lead::stage", "enabled", 1)

		# Criterion allowlist, mirroring the seed: `status` is WATCHED (the completion signal the router
		# diffs, and the only field a `changed to` may test); `custom_task_type` and the 7 activity-schema
		# criterion fields are merely READABLE (no save can diff a field that is not a column).
		# `status`/`custom_task_type` are shared with other automation test files (test_guard_verbs.py,
		# test_watch_entry.py, test_two_lane.py all seed them too, same idempotent blank-grain row) -
		# left in place on teardown (harmless dormant registration, matches this repo's convention of
		# only tearing down allowlist rows exclusively owned by one suite). The 7 TP-specific criterion
		# fields below are unique to this suite - torn down explicitly.
		cls.tp_watch_fields = sorted({f for _t, f, *_ in RULES})
		field_allowlist.seed_watchable("CRM Task", "status")
		for fieldname in {"custom_task_type"} | set(cls.tp_watch_fields):
			field_allowlist.seed_readable("CRM Task", fieldname)
		# can_set allowlist: the REAL settable stage field (custom_substage, NOT the derived
		# custom_stage - see the module docstring's root-cause (2)).
		field_allowlist.seed_settable("CRM Lead", "custom_substage", VERTICAL, GROUP, PROGRAM)
		# A prior test file in this same process may have cached CRM Task's watchable set BEFORE the
		# rows above existed (frappe.flags is process-scoped under this test runner, not
		# request-scoped) - drop it so this suite observes the fresh seed (see test_two_lane.py).
		frappe.flags.pop("_watchable_fields_cache", None)

		cls.rules = [_build_rule(i, *tup) for i, tup in enumerate(RULES)]

	@classmethod
	def tearDownClass(cls):
		lead_names = frappe.get_all("CRM Lead", filters={"first_name": "TP38"}, pluck="name")
		if lead_names:
			frappe.db.delete("CRM Task", {"reference_doctype": "CRM Lead", "reference_docname": ("in", lead_names)})
			frappe.db.delete("CRM Lead", {"name": ("in", lead_names)})
		frappe.db.delete(_RUN_LOG, {"rule": ("like", f"{_PREFIX}%")})
		frappe.db.delete("CRM Automation Action", {"parent": ("like", f"{_PREFIX}%")})
		frappe.db.delete("CRM Automation Criterion", {"parent": ("like", f"{_PREFIX}%")})
		frappe.db.delete(_DT, {"rule_name": ("like", f"{_PREFIX}%")})
		frappe.db.delete(field_allowlist.DOCTYPE, {"doctype_name": "CRM Task", "fieldname": ("in", cls.tp_watch_fields)})
		frappe.db.delete(field_allowlist.DOCTYPE, {
			"doctype_name": "CRM Lead", "fieldname": "custom_substage",
			"vertical": VERTICAL, "group": GROUP, "program": PROGRAM,
		})
		router.clear_live_doctypes_cache()

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.flags.in_test = True  # synchronous enqueue - the effect lane runs inside .save()

	def tearDown(self):
		frappe.flags.in_test = False
		router.clear_live_doctypes_cache()

	def _spy_sends(self):
		"""S.6/Part D - a real spy (never a hardcoded verdict) proving no tuple's fire ever reaches the
		WhatsApp/email adapters (none of the 38 tuples use Send WhatsApp/Send Email, but the engine is
		the thing under test, not the tuple list - this proves the engine itself never side-fires one)."""
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

	# -- (1) the 38-tuple proof --------------------------------------------------------------

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

		# (a) stage
		stage_after = frappe.db.get_value("CRM Lead", lead.name, "custom_stage")
		if set_stage:
			self.assertEqual(stage_after, set_stage, f"tuple {idx}: custom_stage was not set to {set_stage!r}")
		else:
			self.assertEqual(stage_after, BASELINE_STAGE, f"tuple {idx}: custom_stage moved when it should not have")

		# (b) follow-up task
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

		# (c) Run Log
		logs = frappe.get_all(_RUN_LOG, filters={"rule": _rule_name(idx)}, fields=["outcome"])
		self.assertTrue(logs, f"tuple {idx}: no Run Log row written")
		self.assertEqual(len(logs), 1, f"tuple {idx}: more than one Run Log row - a stray rule cross-fired")
		self.assertEqual(logs[0].outcome, "Success", f"tuple {idx}: fire did not succeed: {logs[0]}")

	# -- (2) metamorphic recall guard --------------------------------------------------------

	def test_recall_guard_one_planted_bad_per_criterion_family(self):
		"""For one tuple per DISTINCT criterion-field family, mutate the criterion value to something
		that matches none of that task type's rules -> the rule must NOT fire: no stage move, no
		follow-up task, no Run Log row. Proves the engine isn't blind (S.6) across every field family
		the 38 tuples actually use, not just the happy path."""
		for family, idx in _RECALL_FAMILY_INDEXES.items():
			with self.subTest(family=family, idx=idx):
				self._assert_planted_bad_does_not_fire(idx)

	def _assert_planted_bad_does_not_fire(self, idx):
		trigger, field, _value, _set_stage, _create_task, _due_from = RULES[idx]
		task_type = _tt(trigger)
		lead = _make_lead(f"recall-{idx}")
		task = _make_trigger_task(lead.name, task_type)
		_apply_activity_field(task, task_type, field, "TP38-RECALL-NO-SUCH-VALUE")
		task.status = "Done"
		task.save(ignore_permissions=True)

		stage_after = frappe.db.get_value("CRM Lead", lead.name, "custom_stage")
		self.assertEqual(stage_after, BASELINE_STAGE, f"recall {idx} ({field}): stage moved on a non-matching value")
		followups = _new_tasks_for(lead.name, exclude=task.name)
		self.assertFalse(followups, f"recall {idx} ({field}): a follow-up task was created on a non-matching value")
		# Scoped to THIS recall task's own trigger_docname, not just the rule - the happy-path suite
		# (test_all_38_tuples_fire_correctly, runs first alphabetically) already wrote a LEGITIMATE Run
		# Log row for this same rule on ITS OWN task; a bare rule-name filter would wrongly catch that.
		logs = frappe.get_all(_RUN_LOG, filters={"rule": _rule_name(idx), "trigger_docname": str(task.name)}, fields=["name"])
		self.assertFalse(logs, f"recall {idx} ({field}): a Run Log row was written on a non-matching value")


if __name__ == "__main__":
	unittest.main()
