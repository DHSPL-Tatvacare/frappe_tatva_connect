# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""The Desk form's read + command surface for CRM Intake Form. Permission gates that DELEGATE.

Nothing here writes a Web Form. `list_target_fields` delegates to the mapping seam,
`toggle_published` to `builder.publish`, `form_state` to `builder.readiness` — so the field list,
the publish write and the readiness decision each live in exactly one place, and this module is
only the gate in front of them. Every Desk method gates on
`frappe.has_permission("CRM Intake Form", ..., throw=True)`, the same discipline as every other
tatva_connect whitelisted method. No DDL, no eval, no user-string execution.

The one exception is `check_existing_patient`, which the PUBLIC form calls: it has no Desk
permission to gate on (the visitor is a Guest by design), so it self-gates instead — see its
own docstring for what it will and will not answer.
"""
import frappe
from frappe import _
from frappe.rate_limiter import rate_limit

from tatva_connect.intake import builder

# ONE resolver, shared with the save-time validation — target_table -> the doctype whose
# fields can be picked (lead = CRM Lead; child tables -> the child doctype; note -> None).
from tatva_connect.intake.intake import target_doctype as _resolve_doctype
from tatva_connect.lead import mapping
from tatva_connect.whatsapp.phone import to_e164


@frappe.whitelist()
def list_target_fields(target_table, intake_form=None, vertical=None, group=None, program=None):
	"""The fields this form may map into `target_table`, as [{fieldname, label, fieldtype}].

	The list is the ONE mapping seam (`lead/mapping.py`), scoped to the section AND to the form's grain —
	NOT a raw get_meta walk, which offered every column on the doctype including ones this grain must
	never write. The grain axes come from the CALLER (the open, possibly unsaved builder form), so the
	list narrows the moment an operator picks a grain — no save, no round trip.

	Gated read-only: requires read on CRM Intake Form (System-Manager-only doctype).
	"""
	frappe.has_permission("CRM Intake Form", "read", throw=True)

	if not _resolve_doctype(target_table):
		return []  # note (free-text) or unknown table -> nothing to pick

	grain = ((vertical or "").strip(), (group or "").strip(), (program or "").strip())
	if not any(grain):
		return []  # no grain chosen yet — the client shows "pick the grain first"

	return [{"fieldname": f["fieldname"], "label": f["label"], "fieldtype": f["fieldtype"]}
	        for f in mapping.mappable_fields(section=target_table, grain=grain)]


@frappe.whitelist()
def toggle_published(intake_form):
	"""Take one form live, or withdraw it. Gated on write to CRM Intake Form, then DELEGATED —
	`builder` is the one writer of the Web Form, exactly as `list_target_fields` delegates to
	`mapping`. Returns the new published state."""
	frappe.has_permission("CRM Intake Form", "write", throw=True)

	cfg = frappe.get_doc("CRM Intake Form", intake_form)
	return builder.publish(cfg, not _published(cfg))


@frappe.whitelist()
def form_state(intake_form):
	"""Everything the Desk form paints, in ONE call: is it live, at what address, why it cannot go
	live, and the grain it stamps. The script decides none of this — `readiness` is the server's
	one answer, and the script only renders it (N3)."""
	frappe.has_permission("CRM Intake Form", "read", throw=True)

	cfg = frappe.get_doc("CRM Intake Form", intake_form)
	return {
		# The Web Form NAME (autonamed off the title), never the route — they coincide by accident.
		"web_form": builder.web_form_name_for(cfg),
		"published": _published(cfg),
		"route": cfg.route,
		"reasons": builder.readiness(cfg),
		"grain": {
			"vertical": cfg.custom_vertical,
			"group": cfg.custom_group,
			"program": cfg.custom_current_program,
			"source": cfg.source,
		},
	}


def _published(cfg) -> bool:
	"""Is this form's Web Form live? One source, already indexed — no shadow flag."""
	wf_name = builder.web_form_name_for(cfg)
	return bool(wf_name and frappe.db.get_value("Web Form", wf_name, "published"))


# The two intake switches this module reads. Named, not typed twice — a mistyped key reads as
# "disabled" and nothing goes red, which is the one way a switch check fails silently.
_INTAKE_SWITCH = "Lead::Enrolment::intake"
_RATE_SWITCH = "Intake::RateLimit::enforcement"


def _checks_per_hour():
	"""The cap, read at call time off `CRM Intake Settings` (blank falls back to the intake DEFAULTS).

	Passed to frappe's limiter as a CALLABLE, which is what its `limit` parameter is for
	(`rate_limiter.py`: `_limit = limit() if callable(limit) else limit`) — so an operator raising the
	cap takes effect on the next request, with no reload and no second copy of the number here."""
	from tatva_connect.intake import guards

	return guards.rate_cap("checks_per_hour")


