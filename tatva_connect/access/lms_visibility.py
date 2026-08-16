# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""LMS visibility — the ONE brain for "which training may this user see".

Frappe LMS is built for a public course marketplace: a stranger browses a catalogue, self-enrols, and
`published` is what puts a course in the shop window, and anyone may browse the window. THIS DEPLOYMENT
IS INTERNAL STAFF TRAINING: there is no catalogue and no self-enrolment, so `published` alone was never
enough — it let a learner see training nobody assigned them. TWO conditions decide visibility on every
surface: AM I IN IT, AND IS IT LIVE? Assignment says who, `published` says when, and a non-privileged
caller needs both.

    LMS Batch    an LMS Batch Enrollment names me AND it is published, or I instruct the batch
    LMS Program  an LMS Program Member row names me AND it is published
    LMS Course   an LMS Enrollment names me, or it sits in a batch or program I am in — AND it is
                 published — or I instruct the course
    LMS Quiz     the quiz's course is one I can see

This is STRICTER than the audit asked for: published-but-unassigned content goes dark too, which is the
half `published` alone could never express. An assigned but UNPUBLISHED batch stays hidden, deliberately
— an author's draft is not a learner's training, and the Batches page was already right to hide it.

"Or do I run it" is the second half of the same question, and it is why `Course Instructor` counts:
lms's own `can_modify_course` / `can_modify_batch` say an instructor runs the thing, and lms's catalog
endpoints let an author narrow to what they `created` — a filter that would otherwise reach past this
rule rather than inside it.

Privileged roles see everything, because they author and administer it. The set is DECLARED here and not
imported from lms, even though lms spells a similar set in `utils.PRIVILEGED_ROLES` and in
`LMS Quiz.check_answer`: a security boundary of ours must not move when an upstream constant does.

Membership is read with `frappe.get_all`, which bypasses the permission engine — deliberately, so
building a permission_query_conditions clause cannot recurse back into the hook that asked for it.
Every one of these reads is keyed on the caller's own user or on a set already resolved from it, so
the bypass never reaches a row the rule has not already admitted.
"""
import frappe
from frappe import _

from tatva_connect.access import request_cache

# Moderator is lms's own staff test (`has_moderator_role`), so our read boundary agrees with it or a Moderator is scoped like a learner on batches they administer.
PRIVILEGED_ROLES = frozenset({"System Manager", "Moderator", "Course Creator", "Batch Evaluator"})


def is_privileged(user=None):
	"""Author, evaluator or administrator — sees every batch, program, course and quiz."""
	user = user or frappe.session.user
	return user == "Administrator" or bool(PRIVILEGED_ROLES & set(frappe.get_roles(user)))


def _live(doctype, names):
	"""Assignment says WHO may see it; `published` says WHEN. A non-privileged caller needs both."""
	if not names:
		return set()
	return set(frappe.get_all(
		doctype, filters={"name": ["in", sorted(names)], "published": 1}, pluck="name"
	))  # authz-ok: tier-c — narrowing a set the rule has already admitted


def _instructs(parenttype, user):
	"""The batches or courses this user is a Course Instructor on — lms's own "runs it" linkage."""
	return frappe.get_all(
		"Course Instructor", filters={"instructor": user, "parenttype": parenttype}, pluck="parent"
	)  # authz-ok: tier-c — self-scoped read of the caller's own instructor rows


def _child_courses(doctype, parents):
	"""The courses a child table names under `parents`; nothing at all when the caller is in no parent."""
	if not parents:
		return []
	return frappe.get_all(
		doctype, filters={"parent": ["in", sorted(parents)]}, pluck="course"
	)  # authz-ok: tier-c — `parents` is already the caller's admitted set


def visible_batches(user=None):
	"""Every LMS Batch this caller is in or instructs. Request-cached: a list read asks once."""
	user = user or frappe.session.user

	def build():
		enrolled = frappe.get_all(
			"LMS Batch Enrollment", filters={"member": user}, pluck="batch"
		)  # authz-ok: tier-c — self-scoped on member
		# Published applies to what you are IN, never to what you RUN: an author must still see their own draft.
		return _live("LMS Batch", enrolled) | set(_instructs("LMS Batch", user))

	return request_cache("tatva_connect:lms_visible_batches", user, build)


def visible_programs(user=None):
	"""Every LMS Program whose member table names this caller. A program has no instructor table."""
	user = user or frappe.session.user

	def build():
		member_of = frappe.get_all(
			"LMS Program Member", filters={"member": user}, pluck="parent"
		)  # authz-ok: tier-c — self-scoped on member
		return _live("LMS Program", member_of)

	return request_cache("tatva_connect:lms_visible_programs", user, build)


def visible_courses(user=None):
	"""Every LMS Course this caller is enrolled in, reaches through a batch or program, or instructs."""
	user = user or frappe.session.user

	def build():
		enrolled = frappe.get_all(
			"LMS Enrollment", filters={"member": user}, pluck="course"
		)  # authz-ok: tier-c — self-scoped on member
		reached = (
			set(enrolled)
			| set(_child_courses("Batch Course", visible_batches(user)))
			| set(_child_courses("LMS Program Course", visible_programs(user)))
		)
		return _live("LMS Course", reached) | set(_instructs("LMS Course", user))

	return request_cache("tatva_connect:lms_visible_courses", user, build)


def can_see_batch(batch, user=None):
	return is_privileged(user) or batch in visible_batches(user)


def can_see_program(program, user=None):
	return is_privileged(user) or program in visible_programs(user)


def can_see_course(course, user=None):
	return is_privileged(user) or course in visible_courses(user)


def can_see_quiz(quiz, user=None):
	"""A quiz is visible exactly when its course is.

	`LMS Quiz.course` is fetched from the lesson the quiz is embedded in, so it IS the "sits in a
	lesson of a course" link and there is no chapter chain to walk. A quiz embedded nowhere carries no
	course and so reaches nobody but a privileged caller.
	"""
	return is_privileged(user) or can_see_course(frappe.db.get_value("LMS Quiz", quiz, "course"), user)


def require_course(course):
	"""Deny unless the caller is in this course — the wrappers' one-line question."""
	if not can_see_course(course):
		frappe.throw(_("You are not enrolled in this course."), frappe.PermissionError)


def require_quiz(quiz):
	"""Deny unless the caller is in the course this quiz belongs to."""
	if not can_see_quiz(quiz):
		frappe.throw(_("You are not enrolled in this quiz's course."), frappe.PermissionError)
