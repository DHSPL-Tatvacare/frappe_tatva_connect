# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Security audit Aug'26 — the 9 LMS findings, closed by ONE rule: am I in it, or do I run it.

Every finding in that audit was a variation of the same mistake: LMS is a public-marketplace product,
so it treats `published` as the visibility flag, and this deployment is internal staff training where
nobody browses and everybody is assigned. `access/lms_visibility.py` replaces `published` with
membership, and this module is the regression wall for it.

Discipline is the sibling suite's (tests/security/test_lms_assessment.py): seed a real object, drive
the REAL entry point as a real non-privileged persona, and assert the OUTCOME — the row is absent, the
call is refused, the field is gone — never that a function was called. Findings that travel through
`override_whitelisted_methods` are driven through `dispatch()` so the override is honoured; a direct
import would prove only that a helper exists.

The fixture is deliberately UNPUBLISHED throughout. Under the old rule that alone hid it; under the
new one it is irrelevant, and two tests below prove both halves of that — an assigned student SEES
their unpublished batch, an unassigned one does not see it even when published.

WHAT RED LOOKS LIKE ON TODAY'S CODE, per test:

  F1 batch_courses   — the Batch Course rows of a batch the student is not in come back; today there
                       is no LMS Batch condition, so the parent join constrains nothing. Asserted
                       empty, so today it fails on a non-empty list.
  F2 reviews         — get_reviews returns the review plus the reviewer's full name; today it has no
                       gate at all, so the PermissionError this test expects is never raised.
  F3 outline         — get_course_outline returns the chapter/lesson tree of an unenrolled course;
                       today only `guest_access_allowed()` stands in the way, so no raise.
  F4 program_members — the LMS Program Member rows of a program the student is not in come back with
                       every colleague's full_name; today no LMS Program condition exists, so the list
                       is non-empty and `full_name` is present.
  F5 quiz_get        — frappe.client.get("LMS Quiz", …) returns the quiz doc; today LMS Student holds
                       read=1 on LMS Quiz and no has_permission hook denies it, so no raise.
  F6 check_answer    — check_answer grades an option for a quiz the student never opened; today it
                       checks only that the question belongs to the quiz and that show_answers is on,
                       so it returns a correctness verdict instead of raising.
  F7 program_courses — same shape as F4 for LMS Program Course; today the list is non-empty.
  F8 enrolment_idor  — an LMS Enrollment inserted with another user's email is accepted and the
                       victim really gains it; today nothing refuses the insert, so the
                       PermissionError this test expects is never raised.
  F9 quiz_count      — get_count("LMS Quiz") counts every quiz on the site; today no LMS Quiz
                       condition exists, so the count includes the out-of-scope quiz.

  The three over-block tests are the mirror image and are RED for the opposite reason: `assigned
  student sees their unpublished batch` fails on TODAY's code because the old `published`
  filter hides it (this is the live bug the change repairs, not a new assertion); the privileged and
  admin-assignment tests pass today and must keep passing — they are the guard against the fix going
  too far, and they would go red on an over-broad implementation.

Run:
    bench --site dev.localhost run-tests --app tatva_connect \
        --module tatva_connect.tests.access.test_lms_membership_visibility
"""
import frappe
from frappe.client import get as client_get
from frappe.client import get_count as client_get_count
from frappe.client import get_list as client_get_list
from frappe.tests.utils import FrappeTestCase

from tatva_connect.access import lms_visibility
from tatva_connect.access.native_guards import _quiz_start_key

TAG = "lms-aug-audit"

# The brain memoises its membership sets per REQUEST; a test run is one request, so the fixtures a
# later test creates would be judged against an earlier test's answer. Reset the buckets per test.
_MEMO_BUCKETS = (
	"tatva_connect:lms_visible_batches",
	"tatva_connect:lms_visible_programs",
	"tatva_connect:lms_visible_courses",
)


def dispatch(cmd, **kwargs):
	"""Mirror frappe.handler.execute_cmd's override resolution, so override_whitelisted_methods is
	honoured — a direct import of the native function would bypass the guard and go falsely green."""
	for hook in (frappe.get_hooks("override_whitelisted_methods") or {}).get(cmd, []):
		cmd = hook
		break
	return frappe.call(frappe.get_attr(cmd), **kwargs)


def _mk_user(email, roles):
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": email.split("@")[0], "send_welcome_email": 0}
		).insert(ignore_permissions=True)  # authz-ok: tier-a — test persona seeding
	user = frappe.get_doc("User", email)
	user.add_roles(*roles)
	return email


class TestLMSMembershipVisibility(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# The faithful audit actor is a bare LMS Student assigned to NOTHING. `member` is the same role
		# with an assignment, and exists so every deny below is proved to be about membership rather
		# than about the role. `author` is privileged and also the fixture's required instructor.
		cls.outsider = _mk_user(f"{TAG}-outsider@example.com", ["LMS Student"])
		cls.member = _mk_user(f"{TAG}-member@example.com", ["LMS Student"])
		cls.author = _mk_user(f"{TAG}-author@example.com", ["Course Creator"])

	@classmethod
	def _purge(cls):
		"""Delete every fixture this class owns, newest dependency first.

		The fixtures are named from their titles, so they collide on a re-run. `FrappeTestCase` rolls
		the DB back per test, but lms commits inside its own enrolment cascade, so a row can outlive the
		rollback that was meant to remove it — and the next test's insert then hits a duplicate primary
		key. Purging first makes the seed idempotent whatever the previous run left behind.
		"""
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
		# F8's subject must be a course the VICTIM is eligible for, or lms's own before_insert refuses the
		# insert (unpublished / already-enrolled) and the IDOR never gets a chance to be proved.
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
		# `member` joins the BATCH and lms's own cascade mirrors the course enrolment; seeding both is a duplicate lms refuses.
		frappe.get_doc(
			{"doctype": "LMS Batch Enrollment", "member": self.member, "batch": self.batch.name}
		).insert(ignore_permissions=True)  # authz-ok: tier-a — test fixture seeding
		# Seeded as the enrolled member, after the enrolment: lms refuses a review from anyone not in the course.
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

	def test_f4_member_pii_is_behind_a_permission_level(self):
		# The second half of F4: even a legitimate reader must not get a colleague's name and progress
		# at permlevel 0. The grant lives in lockdown.FIELD_LEVELS against the PARENT doctype.
		from tatva_connect.access import lockdown

		self.assertEqual(lockdown._PERMLEVEL_1_FIELDS["LMS Program Member"], ("full_name", "progress"))
		self.assertIn("LMS Program", lockdown.FIELD_LEVELS)
		self.assertEqual(
			set(lockdown.FIELD_LEVELS["LMS Program"][1]),
			{"System Manager", "Course Creator"},
		)

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
		# Membership alone is not "I am taking it": without the start stamp get_quiz_with_questions
		# writes, there is no open attempt and the answer key stays shut.
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
		# Refused outright, not rewritten: an enrolment a student writes for themselves would MINT the visibility this whole rule decides.
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
		# Assignment says WHO, published says WHEN. An author's draft is not a learner's training, so the
		# assigned member sees nothing until the author publishes — and then sees it immediately.
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
		# The assignment flow. Breaking this breaks the product.
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
