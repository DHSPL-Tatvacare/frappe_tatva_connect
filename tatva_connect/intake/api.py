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

from tatva_connect.intake import builder, layers

# ONE resolver, shared with the save-time validation — target_table -> the doctype whose
# fields can be picked (lead = CRM Lead; child tables -> the child doctype; note -> None).
from tatva_connect.intake.intake import target_doctype as _resolve_doctype
from tatva_connect.lead import mapping
from tatva_connect.whatsapp.phone import to_e164


@frappe.whitelist()
def list_target_fields(target_table, intake_form=None, vertical=None, group=None, program=None, target=None):
	"""The fields this form may map into `target_table`, as [{fieldname, label, fieldtype}].

	The list is the ONE mapping seam (`lead/mapping.py`), scoped to the section AND to the form's grain —
	NOT a raw get_meta walk, which offered every column on the doctype including ones this grain must
	never write. The grain axes come from the CALLER (the open, possibly unsaved builder form), so the
	list narrows the moment an operator picks a grain — no save, no round trip.

	Gated read-only: requires read on CRM Intake Form (System-Manager-only doctype).
	"""
	frappe.has_permission("CRM Intake Form", "read", throw=True)

	if target in layers.LAYERS:
		return layers.fields_of(target, target_table)

	if not _resolve_doctype(target_table):
		return []  # note (free-text) or unknown table -> nothing to pick

	grain = ((vertical or "").strip(), (group or "").strip(), (program or "").strip())
	if not any(grain):
		return []  # no grain chosen yet — the client shows "pick the grain first"

	return [{"fieldname": f["fieldname"], "label": f["label"], "fieldtype": f["fieldtype"]}
	        for f in mapping.mappable_fields(section=target_table, grain=grain)]


@frappe.whitelist()
def target_profile(target=None):
	"""What the builder offers for a target, in one call: where answers may land (none for a lead, which reads its sections) and the list Source picks from."""
	frappe.has_permission("CRM Intake Form", "read", throw=True)
	target = target or layers.LEAD
	return {
		"target": target,
		"destinations": layers.destinations(target) if target in layers.LAYERS else [],
		"source_doctype": layers.source_doctype(target),
	}


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
	"""Everything the Desk form paints, in ONE call: is it live, at what address, and why it cannot go
	live. The script decides none of this — `readiness` is the server's one answer, and the script
	only renders it (N3)."""
	frappe.has_permission("CRM Intake Form", "read", throw=True)

	cfg = frappe.get_doc("CRM Intake Form", intake_form)
	return {
		# The Web Form NAME (autonamed off the title), never the route — they coincide by accident.
		"web_form": builder.web_form_name_for(cfg),
		"published": _published(cfg),
		"route": cfg.route,
		"reasons": builder.readiness(cfg),
	}


def _published(cfg) -> bool:
	"""Is this form's Web Form live? One source, already indexed — no shadow flag."""
	wf_name = builder.web_form_name_for(cfg)
	return bool(wf_name and frappe.db.get_value("Web Form", wf_name, "published"))


# The one switch this module reads. Named, not typed inline — a mistyped key reads as "disabled" and
# nothing goes red, which is the single way a switch check fails silently. The rate-limit switch is
# not read here: `guards` owns that gate, as it does for every other intake limit.
_INTAKE_SWITCH = "Lead::Enrolment::intake"


