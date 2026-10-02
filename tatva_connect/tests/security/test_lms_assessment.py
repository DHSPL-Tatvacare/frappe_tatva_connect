# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""A student cannot read hidden LMS grading fields or game quiz scoring, time limits or attempts.
The author keeps full access; the concurrent-submit race is proven in the live HTTP replay, not here.
"""
import json

import frappe
from frappe.client import get as client_get
from frappe.client import get_list as client_get_list
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime
from lms.lms.doctype.lms_quiz.lms_quiz import submit_quiz
from lms.lms.doctype.lms_quiz_submission.lms_quiz_submission import MaximumAttemptsExceededError

from tatva_connect.access import native_guards
from tatva_connect.tests.authz.base import dispatch, mk_user

TAG = "lms-vapt-jul"


class TestLMSVaptJul(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# A plain student is the attacker; a Course Creator is the author whose access must survive the fix.
		cls.student = mk_user(f"{TAG}-student@example.com", ["LMS Student"])
		cls.creator = mk_user(f"{TAG}-creator@example.com", ["Course Creator"])

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
		# lms commits inside its enrolment path, so clear rows that outlived the last test's rollback.
		for doctype, filters in (
			("LMS Enrollment", {"member": self.student}),
			("LMS Course", {"name": ["like", f"{TAG}-%"]}),
		):
			for name in frappe.get_all(doctype, filters=filters, pluck="name"):
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True, ignore_missing=True)
		self.course = frappe.get_doc(
			{
				"doctype": "LMS Course",
				"title": f"{TAG}-course",
				"description": "quiz host",
				"short_introduction": "quiz host",
				"published": 1,
				"instructors": [{"instructor": self.creator}],
			}
		).insert(ignore_permissions=True)
		# A quiz reaches a student only through an enrolled course, and only an admin can enrol a student.
		frappe.get_doc(
			{"doctype": "LMS Enrollment", "member": self.student, "course": self.course.name}
		).insert(ignore_permissions=True)

	def _quiz(self, show_answers=0, max_attempts=1):
		return frappe.get_doc(
			{
				"doctype": "LMS Quiz",
				"title": f"{TAG}-quiz",
				"course": self.course.name,
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

	# N5: reading the exercise through client.get must not leak the child grading fields.
	def test_N5_student_cannot_read_test_case_values_via_client_get(self):
		frappe.set_user(self.student)
		child = (client_get("LMS Programming Exercise", self.exercise.name) or {}).get("test_cases")[0]
		self.assertIsNone(child.get("input"), "N5: student read the hidden test-case input")
		self.assertIsNone(child.get("expected_output"), "N5: student read the hidden expected_output")

	# N4: listing the child doctype must not return the grading fields.
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

	# Paired check: the author still sees the values.
	def test_N4N5_course_creator_still_reads_test_cases(self):
		frappe.set_user(self.creator)
		child = (client_get("LMS Programming Exercise", self.exercise.name) or {}).get("test_cases")[0]
		self.assertEqual(child.get("input"), "SECRET_IN_1", "over-block: author lost test-case access")
		self.assertEqual(child.get("expected_output"), "SECRET_OUT_1")

	# N1: the server grades the submitted answer and never trusts a client score.
	def test_N3_check_answer_refused_when_show_answers_is_off(self):
		"""Through our override, so an lms upgrade that drops its own show_answers check goes red here."""
		quiz = self._quiz(show_answers=0)
		frappe.set_user(self.student)
		with self.assertRaises(frappe.PermissionError):
			dispatch("lms.lms.doctype.lms_quiz.lms_quiz.check_answer", quiz=quiz.name, question=self.question.name,
			         question_type="Choices", answers=json.dumps(["A"]))

	def test_N1_server_grades_from_stored_answers(self):
		quiz = self._quiz(show_answers=0, max_attempts=0)  # 0 = unlimited, so both submits land
		frappe.set_user(self.student)
		right = submit_quiz(quiz.name, json.dumps([{"question_name": self.question.name, "answer": ["A"]}]))
		wrong = submit_quiz(quiz.name, json.dumps([{"question_name": self.question.name, "answer": ["B"]}]))
		self.assertEqual(right.get("score"), 1, "N1: correct answer not graded to full marks")
		self.assertEqual(wrong.get("score"), 0, "N1: wrong answer scored — server trusted the client")

	# N2: our locking wrapper keeps the single-attempt limit.
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

	# N6: a submit after the quiz duration has run out is rejected.
	def test_N6_late_submit_rejected(self):
		quiz = self._quiz(max_attempts=0)
		frappe.db.set_value("LMS Quiz", quiz.name, "duration", "1")  # 1 minute
		frappe.set_user(self.student)
		# Opened 2 minutes ago, past the 1-minute duration plus 30s grace.
		frappe.cache().set_value(native_guards._quiz_start_key(quiz.name), now_datetime().timestamp() - 120)
		with self.assertRaises(frappe.exceptions.ValidationError):
			native_guards.submit_quiz(
				quiz.name, json.dumps([{"question_name": self.question.name, "answer": ["A"]}])
			)

	# N6 pair: a submit inside the duration still succeeds.
	def test_N6_intime_submit_allowed(self):
		quiz = self._quiz(max_attempts=0)
		frappe.db.set_value("LMS Quiz", quiz.name, "duration", "30")
		frappe.set_user(self.student)
		frappe.cache().set_value(native_guards._quiz_start_key(quiz.name), now_datetime().timestamp())
		r = native_guards.submit_quiz(
			quiz.name, json.dumps([{"question_name": self.question.name, "answer": ["A"]}])
		)
		self.assertEqual(r.get("score"), 1)

	# The certified-participant directory exposes other members' details, so it is staff-only.
	def test_certified_participants_denied_to_student(self):
		frappe.set_user(self.student)
		with self.assertRaises(frappe.PermissionError):
			native_guards.get_certified_participants()

	def test_certified_participants_allowed_for_staff(self):
		# Staff still get the directory in its native list shape.
		frappe.set_user(self.creator)
		rows = native_guards.get_certified_participants()
		self.assertIsInstance(rows, list)
