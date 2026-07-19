# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Authorization gates over native crm whitelisted methods that BYPASS the permission engine
(get_all / ignore_permissions / un-gated get_doc) — so neither the doctype matrix NOR a
doctype's has_permission hook can reach them.

Each wrapper asserts the caller may act on the target, then delegates to the UNCHANGED native
function. Wired via override_whitelisted_methods (hooks.py): the crm endpoint runs our gate
first; the native code runs verbatim once the gate passes. No crm fork.

The native function is imported DIRECTLY (not dispatched), so it never re-enters the override —
no recursion. Contract (same as the brain, §5): a read leak is gated on READ of the referenced
doc; a write/create is gated on the doctype's write/create. Row-scope is a separate layer.
"""
import frappe
from frappe import _
from frappe.utils import cint, now_datetime


def _require_read(doctype, name):
	frappe.has_permission(doctype, "read", name, throw=True)


# --- Telephony / Call Log (call_sid / call_log_name == CRM Call Log name) ----------------------
@frappe.whitelist()
def add_task_to_call_log(call_sid, task):
	_require_read("CRM Call Log", call_sid)
	from crm.integrations.api import add_task_to_call_log as _native

	return _native(call_sid, task)


@frappe.whitelist()
def add_note_to_call_log(call_sid, note):
	_require_read("CRM Call Log", call_sid)
	from crm.integrations.api import add_note_to_call_log as _native

	return _native(call_sid, note)


@frappe.whitelist()
def get_recording_url(call_log_name):
	_require_read("CRM Call Log", call_log_name)
	from crm.integrations.api import get_recording_url as _native

	return _native(call_log_name)


@frappe.whitelist()
def set_default_calling_medium(medium):
	frappe.has_permission("CRM Telephony Agent", "create", throw=True)
	from crm.integrations.api import set_default_calling_medium as _native

	return _native(medium)


# --- Generic doc (doctype + name supplied by caller) -------------------------------------------
@frappe.whitelist()
def get_assigned_users(doctype, name, default_assigned_to=None):
	_require_read(doctype, name)
	from crm.api.doc import get_assigned_users as _native

	return _native(doctype, name, default_assigned_to)


@frappe.whitelist()
def get_linked_docs_of_document(doctype, docname):
	_require_read(doctype, docname)
	from crm.api.doc import get_linked_docs_of_document as _native

	return _native(doctype, docname)


# --- Deal ---------------------------------------------------------------------------------------
@frappe.whitelist()
def create_deal(doc):
	frappe.has_permission("CRM Deal", "create", throw=True)
	from crm.fcrm.doctype.crm_deal.crm_deal import create_deal as _native

	return _native(doc)


@frappe.whitelist()
def get_deal_contacts(name):
	_require_read("CRM Deal", name)
	from crm.fcrm.doctype.crm_deal.api import get_deal_contacts as _native

	return _native(name)


# NOTE: crm's deal-contact MUTATORS (add_contact / remove_contact / set_primary_contact) already
# gate on `has_permission("CRM Deal", "write")` themselves, so they are NOT wrapped here — adding a
# second identical gate would be a redundant parallel path. Only engine-bypassing methods are wrapped.


# --- Contact lookup (no specific doc -> doctype-level READ gate) --------------------------------
@frappe.whitelist()
def get_contact_by_phone_number(phone_number):
	frappe.has_permission("Contact", "read", throw=True)
	from crm.integrations.api import get_contact_by_phone_number as _native

	return _native(phone_number)


@frappe.whitelist()
def get_contact_lead_or_deal_from_number(number):
	frappe.has_permission("Contact", "read", throw=True)
	from crm.integrations.api import get_contact_lead_or_deal_from_number as _native

	return _native(number)


# --- WhatsApp ----------------------------------------------------------------------------------
@frappe.whitelist()
def get_whatsapp_messages(reference_doctype, reference_name):
	_require_read(reference_doctype, reference_name)
	from crm.api.whatsapp import get_whatsapp_messages as _native

	return _attachment_details(_native(reference_doctype, reference_name))


def _attachment_details(rows):
	"""Stamp each attachment row with its File's real name and size, for the chat bubble.

	The bubble had a generic icon and the literal word "Document" — no name, no size, no type. All
	three exist on the File row, which is where display metadata is READ from (M3: a URL is not a
	filename and not a size; read the row). The message body is not a substitute: it is the provider's
	caption, absent on a caption-less image and equal to the filename only for documents.

	One query for the whole thread, keyed by file_url — not one per bubble.
	"""
	urls = [r.get("attach") for r in rows if r.get("attach")]
	if not urls:
		return rows
	files = frappe.get_all(
		"File", filters={"file_url": ["in", list(set(urls))]}, fields=["file_url", "file_name", "file_size"]
	)
	by_url = {f.file_url: f for f in files}
	for row in rows:
		found = by_url.get(row.get("attach"))
		if found:
			row["file_name"] = found.file_name
			row["file_size"] = found.file_size
	return rows


# --- CRM (assignment rules / saved views) ------------------------------------------------------
@frappe.whitelist()
def get_assignment_rules_list():
	# Native reads Assignment Rules for CRM Lead/Deal via get_all (engine-bypass). Gate on CRM Lead
	# read — every CRM user (Sales User/Manager) holds it; a no-App-Access user does not.
	frappe.has_permission("CRM Lead", "read", throw=True)
	from crm.api.assignment_rule import get_assignment_rules_list as _native

	return _native()


@frappe.whitelist()
def get_views(doctype=None):
	# doctype is OPTIONAL to native and the frontend calls it bare: gating a blank one raised DoesNotExist -> 404 for every non-Administrator.
	from crm.api.views import get_views as _native

	if doctype:
		frappe.has_permission(doctype, "read", throw=True)
		return _native(doctype)

	# Native annotates doctype as `str` and frappe enforces it, so an unnamed call passes "" — never None.
	# Unnamed: keep native's contract but drop views whose doctype the caller cannot read.
	return [v for v in _native("") if frappe.has_permission(v.get("dt"), "read")]


# --- Helpdesk (agent-only internal) ------------------------------------------------------------
@frappe.whitelist()
def get_article_stats(article_name):
	# Native reads view/like/dislike counts via db.get_value/count with NO perm check (engine-bypass,
	# S.7). HD Article is locked to agents (lockdown.py), so this read-gate now denies non-agents.
	_require_read("HD Article", article_name)
	from helpdesk.api.article import get_article_stats as _native

	return _native(article_name)


# --- LMS (internal training only — Mode 2) -----------------------------------------------------
# The LMS catalog endpoints are allow_guest + engine-bypass (get_all/get_value); a DocPerm lock can't
# reach them. We NARROW instead of gate (metamorphic — narrow, never widen): a non-privileged caller
# only ever sees PUBLISHED rows, so the draft-enumeration leak (filters={"published":0}) is closed
# without breaking browse/enrol for anyone who is allowed to see drafts.
_LMS_PRIVILEGED_ROLES = {"System Manager", "Moderator", "Course Creator"}


def _lms_privileged():
	"""True if the caller may see unpublished LMS content (author/moderator/admin)."""
	return bool(_LMS_PRIVILEGED_ROLES & set(frappe.get_roles()))


def _force_published(filters):
	"""Parse the request `filters` and force published=1 for a non-privileged caller."""
	if isinstance(filters, str):
		filters = frappe.parse_json(filters) or {}
	filters = dict(filters or {})
	if not _lms_privileged():
		filters["published"] = 1
	return filters


@frappe.whitelist(allow_guest=True)  # guest-ok: mirrors native allow_guest; _force_published narrows a non-privileged caller to published rows
def get_courses(filters=None, start=0):
	from lms.lms.utils import get_courses as _native

	return _native(_force_published(filters), start)


@frappe.whitelist(allow_guest=True)  # guest-ok: mirrors native allow_guest; _force_published narrows a non-privileged caller to published rows
def get_batches(filters=None, start=0, order_by="start_date"):
	from lms.lms.utils import get_batches as _native

	return _native(_force_published(filters), start, order_by)


@frappe.whitelist(allow_guest=True)  # guest-ok: mirrors native allow_guest; _lms_privileged gate strips the creator email for a non-privileged caller
def get_job_details(job):
	# Public job board by design; the only leak is the creator's email (`owner`). Strip it for a
	# non-privileged caller; the native return is otherwise unchanged.
	from lms.lms.api import get_job_details as _native

	data = _native(job)
	if data and not _lms_privileged():
		data.pop("owner", None)
	return data


# --- LMS quiz assessment integrity (VAPT Jul N2, N6) -------------------------------------------
# Not authz — these harden the quiz SUBMISSION against a race and a client-side-only timer. Same seam
# (wrap -> delegate to native), so no fork. N1 (server already re-grades) and N3 (check_answer already
# enforces show_answers) need NO wrapper — proven live; tests/security/test_lms_vapt_jul.py pins them.
_QUIZ_START_TTL = 24 * 60 * 60  # keep the start stamp a day so elapsed stays computable past the deadline
_QUIZ_GRACE_SEC = 30  # clock skew + in-flight submit; a real submit lands well inside this


def _quiz_start_key(quiz):
	return f"lms_quiz_start:{quiz}:{frappe.session.user}"


def _enforce_quiz_deadline(quiz):
	"""Best-effort server-side timer (N6). Rejects a submit that arrives after the quiz's duration from the
	recorded open. RESIDUAL: a client that never calls get_quiz_with_questions leaves no start stamp, so we
	cannot enforce and must allow — documented in the VAPT exception list. Closes the audit's exact PoC."""
	start = frappe.cache().get_value(_quiz_start_key(quiz))
	if not start:
		return
	duration = cint(frappe.db.get_value("LMS Quiz", quiz, "duration"))
	if not duration:
		return  # untimed quiz
	if now_datetime().timestamp() - float(start) > duration * 60 + _QUIZ_GRACE_SEC:
		frappe.throw(_("The time allotted for this quiz has elapsed."), frappe.ValidationError)