@frappe.whitelist(allow_guest=True, methods=["POST"])  # guest-ok: the enrolment forms are anonymous by design, so the visitor typing the number IS a Guest; intake's own per-IP limiter bounds EVERY call as its first act, and the body then self-gates to an enabled intake form whose contract asked for the warning, answering a bare yes/no about ONE number the caller already typed
def check_existing_patient(web_form, phone):
	"""Is the number just typed already a lead on this form's line — asked BEFORE the form is filled.

	The whole feature: a rep or patient typing a number the CRM already holds on this form's
	Vertical + Group is told so, and decides whether to carry on. Answering here — on the field's own
	change event — rather than at submit is deliberate: frappe's website `frappe.call`
	(`website.js`, the one a public form gets, NOT the Desk one) has no error-handler seam, so a
	refusal raised at submit reaches the visitor as a dead-end msgprint they cannot get past. The
	submit path is therefore untouched by this feature and cannot start failing because of it.

	BOUNDED FIRST, before it looks at anything. `guards.throttle_existing_check` is the same limiter,
	switch and settings cap the submit throttle and the upload doorman use — intake gets ONE limiting
	mechanism, not a second one for this. Spending it first is what closes the hole: counting only once
	the number parsed left a caller sending junk with no ceiling at all.

	It answers "no" — never an error — for every state that is not a real question: a form whose flag
	is off, a site whose intake switch is off, a Web Form that is not an intake sink, and a number
	still being typed. The ONE thing it does raise is the rate limit, which the visitor must see.

	The merge itself is unchanged: submitting still folds onto the existing lead through the one
	brain (`api.partner._upsert_one`), whatever this answered.

	Returns `{"exists": bool, "message": str}` — the message is composed HERE, so the client holds no
	business text, and it names NOTHING about the record it found (see `_already_enrolled_message`).
	"""
	from tatva_connect import automation
	from tatva_connect.intake import guards
	from tatva_connect.intake.intake import _intake_doctypes
	from tatva_connect.lead.leads import existing_lead

	guards.throttle_existing_check()
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
		return no  # still being typed — not a question

	if not existing_lead(mobile, cfg.custom_vertical, cfg.custom_group):
		return no
	return {"exists": True, "message": _already_enrolled_message()}


@frappe.whitelist(allow_guest=True, methods=["POST"])  # guest-ok: intake forms are anonymous by design; intake's per-IP limiter bounds every call first, and the body answers only for a published intake form's own Link question that carries link_filters, with names from a list that same page already ships whole
def link_options(web_form, fieldname, values=None):
	"""The picks a question's link_filters allow for the answers given so far, or [] when it asks nothing of the kind."""
	from tatva_connect.intake import guards
	from tatva_connect.intake.intake import _intake_doctypes, resolve_link_filters

	guards.throttle_existing_check("link-ip")
	sink, published = frappe.db.get_value("Web Form", web_form, ["doc_type", "published"]) or (None, 0)
	if not (published and sink in _intake_doctypes()):
		return []
	df = frappe.get_meta(sink).get_field(fieldname)
	if not (df and df.fieldtype == "Link" and df.link_filters):
		return []
	answers = frappe.parse_json(values) if isinstance(values, str) else (values or {})
	filters = resolve_link_filters(frappe.parse_json(df.link_filters), answers)
	return frappe.get_all(df.options, filters=filters, pluck="name")  # authz-ok: tier-c — the same names frappe's get_link_options ships whole to this public page, only narrowed


def _lookup_phone(raw):
	"""The number in the form leads are STORED under, or "" if it is not a number yet.

	`to_e164` is the one brain for that and it REFUSES what it cannot shape — right for a write, wrong
	here: this is a LOOKUP on a field that fires while the visitor is still typing, and "+91-98" has an
	answer (nothing), not an error. `api._base._norm_phone` reads a partner's search the same way; it is
	not reused because it passes the unshapeable value THROUGH, and a fragment must be distinguishable
	from a number here, not silently searched for.

	A fragment still COSTS a request — the limiter runs at the door, above this — so this decides what
	is answered, never what is charged.
	"""
	try:
		return to_e164(frappe.cstr(raw or ""))
	except frappe.ValidationError:
		frappe.clear_last_message()  # a fragment is not news; the visitor is mid-keystroke
		return ""


def _already_enrolled_message() -> str:
	"""The warning, and it describes NOTHING but the consequence of carrying on.

	It names no programme, no group and no person — deliberately, and this is the narrowest the text
	can be while still being useful. The person filling the form already sees the programme in the
	banner and the title, so repeating it back adds nothing they do not have; but the reply also
	leaves this server over an anonymous door, and a hit that named the programme would turn one
	answer into "this number is enrolled in THAT programme". Abstract, the same answer says only
	"known number", which is what the form needs and nothing more.

	Takes no argument for exactly that reason: there is no per-form value left to interpolate, so
	there is nothing a future edit can reach for without deciding to widen this on purpose.
	"""
	return _(
		"This number is already enrolled. Submitting this form will update the existing details. "
		"Continue?"
	)
