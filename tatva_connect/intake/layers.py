"""Intake targets beyond CRM Lead: the record a form creates, and the related records its answers fill and link onto it."""
import frappe
from frappe.model import no_value_fields, table_fields

from tatva_connect.helpdesk import TICKET
from tatva_connect.helpdesk.contact import contact_for
from tatva_connect.taxonomy import grain

LEAD = "CRM Lead"
# Frappe's own spelling for a link filter that reads another field of the same record.
EVAL_DOC = "eval:doc."
CONTACT = "Contact"
# The lead's own phone question and source field; a layer declares its own.
LEAD_PHONE = ("lead", "mobile_no")
LEAD_SOURCE_FIELD = "source"

# target -> result column on the submission, the phone question, the field its source is stamped on, where the contact's email lands, and each related doctype: (link on the target, the fields its resolver reads).
LAYERS = {
	TICKET: frappe._dict(
		result="ticket",
		phone=(CONTACT, "mobile_no"),
		email=(CONTACT, "email_id"),
		source_field="custom_ticket_source",
		email_field="raised_by",
		# Helpdesk files the ticket's description as its first message; the customer is its author and uploads sit on it.
		message=frappe._dict(doctype="Communication", link=("reference_doctype", "reference_name"),
		                     author=("sender", "sender_full_name")),
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


def source_doctype(target):
	"""The list a form's Source picks from: whatever the target's own source field links to."""
	layer = LAYERS.get(target)
	field = layer.source_field if layer else LEAD_SOURCE_FIELD
	return frappe.get_meta(target).get_field(field).options


def result_field(cfg):
	"""The submission column the fold stamps with the record it built."""
	layer = layer_of(cfg)
	return layer.result if layer else "lead"


def target_pair(m):
	"""(table, field) a mapping row lands its answer on."""
	return (m.target_table or "").strip(), (m.target_field or "").strip()


def question_name(m):
	"""The question a mapping row asks: its column on the submission and its field on the page."""
	return (m.source_field or "").strip()


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
	skip = ({related[0] for related in layer.related.values()} | {layer.email_field, layer.source_field}
	        | set(filter(None, grain.columns(target))))
	return [_field(df) for df in frappe.get_meta(target).fields
	        if df.fieldtype not in no_value_fields and df.fieldtype not in table_fields
	        and not df.read_only and not df.hidden and df.fieldname not in skip]


def eval_ref(value):
	"""The field a link-filter value reads (`eval:doc.<field>`), or None for a fixed value."""
	if isinstance(value, str) and value.startswith(EVAL_DOC):
		return value[len(EVAL_DOC):]
	return None


def question_target(cfg, m):
	"""(doctype, field) the question's answer lands on, or None for a note, a layout row or an unmapped input."""
	table, field = target_pair(m)
	if not (table and field):
		return None
	if layer_of(cfg):
		return (table, field) if table in destinations(target_of(cfg)) else None
	from tatva_connect.intake.intake import target_doctype

	doctype = target_doctype(table)
	return (doctype, field) if doctype else None


def matched_by(cfg, m):
	"""The field on this question's list that holds its Depends On answer: the one named, else the one that links to that question's list."""
	if (m.get("filter_field") or "").strip():
		return m.filter_field.strip()
	parent = next((p for p in cfg.mappings if question_name(p) == (m.depends_on_question or "").strip()), None)
	return next((df.fieldname for df in frappe.get_meta(m.options).fields
	             if parent and df.fieldtype == "Link" and df.options == parent.options), None)


def question_filters(cfg):
	"""{question: link_filters} each Link question carries: what the field it fills declares, rewritten to this form's questions, and its own Depends On."""
	fills = {question_target(cfg, m): question_name(m) for m in cfg.mappings}
	out = {}
	for m in cfg.mappings:
		if (m.get("fieldtype") or "").strip() != "Link":
			continue
		target = question_target(cfg, m)
		df = target and frappe.get_meta(target[0]).get_field(target[1])
		rules = []
		if (m.get("depends_on_question") or "").strip() and matched_by(cfg, m):
			rules.append([m.options, matched_by(cfg, m), "=", EVAL_DOC + (m.depends_on_question or "").strip()])
		for doctype, field, op, value in frappe.parse_json((df and df.link_filters) or "[]"):
			if doctype != m.options:
				continue
			if eval_ref(value):
				question = fills.get((target[0], eval_ref(value)))
				if not question:
					continue  # the form does not ask for the field this rule reads, so only its fixed rules apply
				value = EVAL_DOC + question
			rules.append([doctype, field, op, value])
		if rules:
			out[question_name(m)] = rules
	return out


def _field(df):
	return {"fieldname": df.fieldname, "label": df.label, "fieldtype": df.fieldtype}


def fold(doc, cfg):
	"""A submission row -> the target record, its contact found by phone or email (or created, when the form allows) and linked onto it."""
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
		# Found is always linked; a new person is created only when the form says so, else an agent links one on the ticket.
		contact = contact_for(person.get("mobile_no"), person.get("first_name"), person.get("email_id"),
		                      create=bool(cfg.get("auto_create_contact")))
		record.set(layer.related[CONTACT][0], contact)
		# The typed email, else the one the person's Contact already holds; never the session user.
		email = person.get("email_id") or (contact and frappe.db.get_value(CONTACT, contact, "email_id"))
		record.set(layer.email_field, email)
		record.set(layer.source_field, cfg.source)
		record.insert(ignore_permissions=True)  # authz-ok: tier-b — guest submit: every value comes from the form's own questions
		home = _first_message(layer.message, target, record.name)
		if home and email:
			frappe.db.set_value(*home, dict(zip(layer.message.author, (email, person.get("first_name")), strict=True)))
		# Uploads sit where the record's own screen lists them: its first message when it has one, else the record.
		attach_files(doc, *(home or (target, record.name)))
	stamp(doc, layer.result, record.name)


def _first_message(message, doctype, name):
	"""(doctype, name) of the record's first message, when its layer declares one and the record wrote it."""
	if not message:
		return None
	first = frappe.get_all(message.doctype, filters=dict(zip(message.link, (doctype, name), strict=True)),
	                       order_by="creation asc", limit=1, pluck="name")
	return (message.doctype, first[0]) if first else None
