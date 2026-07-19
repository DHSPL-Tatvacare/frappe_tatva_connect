# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""VAPT Jun'26 (Jul cut) — the 6 NEW LMS findings, as bench regression.

These are a DIFFERENT class from the authz endpoint sweep (which judges row/action escalation:
`actual ⊆ native`). A student is LEGITIMATELY allowed the row here — the leak is at the FIELD level
(N4/N5) or in assessment LOGIC (N1/N2/N3/N6), neither of which the row oracle can see. So they live in
their own module, but keep the suite's discipline: seed a real object, drive the real endpoint as a real
non-privileged persona, and assert the OUTCOME (the sensitive value is gone / the action is refused),
never a call.

Verified reality (dev.localhost, 2026-07-17) — three of six need no code, and this module PINS that so a
future LMS upgrade cannot silently regress it:
  N1  server already re-grades from stored answers (not client correctness)  -> pin
  N3  check_answer already enforces `show_answers` server-side               -> pin
  N2  sequential single-attempt enforced; the RACE (count-then-insert) is    -> FIX (atomic lock);
      the real hole — proven in the LIVE HTTP replay, not here (a rolled-back    race proof is HTTP-native
      FrappeTestCase cannot fire two committed concurrent connections)          (tests/live), see run_lms_replay
  N4  LMS Test Case input/expected_output readable by a student              -> FIX (permlevel)
  N5  exercise (child test_cases) readable by a student via client.get       -> FIX (permlevel)
  N6  no server start-time exists; best-effort start-stamp on quiz-open       -> FIX (best-effort), residual
