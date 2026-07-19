"""Builder discovery for the Facebook Lead Form desk form.

Read-only: the list of catalog field_keys a form's questions may map to, scoped to the grain of the
contract its Lead Sync Source is created against. The SAME set `allowed_field_keys` hands ingestion — so
what an operator can pick and what a lead can actually carry are one answer, not two.
"""
import frappe

from tatva_connect.lead_sync.contract import allowed_field_keys
from tatva_connect.lead_sync.form import contract_for_form


@frappe.whitelist()
def list_mappable_fields(facebook_lead_form):
	"""[{value: field_key, label, section}] for the question picker; [] when no source/contract yet.

	Gated on read of Lead Sync Source (System-Manager-only), so this surfaces the catalog to builders only.
	"""
	frappe.has_permission("Lead Sync Source", "read", throw=True)

	contract = contract_for_form(facebook_lead_form)
	if not contract:
		return []

	keys = allowed_field_keys(contract)
	if not keys:
		return []

	rows = frappe.get_all(
		"CRM Lead API Field",
		filters={"field_key": ("in", list(keys))},
		fields=["field_key", "label", "section"],
		order_by="section asc, label asc",
	)
	return [
		{"value": r.field_key, "label": f"{r.label or r.field_key} ({r.section})", "section": r.section}
		for r in rows
	]
