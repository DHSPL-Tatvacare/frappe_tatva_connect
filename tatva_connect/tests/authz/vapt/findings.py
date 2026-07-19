# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Layer 3 — the Jun'26 VAPT findings as GROUND TRUTH for coverage + recall, NOT hand-written pins.

Each finding names the PRIMITIVE that reproduces it: its endpoint (a key in endpoints.{GENERIC,APP}_
ENDPOINTS) and, for a doctype-parametric endpoint, the doctype. The generated endpoint sweep
(cases.generate_http_cases) MUST produce a case covering every finding; `uncovered()` proves the
generator is not blind to any known hole. The actual HTTP request is BUILT from the primitive
(endpoints.build_params) at run time — never copied out of the PDF here.

So the report is used the RIGHT way: it seeds the sensitive-doctype/endpoint primitives and validates
coverage. Add a finding = add its (endpoint_key, doctype) tuple; if the generator doesn't already emit
that case, coverage fails and we extend a primitive (never a bespoke test).
"""


def _v(id, severity, endpoint_key, doctype=None, page=None):
	return {"id": id, "severity": severity, "endpoint_key": endpoint_key, "doctype": doctype, "page": page}


# id, severity, the endpoint primitive that reproduces it, and (for parametric endpoints) the doctype.
VAPT_FINDINGS = [
	_v("vapt-01-comment-set-value", "P1", "client-set-value", "Comment", page=5),
	_v("vapt-02-hd-ticket-contact", "P2", "hd-ticket-contact", "HD Ticket", page=8),
	_v("vapt-03-hd-list-data", "P2", "hd-list-data", "HD Ticket", page=11),
	_v("vapt-04-lms-job-details", "P2", "lms-job-details", "Job Opportunity", page=15),
	_v("vapt-05-hd-ticket-get", "P2", "client-get", "HD Ticket", page=28),
	_v("vapt-06-lms-job-opportunities", "P2", "lms-job-opportunities", "Job Opportunity", page=31),
	_v("vapt-07-lms-batches", "P2", "lms-batches", "LMS Batch"),
	_v("vapt-08-crm-get-data-contacts", "P2", "crm-get-data", "Contact", page=21),
	_v("vapt-09-contact-delete", "P2", "client-delete", "Contact", page=22),
	_v("vapt-10-hd-ticket-activities", "P3", "hd-ticket-activities", "HD Ticket", page=18),
	_v("vapt-11-crm-call-log-read", "P3", "client-get", "CRM Call Log"),
	_v("vapt-12-private-file-link", "P3", "client-get", "File"),
	_v("vapt-13-crm-deal-contacts", "P3", "crm-get-data", "CRM Deal"),
	_v("vapt-14-getdoc-docinfo", "P3", "client-get", "CRM Lead"),
	_v("vapt-15-lms-courses", "P3", "lms-courses", "LMS Course"),
	_v("vapt-16-crm-call-log-modify", "P3", "client-set-value", "CRM Call Log"),
	_v("vapt-17-contact-create", "P3", "client-insert", "Contact"),
	_v("vapt-18-lms-programs", "P3", "lms-programs", "LMS Program"),
	_v("vapt-19-assigned-users", "P3", "client-get-list", "ToDo"),
	_v("vapt-20-linked-docs", "P3", "linked-docs", "CRM Lead"),
	_v("vapt-21-crm-deal-create", "P3", "client-insert", "CRM Deal"),
	_v("vapt-22-article-stats", "P3", "lms-article-stats"),
	_v("vapt-23-assignment-rules-list", "P3", "client-get-list", "Assignment Rule"),
	_v("vapt-24-installed-apps", "P4", "installed-apps"),
	# --- Jul cut: the 6 NEW LMS findings (report total 32). Each names the primitive that REACHES the
	# surface; two of them (N4/N5 answer-key strip, N2/N6 race+timer) are judged by the field oracle and
	# the integrity module respectively, because a row oracle cannot see a field strip or a race.
	_v("vapt-25-lms-testcase-list", "P2", "client-get-list", "LMS Programming Exercise"),
	_v("vapt-26-lms-exercise-get", "P2", "client-get", "LMS Programming Exercise"),
	_v("vapt-27-lms-quiz-submit-race", "P2", "lms-submit-quiz", "LMS Quiz"),
	_v("vapt-28-lms-quiz-timer", "P2", "lms-submit-quiz", "LMS Quiz"),
	_v("vapt-29-lms-check-answer", "P2", "lms-check-answer", "LMS Quiz"),
	_v("vapt-30-lms-quiz-questions", "P2", "lms-quiz-questions", "LMS Quiz"),
]

# Known-benign residual escalations (verified live 2026-07-09, real DB): the wire stays LIVE (a NEW
# escalation is the real signal), these are just non-sensitive by nature.
#   * File list endpoints (client.get_list / reportview.get / hd-list-data) return the public folder
#     SKELETON only (Home, Home/Attachments, ... — is_folder=1, is_private=0), never private content.
#   * get_apps returns only the caller's own role-entitled app (e.g. LMS for an LMS Student), not the
#     full namespace.
# A File row with is_folder=0/is_private=1, or an app the caller has no role for, WOULD be a true leak.


def _covers(finding, case):
	"""A generated case covers a finding iff it hits the same endpoint (and doctype, for a parametric one)."""
	if case.endpoint_key != finding["endpoint_key"]:
		return False
	return finding["doctype"] in (None, case.doctype)


def uncovered(generated_cases):
	"""Finding ids that NO generated case reproduces — a blind spot in the generator (fail the build)."""
	return [f["id"] for f in VAPT_FINDINGS if not any(_covers(f, c) for c in generated_cases)]
