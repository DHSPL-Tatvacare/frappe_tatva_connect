# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Create Lead: the grain axis fields are hidden server-side (GrainSelect stamps them), a section left with nothing visible hides itself, and mobile is marked required. Hidden, never removed — the layout editor saves `field.fieldname` back, so removing here would erase the row."""
import frappe

from tatva_connect.taxonomy.picklist import _LEAD_AXES


@frappe.whitelist()
def get_fields_layout(doctype: str, type: str, parent_doctype: str | None = None):
	from crm.fcrm.doctype.crm_fields_layout.crm_fields_layout import get_fields_layout as _native

	tabs = _native(doctype, type, parent_doctype)
	if doctype != "CRM Lead" or type != "Quick Entry":
		return tabs

	hide_grain = "System Manager" not in frappe.get_roles()
	for tab in tabs:
		for section in tab.get("sections") or []:
			# A layout fieldname with no meta field stays a plain string (crm_fields_layout.py:77).
			fields = [f for c in section.get("columns") or [] for f in c.get("fields") or [] if isinstance(f, dict)]
			for field in fields:
				if field["fieldname"] == "mobile_no":
					field["reqd"] = 1
				if hide_grain and field["fieldname"] in _LEAD_AXES:
					field["hidden"] = 1
			if not any(not f.get("hidden") for f in fields):
				section["hidden"] = True
	return tabs


@frappe.whitelist()
def save_fields_layout(doctype: str, type: str, layout: str):
	"""Drop the read-time `section.hidden` we computed above — the editor echoes the whole payload back, and persisting it would hide the section from a System Manager, who never gets the flag. The editor itself never sets it."""
	from crm.fcrm.doctype.crm_fields_layout.crm_fields_layout import save_fields_layout as _native

	tabs = frappe.parse_json(layout)
	for tab in tabs if isinstance(tabs, list) else []:
		for section in tab.get("sections") or []:
			section.pop("hidden", None)
	return _native(doctype, type, frappe.as_json(tabs))
