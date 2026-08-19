# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Every effect verb DECLARES the record it acts on, and one resolver answers for all of them.

Four verbs answered this question four different ways and nothing declared any of them. `Update Field`
honoured the author's `target_doctype`; `Create Note`, `Assign to User` and the two child-row verbs always
wrote the lead, even when a Task or a File fired the journey. An author who learned one rule guessed wrong on
the next, and there was nothing to read that said which was which.

So the answer moves into the declaration (`target` on the verb) and the decision into ONE place
(`actions.resolve_target`). This suite is that declaration's lock: a NEW verb added with no `target` fails
`test_every_effect_verb_declares_the_record_it_acts_on` without anybody editing this file, which is the
only kind of declaration worth having.

Behaviour is deliberately UNCHANGED — every verb resolves to the record it already resolved to. What
changed is that the rule is now written down once instead of implied five times.
"""
import frappe
from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import actions, fields, subjects
from tatva_connect.workflow_engine import refs

_LEAD = "CRM Lead"
_LEAD_NAME = "WF-TARGET-LEAD"
_TRIGGER = frappe._dict(doctype="CRM Task", name="WF-TARGET-TASK")
_WEEK = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")


def _OTHER_DAY():
	"""Any weekday that is not today — the rule's own gate reads `get_weekday`, so this must not match it."""
	return next(d for d in _WEEK if d != frappe.utils.get_weekday())

# What each declared kind must resolve to, given the lead and trigger doc above. Written out so the test
# compares against an INDEPENDENT statement of the rule rather than against the resolver's own logic.
_EXPECTED = {
	actions.TARGET_LEAD: (_LEAD, _LEAD_NAME),
	actions.TARGET_NONE: (None, None),
}


def _action(verb, **config):
	"""An action row as the interpreter builds one — the verb's config plus the `action_type` stamp."""
	return frappe._dict(action_type=verb, **config)


class TestWriteTarget(FrappeTestCase):
	def test_every_effect_verb_declares_the_record_it_acts_on(self):
		"""The lock. A verb added tomorrow with no `target` fails here with no edit to this file."""
		for verb in actions.verbs_in_lane("effect"):
			with self.subTest(verb=verb):
				self.assertIn(
					actions.target_of(verb), actions.TARGET_KINDS,
					f"{verb} does not declare which record it acts on — declare `target` on it in VERBS",
				)

	def test_a_guard_verb_declares_no_target(self):
		"""A guard runs inside `validate` to block a save. It acts on nothing, so a target would be a lie."""
		for verb in actions.verbs_in_lane("guard"):
			with self.subTest(verb=verb):
				self.assertIsNone(actions.target_of(verb), f"{verb} is a guard and writes no record")

	def test_a_verb_whose_target_is_authored_takes_a_target_parameter(self):
		"""`authored` means the AUTHOR picks, so there must be a control for them to pick with."""
		for verb in actions.verbs_in_lane("effect"):
			if actions.target_of(verb) != actions.TARGET_AUTHORED:
				continue
			with self.subTest(verb=verb):
				self.assertTrue(
					actions.authored_target_field(verb),
					f"{verb} declares an authored target but takes no Target parameter to author it with",
				)

	def test_the_resolver_returns_what_each_verb_declares(self):
		"""One resolver, every verb, compared against the declaration rather than against itself."""
		for verb in actions.verbs_in_lane("effect"):
			kind = actions.target_of(verb)
			config = {}
			if kind == actions.TARGET_AUTHORED:
				config[actions.authored_target_field(verb)] = _LEAD
			with self.subTest(verb=verb, target=kind):
				self.assertEqual(
					actions.resolve_target(_action(verb, **config), _LEAD_NAME, _TRIGGER),
					_EXPECTED.get(kind, (_LEAD, _LEAD_NAME)),
				)

	def test_an_authored_target_may_be_the_record_that_fired_the_run(self):
		"""The whole point of `authored`: a Task-triggered workflow may write onto that Task."""
		verb = next(v for v in actions.verbs_in_lane("effect")
		            if actions.target_of(v) == actions.TARGET_AUTHORED)
		action = _action(verb, **{actions.authored_target_field(verb): _TRIGGER.doctype})
		self.assertEqual(
			actions.resolve_target(action, _LEAD_NAME, _TRIGGER), (_TRIGGER.doctype, _TRIGGER.name)
		)

	def test_an_authored_target_the_run_cannot_reach_is_refused(self):
		"""Neither the lead nor the trigger doc — raise loudly rather than misfire on a name that isn't its."""
		verb = next(v for v in actions.verbs_in_lane("effect")
		            if actions.target_of(v) == actions.TARGET_AUTHORED)
		action = _action(verb, **{actions.authored_target_field(verb): "CRM Organization"})
		with self.assertRaises(ValueError):
			actions.resolve_target(action, _LEAD_NAME, _TRIGGER)

	def test_an_undeclared_verb_is_refused_rather_than_guessed(self):
		"""Fail closed. Guessing "probably the lead" is how the four different answers grew in the first place."""
		with self.assertRaises(ValueError):
			actions.resolve_target(_action("No Such Verb"), _LEAD_NAME, _TRIGGER)

	def test_the_reachable_targets_are_the_lead_the_subject_and_the_declared_write_targets(self):
		"""The publish gate and the authoring vocabulary both ask this — so it is answered in one place.

		The declared half is READ, never retyped: a doctype added to `WRITE_TARGETS` must not need this file
		edited, or the test is a snapshot of the day it was written rather than a lock on the rule.
		"""
		declared = list(subjects.WRITE_TARGETS)
		self.assertEqual(actions.reachable_targets("CRM Task"), [_LEAD, "CRM Task", *declared])
		self.assertEqual(actions.reachable_targets(_LEAD), [_LEAD, *declared])
		self.assertEqual(actions.reachable_targets(None), [_LEAD, *declared])


