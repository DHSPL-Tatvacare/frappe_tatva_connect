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
from tatva_connect.whatsapp.phone import lookup_e164


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


@frappe.whitelist(allow_guest=True, methods=["POST"])  # guest-ok: the enrolment forms are anonymous by design, so the visitor submitting the number IS a Guest; intake's own per-IP limiter bounds EVERY call as its first act, and the body then self-gates to an enabled intake form whose contract asked for the warning, answering a bare yes/no about ONE number the caller already typed
def check_existing_patient(web_form, phone):
	"""Is this number already a lead on the form's line, asked once at submit after the limiter; answers `{exists, ask, message}`, never an error but the limit."""
	from tatva_connect import automation
	from tatva_connect.helpdesk.contact import find_contact
	from tatva_connect.intake import guards
	from tatva_connect.intake.intake import INTAKE_SWITCH, _intake_doctypes
	from tatva_connect.intake.layers import layer_of
	from tatva_connect.lead.leads import existing_lead

	guards.throttle_existing_check()
	no = {"exists": False, "ask": False, "message": ""}

	# Self-gate: only an ENABLED intake form's own Web Form may ask, resolved through the ONE brain
	# that says what an intake sink is — never a caller-named doctype.
	sink = frappe.db.get_value("Web Form", web_form, "doc_type") if web_form else None
	intake_form = _intake_doctypes().get(sink) if sink else None
	if not intake_form:
		return no
	# No fold means no merge to warn about: with the feature switch off a submission lands nowhere.
	if not automation.is_enabled(INTAKE_SWITCH):
		return no
	cfg = frappe.get_cached_doc("CRM Intake Form", intake_form)
	if not cfg.get("warn_if_already_enrolled"):
		return no

	mobile = lookup_e164(phone)  # a lookup: a fragment finds nothing, and the submit refuses it in its own words
	if not mobile:
		return no  # not a number — the submit refuses it in its own words

	# A lead form asks, because submitting updates that lead; a layer form only tells, because the record links to the person either way.
	if layer_of(cfg):
		return {"exists": True, "ask": False, "message": _already_known_message()} if find_contact(mobile) else no
	if not existing_lead(mobile, cfg.custom_vertical, cfg.custom_group):
		return no
	return {"exists": True, "ask": True, "message": _already_enrolled_message()}


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


def _already_known_message() -> str:
	"""The notice for a form that links to a person: it names nothing, for the same reason as `_already_enrolled_message`."""
	return _("This number belongs to an existing contact. Your request will be linked to it.")
