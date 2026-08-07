# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""LMS Enrollment — the server names who an enrolment is FOR, before lms validates it.

An enrolment is not a record about the person who created it; it is a record about its `member`, and
`member` arrives from the request. `if_owner` does not help: frappe makes the creator the owner, so a
student who inserts a row naming a colleague owns the forgery and passes every own-records check.

THIS IS A CONTROLLER, NOT A doc_event, AND THAT IS THE WHOLE POINT. `Document.run_method` composes the
controller method FIRST and every hooked handler after it (frappe/model/document.py:1580-1581), so the
earliest doc_event frappe offers still lands after `LMSEnrollment.before_insert` has already run
`validate_duplicate_enrollment` and `validate_course_enrollment_eligibility` — both of which read
`self.member`. A hook could only correct the field once those had judged the wrong person: the
duplicate check would clear a row that duplicates the caller's own, and the eligibility check would
admit a course on the strength of a colleague's batch membership. Naming the member here, before
`super()`, means lms's own validations see the truth and need no compensation afterwards.

Enrolment is also the rule's own back door. `lms_visibility.visible_courses` reads LMS Enrollment, so a
row a student writes for themselves would MINT the visibility the rest of this package decides — the
whole membership rule bypassed by one insert. Internal training has no self-enrolment: a non-privileged
caller may only carry an enrolment that lms's own batch cascade is creating for a batch they are
already in (lms_batch_enrollment.py validate_course_enrollment), and nothing else.
"""
import frappe
from frappe import _
from lms.lms.doctype.lms_enrollment.lms_enrollment import LMSEnrollment

from tatva_connect.access import lms_visibility


class TatvaLMSEnrollment(LMSEnrollment):
	def before_insert(self):
		self._name_the_member()
		self._refuse_self_enrollment()
		super().before_insert()

	def validate(self):
		# An edit is re-decided, never trusted: `member` is as writable on update as it is on insert.
		self._name_the_member()
		super().validate()

	def _name_the_member(self):
		"""A privileged caller MAY name someone else — that is the assignment flow, and it must work."""
		if lms_visibility.is_privileged():
			return
		self.member = frappe.session.user

	def _refuse_self_enrollment(self):
		if lms_visibility.is_privileged() or self._rides_a_batch_the_member_is_in():
			return
		frappe.throw(
			_("Training is assigned, not self-selected. Ask your manager to add you to this course."),
			frappe.PermissionError,
		)

	def _rides_a_batch_the_member_is_in(self):
		"""lms mirrors one LMS Enrollment per Batch Course row when a member joins a batch."""
		return bool(self.enrollment_from_batch) and frappe.db.exists(
			"LMS Batch Enrollment", {"batch": self.enrollment_from_batch, "member": self.member}
		)
