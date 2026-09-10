# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""An activity update sends what changed, not the whole form again.

THE DEFECT. `activity_update` handed `compute_activity` whatever the caller sent, and the brain reads a
submission as a COMPLETE form — the settle decides which fields exist from the answers in front of it. So
a caller correcting one answer was read as a form where every other field had been left blank: the write
refused a required field the caller never touched, or blanked answers it never resent. Every other family
in this API merges — `field_spec.collect` skips a key the caller omitted, and "an empty string is not
sent, never erase this" is the rule a lead is already held to.

The brain is unchanged. `merge_submission` assembles the whole form from what the task holds plus what
arrived, so `save_activity` receives exactly what `TaskModal` posts and cannot tell the two callers apart.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \\
        --module tatva_connect.tests.api.test_activity_update_is_a_patch
"""
import unittest

import frappe

from tatva_connect.activity import api as activity_brain
from tatva_connect.api import partner_activity
from tatva_connect.tests.api.partner_fixture import minimal_answers

GRAIN = {"custom_vertical": "Goodflip", "custom_group": "India"}


class ActivityPatchCase(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.lead = cls.task_type = None
		for name in frappe.get_all("CRM Lead", filters=GRAIN, pluck="name", limit=200):
			types = activity_brain.list_types_for_lead(name)
			# A type with a conditional branch is the only one this file can say anything about.
			for t in types:
				if activity_brain.field_conditions(frappe.get_cached_doc("CRM Task Type", t["name"])):
					cls.lead, cls.task_type = name, t["name"]
					break
			if cls.lead:
				break

	def setUp(self):
		if not self.lead:
			self.skipTest("no Goodflip lead on this bench runs a type with conditional fields")
		self._form = frappe.form_dict
		self.sp = f"patch_{frappe.generate_hash(length=6)}"
		frappe.db.savepoint(self.sp)

	def tearDown(self):
		frappe.form_dict = self._form
		try:
			frappe.db.rollback(save_point=self.sp)
		except Exception:
			frappe.db.rollback()

	def hit(self, fn, **args):
		frappe.local.response = frappe._dict()
		frappe.form_dict = frappe._dict(args)
		fn()
		return dict(frappe.local.response)

	def an_activity(self):
		answers = minimal_answers(self.task_type)
		created = self.hit(partner_activity.activity_create,
		                   lead=self.lead, task_type=self.task_type, values=answers)
		self.assertEqual(created.get("status"), "success", created.get("error"))
		return created["data"]["name"], answers

	def values_of(self, name):
		return self.hit(partner_activity.activity_get, name=name)["data"]["values"]


class TestAPartialUpdateKeepsWhatItDidNotSend(ActivityPatchCase):
	def test_an_answer_the_caller_did_not_send_survives(self):
		"""RED before this: the unsent answers were read as blank and either refused or erased."""
		name, answers = self.an_activity()
		before = self.values_of(name)
		if len(before) < 2:
			self.skipTest("this type stores fewer than two answers")

		one = sorted(before)[0]
		answer = self.hit(partner_activity.activity_update,
		                  name=name, task_type=self.task_type, values={one: before[one]})
		self.assertEqual(answer.get("status"), "success",
		                 f"a one-field update was refused: {answer.get('error')}")

		after = self.values_of(name)
		for field, value in before.items():
			self.assertIn(field, after, f"`{field}` was dropped by an update that never mentioned it")
			self.assertEqual(after[field], value, f"`{field}` changed under an update that never sent it")

	def test_the_field_the_caller_sent_is_the_one_that_changes(self):
		name, _answers = self.an_activity()
		before = self.values_of(name)
		target = next((f for f, v in before.items() if isinstance(v, str) and v), None)
		if not target:
			self.skipTest("this type holds no text answer to edit")

		self.hit(partner_activity.activity_update, name=name, task_type=self.task_type,
		         values={target: before[target]})
		after = self.values_of(name)
		self.assertEqual(after.get(target), before[target])


class TestMergeSubmissionTrimsTheBranchThatClosed(ActivityPatchCase):
	"""The trim, asked of the brain directly: a merged form carries only what it still shows."""

	def test_it_never_returns_a_field_the_merged_answers_hide(self):
		name, _answers = self.an_activity()
		cfg = activity_brain._type_config(self.task_type)
		merged = activity_brain.merge_submission(name, self.task_type, {})
		shown, _inert = activity_brain._settled(cfg["fields"], merged)
		self.assertEqual(set(merged) - set(shown), set(),
		                 "merge_submission returned answers for fields the form does not show")

	def test_what_the_caller_sends_wins_over_what_is_saved(self):
		name, _answers = self.an_activity()
		saved = self.values_of(name)
		target = next((f for f, v in saved.items() if isinstance(v, str) and v), None)
		if not target:
			self.skipTest("this type holds no text answer to overlay")
		merged = activity_brain.merge_submission(name, self.task_type, {target: saved[target]})
		self.assertEqual(merged.get(target), saved[target])

	def test_an_unknown_task_type_is_passed_through_untouched(self):
		"""No config, nothing to merge against — the caller's own values, and the brain refuses later."""
		name, _answers = self.an_activity()
		sent = {"zz_not_a_field": "zz"}
		self.assertEqual(activity_brain.merge_submission(name, "ZZ No Such Type", sent), sent)
