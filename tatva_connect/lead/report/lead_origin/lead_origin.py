# Copyright (c) 2026, TatvaCare and contributors
# For license information, please see license.txt
"""Where one lead came from: its source and the ids its channel stamped, and the other leads that carry the same number."""
import frappe
from frappe import _

LEAD = "CRM Lead"
# The columns a channel stamps when it creates a lead; labels come from the schema.
ORIGIN_FIELDS = ("source", "custom_source_origin", "custom_origin", "custom_origin_vertical", "custom_clinic_source",
	"facebook_lead_id", "facebook_form_id", "custom_external_id", "custom_lsq_prospect_id")


def execute(filters=None):
	filters = frappe._dict(filters or {})
	frappe.has_permission(LEAD, "read", filters.lead, throw=True)
	meta = frappe.get_meta(LEAD)
	fields = [f for f in ORIGIN_FIELDS if meta.has_field(f)]
	lead = frappe.db.get_value(LEAD, filters.lead, ["name", "creation", "owner", "mobile_no", *fields], as_dict=True)
	rows = [
		_row(_("Created"), _("On"), lead.creation),
		_row(_("Created"), _("By"), lead.owner),
	]
	rows += [_row(_("Channel"), meta.get_label(f), lead.get(f)) for f in fields if lead.get(f)]
	if lead.mobile_no:
		same = frappe.get_list(LEAD, filters={"mobile_no": lead.mobile_no, "name": ["!=", lead.name]},
			fields=["name", "custom_vertical", "custom_group", "custom_current_program", "lead_owner", "creation"], order_by="creation")
		for s in same:
			grain = " / ".join(a for a in (s.custom_vertical, s.custom_group, s.custom_current_program) if a)
			rows.append(_row(_("Same number"), grain, _("created {0}, owner {1}").format(s.creation, s.lead_owner or "-"), s.name))
		hidden = frappe.db.count(LEAD, {"mobile_no": lead.mobile_no, "name": ["!=", lead.name]}) - len(same)
		if hidden:
			rows.append(_row(_("Same number"), _("In leads you cannot open"), hidden))
	columns = [
		{"label": _("Section"), "fieldname": "section", "fieldtype": "Data", "width": 130},
		{"label": _("Item"), "fieldname": "item", "fieldtype": "Data", "width": 240},
		{"label": _("Value"), "fieldname": "value", "fieldtype": "Data", "width": 380},
		{"label": _("Lead"), "fieldname": "lead", "fieldtype": "Link", "options": LEAD, "width": 130},
	]
	return columns, rows


def _row(section, item, value, lead=None):
	return {"section": section, "item": item, "value": frappe.utils.cstr(value), "lead": lead}
