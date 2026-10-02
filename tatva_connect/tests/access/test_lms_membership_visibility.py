# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""LMS visibility follows ONE rule: am I in it, or do I run it; `published` alone grants nothing.
Each test drives the real entry point as a real persona and asserts the outcome, never a call."""
import frappe
from frappe.client import get as client_get
from frappe.client import get_count as client_get_count
from frappe.client import get_list as client_get_list
from frappe.tests import IntegrationTestCase

from tatva_connect.access import lms_visibility
from tatva_connect.access.native_guards import _quiz_start_key
from tatva_connect.tests.authz.base import dispatch, mk_user

TAG = "lms-aug-audit"

# Membership sets are memoised per request and a test run is one request, so each test resets them.
_MEMO_BUCKETS = (
	"tatva_connect:lms_visible_batches",
	"tatva_connect:lms_visible_programs",
	"tatva_connect:lms_visible_courses",
)


class TestLMSMembershipVisibility(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# `outsider` and `member` share a role, so every deny is about membership; `author` is the instructor.
		cls.outsider = mk_user(f"{TAG}-outsider@example.com", ["LMS Student"])
		cls.member = mk_user(f"{TAG}-member@example.com", ["LMS Student"])
		cls.author = mk_user(f"{TAG}-author@example.com", ["Course Creator"])

	@classmethod
	def _purge(cls):
		"""Delete every fixture this class owns, newest dependency first.
		lms commits inside its enrolment cascade, so rows can outlive the rollback and collide on a re-run."""
		like = ["like", f"{TAG}-%"]
		for doctype, filters in (
			("LMS Enrollment", {"member": like}),
			("LMS Batch Enrollment", {"member": like}),
			("LMS Course Review", {"course": like}),
			("LMS Quiz", {"name": like}),
			("LMS Program", {"name": like}),
			("LMS Batch", {"name": like}),
			("LMS Course", {"name": like}),
		):
			for name in frappe.get_all(doctype, filters=filters, pluck="name"):
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True, ignore_missing=True)

	def setUp(self):
		frappe.set_user("Administrator")
		self._reset_memo()
		self._purge()
		self.course = frappe.get_doc(
			{
				"doctype": "LMS Course",
				"title": f"{TAG}-course",
				"description": "assigned course",
				"short_introduction": "assigned course",
				"published": 0,
				"instructors": [{"instructor": self.author}],
			}
		).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture seeding
		self.batch = frappe.get_doc(
			{
				"doctype": "LMS Batch",
				"title": f"{TAG}-batch",
				"description": "assigned batch",
				"batch_details": "assigned batch",
				"start_date": "2026-01-01",
				"end_date": "2026-12-31",
				"start_time": "09:00:00",
				"end_time": "17:00:00",
				"timezone": "Asia/Kolkata",
				"published": 0,
				"instructors": [{"instructor": self.author}],
				"courses": [{"course": self.course.name}],
			}
		).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture seeding
		self.program = frappe.get_doc(
			{
				"doctype": "LMS Program",
				"title": f"{TAG}-program",
				"published": 0,
				"program_courses": [{"course": self.course.name}],
				"program_members": [{"member": self.member, "full_name": "Assigned Colleague"}],
			}
		).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture seeding
		self.question = frappe.get_doc(
			{
				"doctype": "LMS Question",
				"question": "Pick A",
				"type": "Choices",
				"option_1": "A",
				"is_correct_1": 1,
				"option_2": "B",
				"is_correct_2": 0,
			}
		).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture seeding
		self.quiz = frappe.get_doc(
			{
				"doctype": "LMS Quiz",
				"title": f"{TAG}-quiz",
				"course": self.course.name,
				"show_answers": 1,
				"passing_percentage": 50,
				"total_marks": 1,
				"questions": [{"question": self.question.name, "marks": 1}],
			}
		).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture seeding
		# F8 needs a course the victim is eligible for, or lms's own before_insert refuses it first.
		self.open_course = frappe.get_doc(
			{
				"doctype": "LMS Course",
				"title": f"{TAG}-open-course",
				"description": "self-serve course",
				"short_introduction": "self-serve course",
				"published": 1,
				"instructors": [{"instructor": self.author}],
			}
		).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture seeding
		# `member` joins the batch and lms's cascade adds the course enrolment, so only the batch is seeded.
		frappe.get_doc(
			{"doctype": "LMS Batch Enrollment", "member": self.member, "batch": self.batch.name}
		).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture seeding
		# Seeded as the enrolled member, because lms refuses a review from anyone not in the course.
		frappe.set_user(self.member)
		self.review = frappe.get_doc(
			{
				"doctype": "LMS Course Review",
				"course": self.course.name,
				"rating": 1,
				"review": "a colleague's opinion",
			}
		).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture seeding
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")
		self._reset_memo()

	def _reset_memo(self):
		for bucket in _MEMO_BUCKETS:
			setattr(frappe.local, bucket, {})

	# --- the nine findings ---------------------------------------------------------------------

	def test_f1_student_cannot_read_courses_of_a_batch_they_are_not_in(self):
		frappe.set_user(self.outsider)
		rows = client_get_list(
			"Batch Course",
			parent="LMS Batch",
			filters={"parent": self.batch.name, "parenttype": "LMS Batch"},
			fields=["course"],
		)
		self.assertEqual(rows, [], "a batch's course list must be invisible to someone not in the batch")

	def test_f2_student_cannot_read_reviews_of_a_course_they_are_not_in(self):
		frappe.set_user(self.outsider)
		with self.assertRaises(frappe.PermissionError):
			dispatch("lms.lms.utils.get_reviews", course=self.course.name)

	def test_f3_student_cannot_read_outline_of_a_course_they_are_not_in(self):
		frappe.set_user(self.outsider)
		with self.assertRaises(frappe.PermissionError):
			dispatch("lms.lms.utils.get_course_outline", course=self.course.name)

	def test_f4_student_cannot_read_members_of_a_program_they_are_not_in(self):
		frappe.set_user(self.outsider)
		rows = client_get_list(
			"LMS Program Member",
			parent="LMS Program",
			filters={"parent": self.program.name, "parenttype": "LMS Program"},
			fields=["member"],
		)
		self.assertEqual(rows, [], "a program's roster must be invisible to someone not in the program")

	def test_f5_student_cannot_read_an_arbitrary_quiz(self):
		frappe.set_user(self.outsider)
		with self.assertRaises(frappe.PermissionError):
			client_get("LMS Quiz", self.quiz.name)

	def test_f6_student_cannot_validate_answers_without_taking_the_quiz(self):
		frappe.set_user(self.outsider)
		with self.assertRaises(frappe.PermissionError):
			dispatch(
				"lms.lms.doctype.lms_quiz.lms_quiz.check_answer",
				quiz=self.quiz.name,
				question=self.question.name,
				question_type="Choices",
				answers='["A"]',
			)

	def test_f6_even_an_enrolled_student_must_have_opened_the_quiz(self):
		# Without the start stamp get_quiz_with_questions writes, there is no open attempt and no answer key.
		frappe.set_user(self.member)
		frappe.cache().delete_value(_quiz_start_key(self.quiz.name))
		with self.assertRaises(frappe.PermissionError):
			dispatch(
				"lms.lms.doctype.lms_quiz.lms_quiz.check_answer",
				quiz=self.quiz.name,
				question=self.question.name,
				question_type="Choices",
				answers='["A"]',
			)

	def test_f7_student_cannot_read_course_list_of_a_program_they_are_not_in(self):
		frappe.set_user(self.outsider)
		rows = client_get_list(
			"LMS Program Course",
			parent="LMS Program",
			filters={"parent": self.program.name, "parenttype": "LMS Program"},
			fields=["course"],
		)
		self.assertEqual(rows, [], "a program's course list must be invisible to someone not in it")

	def test_f8_student_cannot_enrol_another_user(self):
		# Refused outright, not rewritten, because a self-written enrolment would grant visibility.
		frappe.set_user(self.outsider)
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc(
				{"doctype": "LMS Enrollment", "member": self.member, "course": self.open_course.name}
			).insert()
		self.assertFalse(
			frappe.db.exists(
				"LMS Enrollment", {"member": self.member, "course": self.open_course.name}
			),
			"the named victim must have gained no enrolment at all",
		)
		self.assertFalse(
			frappe.db.exists(
				"LMS Enrollment", {"member": self.outsider, "course": self.open_course.name}
			),
			"and the forger must not have enrolled themselves either",
		)

	def test_f9_student_cannot_count_quizzes_outside_their_courses(self):
		frappe.set_user(self.outsider)
		self.assertEqual(client_get_count("LMS Quiz", filters={"name": self.quiz.name}), 0)

	# --- the over-block direction: the fix must not break the product --------------------------

	def test_an_assigned_batch_is_hidden_until_it_is_published(self):
		# Assignment says who and published says when, so the member sees the batch only once it is published.
		frappe.set_user(self.member)
		self.assertNotIn(
			self.batch.name, [b["name"] for b in dispatch("lms.lms.utils.get_batches")],
			"an unpublished draft reached the learner it is assigned to",
		)
		frappe.set_user("Administrator")
		frappe.db.set_value("LMS Batch", self.batch.name, "published", 1)
		self._reset_memo()
		frappe.set_user(self.member)
		self.assertIn(
			self.batch.name, [b["name"] for b in dispatch("lms.lms.utils.get_batches")],
			"publishing did not release the batch to the member assigned to it",
		)

	def test_outsider_does_not_see_the_batch_even_when_it_is_published(self):
		frappe.set_user("Administrator")
		frappe.db.set_value("LMS Batch", self.batch.name, "published", 1)
		frappe.set_user(self.outsider)
		names = [b["name"] for b in dispatch("lms.lms.utils.get_batches")]
		self.assertNotIn(self.batch.name, names, "published is not a permission")

	def test_privileged_author_still_sees_everything(self):
		frappe.set_user(self.author)
		self.assertTrue(lms_visibility.can_see_batch(self.batch.name))
		self.assertTrue(lms_visibility.can_see_course(self.course.name))
		self.assertTrue(lms_visibility.can_see_program(self.program.name))
		self.assertTrue(lms_visibility.can_see_quiz(self.quiz.name))
		self.assertEqual(client_get("LMS Quiz", self.quiz.name)["name"], self.quiz.name)

	def test_privileged_caller_may_still_enrol_someone_else(self):
		# The assignment flow: an admin enrols someone else.
		frappe.set_user(self.author)
		other = frappe.get_doc(
			{"doctype": "LMS Course", "title": f"{TAG}-course-2", "description": "x",
			 "short_introduction": "x", "instructors": [{"instructor": self.author}]}
		).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture seeding
		enrollment = frappe.get_doc(
			{"doctype": "LMS Enrollment", "member": self.outsider, "course": other.name}
		).insert(ignore_permissions=True)  # authz-ok: tier-a — drives the doc_event, not the DocPerm matrix
		self.assertEqual(enrollment.member, self.outsider, "an admin may name the member they are assigning")

	def test_assignment_reaches_the_course_through_the_batch(self):
		# `member` holds no LMS Enrollment for a batch-only course, and must still see it.
		frappe.set_user("Administrator")
		batch_only = frappe.get_doc(
			{"doctype": "LMS Course", "title": f"{TAG}-course-3", "description": "x",
			 "short_introduction": "x", "instructors": [{"instructor": self.author}]}
		).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture seeding
		self.batch.append("courses", {"course": batch_only.name})
		self.batch.published = 1  # both conditions: the member is in the batch AND the batch is live
		self.batch.save(ignore_permissions=True)  # authz-ok: tier-a — test fixture seeding
		frappe.db.set_value("LMS Course", batch_only.name, "published", 1)
		self._reset_memo()
		frappe.set_user(self.member)
		self.assertTrue(lms_visibility.can_see_course(batch_only.name))
		self.assertFalse(lms_visibility.can_see_course(batch_only.name, user=self.outsider))


class TestAColleaguesNameAndProgressStayHidden(IntegrationTestCase):
	"""Read off the migrated site, so dropping the level-1 declaration and migrating goes red."""

	def test_member_name_and_progress_need_permission_level_one(self):
		meta = frappe.get_meta("LMS Program Member")
		for fieldname in ("full_name", "progress"):
			with self.subTest(field=fieldname):
				self.assertGreaterEqual(meta.get_field(fieldname).permlevel, 1)
		readers = {p.role for p in frappe.get_all("Custom DocPerm", filters={"parent": "LMS Program", "permlevel": 1, "read": 1},
		                                          fields=["role"])}
		self.assertEqual(readers, {"System Manager", "Course Creator"})
