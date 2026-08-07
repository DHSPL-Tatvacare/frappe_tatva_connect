# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""LMS row visibility — the hook entry points.

Thin consumers of `access/lms_visibility.py`, in the shape of `tasks/permissions.py` and
`notes/permissions.py`: the rule lives once in the brain, and these functions only carry it into the
two seams frappe offers for a row — `permission_query_conditions` for a list or a count, and
`has_permission` for a single-doc read. Who an enrolment is FOR is decided a step earlier, in the
`LMS Enrollment` controller (`access/lms_enrollment.py`), because no hook can precede lms's own.

A condition here is a NAME LIST, not a join. The rule is decided in Python (this batch is mine, this
course sits in a program I am in) and this module only spells the answer as SQL, so there is no second
matcher to drift out of step with the first. An internal training deployment holds tens of batches and
programs, not thousands.

THE CHILD DOCTYPES NEED NO ENTRY HERE, and that is a measured fact rather than an assumption.
`frappe.client.get_list` on `Batch Course` / `LMS Program Member` / `LMS Program Course` resolves its
permissions against the PARENT: frappe sets `permission_doctype = parent_doctype or self.doctype`
(frappe/database/query.py:275), joins the parent table in, and applies the PARENT's
`permission_query_conditions` (frappe/database/query.py:1547-1558, 1609-1620). A call that names no
parent is refused outright before any row is read (frappe/permissions.py:826-831). So registering
`LMS Batch` and `LMS Program` is what closes the child-table reads, and a `Batch Course` entry would
be dead code. `frappe.client.get_count` inherits the same conditions — it wraps the fully filtered
subquery in a COUNT (frappe/desk/reportview.py:70-71) — which is what closes the quiz-count read.
"""
import frappe

from tatva_connect.access import lms_visibility


def _names_clause(doctype, column, names):
	"""`table.column in (…)` for a decided-in-Python answer, or `1=0` when the caller is in nothing.

	`1=0` is the only honest empty answer: returning "" would mean "no extra conditions" and silently
	widen the list to every row — the same trap `access/visibility.py` documents.
	"""
	if not names:
		return "1=0"
	quoted = ", ".join(frappe.db.escape(n) for n in sorted(names))
	return f"`tab{doctype}`.`{column}` in ({quoted})"


def get_batch_permission_query_conditions(user=None):
	if lms_visibility.is_privileged(user):
		return ""
	return _names_clause("LMS Batch", "name", lms_visibility.visible_batches(user))


def get_program_permission_query_conditions(user=None):
	if lms_visibility.is_privileged(user):
		return ""
	return _names_clause("LMS Program", "name", lms_visibility.visible_programs(user))


def get_quiz_permission_query_conditions(user=None):
	# A quiz is scoped by the course it belongs to, so the clause names that column rather than listing
	# every quiz — one row per visible course instead of one per quiz, same rule either way.
	if lms_visibility.is_privileged(user):
		return ""
	return _names_clause("LMS Quiz", "course", lms_visibility.visible_courses(user))


def has_batch_permission(doc, ptype, user):
	return lms_visibility.can_see_batch(doc.get("name"), user)


def has_program_permission(doc, ptype, user):
	return lms_visibility.can_see_program(doc.get("name"), user)


def has_quiz_permission(doc, ptype, user):
	# The quiz doc is already in hand, so its own `course` column answers without a second read.
	return lms_visibility.can_see_course(doc.get("course"), user)