class TestDeclaredWriteTarget(FrappeTestCase):
	"""W13 — a record the journey does not FIND but MAKES.

	`Update Field` already asked the two questions this needs: which record, and which fields. What is new is
	that a target in `subjects.WRITE_TARGETS` resolves to whatever this journey has already made — nothing on
	the first write, so `save()` inserts, and the name after that, so `save()` updates. One node, one call, and
	the author configures neither.

	Skipped when nothing is declared: an empty tuple is the shipped default and must stay a free no-op.
	"""

	def setUp(self):
		if not subjects.WRITE_TARGETS:
			self.skipTest("no write target declared — the feature is dormant, which is its own contract")
		self.target = subjects.WRITE_TARGETS[0]
		self.lead = frappe.db.get_value(_LEAD, {}, "name")
		self.state = refs.Values(buckets={})
		self.ctx = self.state.writing_as("n1")
		self.addCleanup(frappe.db.rollback)

	def _write(self, **values):
		action = _action("Update Field", target_doctype=self.target, updates=[
			{"name": k, "mode": refs.LITERAL, "value": v} for k, v in values.items()
		])
		actions._action_set_field(action, self.lead, self.ctx, ("", "", ""), None)
		return actions.wrote_name(self.ctx, self.target)

	def _a_writable_field(self):
		return fields._meta_writable_rows(self.target)[0].fieldname

	def test_the_first_write_inserts_and_the_second_updates_the_same_record(self):
		"""The whole of insert-or-update, and the author picks neither. Two records here would mean a journey
		raising a fresh ticket every time it touched one."""
		field = "subject" if self.target == "HD Ticket" else self._a_writable_field()
		first = self._write(**{field: "written by a workflow"})
		self.assertTrue(first, "the first write produced no record")
		second = self._write(**{field: "written again"})
		self.assertEqual(first, second)
		self.assertEqual(frappe.db.get_value(self.target, first, field), "written again")

	def test_the_name_is_remembered_under_the_engines_own_key(self):
		"""A bare name would land in the writing NODE's bucket, where the next node cannot see it — and every
		write would insert. It goes to `_engine`, which is shared and persists across a park."""
		name = self._write(**{"subject" if self.target == "HD Ticket" else self._a_writable_field(): "x"})
		self.assertEqual(self.state.buckets["_engine"]["wrote"], {refs.slug(self.target): name})

	def test_a_read_only_field_is_refused_before_anything_is_written(self):
		"""The meta answers what may be set, and `read_only` is what keeps it to the fields a person could set
		by hand rather than the ones the app computes for itself."""
		locked = next((df.fieldname for df in frappe.get_meta(self.target).fields if df.read_only), None)
		if not locked:
			self.skipTest(f"{self.target} declares no read-only field to refuse")
		self.assertFalse(fields.is_settable(self.target, locked, ("", "", "")))
		with self.assertRaises(PermissionError):
			self._write(**{locked: "x"})

	def test_the_publish_gate_admits_the_target_and_its_fields(self):
		"""Publish and runtime must agree, or a workflow goes green and dies on a live patient."""
		self.assertIn(self.target, actions.reachable_targets(_LEAD))
		self.assertTrue(fields.is_set_declared(self.target, self._a_writable_field()))
		self.assertFalse(fields.is_set_declared(self.target, "no_such_field_anywhere"))


