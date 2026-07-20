# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Every effect verb DECLARES the record it acts on, and one resolver answers for all of them.

Four verbs answered this question four different ways and nothing declared any of them. `Update Field`
honoured the author's `target_doctype`; `Create Note`, `Assign to User` and the two child-row verbs always
wrote the lead, even when a Task or a File fired the run. An author who learned one rule guessed wrong on
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

from tatva_connect.automation import actions

_LEAD = "CRM Lead"
_LEAD_NAME = "WF-TARGET-LEAD"
_TRIGGER = frappe._dict(doctype="CRM Task", name="WF-TARGET-TASK")

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

	def test_the_reachable_targets_are_the_lead_and_the_subject(self):
		"""The publish gate and the authoring vocabulary both ask this — so it is answered in one place."""
		self.assertEqual(actions.reachable_targets("CRM Task"), [_LEAD, "CRM Task"])
		self.assertEqual(actions.reachable_targets(_LEAD), [_LEAD])
		self.assertEqual(actions.reachable_targets(None), [_LEAD])