"""
import json

import frappe
from frappe.client import get as client_get
from frappe.client import get_list as client_get_list
from frappe.tests.utils import FrappeTestCase
from frappe.utils import now_datetime
from lms.lms.doctype.lms_quiz.lms_quiz import check_answer, submit_quiz
from lms.lms.doctype.lms_quiz_submission.lms_quiz_submission import MaximumAttemptsExceededError

from tatva_connect.access import native_guards

TAG = "lms-vapt-jul"


def _mk_user(email, roles):
	if not frappe.db.exists("User", email):
		u = frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": email.split("@")[0], "send_welcome_email": 0}
		).insert(ignore_permissions=True)
	else:
		u = frappe.get_doc("User", email)
	u.add_roles(*roles)
	return email


class TestLMSVaptJul(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# Personas built here (rolled back with the class txn): a plain LMS Student is the faithful VAPT
		# actor; a Course Creator is the legitimate author whose access must SURVIVE the fix (no over-block).
		cls.student = _mk_user(f"{TAG}-student@example.com", ["LMS Student"])
		cls.creator = _mk_user(f"{TAG}-creator@example.com", ["Course Creator"])

	def setUp(self):
		frappe.set_user("Administrator")
		self.exercise = frappe.get_doc(
			{
				"doctype": "LMS Programming Exercise",
				"title": f"{TAG}-ex",
				"problem_statement": "add two numbers",
				"language": "Python",
				"test_cases": [
					{"input": "SECRET_IN_1", "expected_output": "SECRET_OUT_1"},
					{"input": "SECRET_IN_2", "expected_output": "SECRET_OUT_2"},
				],
			}
		).insert(ignore_permissions=True)
		self.question = frappe.get_doc(
			{
				"doctype": "LMS Question",
				"question": "Pick A",
				"type": "Choices",
				"option_1": "A",
				"is_correct_1": 1,
				"option_2": "B",
				"is_correct_2": 0,
				"multiple": 0,
			}
		).insert(ignore_permissions=True)

	def _quiz(self, show_answers=0, max_attempts=1):
		return frappe.get_doc(
			{
				"doctype": "LMS Quiz",
				"title": f"{TAG}-quiz",
				"show_answers": show_answers,
				"max_attempts": max_attempts,
				"passing_percentage": 50,
				"questions": [{"question": self.question.name, "marks": 1, "type": "Choices"}],
			}
		).insert(ignore_permissions=True)

	def tearDown(self):
		frappe.set_user("Administrator")
		for q in frappe.get_all("LMS Quiz", filters={"title": f"{TAG}-quiz"}, pluck="name"):
			frappe.cache().delete_value(f"lms_quiz_start:{q}:{self.student}")

	# --- N5: exercise read via client.get must not leak the child grading fields ------------------
	def test_N5_student_cannot_read_test_case_values_via_client_get(self):
		frappe.set_user(self.student)
		child = (client_get("LMS Programming Exercise", self.exercise.name) or {}).get("test_cases")[0]
		self.assertIsNone(child.get("input"), "N5: student read the hidden test-case input")
		self.assertIsNone(child.get("expected_output"), "N5: student read the hidden expected_output")

	# --- N4: get_list on the child doctype must not return the grading fields ---------------------
	def test_N4_student_cannot_list_test_case_values(self):
		frappe.set_user(self.student)
		rows = client_get_list(
			"LMS Test Case",
			filters={
				"parent": self.exercise.name,
				"parenttype": "LMS Programming Exercise",
				"parentfield": "test_cases",
			},
			fields=["input", "expected_output", "name"],
			parent="LMS Programming Exercise",
		)
		for r in rows:
			self.assertIsNone(r.get("input"), "N4: student listed the hidden test-case input")
			self.assertIsNone(r.get("expected_output"), "N4: student listed the hidden expected_output")

	# --- metamorphic pair: the legitimate author must STILL see the values (no over-block) --------
	def test_N4N5_course_creator_still_reads_test_cases(self):
		frappe.set_user(self.creator)
		child = (client_get("LMS Programming Exercise", self.exercise.name) or {}).get("test_cases")[0]
		self.assertEqual(child.get("input"), "SECRET_IN_1", "over-block: author lost test-case access")
		self.assertEqual(child.get("expected_output"), "SECRET_OUT_1")

	# --- N3: check_answer must refuse when the quiz has "Show Answers" off (already upstream) ------
	def test_N3_check_answer_denied_when_show_answers_off(self):
		quiz = self._quiz(show_answers=0)
		frappe.set_user(self.student)
		with self.assertRaises(frappe.PermissionError):
			check_answer(quiz.name, self.question.name, "Choices", json.dumps(["A"]))

	# --- N1: the server grades from the submitted answer, never a client-supplied score -----------
	def test_N1_server_grades_from_stored_answers(self):
		quiz = self._quiz(show_answers=0, max_attempts=0)  # 0 = unlimited, so both submits land
		frappe.set_user(self.student)
		right = submit_quiz(quiz.name, json.dumps([{"question_name": self.question.name, "answer": ["A"]}]))
		wrong = submit_quiz(quiz.name, json.dumps([{"question_name": self.question.name, "answer": ["B"]}]))
		self.assertEqual(right.get("score"), 1, "N1: correct answer not graded to full marks")
		self.assertEqual(wrong.get("score"), 0, "N1: wrong answer scored — server trusted the client")

	# --- N2 (sequential half): the single-attempt ceiling holds; the RACE is proven in the live
	#     HTTP replay (single-packet parallel submit), which a rolled-back FrappeTestCase cannot fire.
	def test_N2_sequential_single_attempt_enforced(self):
		quiz = self._quiz(max_attempts=1)
		frappe.set_user(self.student)
		submit_quiz(quiz.name, json.dumps([{"question_name": self.question.name, "answer": ["A"]}]))
		with self.assertRaises(MaximumAttemptsExceededError):
			submit_quiz(quiz.name, json.dumps([{"question_name": self.question.name, "answer": ["A"]}]))

	# --- N2 (through our wrapper): the atomic-lock wrapper must not break the single-attempt guard -----
	def test_N2_wrapper_preserves_single_attempt(self):
		quiz = self._quiz(max_attempts=1)
		frappe.set_user(self.student)
		r = native_guards.submit_quiz(
			quiz.name, json.dumps([{"question_name": self.question.name, "answer": ["A"]}])
		)
		self.assertEqual(r.get("score"), 1)
		with self.assertRaises(MaximumAttemptsExceededError):
			native_guards.submit_quiz(
				quiz.name, json.dumps([{"question_name": self.question.name, "answer": ["A"]}])
			)

	# --- N6: a submit after the quiz's duration has elapsed (from the recorded open) is rejected -------
	def test_N6_late_submit_rejected(self):
		quiz = self._quiz(max_attempts=0)
		frappe.db.set_value("LMS Quiz", quiz.name, "duration", "1")  # 1 minute
		frappe.set_user(self.student)
		# simulate an open 2 minutes ago (past the 1-min duration + 30s grace)
		frappe.cache().set_value(native_guards._quiz_start_key(quiz.name), now_datetime().timestamp() - 120)
		with self.assertRaises(frappe.exceptions.ValidationError):
			native_guards.submit_quiz(
				quiz.name, json.dumps([{"question_name": self.question.name, "answer": ["A"]}])
			)

	# --- N6 metamorphic: an in-time submit (opened just now) still succeeds ----------------------------
	def test_N6_intime_submit_allowed(self):
		quiz = self._quiz(max_attempts=0)
		frappe.db.set_value("LMS Quiz", quiz.name, "duration", "30")
		frappe.set_user(self.student)
		frappe.cache().set_value(native_guards._quiz_start_key(quiz.name), now_datetime().timestamp())
		r = native_guards.submit_quiz(
			quiz.name, json.dumps([{"question_name": self.question.name, "answer": ["A"]}])
		)
		self.assertEqual(r.get("score"), 1)