@frappe.whitelist()
def submit_quiz(quiz, results=None):
	# N2: the native single-attempt guard is a NON-locking count() then insert (LMSQuizSubmission.validate)
	# — a TOCTOU a single-packet attack bypasses (verified live: 12 concurrent submits -> 8 rows under a
	# max_attempts=1 quiz). A count() reads this txn's REPEATABLE-READ snapshot, so a peer's already-
	# committed submit is invisible; locking the quiz row does NOT help (the count still reads the snapshot).
	# Gate with a LOCKING read on the submission rows: FOR UPDATE reads the latest committed rows AND gap-
	# locks the (quiz, member) range, so concurrent submits serialize and the ceiling holds.
	from lms.lms.doctype.lms_quiz.lms_quiz import submit_quiz as _native
	from lms.lms.doctype.lms_quiz_submission.lms_quiz_submission import MaximumAttemptsExceededError

	_enforce_quiz_deadline(quiz)
	max_attempts = cint(frappe.db.get_value("LMS Quiz", quiz, "max_attempts"))
	if max_attempts:
		existing = frappe.db.sql(
			"SELECT name FROM `tabLMS Quiz Submission` WHERE quiz=%(q)s AND member=%(m)s FOR UPDATE",
			{"q": quiz, "m": frappe.session.user},
		)  # sqli-ok: constant identifiers; quiz + member bound via %(...)s
		if len(existing) >= max_attempts:
			frappe.throw(
				_("You have exceeded the maximum number of attempts ({0}) for this quiz").format(max_attempts),
				MaximumAttemptsExceededError,
			)
	return _native(quiz, results)


@frappe.whitelist()
def get_quiz_with_questions(quiz):
	# N6: stamp the open time so submit_quiz can reject a late replay. Stamp only if absent — a re-open must
	# not extend the clock. Delegates unchanged; the native call also carries its own has_lms_role gate.
	from lms.lms.utils import get_quiz_with_questions as _native

	data = _native(quiz)
	cache = frappe.cache()
	key = _quiz_start_key(quiz)
	if cache.get_value(key) is None:
		cache.set_value(key, now_datetime().timestamp(), expires_in_sec=_QUIZ_START_TTL)
	return data
