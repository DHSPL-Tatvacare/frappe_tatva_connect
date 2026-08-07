"""Shim for an upstream LMS race: a student never sees a course's contents.

CourseOverview.vue builds its outline resource with `auto: true` and
`course: props.course.data?.name`, but the parent's course fetch is still in flight when it mounts,
so the call goes out with no course. The native fn takes course as a required positional, so it
500s, and no watcher retries. Every student gets "Course Content coming soon!" on every course; a
moderator never sees it. Present in 2.55.0 and still on develop at 2.58.1.

THIS MODULE RESOLVES A COURSE AND DECIDES NOTHING. It used to carry its own whitelisted endpoint and
its own `published` bound, which was right when nothing else gated the outline. `access/native_guards
.get_course_outline` now owns that door and asks `lms_visibility.require_course`, so a second endpoint
here would be a second, ungated way in, and a second bound here would be a second rule to drift. The
`published` narrowing is gone for the same reason it is gone everywhere else: it hid a student's own
UNPUBLISHED course from the recovery path while membership said they may read it.

A forged Referer therefore buys nothing — it can name any course, and the caller's gate refuses the
ones the caller is not in.

Delete when upstream adds the watcher.
"""

import re
from urllib.parse import unquote

import frappe

_COURSE_ROUTE = re.compile(r"/courses/([^/?#]+)")


def _course_from_referer() -> str | None:
	"""The course the Referer names, or None. A hint, never a fact — the caller gates it."""
	referer = frappe.request.headers.get("Referer") if getattr(frappe, "request", None) else None
	if not referer:
		return None

	match = _COURSE_ROUTE.search(referer)
	if not match:
		return None

	return frappe.db.get_value("LMS Course", unquote(match.group(1)), "name")
