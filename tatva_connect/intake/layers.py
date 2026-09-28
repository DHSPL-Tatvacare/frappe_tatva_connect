"""Intake targets beyond CRM Lead: the record a form creates, and the related records its answers fill and link onto it."""
import frappe
from frappe.model import no_value_fields, table_fields

from tatva_connect.helpdesk import TICKET
from tatva_connect.helpdesk.contact import contact_for_phone
from tatva_connect.taxonomy import grain

LEAD = "CRM Lead"
CONTACT = "Contact"
# The lead's own phone question; a layer declares its own.
LEAD_PHONE = ("lead", "mobile_no")

# target -> result column on the submission, the phone question, where the contact's email lands, and each related doctype: (link on the target, the fields its resolver reads).
LAYERS = {
	TICKET: frappe._dict(
		result="ticket",
		phone=(CONTACT, "mobile_no"),
		email_field="raised_by",
		related={CONTACT: ("contact", ("first_name", "mobile_no", "email_id"))},
	),
}


def target_of(cfg):
	"""The record this form creates; blank is the lead, as every form was before targets existed."""
	return cfg.get("target") or LEAD


def layer_of(cfg):
	"""The form's layer, or None for a lead form, which keeps the lead's own machinery."""
	return LAYERS.get(target_of(cfg))


def phone_of(cfg):
	"""(table, field) of the one question that carries the phone."""
	layer = layer_of(cfg)
	return layer.phone if layer else LEAD_PHONE


def result_field(cfg):
	"""The submission column the fold stamps with the record it built."""
	layer = layer_of(cfg)
	return layer.result if layer else "lead"


def target_pair(m):
	"""(table, field) a mapping row lands its answer on."""
	return (m.target_table or "").strip(), (m.target_field or "").strip()


def destinations(target):
	"""The doctypes a question on this target's form may land on: the target, then its related records."""
	return [target, *LAYERS[target].related]


def fields_of(target, table):
	"""[{fieldname, label, fieldtype}] a question may map into on `table`, read from the live schema."""
	layer = LAYERS[target]
	if table in layer.related:
		meta = frappe.get_meta(table)
		return [_field(meta.get_field(f)) for f in layer.related[table][1]]
	if table != target:
		return []
	# The fold sets the related links and the email itself; a layer target carries no grain.
	skip = {related[0] for related in layer.related.values()} | {layer.email_field} | set(filter(None, grain.columns(target)))
	return [_field(df) for df in frappe.get_meta(target).fields
	        if df.fieldtype not in no_value_fields and df.fieldtype not in table_fields
	        and not df.read_only and not df.hidden and df.fieldname not in skip]


def _field(df):
	return {"fieldname": df.fieldname, "label": df.label, "fieldtype": df.fieldtype}


def fold(doc, cfg):
	"""A submission row -> the target record, its contact found or created by phone and linked onto it."""
	from tatva_connect.api._base import trusted_permissions
	from tatva_connect.intake.intake import attach_files, stamp

	target = target_of(cfg)
	layer = LAYERS[target]
	values = {}
	for m in cfg.mappings:
		table, field = target_pair(m)
		value = frappe.cstr(doc.get(m.source_field) or "").strip()  # raw, so a Link keeps its key
		if table and field and value:
			values.setdefault(table, {})[field] = value

	person = values.get(CONTACT, {})
	with trusted_permissions():  # authz-ok: tier-b — the published, enabled intake form is the gate; the visitor is a Guest by design
		record = frappe.new_doc(target)
		record.update(values.get(target, {}))
		if person.get("mobile_no"):
			record.set(layer.related[CONTACT][0],
			           contact_for_phone(person["mobile_no"], person.get("first_name"), person.get("email_id")))
		record.set(layer.email_field, person.get("email_id"))
		record.insert(ignore_permissions=True)  # authz-ok: tier-b — guest submit: every value comes from the form's own questions
		attach_files(doc, target, record.name)
	stamp(doc, layer.result, record.name)
