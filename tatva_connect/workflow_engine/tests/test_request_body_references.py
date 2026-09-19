# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A `$ctx.` REFERENCE RESOLVES WHEREVER IT SITS — alone as a value, or inside a sentence.

`_resolve` tested `startswith` and swapped the WHOLE string, which served one of three author intents:

  * `"$ctx.crm_lead.lead_name"`               -> the value. Correct, and unchanged here.
  * `"Summarise for $ctx.crm_lead.lead_name"` -> sent VERBATIM, answered with HTTP 200, `succeeded`, every
    step logged ok, and raw template syntax in a summary a patient reads.
  * `"$ctx.crm_lead.lead_name has converted"` -> `None`. The trailing sentence was taken as the name.

`body_references` fed the publish gate through the same `startswith`, so the two halves agreed only by
both being blind. They now share `refs.references_in`. A whole-value reference keeps its own TYPE, which
is why it stays a separate question from substitution into text.
"""
import json

from frappe.tests.utils import FrappeTestCase

from tatva_connect.automation import actions
from tatva_connect.workflow_engine import refs

_NAME = "Test Patient"
_CITY = "Hyderabad"


def _ctx():
	"""Journey state carrying one node's values — the shape a run really holds, via the writer view."""
	values = refs.Values(buckets={})
	writer = values.writing_as("plan")
	writer["name"] = _NAME
	writer["city"] = _CITY
	writer["tokens"] = 220
	writer["nothing"] = None
	return values


def _sent(body):
	"""What `build_request_body` really puts on the wire for an authored body."""
	return actions.build_request_body(json.dumps(body), _ctx())


class TestAReferenceAloneIsUnchanged(FrappeTestCase):
	"""The shape that already worked — pinned, because the fix must not reach it."""

	def test_it_resolves_to_the_value(self):
		self.assertEqual(_sent({"content": "$ctx.plan.name"})["content"], _NAME)

	def test_it_keeps_the_value_s_own_type(self):
		"""An Int stays an Int. Stringifying here would send `"max_tokens": "220"` and break the call."""
		self.assertEqual(_sent({"max_tokens": "$ctx.plan.tokens"})["max_tokens"], 220)

	def test_a_value_that_is_really_none_stays_none(self):
		self.assertIsNone(_sent({"content": "$ctx.plan.nothing"})["content"])

	def test_text_naming_nothing_is_left_alone(self):
		self.assertEqual(_sent({"content": "plain words"})["content"], "plain words")

	def test_a_non_string_leaf_is_untouched(self):
		body = _sent({"n": 7, "flag": True, "empty": None})
		self.assertEqual((body["n"], body["flag"], body["empty"]), (7, True, None))


class TestAReferenceInsideTextIsSubstituted(FrappeTestCase):
	"""THE red: the first went out verbatim, the second resolved to None."""

	def test_a_reference_in_the_middle_of_a_sentence(self):
		self.assertEqual(
			_sent({"content": "Summarise the plan for $ctx.plan.name today"})["content"],
			f"Summarise the plan for {_NAME} today",
		)

	def test_a_reference_first_with_text_after_it(self):
		self.assertEqual(
			_sent({"content": "$ctx.plan.name has just converted"})["content"],
			f"{_NAME} has just converted",
		)

	def test_several_references_in_one_string(self):
		self.assertEqual(
			_sent({"content": "$ctx.plan.name lives in $ctx.plan.city"})["content"],
			f"{_NAME} lives in {_CITY}",
		)

	def test_a_sentence_keeps_its_punctuation(self):
		"""The grammar stops at the full stop rather than swallowing it as another segment."""
		self.assertEqual(
			_sent({"content": "Write to $ctx.plan.name."})["content"], f"Write to {_NAME}."
		)

	def test_it_resolves_at_depth(self):
		"""Where a real prompt lives — inside a list of objects."""
		body = _sent({"messages": [{"role": "user", "content": "Brief on $ctx.plan.name"}]})
		self.assertEqual(body["messages"][0]["content"], f"Brief on {_NAME}")

	def test_a_reference_nothing_answers_is_loud(self):
		"""Never an empty string: a blank in a prompt is the "Hi ," defect wearing a different hat."""
		with self.assertRaises(refs.UnknownReference):
			_sent({"content": "Brief on $ctx.plan.missing please"})


class TestThePublishGateSeesWhatTheRuntimeResolves(FrappeTestCase):
	"""Two halves, one scanner — the gate was blind to exactly what the runtime could not resolve."""

	def test_it_reports_a_reference_inside_text(self):
		found = actions.body_references(json.dumps({"content": "Summarise for $ctx.plan.name today"}))
		self.assertEqual(found, ["plan.name"])

	def test_it_still_reports_a_whole_value_reference(self):
		found = actions.body_references(json.dumps({"content": "$ctx.plan.name"}))
		self.assertEqual(found, ["plan.name"])

	def test_it_reports_every_reference_in_one_string(self):
		found = actions.body_references(json.dumps({"content": "$ctx.plan.name in $ctx.plan.city"}))
		self.assertEqual(sorted(found), ["plan.city", "plan.name"])

	def test_it_reports_nothing_for_a_body_that_reads_nothing(self):
		self.assertEqual(actions.body_references(json.dumps({"content": "plain words"})), [])

	def test_every_name_it_reports_is_one_the_runtime_would_look_up(self):
		"""The invariant itself: the gate's list and the runtime's substitutions are the same scanner."""
		body = {"messages": [{"content": "$ctx.plan.name in $ctx.plan.city"}, {"content": "$ctx.plan.tokens"}]}
		for name in actions.body_references(json.dumps(body)):
			self.assertIsNotNone(refs.parse(name), f"{name} is reported but is not a readable reference")
