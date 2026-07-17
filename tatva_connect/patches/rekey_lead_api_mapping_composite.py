"""Re-key CRM Lead API Mapping from login-keyed (`field:partner_user`) to the grain composite
`vertical::crm_group::program::contract_name`, bringing the last grain-scoped master in line with
CRM Task Type / CRM Hospital / CRM Picklist Value (11-naming-conventions). The login was the only thing
a contract could be named after, so a surface with no login — an intake form, a Facebook crawl — could
not point at one and grew its own grain fields instead. Idempotent, forward-only.

Per record:
  * already composite ("::" in name): skipped.
  * has a partner_user: contract_name backfilled from it, then rename_doc (cascades allowed_fields /
    allowed_programs child rows and any Link automatically).
  * no partner_user and no contract_name: cannot be named — logged and skipped, never guessed.

Display behaviour is unchanged: partner resolution reads the partner_user COLUMN, never the name.
"""
import frappe

DT = "CRM Lead API Mapping"


def _composite(vertical, crm_group, program, contract_name):
	return "{}::{}::{}::{}".format(vertical or "", crm_group or "", program or "", contract_name)


def execute():
	for name in [r.name for r in frappe.get_all(DT, fields=["name"])]:
		if "::" in name:
			continue  # already composite — idempotent
		row = frappe.db.get_value(
			DT, name, ["partner_user", "vertical", "crm_group", "program", "contract_name"], as_dict=True
		)
		if not row:
			continue
		contract_name = (row.contract_name or "").strip() or (row.partner_user or "").strip()
		if not contract_name:
			frappe.log_error(
				f"{DT} '{name}' has neither partner_user nor contract_name — cannot derive a composite key.",
				"rekey_lead_api_mapping_composite",
			)
			continue
		if not row.vertical:
			frappe.log_error(
				f"{DT} '{name}' has no vertical — cannot derive a composite key.",
				"rekey_lead_api_mapping_composite",
			)
			continue
		new = _composite(row.vertical, row.crm_group, row.program, contract_name)
		if new == name or frappe.db.exists(DT, new):
			continue
		# Set contract_name first: autoname reads it, and the row must be consistent with its new key.
		frappe.db.set_value(DT, name, "contract_name", contract_name, update_modified=False)
		frappe.rename_doc(DT, name, new, force=True)
	frappe.db.commit()
