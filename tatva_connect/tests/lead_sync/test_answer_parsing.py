# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""What Graph sent, read exactly once, with nothing dropped and nothing invented.

Graph omits the `values` KEY entirely for an optional question the person did not answer — it does not
send an empty list. Indexing it blindly raised KeyError on the first such lead and aborted the whole
sync, so every later lead in that batch was lost too.

A checkbox question answers with SEVERAL values. Keeping `values[0]` was a wrong clinical record: a
patient ticking high cholesterol and fatty liver lost the fatty liver, silently, and no surface could
show it had ever been said.

The shapes here are copied from a real /{form_id}/leads response (10 leads, 3 entries with no `values`
across 2 of them). The parse under test is the real one — a local copy of it would test itself.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.lead_sync.test_answer_parsing
"""
from frappe.tests.utils import FrappeTestCase

from tatva_connect.lead_sync.source import answers


class TestAnswerParsing(FrappeTestCase):
	def test_a_skipped_question_is_absent_not_blank(self):
		lead = {
			"id": "1",
			"field_data": [
				{"name": "full_name", "values": ["Vikas Saxena"]},
				{"name": "what_is_your_current_diabetes_treatment?"},  # skipped — no `values` key
				{"name": "phone_number", "values": ["+919425145100"]},
			],
		}
		self.assertEqual(
			answers(lead), [("full_name", "Vikas Saxena"), ("phone_number", "+919425145100")]
		)

	def test_a_question_answered_blank_is_kept_and_is_not_the_same_as_a_skipped_one(self):
		"""An empty list is a question that was asked and answered blank. It is a fact about the patient,
		and it is a different fact from never having answered."""
		lead = {"id": "2", "field_data": [{"name": "city", "values": []}, {"name": "email", "values": ["a@b.c"]}]}
		self.assertEqual(answers(lead), [("city", ""), ("email", "a@b.c")])

	def test_a_multi_select_answer_keeps_every_value(self):
		lead = {"id": "3", "field_data": [
			{"name": "have_you_been_diagnosed_with_any_of_these_conditions?",
			 "values": ["high_cholesterol", "fatty_liver"]},
		]}
		self.assertEqual(
			answers(lead),
			[("have_you_been_diagnosed_with_any_of_these_conditions?", "high_cholesterol, fatty_liver")],
		)

	def test_a_lead_with_no_field_data_is_empty_not_a_crash(self):
		self.assertEqual(answers({"id": "4"}), [])
