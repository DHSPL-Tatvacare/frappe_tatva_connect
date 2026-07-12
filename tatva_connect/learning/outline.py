"""Shim for an upstream LMS race: a student never sees a course's contents.

CourseOverview.vue builds its outline resource with `auto: true` and
`course: props.course.data?.name`, but the parent's course fetch is still in flight when it mounts,
so the call goes out with no course. The native fn takes course as a required positional, so it
500s, and no watcher retries. Every student gets "Course Content coming soon!" on every course; a
moderator never sees it. Present in 2.55.0 and still on develop at 2.58.1.

Recovers the course from the Referer and delegates to the unchanged native fn. Delete when upstream
adds the watcher.
"""

import re
from urllib.parse import unquote

import frappe

from tatva_connect.access.native_guards import _lms_privileged

_COURSE_ROUTE = re.compile(r"/courses/([^/?#]+)")


def _published_course_from_referer() -> str | None:
	"""The course named by the Referer, NARROWED to one the caller may already see.

	The Referer is client-supplied, so it is a hint and never a fact: it names the course the browser
	claims to be on, and an unauthenticated caller can forge it to name any other. It is therefore
	honoured only for a course that caller could already reach — a non-privileged one gets a published
	course only, the same bound `native_guards._force_published` puts on the catalog, so a crafted
	Referer cannot read a DRAFT course's outline. Anything else resolves to None.
	"""
	referer = frappe.request.headers.get("Referer") if getattr(frappe, "request", None) else None
	if not referer:
		return None

	match = _COURSE_ROUTE.search(referer)
	if not match:
		return None

	filters: dict[str, object] = {"name": unquote(match.group(1))}
	if not _lms_privileged():
		filters["published"] = 1
	return frappe.db.get_value("LMS Course", filters, "name")


@frappe.whitelist(allow_guest=True)  # guest-ok: mirrors native allow_guest; _published_course_from_referer narrows a forged referer to a published course, and the native fn keeps its own guest gate
def get_course_outline(course: str | None = None, progress: bool = False) -> list:
	from lms.lms.utils import get_course_outline as _native

	# Only the RACE is repaired. A call that already names its course is passed straight through, so
	# the native fn — and its own `guest_access_allowed()` gate — decides exactly as it does today.
	course = course or _published_course_from_referer()
	if not course:
		return []

	return _native(course, progress)
