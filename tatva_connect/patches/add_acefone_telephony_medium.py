"""Append "Acefone" to the telephony_medium / default_medium Selects via code Property Setters (stock options drift between crm versions); idempotent."""
import frappe

_TARGETS = [
	("CRM Call Log", "telephony_medium"),
	("CRM Telephony Agent", "default_medium"),
]
_OPTION = "Acefone"


def execute():
	for doctype, fieldname in _TARGETS:
		_add_option(doctype, fieldname)


def _current_options(doctype: str, fieldname: str) -> str:
	"""The live Select options: a Property Setter override if present, else the field's own definition."""
	ps = frappe.db.get_value(
		"Property Setter",
		{"doc_type": doctype, "field_name": fieldname, "property": "options"},
		"value",
	)
	if ps is not None:
		return ps
	meta = frappe.get_meta(doctype)
	df = meta.get_field(fieldname)
	return df.options if df else ""


def _add_option(doctype: str, fieldname: str):
	if not frappe.get_meta(doctype).get_field(fieldname):
		# Field missing (unexpected crm version) — log and skip, don't abort migrate.
		frappe.log_error(
			title="Acefone medium patch: field missing",
			message=f"{doctype}.{fieldname} not found; skipped.",
		)
		return

	options = _current_options(doctype, fieldname) or ""
	lines = options.split("\n")
	if _OPTION in [ln.strip() for ln in lines]:
		return  # already present

	new_options = (options.rstrip("\n") + "\n" + _OPTION) if options else _OPTION
	frappe.make_property_setter(
		{
			"doctype": doctype,
			"fieldname": fieldname,
			"property": "options",
			"property_type": "Text",
			"value": new_options,
		},
		is_system_generated=False,
	)
