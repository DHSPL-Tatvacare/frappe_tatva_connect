"""Every enabled intake form is synced once, so a form published before business-line rules existed offers only its own line's choices."""
import frappe

from tatva_connect.intake import builder


def execute():
	"""Idempotent: `builder.sync_form` is the same sync every form save runs."""
	for name in frappe.get_all("CRM Intake Form", filters={"enabled": 1}, pluck="name"):
		try:
			builder.sync_form(frappe.get_doc("CRM Intake Form", name))
		except Exception:
			frappe.log_error(title="Intake: business-line re-sync", message=f"form={name}\n{frappe.get_traceback()}")  # one broken form must not hold every deploy; the form keeps its old lists until saved
