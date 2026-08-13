# Copyright (c) 2026, TatvaCare and Contributors
# See license.txt
"""Create Lead: the grain axis fields are hidden server-side (GrainSelect stamps them), a section left with nothing visible hides itself, and mobile is marked required. Hidden, never removed — the layout editor saves `field.fieldname` back, so removing here would erase the row. One layout, and the picked grain decides which SECTIONS of it are drawn: a section survives if the leaf's contract ticks any field in it, so today's sections never lose a field and only a grain-specific block can vanish. No grain, or no contract covering the leaf, filters nothing."""
import frappe

from tatva_connect.access import entitlement, request_cache
from tatva_connect.taxonomy import grain

_FIELDNAME_CACHE = "tatva_connect:quick_entry_catalogued_fieldnames"

# Records whose grain the server stamps, so their axis fields are hidden on the create form rather than offered.
_GRAIN_STAMPED = ("CRM Lead", "CRM Deal")


@frappe.whitelist()
def get_fields_layout(doctype: str, type: str, parent_doctype: str | None = None,
                      vertical: str | None = None, group: str | None = None, program: str | None = None):
	from crm.fcrm.doctype.crm_fields_layout.crm_fields_layout import get_fields_layout as _native

	tabs = _native(doctype, type, parent_doctype)
	if type != "Quick Entry" or doctype not in _GRAIN_STAMPED:
		return tabs

	hide_grain = "System Manager" not in frappe.get_roles()
	# The contract decides which SECTIONS are drawn off the LEAD catalog; a deal carries none, so it takes the axis hiding alone.
	visible, required = _contract_view(vertical, group, program) if doctype == "CRM Lead" else (None, ())
	for tab in tabs:
		for section in tab.get("sections") or []:
			# A layout fieldname with no meta field stays a plain string (crm_fields_layout.py:77).
			fields = [f for c in section.get("columns") or [] for f in c.get("fields") or [] if isinstance(f, dict)]
			for field in fields:
				if field["fieldname"] in required or (field["fieldname"] == "mobile_no" and doctype == "CRM Lead"):
					field["reqd"] = 1
				if hide_grain and field["fieldname"] in grain.columns(doctype):
					field["hidden"] = 1
			if visible is not None and not _section_belongs(fields, visible):
				for field in fields:
					field["hidden"] = 1
			if not any(not f.get("hidden") for f in fields):
				section["hidden"] = True
	return tabs


def _section_belongs(fields, visible):
	"""True unless the catalog knows this section's fields and the contract ticks none — uncatalogued is nobody's to hide.

	The grain AXIS fields never count towards that. They are catalogued, and a contract need not tick its own
	axes — Goodflip-India ticks none of the three — so counting them hid the Routing section, which is where a
	System Manager picks the grain in the first place. An axis is how the grain is CHOSEN, never grain-specific content."""
	known = set(_catalogued_fieldnames().values()) - {c for c in grain.columns("CRM Lead") if c}
	catalogued = [f["fieldname"] for f in fields if f["fieldname"] in known]
	return not catalogued or any(name in visible for name in catalogued)


def _contract_view(vertical, group, program):
	"""(visible fieldnames, required-on-this-form fieldnames) for the leaf; visible `None` means do not filter."""
	if not any((vertical, group, program)):
		return None, frozenset()
	keys = entitlement.contract_keys_for_lead_grain(vertical or "", group or "", program or "")
	if not keys:
		return None, frozenset()
	required = entitlement.contract_keys_for_lead_grain(
		vertical or "", group or "", program or "", required_only=True)
	catalog = _catalogued_fieldnames()
	return {catalog[k] for k in keys if k in catalog}, {catalog[k] for k in required if k in catalog}


def _catalogued_fieldnames():
	"""{field_key: fieldname} off the catalog — never built as `lead:` + fieldname, since `lead:group` is `custom_group`."""
	def build():
		return {
			row.field_key: row.fieldname
			for row in frappe.get_all(
				"CRM Lead API Field", filters={"section": "lead"}, fields=["field_key", "fieldname"])
			if row.fieldname
		}
	return request_cache(_FIELDNAME_CACHE, "lead", build)


@frappe.whitelist()
def save_fields_layout(doctype: str, type: str, layout: str):
	"""Drop the read-time `section.hidden` we computed above — the editor echoes the whole payload back, and persisting it would hide the section from a System Manager, who never gets the flag. The editor itself never sets it."""
	from crm.fcrm.doctype.crm_fields_layout.crm_fields_layout import save_fields_layout as _native

	tabs = frappe.parse_json(layout)
	for tab in tabs if isinstance(tabs, list) else []:
		for section in tab.get("sections") or []:
			section.pop("hidden", None)
	return _native(doctype, type, frappe.as_json(tabs))