@rate_limit(key="web_form", limit=_checks_per_hour, seconds=3600)
def _spend_check_budget():
	"""One unit of frappe's OWN per-IP limiter, spent for one answerable question.

	Frappe's decorator, used as frappe uses it — it keys on `request_ip` + the named form field
	(`key="web_form"`, so one form's traffic cannot exhaust another's), scopes the counter to this
	`cmd`, and refuses with its own `RateLimitExceededError`. Nothing is hand-rolled here.

	It is a separate function because the decorator counts every call it wraps, and this must be
	counted only when the operator has ARMED intake rate limiting and only for a number that is
	actually a number — see the caller for both.

	The refusal is NOT caught. A visitor who has hit the cap must be told plainly, in frappe's own
	words, rather than shown "no existing patient" and left to find out at submit; a public form that
	goes quiet is what generates the complaint nobody can explain.
	"""
	return None


@frappe.whitelist(allow_guest=True, methods=["POST"])  # guest-ok: the enrolment forms are anonymous by design, so the visitor typing the number IS a Guest; self-gated below to an enabled intake form whose contract asked for the warning, answers a bare yes/no about ONE number the caller already typed, and spends frappe's own per-IP rate limit for each one
def check_existing_patient(web_form, phone):
	"""Is the number just typed already a lead on this form's line — asked BEFORE the form is filled.

	The whole feature: a rep or patient typing a number the CRM already holds on this form's
	Vertical + Group is told so, and decides whether to carry on. Answering here — on the field's own
	change event — rather than at submit is deliberate: frappe's website `frappe.call`
	(`website.js`, the one a public form gets, NOT the Desk one) has no error-handler seam, so a
	refusal raised at submit reaches the visitor as a dead-end msgprint they cannot get past. The
	submit path is therefore untouched by this feature and cannot start failing because of it.

	It answers "no" — never an error — for every state that is not a real question: a form whose flag
	is off, a site whose intake switch is off, a Web Form that is not an intake sink, and a number
	still being typed. The ONE thing it does raise is the rate limit, which the visitor must see.

	The merge itself is unchanged: submitting still folds onto the existing lead through the one
	brain (`api.partner._upsert_one`), whatever this answered.

	Returns `{"exists": bool, "message": str}` — the message is composed HERE, so the client holds no
	business text, and it names only the group the form itself already displays.
	"""
	from tatva_connect import automation
	from tatva_connect.intake.intake import _intake_doctypes
	from tatva_connect.lead.leads import existing_lead

	no = {"exists": False, "message": ""}

	# Self-gate: only an ENABLED intake form's own Web Form may ask, resolved through the ONE brain
	# that says what an intake sink is — never a caller-named doctype.
	sink = frappe.db.get_value("Web Form", web_form, "doc_type") if web_form else None
	intake_form = _intake_doctypes().get(sink) if sink else None
	if not intake_form:
		return no
	# No fold means no merge to warn about: with the feature switch off a submission lands nowhere.
	if not automation.is_enabled(_INTAKE_SWITCH):
		return no
	cfg = frappe.get_cached_doc("CRM Intake Form", intake_form)
	if not cfg.get("warn_if_already_enrolled"):
		return no

	mobile = _lookup_phone(phone)
	if not mobile:
		return no  # still being typed — not a question, and it costs the visitor no budget

	# Armed exactly like every other intake limiter, and spent only now: the budget buys ANSWERS, so a
	# half-typed number can never spend it — the field fires on a debounced keystroke AND on blur.
	if automation.is_enabled(_RATE_SWITCH):
		_spend_check_budget()

	if not existing_lead(mobile, cfg.custom_vertical, cfg.custom_group):
		return no
	return {"exists": True, "message": _already_enrolled_message(cfg)}


def _lookup_phone(raw):
	"""The number in the form leads are STORED under, or "" if it is not a number yet.

	`to_e164` is the one brain for that and it REFUSES what it cannot shape — right for a write, wrong
	here: this is a LOOKUP on a field that fires while the visitor is still typing, and "+91-98" has an
	answer (nothing), not an error. `api._base._norm_phone` reads a partner's search the same way; it is
	not reused because it passes the unshapeable value THROUGH, and this caller has to be able to tell a
	real number from a fragment before it spends the visitor's rate-limit budget on one.
	"""
	try:
		return to_e164(frappe.cstr(raw or ""))
	except frappe.ValidationError:
		frappe.clear_last_message()  # a fragment is not news; the visitor is mid-keystroke
		return ""


def _already_enrolled_message(cfg) -> str:
	"""The warning, naming the group whose lead was found — the axis the anchor actually keys on.

	The programme is deliberately absent: two programmes of one group share ONE lead (program is not
	identity), so a hit says the patient is on the GROUP and naming a programme could be false.
	"""
	where = _(" in {0}").format(cfg.custom_group) if cfg.custom_group else ""
	return _(
		"This patient is already enrolled{0}. Submitting this form will update their existing "
		"details. Continue?"
	).format(where)
