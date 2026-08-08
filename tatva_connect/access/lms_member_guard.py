# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Force `member` to the session user on LMS records a non-privileged user can create.

THE DISEASE (mass-assignment / ownership-spoof IDOR). `frappe.client.insert` accepts a client-supplied
`member`, and these LMS doctypes ship a `pass` controller that never checks it — so a student can create
a record attributed to any victim. Upstream cures this per-doctype (Assignment Submission, Certificate
Request force `member` from the session) but forgot the ones below. This is the same cure, one handler.

NOT the wildcard seam. `doc_events["*"]` is for cross-platform, structural concerns (XSS sanitising). This
is LMS-specific, so it is wired against ONLY these doctypes (hooks.py) — narrow blast radius, one line to
add a doctype. Scope-spoof (CRM grain) is a different axis and lives in its own controller (TatvaCRMLead).

LMS Enrollment is cured separately (TatvaLMSEnrollment) because its own before_insert validates against
`member` and must run first (hooks.py) — a validate hook here would land too late.
"""

import frappe
from frappe import _

from tatva_connect.access import lms_visibility


def enforce_member(doc, method=None):
	"""Reject a `member` that is not the caller, then pin it to the session user.

	Privilege is the ONE brain (`lms_visibility.is_privileged`) — the same author/moderator/evaluator/admin
	set every LMS read gate uses, so the write boundary can never drift from the read boundary."""
	if lms_visibility.is_privileged():
		return
	if doc.get("member") and doc.member != frappe.session.user:
		frappe.throw(_("You cannot create this record for another user."), frappe.PermissionError)
	doc.member = frappe.session.user
