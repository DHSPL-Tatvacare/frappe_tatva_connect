# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""The hover card for ONE lead: the server declares its rows; a missing lead is a 404, an unreadable one a 403."""

import frappe
from frappe import _

from tatva_connect.taxonomy import labels

LEAD = "CRM Lead"

# The card's rows in reading order; every label is the doctype's own.
ROWS = (
	"custom_vertical",
	"custom_group",
	"custom_current_program",
	"custom_substage",
	"custom_stage",
	"source",
	"custom_source_origin",
)


@frappe.whitelist()
def get_lead_preview(name):
	"""The card's payload for one lead. One document read; nothing derived on the client."""
	# Load then gate, the native `read_doc` order: a missing lead is a 404, an unreadable one a 403.
	doc = frappe.get_cached_doc(LEAD, name)
	frappe.has_permission(LEAD, "read", doc=doc, throw=True)

	meta = frappe.get_meta(LEAD)
	readable = meta.get_permlevel_access("read")
	rows = [{"label": _("Lead ID"), "value": doc.name}]
	for fieldname in ROWS:
		df = meta.get_field(fieldname)
		if df.permlevel in readable:
			rows.append({"label": _(df.label), "value": labels.shown(LEAD, fieldname, doc.get(fieldname)) or ""})

	return {
		"title": doc.lead_name or doc.first_name or doc.name,
		"subtitle": doc.mobile_no or "",
		"image": doc.image or "",
		"rows": rows,
	}