class TestAssignFromPool(FrappeTestCase):
	"""W13.3 — the pool is an `Assignment Rule`, and FRAPPE picks from it.

	`do_assignment` is the platform's own "assign from this pool, now": it picks by the rule's strategy,
	writes the ToDo stamped with the rule, notifies, and advances the rota. Not `apply()`, which re-runs
	every rule for the doctype; not `apply_assign()`, which re-tests a condition the workflow already
	decided; and never `get_user()` plus an add of our own, which would split the pick from the bookkeeping
	so round robin never rotates.
	"""

	def setUp(self):
		self.lead = frappe.db.get_value(_LEAD, {}, "name")
		self.users = frappe.get_all(
			"User", pluck="name", limit=2,
			filters={"enabled": 1, "user_type": "System User", "name": ["not in", ("Administrator", "Guest")]},
		)
		if len(self.users) < 2 or not self.lead:
			self.skipTest("needs a lead and two enabled users to show a rotation")
		# Named, because Assignment Rule prompts for one — frappe throws rather than autonaming it.
		self.rule = frappe.get_doc({
			"doctype": "Assignment Rule", "name": "W13 Pool Under Test",
			"document_type": _LEAD, "rule": "Round Robin",
			"description": "pool probe", "assign_condition": "1 == 1", "priority": 0,
			"assignment_days": [{"day": d} for d in _WEEK],
			"users": [{"user": u} for u in self.users],
		}).insert(ignore_permissions=True)
		self.addCleanup(frappe.db.rollback)

	def _fire(self, node_id, **over):
		action = _action("Assign to User", assignee_mode=actions.POOL, assignment_rule=self.rule.name, **over)
		state = refs.Values(buckets={})
		ctx = state.writing_as(node_id)
		actions._action_assign_to_user(action, self.lead, ctx, ("", "", ""), None)
		return ctx.get(refs.OUTPUT), ctx.get(f"{node_id}.assigned_to")

	def test_two_fires_hand_the_record_to_two_different_people(self):
		"""The rota is frappe's and it must actually turn — a pool that always picks the first member is a
		named user with extra steps."""
		first = self._fire("n1")
		second = self._fire("n2")
		self.assertEqual([first[0], second[0]], ["assigned", "assigned"])
		self.assertNotEqual(first[1], second[1])
		self.assertEqual({first[1], second[1]}, set(self.users))

	def test_the_todo_is_stamped_with_the_rule_that_raised_it(self):
		"""Frappe's own bookkeeping, not ours — it is what lets the rota survive the next fire."""
		_, holder = self._fire("n1")
		self.assertEqual(frappe.db.get_value("Assignment Rule", self.rule.name, "last_user"), holder)
		self.assertTrue(frappe.db.exists("ToDo", {
			"reference_type": _LEAD, "reference_name": self.lead,
			"allocated_to": holder, "assignment_rule": self.rule.name, "status": "Open",
		}))

	def test_a_pool_that_does_not_run_today_leaves_by_nobody(self):
		"""An off-duty pool is an outcome the author routes, not an error that kills the journey."""
		self.rule.assignment_days = []
		self.rule.append("assignment_days", {"day": _OTHER_DAY()})
		self.rule.save(ignore_permissions=True)
		frappe.clear_cache(doctype="Assignment Rule")
		edge, holder = self._fire("n1")
		self.assertEqual(edge, "nobody")
		self.assertIsNone(holder)

	def test_the_emitted_variable_is_the_same_word_a_named_user_writes(self):
		"""Pool mode must teach downstream nodes no new vocabulary — `assigned_to` is declared once."""
		self.assertIn("assigned_to", [e["name"] for e in actions.emits_of("Assign to User")])
