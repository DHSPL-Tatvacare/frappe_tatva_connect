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


def _add_option(doctype: str, fieldname: str, option: str = _OPTION):
	"""Append ONE value to a Select's options, idempotently. Shared: the AI Voice medium patch calls this
	rather than copying it, so "how a Select gains an option" has one implementation. `execute()` above is
	unchanged — it still appends Acefone — so this stays true for a site that already ran it."""
	# Both targets are crm-fork doctypes: get_meta on one this site does not have raises, and this step runs on EVERY migrate from schema_setup.
	if not frappe.db.exists("DocType", doctype):
		return
	if not frappe.get_meta(doctype).get_field(fieldname):
		# Field missing (unexpected crm version) — log and skip, don't abort migrate.
		frappe.log_error(
			title="telephony medium patch: field missing",
			message=f"{doctype}.{fieldname} not found; {option!r} skipped.",
		)
		return

	options = _current_options(doctype, fieldname) or ""
	lines = options.split("\n")
	if option in [ln.strip() for ln in lines]:
		return  # already present

	new_options = (options.rstrip("\n") + "\n" + option) if options else option
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
